import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

from conftest import FakeWorker
from fastapi.testclient import TestClient

from cloud_browser.server import create_apps


def authorize(client, cfg):
    verifier = "a" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    params = {
        "client_id": cfg.oauth_client_id,
        "redirect_uri": cfg.oauth_redirect_uris[0],
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "resource": cfg.resource,
        "scope": "browser",
        "state": "test-state",
    }
    page = client.get("/authorize", params=params)
    assert page.status_code == 200
    nonce = re.search("name=nonce value='([^']+)'", page.text)[1]
    result = client.post(
        "/authorize",
        data={"nonce": nonce, "password": "test administrator password"},
        headers={"Origin": cfg.public_origin},
        follow_redirects=False,
    )
    assert result.status_code == 303, result.text
    parsed = parse_qs(urlsplit(result.headers["location"]).query)
    assert parsed["iss"] == [cfg.public_origin]
    assert parsed["state"] == ["test-state"]
    return {
        "grant_type": "authorization_code",
        "code": parsed["code"][0],
        "code_verifier": verifier,
        "client_id": cfg.oauth_client_id,
        "resource": cfg.resource,
        "redirect_uri": cfg.oauth_redirect_uris[0],
    }


def test_oauth_pkce_rotation_revocation_and_endpoint_separation(cfg):
    public, _, service, auth = create_apps(cfg, worker=FakeWorker())
    with TestClient(public, base_url=cfg.public_origin) as client:
        denied = client.post("/mcp", json={})
        assert denied.status_code == 401
        assert "resource_metadata" in denied.headers["www-authenticate"]
        assert client.get("/").status_code == 404
        assert client.get("/novnc/vnc.html").status_code == 404
        form = authorize(client, cfg)
        result = client.post("/token", data=form)
        assert result.status_code == 200, result.text
        token = result.json()
        assert auth.bearer("Bearer " + token["access_token"])
        assert client.post("/token", data=form).status_code == 400
        refresh_form = {
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": cfg.oauth_client_id,
            "resource": cfg.resource,
        }
        rotated = client.post("/token", data=refresh_form).json()
        assert "access_token" in rotated
        assert client.post("/token", data=refresh_form).status_code == 400
        client.post(
            "/revoke", data={"client_id": cfg.oauth_client_id, "token": rotated["refresh_token"]}
        )
        assert not auth.bearer("Bearer " + token["access_token"])
        assert not auth.bearer("Bearer " + rotated["access_token"])


def test_oauth_rejects_wrong_pkce_and_callbacks(cfg):
    public, _, _, _ = create_apps(cfg, worker=FakeWorker())
    with TestClient(public, base_url=cfg.public_origin) as client:
        assert (
            client.get("/authorize", params={"redirect_uri": "https://evil.example"}).status_code
            == 400
        )
        form = authorize(client, cfg)
        form["code_verifier"] = "b" * 64
        assert client.post("/token", data=form).status_code == 400
        assert (
            client.post("/authorize", headers={"Origin": "https://evil.example"}).status_code == 403
        )
        assert client.get("/.well-known/oauth-authorization-server").json()[
            "code_challenge_methods_supported"
        ] == ["S256"]


def test_throttle_persists(cfg):
    public, _, _, auth = create_apps(cfg, worker=FakeWorker())
    with TestClient(public, base_url=cfg.public_origin):
        assert not auth.limited("peer")
        for _ in range(8):
            last = auth.limited("peer")
        assert last


def test_console_cookie_and_csrf_required(cfg):
    public, control, _, _ = create_apps(cfg, worker=FakeWorker())
    with (
        TestClient(public, base_url=cfg.public_origin),
        TestClient(control, base_url=cfg.control_origin) as client,
    ):
        assert client.get("/", follow_redirects=False).status_code == 303
        page = client.get("/login")
        nonce = re.search("name=nonce value='([^']+)'", page.text)[1]
        result = client.post(
            "/login",
            data={"nonce": nonce, "password": "test administrator password"},
            headers={"Origin": cfg.control_origin},
            follow_redirects=False,
        )
        assert result.status_code == 303
        assert client.get("/").status_code == 200
        assert (
            client.post("/revoke-all", data={}, headers={"Origin": cfg.control_origin}).status_code
            == 403
        )
        assert (
            client.post(
                "/revoke-all", data={}, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
