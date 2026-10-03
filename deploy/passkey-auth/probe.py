"""Non-mutating MCP checks plus expiring OAuth requests; no login secrets or tokens emitted."""
import argparse
import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--active", action="store_true")
args = parser.parse_args()
origin = json.loads(Path(os.environ["MCP_PASSKEY_CONFIG"]).read_text())["origin"].rstrip("/")
checks = []
with httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client:
    portal = client.get(origin + "/auth/healthz")
    assert portal.status_code == 200 and portal.json()["ok"]
    for ident in ("youtube", "dlsite", "steam", "bilibili", "taptap", "reddit"):
        path = "" if ident == "youtube" else "/" + ident
        health = client.get(origin + path + "/healthz")
        mcp_url = origin + path + "/mcp"
        unauthorized = client.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "passkey-migration-check", "version": "1.0"}}},
            headers={"Accept": "application/json, text/event-stream"})
        assert health.status_code == 200 and unauthorized.status_code == 401, ident
        metadata = client.get(origin + "/.well-known/oauth-authorization-server" + path).json()
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        authorization = client.get(metadata["authorization_endpoint"], params={
            "client_id": "https://chatgpt.com/oauth/client.json", "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
            "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256",
            "resource": mcp_url, "scope": " ".join(metadata["scopes_supported"]), "state": secrets.token_urlsafe(16)})
        assert authorization.status_code in (302, 303), ident
        login = client.get(authorization.headers["location"])
        if args.active:
            assert login.status_code == 303 and login.headers["location"].startswith(origin + "/auth/connect?"), ident
            bridge = login
        else:
            assert login.status_code == 200, ident
            from urllib.parse import parse_qs, urlsplit
            txn = parse_qs(urlsplit(authorization.headers["location"]).query)["transaction"][0]
            bridge = client.get(origin + f"/auth/login/{ident}", params={"transaction": txn})
            assert bridge.status_code == 303 and bridge.headers["location"].startswith(origin + "/auth/connect?"), ident
        screen = client.get(bridge.headers["location"])
        assert screen.status_code == 200 and "Home MCP" in screen.text, ident
        checks.append({"service": ident, "health": health.status_code, "unauthenticated_mcp": unauthorized.status_code,
                       "passkey_adapter": "ready", "public_login_switched": args.active})
print(json.dumps({"checks": checks}, ensure_ascii=False))
