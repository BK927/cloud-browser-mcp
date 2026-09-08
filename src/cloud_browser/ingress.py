"""Bounded byte relay for the two fixed MCP/control listeners.

No HTTP interpretation, destination selection, authentication bypass, credential
access or payload logging. In particular, CONNECT/Host/path bytes never choose
an upstream. SSE and authenticated control WebSockets remain end-to-end.
"""

import asyncio
import contextlib
import os


class FixedRelay:
    def __init__(self, host: str, port: int, *, limit=32, idle_timeout=300, connect_timeout=5):
        self.host = host
        self.port = port
        self.limit = limit
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout
        self.tasks: set[asyncio.Task] = set()

    def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        # Reject immediately, rather than retaining an unbounded admission queue.
        if len(self.tasks) >= self.limit:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self.tasks.add(task)

        def finished(done):
            self.tasks.discard(done)
            # Also covers cancellation before _handle starts its finally block.
            writer.close()

        task.add_done_callback(finished)

    async def close(self):
        pending = tuple(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _handle(self, reader, writer):
        upstream = None
        pumps = []
        try:
            peer, upstream = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, limit=65536),
                self.connect_timeout,
            )
            loop = asyncio.get_running_loop()
            async with asyncio.timeout(self.idle_timeout) as idle:

                async def pump(source, destination):
                    while chunk := await source.read(65536):
                        idle.reschedule(loop.time() + self.idle_timeout)
                        destination.write(chunk)
                        await destination.drain()
                    # A half-closed request can still have a pending response.
                    if destination.can_write_eof():
                        destination.write_eof()

                pumps = [
                    asyncio.create_task(pump(reader, upstream)),
                    asyncio.create_task(pump(peer, writer)),
                ]
                await asyncio.gather(*pumps)
        except (OSError, TimeoutError):
            # Fail closed without exposing addresses or recording payloads.
            pass
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
                    with contextlib.suppress(OSError, TimeoutError):
                        await asyncio.wait_for(stream.wait_closed(), 1)


async def main():
    target_host = os.environ.get("CB_INGRESS_TARGET", "browser")
    relays = [FixedRelay(target_host, port) for port in (8000, 8001)]
    bind_host = os.environ.get("CB_INGRESS_BIND", "0.0.0.0")
    listen_ports = [
        int(os.environ.get("CB_INGRESS_PUBLIC_PORT", "8000")),
        int(os.environ.get("CB_INGRESS_CONTROL_PORT", "8001")),
    ]
    servers = []
    try:
        for relay, listen_port in zip(relays, listen_ports, strict=True):
            servers.append(
                await asyncio.start_server(relay.accept, bind_host, listen_port, limit=65536)
            )
        await asyncio.gather(*(server.serve_forever() for server in servers))
    finally:
        for server in servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in servers))
        await asyncio.gather(*(relay.close() for relay in relays))


if __name__ == "__main__":
    asyncio.run(main())
