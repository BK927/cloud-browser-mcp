#!/usr/bin/env python3
"""Debian 13 native lifecycle. Never stops/removes Docker or other MCP services."""

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

PREFIX = Path("/opt/cloud-browser")
ETC = Path("/etc/cloud-browser")
UNITS = Path("/etc/systemd/system")
CHROMIUM = "/usr/bin/chromium"
MANAGED = "# Managed by Cloud Browser native installer.\n"
NAMES = ("cloud-browser-ingress", "cloud-browser", "cloud-browser-egress", "cloud-browser-network")


def command(*args, quiet=False, check=True, **kwargs):
    return subprocess.run(
        list(map(str, args)), check=check, stdout=subprocess.DEVNULL if quiet else None, **kwargs
    )


def platform_check():
    if platform.system() != "Linux":
        raise RuntimeError(
            "Native installation requires Debian 13 Linux; use Docker on Windows/macOS"
        )
    values = dict(
        line.split("=", 1)
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line
    )
    if (
        values.get("ID", "").strip('"') != "debian"
        or values.get("VERSION_ID", "").strip('"') != "13"
        or platform.machine() not in ("aarch64", "x86_64")
    ):
        raise RuntimeError("Supported native targets are Debian 13 arm64/amd64")
    if os.geteuid() != 0:
        raise RuntimeError("Run this installer through sudo; passwords are never command arguments")


def write(path, text, mode=0o644):
    if path.is_symlink():
        raise RuntimeError(f"Refusing to overwrite a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".cb-install-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def native_config(args, existing=None):
    cfg = dict(existing or {})
    cfg.setdefault("namespace", "cb-browser")
    cfg.setdefault("host_interface", "cb-host0")
    cfg.setdefault("peer_interface", "cb-peer0")
    cfg.setdefault("browser_user", "cb-browser")
    cfg["network_cidr"] = args.cidr or cfg.get("network_cidr", "10.203.87.0/30")
    network = ipaddress.ip_network(cfg["network_cidr"])
    if network.version != 4 or network.prefixlen != 30 or not network.is_private:
        raise RuntimeError("Choose an unused private IPv4 /30")
    cfg["memory_mib"] = args.memory_mib or cfg.get("memory_mib", 1024)
    if not 256 <= cfg["memory_mib"] <= 65536:
        raise RuntimeError("Memory budget must be 256..65536 MiB")
    cfg["public_port"] = args.public_port or cfg.get("public_port", 8000)
    cfg["control_port"] = args.control_port or cfg.get("control_port", 8001)
    if cfg["public_port"] == cfg["control_port"] or any(
        not 1024 <= cfg[key] <= 65535 for key in ("public_port", "control_port")
    ):
        raise RuntimeError("Use two distinct unprivileged listener ports")
    target = Path(args.data_dir or cfg.get("data_dir", "/var/lib/cloud-browser-native"))
    if (
        not target.is_absolute()
        or target.resolve() != target
        or len(target.parts) < 4
        or not target.name.startswith("cloud-browser")
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(target))
    ):
        raise RuntimeError(
            "Use a dedicated absolute cloud-browser* data directory, never a home or workspace root"
        )
    cfg["data_dir"] = str(target)
    return cfg


def render_units(cfg, python):
    host, peer = map(str, list(ipaddress.ip_network(cfg["network_cidr"]).hosts()))
    common = "Restart=on-failure\nRestartSec=5\nProtectSystem=strict\nProtectHome=true\nPrivateTmp=true\nProtectKernelTunables=true\nProtectControlGroups=true\nRestrictRealtime=true\nLockPersonality=true\nSystemCallArchitectures=native\nUMask=0077\n"
    network = (
        MANAGED
        + f"""[Unit]
Description=Cloud Browser dedicated network namespace
Before=cloud-browser.service cloud-browser-egress.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={python} -I -m cloud_browser.native network-up --config {ETC}/native.json
ExecStop={python} -I -m cloud_browser.native network-down --config {ETC}/native.json
[Install]
WantedBy=multi-user.target
"""
    )
    api = (
        MANAGED
        + f"""[Unit]
Description=Personal Cloud Browser MCP (native)
Requires=cloud-browser-network.service cloud-browser-egress.service
After=cloud-browser-network.service cloud-browser-egress.service
[Service]
User=cb-api
Group=cb-api
SupplementaryGroups=cb-browser
NetworkNamespacePath=/run/netns/{cfg["namespace"]}
EnvironmentFile={ETC}/browser.env
# Last EnvironmentFile wins over operator env and Environment= defaults.
EnvironmentFile={ETC}/runtime.env
Environment=CB_NATIVE_CONFIG={ETC}/native.json CB_DEVELOPMENT=false CB_NETWORK_ISOLATED=true
Environment=CB_DATA_DIR={cfg["data_dir"]} CB_RUNTIME_DIR=/run/cloud-browser CB_MANAGED_DISPLAY=true
Environment=CB_BIND_HOST={peer} CB_PUBLIC_PORT=8000 CB_CONTROL_PORT=8001
Environment=CB_BROWSER_PROXY=http://{host}:3128 CB_BROWSER_GROUP=cb-browser
Environment=CB_CHROMIUM_PATH={PREFIX}/chromium-launcher CB_BROWSER_CLEANUP_COMMAND={PREFIX}/browser-stop
Environment=CB_VNC_WEBSOCKET=ws://127.0.0.1:6080/websockify CB_VNC_PORT=5900 CB_VNC_BRIDGE_PORT=6080
Environment=CB_HEADLESS=false
WorkingDirectory={PREFIX}
ExecStart={python} -I -m cloud_browser.cli serve
MemoryAccounting=yes
MemoryMax={cfg["memory_mib"]}M
MemorySwapMax={cfg["memory_mib"]}M
TasksMax=256
OOMPolicy=stop
KillMode=control-group
TimeoutStopSec=20
RuntimeDirectory=cloud-browser
RuntimeDirectoryMode=0750
ReadWritePaths={cfg["data_dir"]} /run/cloud-browser
PrivateDevices=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
# sudo's fixed Chromium/cleanup wrappers and Chromium's sandbox need setuid.
NoNewPrivileges=false
CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_SETUID CAP_SETGID CAP_KILL CAP_SYS_CHROOT CAP_AUDIT_WRITE
{common.replace("UMask=0077", "UMask=0007")}[Install]
WantedBy=multi-user.target
"""
    )
    egress = (
        MANAGED
        + f"""[Unit]
Description=Cloud Browser checked egress proxy (native)
Requires=cloud-browser-network.service
After=cloud-browser-network.service
[Service]
User=cb-egress
Group=cb-egress
Environment=CB_EGRESS_BIND={host} CB_EGRESS_PORT=3128
ExecStart={python} -I -m cloud_browser.egress
MemoryMax=128M
TasksMax=80
NoNewPrivileges=true
CapabilityBoundingSet=
{common}[Install]
WantedBy=multi-user.target
"""
    )
    ingress = (
        MANAGED
        + f"""[Unit]
Description=Cloud Browser fixed local ingress (native)
Requires=cloud-browser.service
After=cloud-browser.service
[Service]
User=cb-ingress
Group=cb-ingress
Environment=CB_INGRESS_TARGET={peer} CB_INGRESS_BIND=127.0.0.1
Environment=CB_INGRESS_PUBLIC_PORT={cfg["public_port"]} CB_INGRESS_CONTROL_PORT={cfg["control_port"]}
ExecStart={python} -I -m cloud_browser.ingress
MemoryMax=64M
TasksMax=32
NoNewPrivileges=true
CapabilityBoundingSet=
{common}[Install]
WantedBy=multi-user.target
"""
    )
    return {
        "cloud-browser-network.service": network,
        "cloud-browser.service": api,
        "cloud-browser-egress.service": egress,
        "cloud-browser-ingress.service": ingress,
    }


def runtime_environment(units):
    """Public runtime settings only; never copy administrator/OAuth secrets here."""
    return (
        "\n".join(
            value
            for line in units["cloud-browser.service"].splitlines()
            if line.startswith("Environment=")
            for value in line.removeprefix("Environment=").split()
        )
        + "\n"
    )


def wait_healthy(port, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                if json.load(response).get("ok") is True:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError(
        "Native ingress/API did not become healthy; inspect dedicated unit status. Previous release and data were retained."
    )


def tree_files(source):
    for name in (
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "src",
        "docs",
        "deploy",
        "scripts",
    ):
        path = source / name
        for file in sorted(path.rglob("*") if path.is_dir() else [path]):
            if file.is_symlink():
                raise RuntimeError("Installation source cannot contain symlinks")
            if file.is_file() and "__pycache__" not in file.parts:
                yield file


def install(args):
    platform_check()
    source = Path(args.source).resolve()
    if not (source / "pyproject.toml").is_file():
        raise RuntimeError("--source must contain this project's checked-out source")
    ETC.mkdir(mode=0o755, parents=True, exist_ok=True)
    manifest_path = ETC / "install.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    old_cfg = (
        json.loads((ETC / "native.json").read_text()) if (ETC / "native.json").exists() else None
    )
    cfg = native_config(args, old_cfg)
    for path, digest in previous.get("managed_sha256", {}).items():
        target = Path(path)
        if target.is_symlink() or (
            target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest
        ):
            raise RuntimeError(f"Preserve local native customization before updating: {target}")
    for filename, digest in previous.get("unit_sha256", {}).items():
        current = UNITS / filename
        if current.exists() and hashlib.sha256(current.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Preserve local unit customization before updating: {current}")
    if not (ETC / "browser.env").exists() and not args.env_file:
        raise RuntimeError("First install requires --env-file with operator OAuth/console settings")
    active = (
        command(
            "systemctl", "is-active", "--quiet", "cloud-browser.service", check=False, quiet=True
        ).returncode
        == 0
    )
    if active and not args.start:
        raise RuntimeError(
            "Native service is running; --start is required for its controlled update/restart"
        )
    data = Path(cfg["data_dir"])
    if (
        data.exists()
        and any(data.iterdir())
        and (not previous or not old_cfg or old_cfg["data_dir"] != str(data))
    ):
        raise RuntimeError(
            "Existing unowned data directory is not imported or modified; use a fresh comparison directory"
        )
    package_env = os.environ | {"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"}
    command("apt-get", "update", quiet=True, env=package_env)
    command(
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        "python3-venv",
        "chromium",
        "chromium-sandbox",
        "xvfb",
        "xauth",
        "x11vnc",
        "novnc",
        "websockify",
        "fonts-noto-cjk",
        "fonts-liberation",
        "iptables",
        "iproute2",
        "sudo",
        "util-linux",
        "ca-certificates",
        quiet=True,
        env=package_env,
    )
    import grp
    import pwd

    for user in ("cb-api", "cb-browser", "cb-egress", "cb-ingress"):
        try:
            account = pwd.getpwnam(user)
            if (
                account.pw_uid == 0
                or account.pw_uid >= 1000
                or account.pw_shell not in ("/usr/sbin/nologin", "/bin/false")
            ):
                raise RuntimeError(
                    f"Existing {user} is not a dedicated system account; inspect it before installation"
                )
        except KeyError:
            command(
                "useradd",
                "--system",
                "--user-group",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                user,
            )
    command("usermod", "-a", "-G", "cb-browser", "cb-api")
    files = list(tree_files(source))
    digest = hashlib.sha256()
    for file in files:
        digest.update(
            str(file.relative_to(source)).replace("\\", "/").encode() + b"\0" + file.read_bytes()
        )
    release_id = digest.hexdigest()[:16]
    release = PREFIX / "releases" / release_id
    if release.is_symlink():
        raise RuntimeError("Release directory cannot be a symlink")
    already_staged = (release / ".source-sha256").exists()
    if already_staged:
        if (release / ".source-sha256").read_text().strip() != digest.hexdigest() or any(
            not (release / file.relative_to(source)).is_file()
            or (release / file.relative_to(source)).is_symlink()
            or (release / file.relative_to(source)).read_bytes() != file.read_bytes()
            for file in files
        ):
            raise RuntimeError("Existing immutable release differs; preserve and inspect it")
    else:
        release.mkdir(parents=True, exist_ok=True)
        for file in files:
            destination = release / file.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, destination)
            destination.chmod(0o644)
        write(release / ".source-sha256", digest.hexdigest() + "\n")
    venv = release / ".venv"
    if not (venv / "bin/python").exists():
        command("python3", "-m", "venv", venv)
        command(venv / "bin/pip", "install", "uv==0.12.9", quiet=True)
    if not (release / ".dependencies-ready").exists():
        command(
            venv / "bin/uv",
            "sync",
            "--project",
            release,
            "--frozen",
            "--no-dev",
            "--no-editable",
            "--extra",
            "browser",
            quiet=True,
            env=os.environ | {"UV_PROJECT_ENVIRONMENT": str(venv)},
        )
        write(release / ".dependencies-ready", "locked dependencies installed\n")
    python = str(venv / "bin/python")
    units = render_units(cfg, python)
    for filename, text in units.items():
        if (UNITS / filename).exists() and filename not in previous.get("unit_sha256", {}):
            raise RuntimeError(f"Refusing to replace an unowned unit: {filename}")
        write(release / filename, text)
    command("systemd-analyze", "verify", *[release / name for name in units], quiet=True)
    data.mkdir(parents=True, exist_ok=True)
    api_uid = pwd.getpwnam("cb-api").pw_uid
    api_gid = grp.getgrnam("cb-api").gr_gid
    browser_gid = grp.getgrnam("cb-browser").gr_gid
    os.chown(data, api_uid, api_gid)
    data.chmod(0o711)
    write(data / ".native-owned", "Cloud Browser native data\n", 0o400)
    profiles = data / "profiles"
    profiles.mkdir(exist_ok=True)
    os.chown(profiles, api_uid, browser_gid)
    profiles.chmod(0o2770)
    # Chromium's crash/config helpers still need a writable HOME even with an
    # explicit --user-data-dir. Keep it inside the owned data boundary, not /home
    # (which ProtectHome intentionally hides from the service).
    browser_home = data / "browser-home"
    if browser_home.is_symlink():
        raise RuntimeError("Dedicated browser HOME cannot be a symlink")
    browser_home.mkdir(exist_ok=True)
    os.chown(browser_home, pwd.getpwnam("cb-browser").pw_uid, browser_gid)
    browser_home.chmod(0o700)
    command("usermod", "--home", browser_home, "cb-browser")
    if active:
        command("systemctl", "stop", *[name + ".service" for name in NAMES])
    if old_cfg and old_cfg != cfg and marker_exists(old_cfg):
        command(
            python,
            "-I",
            "-m",
            "cloud_browser.native",
            "network-down",
            "--config",
            ETC / "native.json",
        )
    if not (ETC / "browser.env").exists():
        shutil.copyfile(Path(args.env_file).resolve(), ETC / "browser.env")
        os.chown(ETC / "browser.env", 0, api_gid)
        (ETC / "browser.env").chmod(0o640)
    write(ETC / "native.json", json.dumps(cfg, indent=2) + "\n")
    write(ETC / "runtime.env", runtime_environment(units))
    write(
        PREFIX / "chromium-launcher",
        f'#!/bin/sh\nset -eu\nexec sudo -n -H -u cb-browser {CHROMIUM} "$@"\n',
        0o755,
    )
    write(
        PREFIX / "browser-stop",
        f"#!/bin/sh\nset -eu\nexec {python} -I -m cloud_browser.runtime stop-browser --user cb-browser\n",
        0o755,
    )
    sudoers = (
        f'Defaults:cb-api env_keep += "DISPLAY XAUTHORITY"\ncb-api ALL=(cb-browser) NOPASSWD: {CHROMIUM}\n'
        + f'cb-api ALL=(root) NOPASSWD: {PREFIX}/browser-stop ""\n'
    )
    write(release / "sudoers", sudoers, 0o440)
    command("visudo", "-cf", release / "sudoers", quiet=True)
    write(Path("/etc/sudoers.d/cloud-browser-native"), sudoers, 0o440)
    for filename, text in units.items():
        write(UNITS / filename, text)
    current = PREFIX / "current"
    if current.exists() and not current.is_symlink():
        raise RuntimeError("Native current path is not a managed symlink")
    if current.is_symlink():
        current.unlink()
    current.symlink_to(release, target_is_directory=True)
    write(
        manifest_path,
        json.dumps(
            {
                "release": str(release),
                "source_sha256": digest.hexdigest(),
                "previous_release": previous.get("release"),
                "unit_sha256": {
                    name: hashlib.sha256(text.encode()).hexdigest() for name, text in units.items()
                },
                "managed_sha256": {
                    str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (
                        ETC / "native.json",
                        ETC / "runtime.env",
                        PREFIX / "chromium-launcher",
                        PREFIX / "browser-stop",
                        Path("/etc/sudoers.d/cloud-browser-native"),
                    )
                },
            },
            indent=2,
        )
        + "\n",
    )
    command("systemctl", "daemon-reload")
    if args.start:
        # Only dedicated native units. Never docker.service or another MCP.
        command("systemctl", "enable", "--now", "cloud-browser-ingress.service")
        wait_healthy(cfg["control_port"])
    print(
        json.dumps(
            {
                "installed_release": str(release),
                "started": args.start,
                "data_preserved": str(data),
                "docker_untouched": True,
            }
        )
    )


def marker_exists(cfg):
    return (Path("/run") / (cfg["namespace"] + "-isolation.json")).exists()


def uninstall(args):
    platform_check()
    manifest = json.loads((ETC / "install.json").read_text())
    cfg = json.loads((ETC / "native.json").read_text())
    for filename, digest in manifest.get("managed_sha256", {}).items():
        path = Path(filename)
        if path.is_symlink() or (
            path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise RuntimeError(f"Modified native file requires operator review: {path}")
    for filename, digest in manifest["unit_sha256"].items():
        path = UNITS / filename
        if path.is_symlink() or (
            path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise RuntimeError(f"Modified unit requires operator review: {path}")
    installed = [name + ".service" for name in NAMES if (UNITS / (name + ".service")).exists()]
    if installed:
        command("systemctl", "disable", "--now", *installed)
    for filename in manifest["unit_sha256"]:
        (UNITS / filename).unlink(missing_ok=True)
    Path("/etc/sudoers.d/cloud-browser-native").unlink(missing_ok=True)
    command("systemctl", "daemon-reload")
    deleted = None
    if args.purge_data:
        import fcntl

        target = Path(cfg["data_dir"])
        if (
            target.is_symlink()
            or target.resolve() != target
            or len(target.parts) < 4
            or not target.name.startswith("cloud-browser")
            or not (target / ".native-owned").is_file()
            or (target / ".instance.lock").is_symlink()
        ):
            raise RuntimeError("Refusing to purge an unverified data target")
        with (target / ".instance.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            shutil.rmtree(target)
        deleted = str(target)
    print(
        json.dumps(
            {
                "native_units_removed": True,
                "data_deleted": deleted,
                "configuration_and_releases_preserved": True,
                "docker_untouched": True,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["install", "update", "status", "uninstall"])
    parser.add_argument("--source", default=".")
    parser.add_argument("--env-file")
    parser.add_argument("--data-dir")
    parser.add_argument("--cidr")
    parser.add_argument("--memory-mib", type=int)
    parser.add_argument("--public-port", type=int)
    parser.add_argument("--control-port", type=int)
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--purge-data", action="store_true")
    args = parser.parse_args()
    if args.purge_data and args.operation != "uninstall":
        parser.error("--purge-data is only valid with uninstall")
    if args.operation in ("install", "update"):
        install(args)
    elif args.operation == "uninstall":
        uninstall(args)
    else:
        platform_check()
        command(
            "systemctl", "--no-pager", "status", *[name + ".service" for name in NAMES], check=False
        )


if __name__ == "__main__":
    main()
