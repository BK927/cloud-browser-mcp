"""Switch OAuth login entry points only after real owner enrollment; retain an explicit rollback."""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from store import Store

parser = argparse.ArgumentParser()
parser.add_argument("--rollback", action="store_true")
args = parser.parse_args()
config_path = Path(os.environ["MCP_PASSKEY_CONFIG"])
config = json.loads(config_path.read_text())
store = Store(config["database"])
if not args.rollback and not any(row["last_used"] > 0 for row in store.credentials()):
    raise SystemExit("Register a real passkey and confirm sign-in before changing existing login routes.")
backup_path = config_path.parent / f"funnel-before-passkeys-{int(time.time())}.json"
before = json.loads(subprocess.check_output(["tailscale", "serve", "status", "--json"]))
backup_path.write_text(json.dumps(before))
os.chmod(backup_path, 0o600)
changed = []
def change(mode, port, path, target):
    key = f"{urlsplit(config['origin']).hostname}:{port}"
    previous = before.get("Web", {}).get(key, {}).get("Handlers", {}).get(path, {}).get("Proxy")
    changed.append((mode, port, path, previous))
    subprocess.run(["tailscale", mode, "--bg", f"--https={port}", f"--set-path={path}", target], check=True)

try:
    for service in config["services"]:
        target = (service["login_url"] if args.rollback else f"http://127.0.0.1:8090/login/{service['id']}")
        mode = "serve" if service.get("private") else "funnel"
        port = service.get("public_port", 443)
        change(mode, port, service["public_login"], target)
    steam = next(s for s in config["services"] if s["id"] == "steam")
    target = steam["login_url"] if args.rollback else "http://127.0.0.1:8090/login/steam"
    change("funnel", 8443, "/oauth/login", target)
    if "browser" in {s["id"] for s in config["services"]}:
        browser = next(s for s in config["services"] if s["id"] == "browser")
        target = browser["login_url"] if args.rollback else "http://127.0.0.1:8090/login/browser"
        change("funnel", 10000, "/authorize", target)
        if args.rollback:
            change("serve", 9443, "/passkey/finish", "off")
        else:
            change("serve", 9443, "/passkey/finish", "http://127.0.0.1:8090/finish-control")
except BaseException:
    # Leave every changed route with its original authenticated provider on failure.
    for mode, port, path, original in reversed(changed):
        subprocess.run(["tailscale", mode, "--bg", f"--https={port}", f"--set-path={path}", original or "off"], check=False)
    raise
print(json.dumps({"passkey_login_active": not args.rollback, "services": [s["id"] for s in config["services"]]}))
