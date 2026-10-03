import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from app import Portal
from bridge_proof import sign, verify
from starlette.testclient import TestClient
from store import Store
from test_security import ORIGIN, enroll, post

SECRET = "bridge-secret-" * 4
NONCE = "n" * 43


def test_proof_is_bound_to_secret_nonce_audience_and_time():
    proof = sign(SECRET, NONCE, "oauth")
    assert verify(SECRET, NONCE, "oauth", proof)
    assert not verify(SECRET, "x" * 43, "oauth", proof)
    assert not verify(SECRET, NONCE, "control", proof)
    assert not verify("other" * 10, NONCE, "oauth", proof)
    assert not verify(SECRET, NONCE, "oauth", sign(SECRET, NONCE, "oauth", int(time.time()) - 61))
    assert not verify(SECRET, NONCE, "oauth", "malformed")
    assert not verify("", NONCE, "oauth", proof)


@pytest.fixture
def browser_system(tmp_path):
    env = tmp_path / "bridge-env"
    env.write_text("CB_PASSKEY_BRIDGE_SECRET=" + SECRET + "\n")
    posts = []

    def backend(request):
        control = request.url.port == 8001
        audience = "control" if control else "oauth"
        cookie = "cb_login" if control else "cb_oauth"
        if request.method == "GET":
            return httpx.Response(200, text=f"<input type=hidden name=nonce value='{NONCE}'>",
                                  headers={"Set-Cookie": f"{cookie}={NONCE}; Secure; HttpOnly"})
        body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert request.headers["origin"] == ORIGIN + (":9443" if control else "")
        assert request.headers["host"] == "home-mcp.example.test" + (":9443" if control else "")
        assert request.headers["cookie"] == cookie + "=" + NONCE
        assert verify(SECRET, NONCE, audience, body["passkey_assertion"])
        assert "password" not in body
        posts.append(audience)
        if control:
            return httpx.Response(303, headers={"Location": "/", "Set-Cookie": "cb_control=" + "c" * 43 + "; Secure; HttpOnly"})
        return httpx.Response(303, headers={"Location": "https://chatgpt.com/connector_platform_oauth_redirect?code=one-use&state=state"})

    store = Store(str(tmp_path / "state.sqlite"))
    services = [{"id": ident, "name": ident, "kind": "cloud-browser-" + kind,
                 "login_url": f"http://127.0.0.1:{port}/" + path,
                 "host": "home-mcp.example.test" + (":9443" if kind == "control" else ""),
                 "environment_file": str(env)}
                for ident, kind, port, path in [("browser", "oauth", 8000, "authorize"),
                                               ("browser-control", "control", 8001, "login")]]
    portal = Portal({"origin": ORIGIN, "services": services}, store, httpx.MockTransport(backend))
    client = TestClient(portal.app, base_url=ORIGIN)
    client.get("/")
    return portal, store, client, posts


def flow_for(client, ident):
    result = client.get("/login/" + ident, follow_redirects=False)
    assert result.status_code == 303
    return parse_qs(urlsplit(result.headers["location"]).query)["flow"][0]


def test_browser_oauth_requires_passkey_then_uses_bound_proof_once(browser_system):
    _, _, client, posts = browser_system
    flow = flow_for(client, "browser")
    assert post(client, "connect", {"flow": flow}).status_code == 403 and not posts
    enroll(browser_system)
    assert post(client, "connect", {"flow": flow}).status_code == 200
    assert posts == ["oauth"]
    assert post(client, "connect", {"flow": flow}).status_code == 403


def test_private_console_handoff_requires_private_host_and_same_authenticated_session(browser_system):
    portal, _, client, _ = browser_system
    enroll(browser_system)
    response = post(client, "connect", {"flow": flow_for(client, "browser-control")})
    target = response.json()["redirect"]
    assert target.startswith(ORIGIN + ":9443/passkey/finish?")
    query = urlsplit(target).query
    assert client.get("/finish-control?" + query, follow_redirects=False).status_code == 403
    # A different browser cannot redeem even a copied handoff URL.
    other = TestClient(portal.app, base_url=ORIGIN)
    other.get("/")
    assert other.get(ORIGIN + ":9443/finish-control?" + query, follow_redirects=False).status_code == 403
    result = client.get(ORIGIN + ":9443/finish-control?" + query, follow_redirects=False)
    assert result.status_code == 303 and result.headers["location"] == ORIGIN + ":9443/"
    cookie = result.headers["set-cookie"]
    assert "cb_control=" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert client.get(ORIGIN + ":9443/finish-control?" + query, follow_redirects=False).status_code == 403
