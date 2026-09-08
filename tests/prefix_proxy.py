"""Local HTTP test proxy for the documented, fixed stripped-prefix mappings.

This is a test fixture, not a replacement for Tailscale or a deployment service.
"""

import asyncio
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import uvicorn
from conftest import FakeWorker
from starlette.responses import Response, StreamingResponse

from cloud_browser.server import create_apps

HOP_HEADERS = {b"host", b"connection", b"transfer-encoding", b"keep-alive", b"proxy-connection"}


class StrippedPrefixProxy:
    def __init__(self, cfg, upstream):
        self.cfg, self.upstream = cfg, upstream

    async def __call__(self, scope, receive, send):
        host = dict(scope["headers"]).get(b"host")
        if host != urlsplit(self.cfg.public_origin).netloc.encode():
            return await Response(status_code=421)(scope, receive, send)
        path, prefix = scope["path"], self.cfg.public_path_prefix
        mapped = {
            urlsplit(
                self.cfg.resource_metadata_url
            ).path: "/.well-known/oauth-protected-resource/mcp",
            urlsplit(self.cfg.issuer_metadata_url).path: "/.well-known/oauth-authorization-server",
        }.get(path)
        if mapped is None:
            if prefix and not path.startswith(prefix + "/"):
                return await Response(status_code=404)(scope, receive, send)
            mapped = path[len(prefix) :]
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        headers = [
            (key, value) for key, value in scope["headers"] if key.lower() not in HOP_HEADERS
        ]
        headers.append((b"host", urlsplit(self.cfg.public_origin).netloc.encode()))
        query = scope.get("query_string", b"")
        target = mapped + ("?" + query.decode("ascii") if query else "")
        request = self.upstream.build_request(
            scope["method"], target, headers=headers, content=bytes(body)
        )
        upstream = await self.upstream.send(request, stream=True)

        async def content():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        response = StreamingResponse(content(), status_code=upstream.status_code)
        response.raw_headers = [
            (key, value) for key, value in upstream.headers.raw if key.lower() not in HOP_HEADERS
        ]
        await response(scope, receive, send)


@asynccontextmanager
async def serve_prefix(cfg, prefix):
    sockets = [socket.socket(), socket.socket()]
    for listener in sockets:
        listener.bind(("127.0.0.1", 0))
    front_port, back_port = (listener.getsockname()[1] for listener in sockets)
    cfg.public_origin = f"http://127.0.0.1:{front_port}"
    cfg.public_path_prefix = prefix
    public, control, service, auth = create_apps(cfg, worker=FakeWorker())
    backend_url = f"http://127.0.0.1:{back_port}"
    async with httpx.AsyncClient(
        base_url=backend_url, trust_env=False, follow_redirects=False, timeout=10
    ) as upstream:
        proxy = StrippedPrefixProxy(cfg, upstream)
        servers = [
            uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    log_level="error",
                    access_log=False,
                    proxy_headers=False,
                    lifespan=lifespan,
                )
            )
            for app, port, lifespan in ((proxy, front_port, "off"), (public, back_port, "on"))
        ]
        tasks = [
            asyncio.create_task(server.serve(sockets=[listener]))
            for server, listener in zip(servers, sockets, strict=True)
        ]
        try:
            for _ in range(200):
                if all(server.started for server in servers):
                    break
                await asyncio.sleep(0.02)
            assert all(server.started for server in servers)
            yield SimpleNamespace(
                cfg=cfg,
                public=public,
                control=control,
                service=service,
                auth=auth,
                backend_url=backend_url,
            )
        finally:
            for server in servers:
                server.should_exit = True
            await asyncio.wait_for(asyncio.gather(*tasks), 15)
            for listener in sockets:
                listener.close()
