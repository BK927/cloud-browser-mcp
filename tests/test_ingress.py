import asyncio
from contextlib import asynccontextmanager

import pytest

from cloud_browser.ingress import FixedRelay


@asynccontextmanager
async def relay_to(handler, **kwargs):
    upstream = await asyncio.start_server(handler, "127.0.0.1", 0)
    relay = FixedRelay("127.0.0.1", upstream.sockets[0].getsockname()[1], **kwargs)
    server = await asyncio.start_server(relay.accept, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], relay
    finally:
        server.close()
        upstream.close()
        await server.wait_closed()
        await upstream.wait_closed()
        await relay.close()


async def echo(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("payload", [
    b"GET /mcp HTTP/1.1\r\nHost: attacker.example\r\n\r\n",
    b"CONNECT 169.254.169.254:80 HTTP/1.1\r\nHost: metadata\r\n\r\n",
    b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n\x82\x03\x00\xff\x01",
    b"event: message\ndata: {\"opaque\":true}\n\n",
    bytes(range(256)) * 4096,
], ids=["http", "connect-not-a-proxy", "websocket", "sse", "one-megabyte"])
async def test_relay_is_fixed_destination_and_byte_transparent(payload):
    async with relay_to(echo) as (port, _), asyncio.timeout(5):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(payload)
        await writer.drain()
        assert await reader.readexactly(len(payload)) == payload
        writer.close()
        await writer.wait_closed()


async def test_half_closed_request_keeps_response_alive():
    async def respond(reader, writer):
        assert await reader.read() == b"request"
        writer.write(b"response after request EOF")
        await writer.drain()
        writer.close()

    async with relay_to(respond) as (port, _), asyncio.timeout(3):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"request")
        writer.write_eof()
        assert await reader.read() == b"response after request EOF"
        writer.close()
        await writer.wait_closed()


async def test_connection_limit_rejects_without_waiting_queue():
    async with relay_to(echo, limit=1) as (port, relay), asyncio.timeout(3):
        first, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ready")
        await writer.drain()
        assert await first.readexactly(5) == b"ready"
        refused, second = await asyncio.open_connection("127.0.0.1", port)
        assert await refused.read() == b""
        assert len(relay.tasks) == 1
        second.close()
        writer.close()
        await second.wait_closed()
        await writer.wait_closed()


async def test_idle_connection_expires_and_slot_is_reusable():
    async with relay_to(echo, idle_timeout=0.1) as (port, relay), asyncio.timeout(3):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
        await relay.close()
        assert not relay.tasks


async def test_one_way_server_activity_refreshes_idle_deadline():
    async def events(reader, writer):
        try:
            for _ in range(8):
                writer.write(b"event\n")
                await writer.drain()
                await asyncio.sleep(0.03)
        finally:
            writer.close()

    async with relay_to(events, idle_timeout=0.1) as (port, _), asyncio.timeout(3):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await reader.read() == b"event\n" * 8
        writer.close()
        await writer.wait_closed()


async def test_shutdown_closes_active_connections():
    async with relay_to(echo) as (port, relay), asyncio.timeout(3):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ready")
        await writer.drain()
        assert await reader.readexactly(5) == b"ready"
        await relay.close()
        assert await reader.read() == b""
        assert not relay.tasks
        writer.close()
        await writer.wait_closed()


async def test_upstream_failure_closes_connection_without_response():
    # A closed bound port, rather than an unrelated running service.
    closed = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = closed.sockets[0].getsockname()[1]
    closed.close()
    await closed.wait_closed()
    relay = FixedRelay("127.0.0.1", port)
    server = await asyncio.start_server(relay.accept, "127.0.0.1", 0)
    try:
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", server.sockets[0].getsockname()[1]
            )
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await relay.close()
