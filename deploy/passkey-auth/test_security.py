"""Real WebAuthn signatures, replay resistance, enrollment ownership and OAuth separation."""
import hashlib
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import cbor2
import httpx
import pytest
from app import OWNER, Portal, allowed_callback, encode
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient
from store import Store

ORIGIN = "https://home-mcp.example.test"


@pytest.fixture
def system(tmp_path):
    secret_file = tmp_path / "environment"
    secret_file.write_text('MCP_OAUTH_LOGIN_SECRET="' + "x" * 40 + '"\n')
    posts = []

    def backend(request):
        if request.method == "GET":
            return httpx.Response(200 if request.url.params.get("transaction") == "valid" else 400,
                                  text="signed transaction accepted")
        body = parse_qs(request.content.decode())
        assert body == {"transaction": ["valid"], "access_key": ["x" * 40]}
        posts.append(body)
        return httpx.Response(302, headers={"Location":
            "https://chatgpt.com/connector_platform_oauth_redirect?code=one-use-code&state=client-state"})

    store = Store(str(tmp_path / "state.sqlite"))
    portal = Portal({"origin": ORIGIN, "services": [{"id": "one", "name": "MCP One",
        "login_url": "http://127.0.0.1:8084/oauth/login", "host": "home-mcp.example.test",
        "environment_file": str(secret_file)}]}, store, httpx.MockTransport(backend))
    client = TestClient(portal.app, base_url=ORIGIN)
    assert client.get("/").status_code == 200
    return portal, store, client, posts


def post(client, path, body, **headers):
    csrf = client.get("/api/status").json()["csrf"]
    return client.post("/api/" + path, json=body,
                       headers={"Origin": ORIGIN, "X-Passkey-CSRF": csrf, **headers})


def registration(options, key=None, credential_id=None, *, origin=ORIGIN, flags=0x45, rp=None):
    key = key or ec.generate_private_key(ec.SECP256R1())
    credential_id = credential_id or secrets.token_bytes(32)
    point = key.public_key().public_numbers()
    public_key = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: point.x.to_bytes(32,"big"), -3: point.y.to_bytes(32,"big")})
    data = (hashlib.sha256((rp or options["rp"]["id"]).encode()).digest() + bytes([flags]) + bytes(4)
            + bytes(16) + len(credential_id).to_bytes(2,"big") + credential_id + public_key)
    client_data = json.dumps({"type": "webauthn.create", "challenge": options["challenge"],
                              "origin": origin, "crossOrigin": False}).encode()
    credential = {"id": encode(credential_id), "rawId": encode(credential_id), "type": "public-key",
        "response": {"clientDataJSON": encode(client_data),
                     "attestationObject": encode(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": data})),
                     "transports": ["internal"]}, "clientExtensionResults": {}, "authenticatorAttachment": "platform"}
    return key, credential_id, credential


def assertion(options, key, credential_id, *, origin=ORIGIN, rp=None, flags=0x05, counter=1,
              challenge=None, wrong_signature=False, user_handle=OWNER):
    client_data = json.dumps({"type": "webauthn.get", "challenge": challenge or options["challenge"],
                              "origin": origin, "crossOrigin": False}).encode()
    auth_data = hashlib.sha256((rp or options["rpId"]).encode()).digest() + bytes([flags]) + counter.to_bytes(4,"big")
    signature = key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
    if wrong_signature:
        signature = bytes([signature[0] ^ 1]) + signature[1:]
    return {"id": encode(credential_id), "rawId": encode(credential_id), "type": "public-key",
        "response": {"clientDataJSON": encode(client_data), "authenticatorData": encode(auth_data),
                     "signature": encode(signature), "userHandle": encode(user_handle)},
        "clientExtensionResults": {}, "authenticatorAttachment": "platform"}


def enroll(system, label="PC"):
    _, store, client, _ = system
    ticket = store.put("enroll", {"label": label}, 300)
    start = post(client, "register/options", {"ticket": ticket})
    assert start.status_code == 200, start.text
    start = start.json()
    key, cid, credential = registration(start["options"])
    result = post(client, "register/verify", {"ceremony": start["ceremony"], "credential": credential})
    assert result.status_code == 200, result.text
    return key, cid


def test_closed_enrollment_and_cookie_security(system):
    _, store, client, posts = system
    assert post(client, "register/options", {"ticket": "invented"}).status_code == 403
    assert post(client, "enroll-ticket", {"label": "intruder"}).status_code == 403
    assert post(client, "connect", {"flow": "invented"}).status_code == 403
    assert not store.credentials() and not posts
    other = TestClient(system[0].app, base_url=ORIGIN)
    cookie = other.get("/").headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


@pytest.mark.parametrize("change", ["origin", "csrf", "missing_origin", "non_json"])
def test_csrf_and_origin(system, change):
    _, _, client, _ = system
    headers = {"Origin": ORIGIN, "X-Passkey-CSRF": client.get("/api/status").json()["csrf"]}
    if change == "origin": headers["Origin"] = "https://evil.example"
    if change == "csrf": headers["X-Passkey-CSRF"] = "invalid"
    if change == "missing_origin": headers.pop("Origin")
    result = (client.post("/api/register/options", content='{}', headers=headers) if change == "non_json"
              else client.post("/api/register/options", json={"ticket": "unknown"}, headers=headers))
    assert result.status_code == 403


def test_register_real_key_ticket_and_ceremony_are_single_use(system):
    _, store, client, _ = system
    ticket = store.put("enroll", {"label": "Laptop"}, 300)
    start = post(client, "register/options", {"ticket": ticket}).json()
    assert post(client, "register/options", {"ticket": ticket}).status_code == 403
    _, cid, cred = registration(start["options"])
    body = {"ceremony": start["ceremony"], "credential": cred}
    assert post(client, "register/verify", body).status_code == 200
    assert post(client, "register/verify", body).status_code == 403
    assert store.credential(encode(cid))["label"] == "Laptop"
    assert client.get("/api/status").json()["authenticated"] is True
    reopened = Store(store.path)
    assert reopened.credential(encode(cid))["public_key"]


@pytest.mark.parametrize("kwargs", [{"origin": "https://evil.example"}, {"rp": "evil.example"},
                                    {"flags": 0x41}, {"flags": 0x4D}])
def test_registration_requires_correct_origin_rp_uv_and_device_binding(system, kwargs):
    _, store, client, _ = system
    ticket = store.put("enroll", {"label": "PC"}, 300)
    start = post(client, "register/options", {"ticket": ticket}).json()
    _, _, cred = registration(start["options"], **kwargs)
    assert post(client, "register/verify", {"ceremony": start["ceremony"], "credential": cred}).status_code == 403
    assert not store.credentials()


def test_real_authentication_and_replay(system):
    _, store, client, _ = system
    key, cid = enroll(system)
    assert post(client, "logout", {}).status_code == 200
    client.get("/")
    start = post(client, "auth/options", {}).json()
    body = {"ceremony": start["ceremony"], "credential": assertion(start["options"], key, cid)}
    assert post(client, "auth/verify", body).status_code == 200
    assert post(client, "auth/verify", body).status_code == 403
    assert store.credential(encode(cid))["sign_count"] == 1


def test_registration_completion_is_confirmed_by_server_session(system):
    _, _, client, _ = system
    assert client.get("/api/status?registered=1").json()["registration_completed"] is False
    key, cid = enroll(system, "Google passkey")
    status = client.get("/api/status").json()
    assert status["registration_completed"] is True
    assert status["current_passkey"]["label"] == "Google passkey"
    post(client, "logout", {})
    client.get("/")
    assert client.get("/api/status").json()["registration_completed"] is False
    start = post(client, "auth/options", {}).json()
    assert post(client, "auth/verify", {"ceremony": start["ceremony"], "credential": assertion(start["options"], key, cid)}).status_code == 200
    status = client.get("/api/status?registered=1").json()
    assert status["authenticated"] is True and status["registration_completed"] is False


@pytest.mark.parametrize("registration_flags,authentication_flags", [(0x4D, 0x0D), (0x5D, 0x1D)])
def test_synced_passkey_policy_preserves_real_signature_and_verification(system, registration_flags, authentication_flags):
    portal, store, client, _ = system
    portal.device_bound = False
    ticket = store.put("enroll", {"label": "Synced owner passkey"}, 300)
    start = post(client, "register/options", {"ticket": ticket}).json()
    key, cid, credential = registration(start["options"], flags=registration_flags)
    assert post(client, "register/verify", {"ceremony": start["ceremony"], "credential": credential}).status_code == 200
    assert store.credential(encode(cid))["device_type"] == "multi_device"
    post(client, "logout", {})

    # Another browser uses the same synced private key; zero counters remain valid.
    other = TestClient(portal.app, base_url=ORIGIN)
    other.get("/")
    start = post(other, "auth/options", {}).json()
    valid = assertion(start["options"], key, cid, flags=authentication_flags, counter=0)
    assert post(other, "auth/verify", {"ceremony": start["ceremony"], "credential": valid}).status_code == 200
    assert other.get("/api/status").json()["authenticated"] is True
    post(other, "logout", {})
    other.get("/")
    start = post(other, "auth/options", {}).json()
    invalid = assertion(start["options"], key, cid, flags=authentication_flags, counter=0, wrong_signature=True)
    assert post(other, "auth/verify", {"ceremony": start["ceremony"], "credential": invalid}).status_code == 403
    assert other.get("/api/status").json()["authenticated"] is False


@pytest.mark.parametrize("kwargs", [{"origin": "https://evil.example"}, {"rp": "evil.example"},
    {"flags": 0x01}, {"wrong_signature": True}, {"challenge": "wrong"}, {"user_handle": b"other-owner"}])
def test_invalid_assertions_do_not_authenticate(system, kwargs):
    _, _, client, _ = system
    key, cid = enroll(system)
    post(client, "logout", {})
    client.get("/")
    start = post(client, "auth/options", {}).json()
    response = assertion(start["options"], key, cid, **kwargs)
    assert post(client, "auth/verify", {"ceremony": start["ceremony"], "credential": response}).status_code == 403
    assert client.get("/api/status").json()["authenticated"] is False


def test_auth_ceremony_cannot_be_used_in_other_browser(system):
    portal, _, client, _ = system
    key, cid = enroll(system)
    start = post(client, "auth/options", {}).json()
    other = TestClient(portal.app, base_url=ORIGIN)
    other.get("/")
    body = {"ceremony": start["ceremony"], "credential": assertion(start["options"], key, cid)}
    assert post(other, "auth/verify", body).status_code == 403


def test_expired_and_atomically_consumed_records(system):
    _, store, _, _ = system
    expired = store.put("enroll", {"label": "PC"}, -1)
    assert store.get("enroll", expired, True) is None
    ticket = store.put("enroll", {"label": "PC"}, 300)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: store.get("enroll", ticket, True), range(8)))
    assert sum(result is not None for result in results) == 1


def test_oauth_connection_only_after_passkey_and_only_once(system):
    _, _, client, posts = system
    assert client.get("/login/one?transaction=forged").status_code == 400
    start = client.get("/login/one?transaction=valid", follow_redirects=False)
    assert start.status_code == 303 and start.headers["location"].startswith(ORIGIN + "/auth/connect?")
    flow = parse_qs(urlsplit(start.headers["location"]).query)["flow"][0]
    assert post(client, "connect", {"flow": flow}).status_code == 403 and not posts
    assert client.get("/api/status?flow=" + flow).json()["target"] == "MCP One"
    enroll(system)
    result = post(client, "connect", {"flow": flow})
    assert result.status_code == 200 and allowed_callback(result.json()["redirect"])
    assert len(posts) == 1
    assert post(client, "connect", {"flow": flow}).status_code == 403 and len(posts) == 1


def test_remove_lost_device_revokes_its_sessions_and_keeps_last_key(system):
    portal, store, client, _ = system
    key, cid = enroll(system, "Main PC")
    old_cookie = client.cookies.get("__Host-personal-mcp-passkey")
    enroll(system, "Laptop")
    assert post(client, "remove", {"id": encode(cid)}).status_code == 200
    assert store.session(old_cookie) is None
    last = store.credentials()[0]["id"]
    assert post(client, "remove", {"id": last}).status_code == 403
    assert store.credential(last)


@pytest.mark.parametrize("location", ["https://evil.example/connector_platform_oauth_redirect", 
    "https://chatgpt.com@evil.example/connector_platform_oauth_redirect", 
    "http://chatgpt.com/connector_platform_oauth_redirect", "https://chatgpt.com:443/connector_platform_oauth_redirect",
    "https://chatgpt.com/other-path", "https://chatgpt.com/connector_platform_oauth_redirect\r\nX: y"])
def test_callback_allowlist(location):
    assert not allowed_callback(location)
