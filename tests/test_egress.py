import asyncio
from unittest.mock import AsyncMock

import pytest

from cloud_browser.egress import EgressProxy


async def request_proxy(request):
    proxy = EgressProxy()
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1]
        )
        writer.write(request)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        await writer.wait_closed()
        return response
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "target",
    [
        b"127.0.0.1:443",
        b"[::1]:443",
        b"169.254.169.254:443",
        b"224.0.0.1:443",
        b"[64:ff9b::a00:1]:443",
    ],
)
async def test_connect_denies_internal_and_translation_addresses(target):
    result = await request_proxy(b"CONNECT " + target + b" HTTP/1.1\r\nHost: test\r\n\r\n")
    assert result.startswith(b"HTTP/1.1 403")


async def test_http_denies_metadata_service():
    result = await request_proxy(
        b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: test\r\n\r\n"
    )
    assert result.startswith(b"HTTP/1.1 403")


async def test_resolved_address_not_hostname_is_used(monkeypatch):
    monkeypatch.setattr(
        "cloud_browser.egress.public_addresses", lambda host, port: ["93.184.215.14"]
    )
    upstream = AsyncMock(side_effect=OSError("intentional test connection failure"))
    original = asyncio.open_connection

    async def routed(host, port, **kwargs):
        if host == "127.0.0.1":
            return await original(host, port, **kwargs)
        return await upstream(host, port, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", routed)
    result = await request_proxy(b"CONNECT example.com:443 HTTP/1.1\r\nHost: test\r\n\r\n")
    assert result.startswith(b"HTTP/1.1 403")
    upstream.assert_awaited_once_with("93.184.215.14", 443)
