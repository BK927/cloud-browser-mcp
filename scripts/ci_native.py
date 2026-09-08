#!/usr/bin/env python3
"""Disposable GitHub Ubuntu runner smoke for the real native unit/network code.

This is NOT a supported Ubuntu installer. Debian 13 installation/Pi measurements
are separate gates. Refuses non-CI hosts and pre-existing native installations.
"""

import json
import os
import secrets
import subprocess
from pathlib import Path
from types import SimpleNamespace

import native_install as installer
from argon2 import PasswordHasher


def main():
    if (
        os.geteuid() != 0
        or os.getenv("GITHUB_ACTIONS") != "true"
        or os.getenv("RUNNER_OS") != "Linux"
    ):
        raise RuntimeError(
            "This destructive-to-its-own-fixture test requires a disposable GitHub Linux runner"
        )
    if installer.PREFIX.exists() or installer.ETC.exists():
        raise RuntimeError("CI must not touch an existing native installation")
    for name in installer.NAMES:
        if (installer.UNITS / (name + ".service")).exists():
            raise RuntimeError("CI unit name is already owned")
    source = Path(__file__).resolve().parents[1]
    environment = Path("/run/cloud-browser-ci.env")
    installer.write(
        environment,
        "\n".join(
            [
                "CB_PUBLIC_ORIGIN=https://ci-mcp.example",
                "CB_CONTROL_ORIGIN=https://ci-control.example",
                'CB_OAUTH_REDIRECT_URIS=["https://chatgpt.com/connector_platform_oauth_redirect"]',
                "CB_ADMIN_PASSWORD_HASH='" + PasswordHasher().hash(secrets.token_urlsafe(32)) + "'",
                "CB_MANUAL_CONTROL_ENABLED=true",
                "CB_APPROVAL_POLICY=balanced",
                "CB_MEMORY_RESERVE_MB=64",
                "CB_MEMORY_PER_TAB_MB=32",
            ]
        )
        + "\n",
        0o600,
    )
    original_command = installer.command

    def ci_command(*args, **kwargs):
        if args[:2] == ("apt-get", "install"):
            # Ubuntu's chromium package is a Snap wrapper. The runner already
            # supplies sandboxed Google Chrome; only this CI fixture swaps it.
            args = tuple(arg for arg in args if arg not in ("chromium", "chromium-sandbox"))
        return original_command(*args, **kwargs)

    installer.command = ci_command
    installer.CHROMIUM = "/usr/bin/google-chrome"
    installer.platform_check = lambda: None  # Guarded disposable CI fixture only.
    args = SimpleNamespace(
        source=str(source),
        env_file=str(environment),
        data_dir="/var/lib/cloud-browser-native-ci",
        cidr="10.203.87.0/30",
        memory_mib=1024,
        public_port=18000,
        control_port=18001,
        start=True,
        purge_data=False,
    )
    try:
        installer.install(args)
        release = Path(json.loads((installer.ETC / "install.json").read_text())["release"])
        python = str(release / ".venv/bin/python")
        original_command(
            python,
            "-I",
            "-m",
            "cloud_browser.native",
            "verify-network",
            "--config",
            installer.ETC / "native.json",
        )
        pid = subprocess.check_output(
            ["systemctl", "show", "cloud-browser.service", "-p", "MainPID", "--value"], text=True
        ).strip()
        original_command(
            python,
            source / "scripts/native_benchmark.py",
            "--pid",
            pid,
            "--script",
            source / "scripts/ci_runtime_probe.py",
        )
        # Actual kernel UID/namespace denial, with a known live loopback listener:
        # app can reach API health but browser UID cannot reach its control plane.
        code = "import socket; s=socket.socket(); s.settimeout(2); assert s.connect_ex(('10.203.87.2',8001)) != 0"
        original_command(
            "ip",
            "netns",
            "exec",
            "cb-browser",
            "runuser",
            "-u",
            "cb-browser",
            "--",
            "/usr/bin/python3",
            "-c",
            code,
        )
        code = "import socket; s=socket.create_connection(('10.203.87.1',3128),2); s.sendall(b'CONNECT 127.0.0.1:443 HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n\\r\\n'); assert b'403' in s.recv(1024)"
        original_command(
            "ip",
            "netns",
            "exec",
            "cb-browser",
            "runuser",
            "-u",
            "cb-browser",
            "--",
            "/usr/bin/python3",
            "-c",
            code,
        )
        print(
            json.dumps(
                {
                    "native_runtime_smoke": "passed",
                    "platform": "Ubuntu CI with Google Chrome; not a Debian/Pi performance result",
                    "budget_mib": 1024,
                }
            )
        )
    finally:
        # No data/profile deletion; runner disposal owns fixture removal. Logs
        # contain only generated test settings, never an operator credential.
        original_command(
            "journalctl",
            "--no-pager",
            "-n",
            "100",
            "-u",
            "cloud-browser.service",
            "-u",
            "cloud-browser-network.service",
            check=False,
        )
        original_command(
            "systemctl", "stop", *[name + ".service" for name in installer.NAMES], check=False
        )
        environment.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
