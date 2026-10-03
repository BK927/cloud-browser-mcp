"""Run via the existing admin SSH account. No secret values are printed."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

HOME = Path.home()
BASE = HOME / "services/mcp-passkey-auth"
SOURCE = Path(__file__).parent
RELEASE = BASE / "releases/20261003-2"
CONFIG_DIR = HOME / ".config/mcp-passkey-auth"
STATE_DIR = HOME / ".local/state/mcp-passkey-auth"
ORIGIN = os.environ.get("MCP_PASSKEY_ORIGIN", "")
if not ORIGIN:
    existing = CONFIG_DIR / "config.json"
    if existing.exists():
        ORIGIN = json.loads(existing.read_text())["origin"]
    else:
        raise SystemExit("Set MCP_PASSKEY_ORIGIN to the fixed HTTPS origin (no path or port).")


def run(*args):
    subprocess.run(args, check=True)


for folder in (RELEASE, CONFIG_DIR, STATE_DIR):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
for name in ("app.py", "bridge_proof.py", "store.py", "portal.html", "portal.js", "requirements.lock", "activate.py", "probe.py", "probe-browser-routes.py", "README.md"):
    shutil.copy2(SOURCE / name, RELEASE / name)
venv = BASE / ".venv"
if not (venv / "bin/python").exists():
    run(sys.executable, "-m", "venv", str(venv))
run(str(venv / "bin/python"), "-m", "pip", "install", "-r", str(RELEASE / "requirements.lock"))

services = []
for ident, title, port, environment in (
    ("youtube", "YouTube MCP", 8080, "youtube-mcp-aio"),
    ("dlsite", "DLsite MCP", 8081, "dlsite-mcp"),
    ("steam", "Steam MCP", 8082, "steam-mcp"),
    ("bilibili", "Bilibili MCP", 8084, "bilibili-mcp-gateway"),
    ("taptap", "TapTap MCP", 8085, "taptap-mcp-gateway"),
    ("reddit", "Reddit MCP", 8086, "reddit-mcp-gateway"),
):
    envfile = HOME / f".config/{environment}/env"
    if not envfile.is_file():
        raise SystemExit(f"missing environment for {ident}")
    # Only check the generated secret's presence; never emit its contents.
    lines = envfile.read_text().splitlines()
    if not any(line.startswith("MCP_OAUTH_LOGIN_SECRET=") for line in lines):
        raise SystemExit(f"missing OAuth login configuration for {ident}")
    services.append({"id": ident, "name": title, "login_url": f"http://127.0.0.1:{port}/oauth/login",
                     "host": urlsplit(ORIGIN).hostname, "environment_file": str(envfile),
                     "public_login": "/oauth/login" if ident == "youtube" else f"/{ident}/oauth/login"})
config_path = CONFIG_DIR / "config.json"
device_bound = False
if config_path.exists():
    previous = json.loads(config_path.read_text())
    if previous["origin"] != ORIGIN:
        raise SystemExit("existing passkey origin differs; do not replace it")
    device_bound = previous.get("device_bound", False)
    existing_ids = {service["id"] for service in services}
    services.extend(service for service in previous.get("services", []) if service["id"] not in existing_ids)
    shutil.copy2(config_path, CONFIG_DIR / f"config.before-{int(time.time())}.json")
config_path.write_text(json.dumps({"origin": ORIGIN, "database": str(STATE_DIR / "state.sqlite3"),
                                  "device_bound": device_bound, "services": services}, indent=2))
os.chmod(config_path, 0o600)
current = BASE / "current"
if current.is_symlink():
    current.unlink()
elif current.exists():
    raise SystemExit("current is not a managed symlink")
current.symlink_to(RELEASE, target_is_directory=True)

unit = HOME / ".config/systemd/user/mcp-passkey-auth.service"
unit.write_text(f"""[Unit]
Description=Personal MCP shared passkey login
After=network-online.target

[Service]
WorkingDirectory={current}
Environment=MCP_PASSKEY_CONFIG={config_path}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={venv}/bin/python -m uvicorn app:create_app --factory --host 127.0.0.1 --port 8090 --no-access-log --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={STATE_DIR}
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=default.target
""")
run("systemctl", "--user", "daemon-reload")
run("systemctl", "--user", "enable", "--now", "mcp-passkey-auth.service")
run("systemctl", "--user", "restart", "mcp-passkey-auth.service")
for attempt in range(20):
    try:
        request = urllib.request.Request("http://127.0.0.1:8090/healthz", headers={"Host": urlsplit(ORIGIN).hostname})
        with urllib.request.urlopen(request, timeout=2) as response:
            if json.load(response).get("version") == "1.0.0":
                break
    except Exception:
        time.sleep(0.5)
else:
    raise SystemExit("The passkey service did not become healthy; Funnel was not changed.")
# Add only the shared portal now. Existing OAuth entry points remain in place
# until a real device has been registered and an operator activates the adapters.
run("tailscale", "funnel", "--bg", "--https=443", "--set-path=/auth", "http://127.0.0.1:8090")
print(json.dumps({"deployed": True, "portal": ORIGIN + "/auth/", "services_prepared": [s["id"] for s in services],
                  "activation_requires_registered_device": True}))
