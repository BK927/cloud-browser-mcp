#!/usr/bin/env python3
"""Disposable CI-only execution of the unchanged production Compose topology."""

import os
import secrets
import subprocess
import time
import urllib.request
from pathlib import Path

from argon2 import PasswordHasher


def main():
    if os.getenv("GITHUB_ACTIONS") != "true" or os.getenv("RUNNER_OS") != "Linux":
        raise RuntimeError("Docker crash/lifecycle smoke is restricted to a disposable CI runner")
    root = Path(__file__).resolve().parents[1]
    environment = root / ".env"
    if environment.exists() or environment.is_symlink():
        raise RuntimeError("CI refuses to overwrite an operator environment")
    existing = subprocess.check_output(
        ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=cb-ci"], text=True
    )
    if existing.strip():
        raise RuntimeError("CI project already has containers; do not take over another run")
    prefix = [
        "docker",
        "compose",
        "--project-name",
        "cb-ci",
        "--project-directory",
        str(root),
        "-f",
        str(root / "compose.yaml"),
    ]

    def compose(*args, **kwargs):
        return subprocess.run([*prefix, *args], check=True, **kwargs)

    with environment.open("x") as file:
        file.write(
            "\n".join(
                [
                    "CB_PUBLIC_ORIGIN=https://ci-mcp.example",
                    "CB_CONTROL_ORIGIN=https://ci-control.example",
                    'CB_OAUTH_REDIRECT_URIS=["https://chatgpt.com/connector_platform_oauth_redirect"]',
                    "CB_ADMIN_PASSWORD_HASH='"
                    + PasswordHasher().hash(secrets.token_urlsafe(32))
                    + "'",
                    "CB_MANUAL_CONTROL_ENABLED=true",
                    "CB_APPROVAL_POLICY=balanced",
                    "CB_PUBLIC_PORT=19000",
                    "CB_CONTROL_PORT=19001",
                    "CB_BROWSER_MEMORY_LIMIT=1024m",
                    "CB_MEMORY_RESERVE_MB=64",
                    "CB_MEMORY_PER_TAB_MB=32",
                ]
            )
            + "\n"
        )
    environment.chmod(0o600)
    try:
        compose("up", "--build", "-d")
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:19001/healthz", timeout=2
                ) as response:
                    assert response.status == 200
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        compose(
            "exec",
            "-T",
            "--user",
            "app",
            "browser",
            "python",
            "-",
            input=(root / "scripts/ci_runtime_probe.py").read_bytes(),
        )
        compose(
            "exec",
            "-T",
            "--user",
            "browser",
            "browser",
            "python",
            "-c",
            "import socket; s=socket.socket(); s.settimeout(2); assert s.connect_ex(('127.0.0.1',8001)) != 0",
        )
        print("Docker MCP/image/control/isolation/crash smoke passed")
    finally:
        subprocess.run([*prefix, "logs", "--tail", "100", "browser"], check=False)
        # The fixed, collision-checked disposable project is the only deletion
        # target. Production volumes, Docker daemon and other MCPs are untouched.
        subprocess.run([*prefix, "down", "--volumes"], check=False)
        environment.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
