"""Check public OAuth front door and tailnet-only console without exposing credentials."""
import base64
import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx

origin = json.loads(Path(os.environ["MCP_PASSKEY_CONFIG"]).read_text())["origin"].rstrip("/")
verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
params = {"client_id": "personal-cloud-browser", "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
          "response_type": "code", "code_challenge_method": "S256", "code_challenge": challenge,
          "scope": "browser", "resource": origin + "/browser/mcp", "state": "passkey-routing-check"}
checks = {}
with httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client:
    for path in ("/auth/login/browser", "/browser/authorize"):
        result = client.get(origin + path, params=params)
        assert result.status_code == 303 and result.headers["location"].startswith(origin + "/auth/connect?"), path
    denied = client.post(origin + "/browser/mcp", json={})
    assert denied.status_code == 401
    assert client.get(origin + "/auth/finish-control?ticket=invalid").status_code == 403
    checks["browser_oauth"] = "passkey-required"
    result = client.get(origin + ":10000/authorize", params=params)
    assert result.status_code == 303 and result.headers["location"].startswith(origin + "/auth/connect?")
    checks["browser_alternate_endpoint"] = "passkey-required"
    result = client.get(origin + ":9443/login")
    assert result.status_code == 303 and result.headers["location"].startswith(origin + "/auth/connect?")
    assert client.get(origin + ":9443/passkey/finish?ticket=invalid").status_code == 403
    checks["private_console"] = "passkey-required"
status = json.loads(subprocess.check_output(["tailscale", "serve", "status", "--json"]))
assert not status["AllowFunnel"].get(urlsplit(origin).hostname + ":9443", False)
assert status["Web"][urlsplit(origin).hostname + ":9443"]["Handlers"]["/"]["Proxy"] == "http://127.0.0.1:8001"
checks["console_remains_tailnet_only"] = True
print(json.dumps({"checks": checks}))
