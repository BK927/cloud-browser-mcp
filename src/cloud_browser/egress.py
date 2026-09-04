"""Minimal explicit HTTP/CONNECT proxy with DNS-pinned public-only destinations.

Use together with the container network and UID firewall, not as a substitute for it.
No URL/header/body logging. No TLS interception.
"""

import asyncio
import contextlib
import os
from urllib.parse import urlsplit

from .security import public_addresses


class EgressProxy:
    def __init__(self):
        self.slots = asyncio.Semaphore(64)

    async def handle(self, client_reader, client_writer):
        upstream_writer = None
        try:
            async with self.slots:
                header = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), 15)
                if len(header) > 32768:
                    raise ValueError("Header too large")
                lines = header.decode("latin-1").split("\r\n")
                method, target, version = lines[0].split(" ")
                if version not in ("HTTP/1.0", "HTTP/1.1"):
                    raise ValueError("Unsupported protocol")
                tunnel = method == "CONNECT"
                parsed = urlsplit("//" + target if tunnel else target)
                if parsed.username or parsed.password or not parsed.hostname:
                    raise ValueError("Invalid destination")
                port = parsed.port or (443 if tunnel else 80)
                if (
                    port not in (80, 443)
                    or (tunnel and port != 443)
                    or (not tunnel and parsed.scheme != "http")
                ):
                    raise ValueError("Unsupported scheme or port")
                addresses = await asyncio.wait_for(
                    asyncio.to_thread(public_addresses, parsed.hostname, port), 10
                )
                # Connect to this checked numeric address; never resolve the name again.
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(addresses[0], port), 15
                )
                if tunnel:
                    client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                    filtered = []
                    for line in lines[1:]:
                        if not line:
                            continue
                        key, sep, value = line.partition(":")
                        if not sep or key[:1].isspace():
                            raise ValueError("Malformed header")
                        if key.lower() not in (
                            "proxy-authorization",
                            "proxy-connection",
                            "connection",
                            "host",
                        ):
                            filtered.append(line)
                    forwarded = (
                        f"{method} {path} {version}\r\nHost: {parsed.netloc}\r\nConnection: close\r\n"
                        + "\r\n".join(filtered)
                        + "\r\n\r\n"
                    )
                    upstream_writer.write(forwarded.encode("latin-1"))
                    await upstream_writer.drain()
                await client_writer.drain()

                async def pump(reader, writer):
                    while chunk := await asyncio.wait_for(reader.read(65536), 120):
                        writer.write(chunk)
                        await writer.drain()

                tasks = [
                    asyncio.create_task(pump(client_reader, upstream_writer)),
                    asyncio.create_task(pump(upstream_reader, client_writer)),
                ]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            with contextlib.suppress(Exception):
                client_writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await client_writer.drain()
        finally:
            for writer in (upstream_writer, client_writer):
                if writer:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()


async def main():
    proxy = EgressProxy()
    server = await asyncio.start_server(
        proxy.handle, "0.0.0.0", int(os.environ.get("CB_EGRESS_PORT", "3128")), limit=32768
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
