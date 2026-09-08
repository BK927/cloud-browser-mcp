import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

from conftest import FakeWorker
from fastapi.testclient import TestClient

from cloud_browser.security import public_document_csp
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
    assert page.headers["referrer-policy"] == "same-origin"
    assert page.headers.get_list("content-security-policy") == [
        public_document_csp(params["redirect_uri"])
    ]
    nonce = re.search("name=nonce value='([^']+)'", page.text)[1]
    result = client.post(
        "/authorize",
        data={"nonce": nonce, "password": "test administrator password"},
        headers={"Origin": cfg.public_origin},
        follow_redirects=False,
    )
    assert result.status_code == 303, result.text
    assert result.headers["referrer-policy"] == "no-referrer"
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
        assert page.headers["referrer-policy"] == "same-origin"
        assert "form-action 'self';" in page.headers["content-security-policy"]
        cookie = client.cookies.get("cb_login")
        icon = client.get("/favicon.ico", follow_redirects=False)
        assert icon.status_code == 204
        assert "set-cookie" not in icon.headers and "location" not in icon.headers
        assert client.cookies.get("cb_login") == cookie
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


def test_authorization_csp_uses_only_validated_callback_and_defaults_elsewhere(cfg):
    cfg.oauth_redirect_uris = [
        "https://callback.example/oauth/return?tenant=personal",
        "https://second.example/different-return",
    ]
    public, control, _, _ = create_apps(cfg, worker=FakeWorker())
    with TestClient(public, base_url=cfg.public_origin) as client:
        authorize(client, cfg)
        query = {
            "client_id": cfg.oauth_client_id,
            "redirect_uri": cfg.oauth_redirect_uris[0],
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": "A" * 43,
            "resource": cfg.resource,
            "scope": "browser",
            "state": "csp-test",
        }
        page = client.get("/authorize", params=query)
        assert page.status_code == 200
        csp = page.headers["content-security-policy"]
        assert "https://callback.example/oauth/return;" in csp
        assert "second.example" not in csp and "tenant=" not in csp
        metadata = client.get("/.well-known/oauth-authorization-server")
        assert metadata.headers.get_list("content-security-policy") == [public_document_csp()]
        assert metadata.headers["referrer-policy"] == "no-referrer"
        for uri in (
            "https://callback.example/oauth/other",
            "https://callback.example/oauth/return?tenant=someone-else",
            "https://callback.example/oauth/return/",
            "https://callback.example.evil.test/oauth/return?tenant=personal",
            "http://callback.example/oauth/return?tenant=personal",
        ):
            rejected = client.get("/authorize", params=query | {"redirect_uri": uri})
            assert rejected.status_code == 400
            assert rejected.headers["content-security-policy"] == public_document_csp()
            assert rejected.headers["referrer-policy"] == "no-referrer"
        assert client.post("/authorize", headers={"Origin": "null"}).status_code == 403
        with TestClient(control, base_url=cfg.control_origin) as private:
            assert private.post("/login", headers={"Origin": "null"}).status_code == 403
            assert private.get("/healthz").headers["referrer-policy"] == "no-referrer"


def test_callback_csp_does_not_embed_query_or_new_directives():
    csp = public_document_csp("https://callback.example/path;injected 'self'\r\n?q=secret#hidden")
    assert len(csp.split(";")) == 4
    assert "q=secret" not in csp and "hidden" not in csp
    assert "\r" not in csp and "\n" not in csp
    assert "default-src 'none'" in csp and "base-uri 'none'" in csp
    form = csp.split(";")[1].strip().split()
    assert len(form) == 3 and form[:2] == ["form-action", "'self'"]
    assert form[2].startswith("https://callback.example/path%3B")
    assert "*" not in csp and "unsafe-inline" not in csp
    assert public_document_csp("https://callback.example/a%20b?x=1") == (
        "default-src 'none'; form-action 'self' https://callback.example/a%20b; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
