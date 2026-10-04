import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from cloud_browser.egress import EgressProxy


async def request_proxy(request, proxy=None):
    proxy = proxy or EgressProxy()
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


async def test_ipv6_listed_first_uses_reachable_ipv4(monkeypatch):
    addresses = ["2606:4700:4700::1111", "93.184.215.14"]
    resolver = Mock(return_value=addresses)
    monkeypatch.setattr("cloud_browser.egress.public_addresses", resolver)
    reader = asyncio.StreamReader()
    reader.feed_data(b"upstream response")
    reader.feed_eof()
    writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
    attempted = []
    original = asyncio.open_connection

    async def routed(host, port, **kwargs):
        if host == "127.0.0.1":
            return await original(host, port, **kwargs)
        attempted.append((host, port))
        if ":" in host:
            raise OSError("IPv6 unavailable")
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", routed)
    result = await request_proxy(b"CONNECT example.com:443 HTTP/1.1\r\nHost: test\r\n\r\n")
    assert result == b"HTTP/1.1 200 Connection Established\r\n\r\nupstream response"
    assert attempted == [("93.184.215.14", 443)]
    resolver.assert_called_once_with("example.com", 443)
    writer.close.assert_called_once()


@pytest.mark.parametrize("failure", [OSError, TimeoutError])
async def test_all_validated_addresses_fail_in_stable_family_order(monkeypatch, failure):
    addresses = ["2606:4700:4700::1111", "93.184.215.14", "2001:4860::8888", "1.1.1.1"]
    resolver = Mock(return_value=addresses)
    monkeypatch.setattr("cloud_browser.egress.public_addresses", resolver)
    upstream = AsyncMock(side_effect=failure("unreachable"))
    original = asyncio.open_connection

    async def routed(host, port, **kwargs):
        if host == "127.0.0.1":
            return await original(host, port, **kwargs)
        return await upstream(host, port, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", routed)
    result = await request_proxy(b"CONNECT example.com:443 HTTP/1.1\r\nHost: test\r\n\r\n")
    assert result.startswith(b"HTTP/1.1 403")
    assert [call.args for call in upstream.await_args_list] == [
        ("93.184.215.14", 443),
        ("1.1.1.1", 443),
        ("2606:4700:4700::1111", 443),
        ("2001:4860::8888", 443),
    ]
    resolver.assert_called_once_with("example.com", 443)


async def test_connect_timeout_falls_back_to_next_validated_address(monkeypatch):
    monkeypatch.setattr("cloud_browser.egress.CONNECT_ATTEMPT_TIMEOUT", 0.02)
    upstream = AsyncMock(side_effect=[None, ("reader", "writer")])

    async def connect(host, port):
        result = await upstream(host, port)
        if result is None:
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert await EgressProxy()._connect(["1.1.1.1", "93.184.215.14"], 443) == (
        "reader",
        "writer",
    )
    assert [call.args for call in upstream.await_args_list] == [
        ("1.1.1.1", 443),
        ("93.184.215.14", 443),
    ]


async def test_connect_total_budget_limits_attempts(monkeypatch):
    monkeypatch.setattr("cloud_browser.egress.CONNECT_ATTEMPT_TIMEOUT", 0.04)
    monkeypatch.setattr("cloud_browser.egress.CONNECT_TIMEOUT", 0.15)
    attempted = []

    async def connect(host, port):
        attempted.append(host)
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    addresses = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "93.184.215.14", "2606:4700::1111"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(EgressProxy()._connect(addresses, 443), 1)
    assert 2 <= len(attempted) <= 4
    assert attempted == addresses[: len(attempted)]


@pytest.mark.parametrize("streaming", [False, True])
async def test_idle_tracks_traffic_in_either_direction(monkeypatch, streaming):
    finished = asyncio.Event()

    async def upstream(reader, writer):
        try:
            if streaming:
                for _ in range(8):
                    writer.write(b"chunk\n")
                    await writer.drain()
                    await asyncio.sleep(0.04)
            else:
                assert await reader.read() == b""  # Fully idle tunnel closed by the proxy.
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    original = asyncio.open_connection
    monkeypatch.setattr(
        "cloud_browser.egress.public_addresses", lambda host, port: ["93.184.215.14"]
    )

    async def routed(host, port, **kwargs):
        if host == "93.184.215.14":
            port = server.sockets[0].getsockname()[1]
            host = "127.0.0.1"
        return await original(host, port, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", routed)
    try:
        result = await request_proxy(
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: test\r\n\r\n",
            EgressProxy(idle_timeout=0.15),
        )
        assert result.startswith(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        assert result.count(b"chunk\n") == (8 if streaming else 0)
        await asyncio.wait_for(finished.wait(), 1)
    finally:
        server.close()
        await server.wait_closed()
