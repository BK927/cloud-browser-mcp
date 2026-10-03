import base64
import hashlib
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import FakeWorker
from fastapi.testclient import TestClient

from cloud_browser.bridge_proof import sign
from cloud_browser.server import create_apps

SECRET = "cloud-browser-bridge-" * 3


@pytest.mark.parametrize("kind", ["oauth", "control"])
@pytest.mark.parametrize("change", ["valid", "wrong_nonce", "wrong_audience", "expired", "wrong_secret", "wrong_origin", "wrong_cookie"])
def test_passkey_bridge_keeps_nonce_origin_pkce_and_session_checks(cfg, kind, change):
    cfg.passkey_bridge_secret = SECRET
    public, control, _, auth = create_apps(cfg, worker=FakeWorker())
    origin = cfg.public_origin if kind == "oauth" else cfg.control_origin
    path = "/authorize" if kind == "oauth" else "/login"
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {"client_id": cfg.oauth_client_id, "redirect_uri": cfg.oauth_redirect_uris[0],
              "response_type": "code", "code_challenge_method": "S256", "code_challenge": challenge,
              "resource": cfg.resource, "scope": "browser", "state": "bridge-test"} if kind == "oauth" else {}
    with TestClient(public if kind == "oauth" else control, base_url=origin) as client:
        page = client.get(path, params=params)
        assert page.status_code == 200
        nonce = re.search("name=nonce value='([^']+)'", page.text)[1]
        proof = sign(SECRET if change != "wrong_secret" else "wrong" * 12,
                     nonce if change != "wrong_nonce" else "x" * 43,
                     kind if change != "wrong_audience" else ("control" if kind == "oauth" else "oauth"),
                     int(time.time()) - (61 if change == "expired" else 0))
        headers = {"Origin": origin if change != "wrong_origin" else "https://evil.example"}
        if change == "wrong_cookie":
            headers["Cookie"] = ("cb_oauth" if kind == "oauth" else "cb_login") + "=wrong"
        form = {"nonce": nonce, "passkey_assertion": proof}
        result = client.post(path, data=form, headers=headers, follow_redirects=False)
        if change != "valid":
            assert result.status_code in (401, 403)
            return
        assert result.status_code == 303
        assert client.post(path, data=form, headers=headers, follow_redirects=False).status_code == 403
        if kind == "control":
            assert client.get("/").status_code == 200
            assert "HttpOnly" in result.headers["set-cookie"]
            assert client.post("/revoke-all", data={}, headers={"Origin": origin}).status_code == 403
        else:
            query = parse_qs(urlsplit(result.headers["location"]).query)
            assert query["state"] == ["bridge-test"]
            token_form = {"grant_type": "authorization_code", "code": query["code"][0],
                          "code_verifier": verifier, "client_id": cfg.oauth_client_id,
                          "resource": cfg.resource, "redirect_uri": cfg.oauth_redirect_uris[0]}
            tokens = client.post("/token", data=token_form)
            assert tokens.status_code == 200
            assert auth.bearer("Bearer " + tokens.json()["access_token"])
            assert client.post("/token", data=token_form).status_code == 400
            client.post("/revoke", data={"client_id": cfg.oauth_client_id, "token": tokens.json()["refresh_token"]})
            assert not auth.bearer("Bearer " + tokens.json()["access_token"])
