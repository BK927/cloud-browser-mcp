import asyncio
import socket
from types import SimpleNamespace

import pytest
import pytest_asyncio

from cloud_browser import egress, security
from cloud_browser.drission import DrissionAdapter
from cloud_browser.models import BrowserError


@pytest_asyncio.fixture
async def proxy():
    server = await asyncio.start_server(egress.EgressProxy().handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", port
    finally:
        server.close()
        await server.wait_closed()


async def wire(port, request):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 3)
    finally:
        writer.close()
        await writer.wait_closed()


def browser_dns_unavailable(*args, **kwargs):
    raise AssertionError("Browser must not resolve external destinations in isolated mode")


def no_target_connections(monkeypatch):
    original = asyncio.open_connection
    targets = []

    async def connect(host, port, **kwargs):
        if host == "127.0.0.1":
            return await original(host, port, **kwargs)
        targets.append((host, port))
        raise OSError("No external traffic permitted by this unit test")

    monkeypatch.setattr(asyncio, "open_connection", connect)
    return targets


async def test_isolated_preflight_uses_egress_dns_without_site_connection(proxy, monkeypatch):
    calls = []
    targets = no_target_connections(monkeypatch)
    monkeypatch.setattr(security, "public_addresses", browser_dns_unavailable)

    def resolve(host, port):
        calls.append((host, port))
        return ["93.184.215.14"]

    monkeypatch.setattr(egress, "public_addresses", resolve)
    await asyncio.to_thread(
        security.validate_url, "https://example.com/path?secret=value", dns_proxy=proxy[0]
    )
    assert calls == [("example.com", 443)]
    assert targets == []
    reply = await wire(proxy[1], b"CB-DNS-CHECK example.com:443 HTTP/1.1\r\nHost: ignored\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 204")
    assert b"93.184" not in reply and b"secret" not in reply
    assert targets == []


@pytest.mark.parametrize("host", ["internal.example", "mixed.example", "rebind.example"])
async def test_proxy_check_rejects_private_or_mixed_dns(proxy, monkeypatch, host):
    original = socket.getaddrinfo
    targets = no_target_connections(monkeypatch)

    def resolve(name, port, *args, **kwargs):
        if name == host:
            return [(0, 0, 0, "", ("8.8.8.8", port)), (0, 0, 0, "", ("10.0.0.1", port))]
        return original(name, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(BrowserError) as exc:
        await asyncio.to_thread(security.validate_url, f"https://{host}/", dns_proxy=proxy[0])
    assert exc.value.code == "INVALID_URL"
    assert targets == []


@pytest.mark.parametrize(
    "target",
    [
        b"127.1:443",
        b"2130706433:443",
        b"169.254.169.254:80",
        b"[::1]:443",
        b"[64:ff9b::a00:1]:443",
        b"example.com:9222",
        b"example.com:0",
        b"example.com",
        b"user:pass@example.com:443",
        b"example.com:443/path",
        b"example.com:443?query",
        b"example.com:443?",
        b"example.com:443#fragment",
        b"example.com:443#",
        b"http://example.com:80/",
    ],
)
async def test_dns_rpc_rejects_unsafe_or_malformed_authorities(proxy, target):
    reply = await wire(proxy[1], b"CB-DNS-CHECK " + target + b" HTTP/1.1\r\nHost: ignored\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 403")
    assert b"X-Cloud-Browser-DNS-Policy: public-v1" in reply


async def test_approval_is_not_cached_across_actual_connect(proxy, monkeypatch):
    targets = no_target_connections(monkeypatch)
    calls = []

    def resolve(host, port):
        calls.append((host, port))
        if len(calls) == 1:
            return ["93.184.215.14"]
        raise ValueError("Rebound to a denied private address")

    monkeypatch.setattr(egress, "public_addresses", resolve)
    await asyncio.to_thread(security.validate_url, "https://example.com/", dns_proxy=proxy[0])
    reply = await wire(proxy[1], b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 403")
    assert len(calls) == 2 and targets == []


@pytest.mark.parametrize(
    "reply",
    [
        b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n",
        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 204 No Content\r\nX-Cloud-Browser-DNS-Policy: other\r\n\r\n",
        b"HTTP/1.1 500 Error\r\nX-Cloud-Browser-DNS-Policy: public-v1\r\nContent-Length: 0\r\n\r\n",
    ],
    ids=["missing-policy", "old-proxy", "wrong-policy", "unavailable"],
)
async def test_unrecognized_proxy_policy_fails_closed(reply):
    async def responder(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(reply)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(responder, "127.0.0.1", 0)
    try:
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        with pytest.raises(BrowserError) as exc:
            await asyncio.to_thread(security.validate_url, "https://example.com/", dns_proxy=url)
        assert exc.value.code == "EGRESS_UNAVAILABLE"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "endpoint",
    ["socks5://proxy:1080", "https://proxy/", "http://user:secret@proxy/", "http://proxy/path"],
)
def test_unsupported_validation_endpoints_do_not_fall_back(endpoint, monkeypatch):
    monkeypatch.setattr(security, "public_addresses", browser_dns_unavailable)
    with pytest.raises(BrowserError) as exc:
        security.validate_url("https://example.com/", dns_proxy=endpoint)
    assert exc.value.code == "EGRESS_UNAVAILABLE"


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://127.0.0.1/", "http://[::1]/", "https://@example.com/"]
)
def test_local_syntax_and_literal_checks_still_run_before_proxy(url, monkeypatch):
    monkeypatch.setattr(security, "_proxy_dns_check", browser_dns_unavailable)
    with pytest.raises(BrowserError) as exc:
        security.validate_url(url, dns_proxy="http://egress:3128")
    assert exc.value.code == "INVALID_URL"


def test_native_validation_still_resolves_locally(monkeypatch):
    def unavailable(*args):
        raise OSError("Simulated native DNS failure")

    monkeypatch.setattr(security, "public_addresses", unavailable)
    monkeypatch.setattr(security, "_proxy_dns_check", browser_dns_unavailable)
    with pytest.raises(BrowserError) as exc:
        security.validate_url("https://example.com/")
    assert exc.value.code == "INVALID_URL"


async def test_stopped_proxy_blocks_open_without_local_dns_or_session(service, monkeypatch):
    server = await asyncio.start_server(egress.EgressProxy().handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    service.cfg.network_isolated = True
    service.cfg.browser_proxy = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(security, "public_addresses", browser_dns_unavailable)
    result = await service.call("open", url="https://example.com/")
    assert result["error"]["code"] == "EGRESS_UNAVAILABLE"
    assert not service.sessions and not service.worker.calls


async def test_service_open_checks_egress_before_allocating_session(service, proxy, monkeypatch):
    service.cfg.network_isolated = True
    service.cfg.browser_proxy = proxy[0]

    def denied(*args):
        raise ValueError("Private destination")

    monkeypatch.setattr(egress, "public_addresses", denied)
    result = await service.call("open", url="https://example.com/")
    assert result["error"]["code"] == "INVALID_URL"
    assert not service.sessions and not service.worker.calls
    monkeypatch.setattr(egress, "public_addresses", lambda *args: ["93.184.215.14"])
    assert (await service.call("open", url="https://example.com/"))["status"] == "ok"


@pytest.mark.parametrize("operation", ["open", "goto", "back", "forward"])
def test_adapter_checks_proxy_before_navigation_or_browser_start(operation, monkeypatch):
    adapter = object.__new__(DrissionAdapter)
    adapter.cfg = SimpleNamespace(network_isolated=True, browser_proxy="http://egress:3128")
    checks = []

    def denied(url, *, dns_proxy=None):
        checks.append((url, dns_proxy))
        raise BrowserError("EGRESS_UNAVAILABLE", "Simulated unavailable proxy")

    def cdp(command):
        assert command == "Page.getNavigationHistory"
        return {
            "currentIndex": 1,
            "entries": [{"id": i, "url": "https://example.com/"} for i in range(3)],
        }

    state = SimpleNamespace(tab=SimpleNamespace(url="https://example.com/", run_cdp=cdp))
    adapter._tab = lambda *args: state
    monkeypatch.setattr("cloud_browser.drission.validate_url", denied)
    with pytest.raises(BrowserError) as exc:
        if operation == "open":
            adapter.open("sid", "https://example.com/")
        else:
            adapter.navigate(
                "sid", "tid", operation, "https://example.com/" if operation == "goto" else None
            )
    assert exc.value.code == "EGRESS_UNAVAILABLE"
    assert checks == [("https://example.com/", "http://egress:3128")]
