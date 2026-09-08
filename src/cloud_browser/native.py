"""Debian native network isolation. Root owns configuration and namespace setup."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .resources import cgroup_paths


def run(*argv, check=True):
    return subprocess.run(
        argv,
        check=check,
        text=True,
        capture_output=True,
        timeout=15,
        env=os.environ | {"LC_ALL": "C"},
    )


def network_manager_active():
    if not shutil.which("nmcli"):
        if (
            run(
                "systemctl", "is-active", "--quiet", "NetworkManager.service", check=False
            ).returncode
            == 0
        ):
            raise RuntimeError("NATIVE_MANAGER_UNVERIFIED: active NetworkManager needs nmcli")
        return False
    result = run("nmcli", "--wait", "2", "-t", "-f", "RUNNING", "general", check=False)
    if result.returncode == 0 and result.stdout.strip() == "running":
        return True
    if result.returncode in (0, 8) and result.stdout.strip() in ("", "not running"):
        return False
    raise RuntimeError("NATIVE_MANAGER_UNVERIFIED: cannot read NetworkManager running state")


def nm_device_state(name):
    result = run(
        "nmcli",
        "--wait",
        "2",
        "-g",
        "GENERAL.STATE",
        "device",
        "show",
        name,
        check=False,
    )
    if result.returncode == 10:
        return None  # Newly created device has not reached NetworkManager yet.
    match = re.match(r"^(\d+)(?:\s|$)", result.stdout.strip())
    if result.returncode != 0 or not match:
        raise RuntimeError(f"NATIVE_MANAGER_UNVERIFIED: cannot read device state for {name}")
    return int(match.group(1))


def unmanage_created_interfaces(config):
    """Only called after this invocation created its collision-checked veth pair."""
    if not network_manager_active():
        return
    for name in (config["host_interface"], config["peer_interface"]):
        # Do not apply a network-manager mutation to an arbitrary existing link.
        links = json.loads(run("ip", "-j", "-d", "link", "show", "dev", name).stdout)
        if len(links) != 1 or links[0].get("linkinfo", {}).get("info_kind") != "veth":
            raise RuntimeError("NATIVE_LINK_CHANGED: expected the newly created veth pair")
        deadline = time.monotonic() + 8
        dispatched = False
        while True:
            if not dispatched:
                result = run(
                    "nmcli",
                    "--wait",
                    "2",
                    "device",
                    "set",
                    name,
                    "managed",
                    "no",
                    check=False,
                )
                if result.returncode not in (0, 10):
                    raise RuntimeError(f"NATIVE_MANAGER_UNVERIFIED: cannot unmanage {name}")
                dispatched = result.returncode == 0
            if dispatched and nm_device_state(name) == 10:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"NATIVE_MANAGER_UNVERIFIED: {name} did not become unmanaged")
            time.sleep(0.2)


def verify_link(config, *, peer=False, inside=False):
    name = config["peer_interface" if peer else "host_interface"]
    address = config["peer_ip" if peer else "host_ip"]
    args = ("ip", "-j", "-d", "address", "show", "dev", name)
    result = ns(config, *args) if peer and not inside else run(*args)
    links = json.loads(result.stdout)
    if len(links) != 1 or links[0].get("linkinfo", {}).get("info_kind") != "veth":
        raise RuntimeError(f"NATIVE_LINK_CHANGED: {name} is not the expected veth")
    ipv4 = {
        (item.get("local"), item.get("prefixlen"))
        for item in links[0].get("addr_info", [])
        if item.get("family") == "inet"
    }
    if ipv4 != {(address, 30)} or "UP" not in links[0].get("flags", []):
        raise RuntimeError(
            f"NATIVE_LINK_NOT_READY: {name} needs its configured IPv4/30 and UP state"
        )


def verify_host_ready(config):
    if network_manager_active() and nm_device_state(config["host_interface"]) != 10:
        raise RuntimeError("NATIVE_MANAGER_UNVERIFIED: host veth is not unmanaged")
    verify_link(config)


def load_config(path):
    import pwd

    path = Path(path)
    info = path.stat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError(
            "Native configuration must be root-owned and not writable by group/others"
        )
    config = json.loads(path.read_text())
    for key in ("namespace", "host_interface", "peer_interface"):
        if not re.fullmatch(r"cb-[a-z0-9-]{1,11}", config[key]):
            raise RuntimeError("Invalid dedicated native interface/namespace name")
    network = ipaddress.ip_network(config["network_cidr"])
    if network.version != 4 or network.prefixlen != 30 or not network.is_private:
        raise RuntimeError("Native link needs a dedicated private IPv4 /30")
    config["host_ip"], config["peer_ip"] = map(str, list(network.hosts()))
    config["browser_uid"] = pwd.getpwnam(config["browser_user"]).pw_uid
    if config["browser_uid"] == 0:
        raise RuntimeError("Dedicated browser user cannot be root")
    return config


def fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def ns(config, *args, check=True):
    return run("ip", "netns", "exec", config["namespace"], *map(str, args), check=check)


def marker_path(config):
    return Path("/run") / (config["namespace"] + "-isolation.json")


def verify_network(config, *, ready=True):
    marker = marker_path(config)
    info = marker.stat()
    if marker.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("Isolation attestation is not protected")
    saved = json.loads(marker.read_text())
    inode = Path("/run/netns", config["namespace"]).stat().st_ino
    if inode == Path("/proc/1/ns/net").stat().st_ino:
        raise RuntimeError("Native namespace cannot be the host network namespace")
    expected = {"fingerprint": fingerprint(config), "network_inode": inode}
    if ready or "host_network_inode" in saved:
        expected["host_network_inode"] = Path("/proc/self/ns/net").stat().st_ino
    if saved != expected:
        raise RuntimeError("Native namespace identity/configuration changed")
    if ns(config, "ip", "route", "show", "default").stdout.strip():
        raise RuntimeError("Browser namespace must not have a default route")
    for args in browser_rules(config):
        ns(config, "iptables", "-C", *args)
    run("iptables", "-C", "INPUT", "-d", config["host_ip"], "-j", "CB_NATIVE_INGRESS")
    run(
        "iptables",
        "-C",
        "CB_NATIVE_INGRESS",
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED,RELATED",
        "-j",
        "ACCEPT",
    )
    run(
        "iptables",
        "-C",
        "CB_NATIVE_INGRESS",
        "-i",
        config["host_interface"],
        "-p",
        "tcp",
        "--dport",
        "3128",
        "-j",
        "ACCEPT",
    )
    run("iptables", "-C", "CB_NATIVE_INGRESS", "-j", "REJECT")
    if ready:
        verify_host_ready(config)
        verify_link(config, peer=True)


def browser_rules(config):
    return [
        (
            "OUTPUT",
            "-m",
            "owner",
            "--uid-owner",
            str(config["browser_uid"]),
            "-j",
            "CB_NATIVE_BROWSER",
        ),
        (
            "CB_NATIVE_BROWSER",
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-j",
            "ACCEPT",
        ),
        (
            "CB_NATIVE_BROWSER",
            "-p",
            "tcp",
            "-d",
            config["host_ip"],
            "--dport",
            "3128",
            "-j",
            "ACCEPT",
        ),
        ("CB_NATIVE_BROWSER", "-j", "REJECT"),
    ]


def network_up(config):
    if os.geteuid() != 0:
        raise RuntimeError("Namespace setup requires root")
    existing = [line.split()[0] for line in run("ip", "netns", "list").stdout.splitlines()]
    if config["namespace"] in existing:
        verify_network(config)
        return
    network = ipaddress.ip_network(config["network_cidr"])
    addresses = json.loads(run("ip", "-j", "address", "show").stdout)
    for interface in addresses:
        if interface["ifname"] in (config["host_interface"], config["peer_interface"]):
            raise RuntimeError("Native interface name is already in use")
        for address in interface.get("addr_info", []):
            if address["family"] == "inet" and network.overlaps(
                ipaddress.ip_network(f"{address['local']}/{address['prefixlen']}", strict=False)
            ):
                raise RuntimeError(
                    "Native /30 overlaps an existing host network; choose another CIDR"
                )
    if run("iptables", "-S", "CB_NATIVE_INGRESS", check=False).returncode == 0:
        raise RuntimeError(
            "Native firewall chain already exists without its namespace; inspect before cleanup"
        )
    run("ip", "netns", "add", config["namespace"])
    # An incomplete setup stays isolated; never silently drop into the host network.
    run(
        "ip",
        "link",
        "add",
        config["host_interface"],
        "type",
        "veth",
        "peer",
        "name",
        config["peer_interface"],
    )
    # Finish per-device manager handoff BEFORE setting addresses or moving the
    # peer. Never reload the manager, edit connection profiles or touch eth*/wlan*.
    unmanage_created_interfaces(config)
    run("ip", "link", "set", config["peer_interface"], "netns", config["namespace"])
    run("ip", "address", "add", config["host_ip"] + "/30", "dev", config["host_interface"])
    run("ip", "link", "set", config["host_interface"], "up")
    ns(config, "ip", "address", "add", config["peer_ip"] + "/30", "dev", config["peer_interface"])
    ns(config, "ip", "link", "set", config["peer_interface"], "up")
    ns(config, "ip", "link", "set", "lo", "up")
    ns(
        config,
        "sysctl",
        "-q",
        "-w",
        "net.ipv6.conf.all.disable_ipv6=1",
        "net.ipv6.conf.default.disable_ipv6=1",
    )
    ns(config, "iptables", "-N", "CB_NATIVE_BROWSER")
    for args in browser_rules(config):
        ns(config, "iptables", "-A", *args)
    # Defense in depth if IPv6 is re-enabled by an administrator later.
    ns(
        config,
        "ip6tables",
        "-A",
        "OUTPUT",
        "-m",
        "owner",
        "--uid-owner",
        config["browser_uid"],
        "-j",
        "REJECT",
    )
    run("iptables", "-N", "CB_NATIVE_INGRESS")
    run(
        "iptables",
        "-A",
        "CB_NATIVE_INGRESS",
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED,RELATED",
        "-j",
        "ACCEPT",
    )
    run(
        "iptables",
        "-A",
        "CB_NATIVE_INGRESS",
        "-i",
        config["host_interface"],
        "-p",
        "tcp",
        "--dport",
        "3128",
        "-j",
        "ACCEPT",
    )
    run("iptables", "-A", "CB_NATIVE_INGRESS", "-j", "REJECT")
    run("iptables", "-I", "INPUT", "1", "-d", config["host_ip"], "-j", "CB_NATIVE_INGRESS")
    marker = marker_path(config)
    if marker.is_symlink():
        raise RuntimeError("Isolation marker cannot be a symlink")
    marker.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint(config),
                "network_inode": Path("/run/netns", config["namespace"]).stat().st_ino,
                "host_network_inode": Path("/proc/self/ns/net").stat().st_ino,
            }
        )
    )
    marker.chmod(0o644)
    verify_network(config)


def network_down(config):
    if os.geteuid() != 0:
        raise RuntimeError("Namespace removal requires root")
    marker = marker_path(config)
    if not marker.exists():
        raise RuntimeError("Refusing to remove an unowned namespace; inspect failed setup manually")
    verify_network(config, ready=False)
    # Only exact dedicated targets. No flushing host INPUT/OUTPUT or Docker rules.
    run("iptables", "-D", "INPUT", "-d", config["host_ip"], "-j", "CB_NATIVE_INGRESS")
    run("iptables", "-F", "CB_NATIVE_INGRESS")
    run("iptables", "-X", "CB_NATIVE_INGRESS")
    run("ip", "link", "delete", config["host_interface"])
    run("ip", "netns", "delete", config["namespace"])
    marker.unlink()


def verify_runtime(settings):
    """Runs as the API user inside systemd before binding either listener."""
    config = load_config(settings.native_config)
    marker = marker_path(config)
    info = marker.stat()
    if marker.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("Protected root isolation attestation is required")
    saved = json.loads(marker.read_text())
    inode = Path("/proc/self/ns/net").stat().st_ino
    if saved.get("fingerprint") != fingerprint(config) or saved.get("network_inode") != inode:
        raise RuntimeError("API is not in its attested browser namespace")
    if run("ip", "route", "show", "default").stdout.strip():
        raise RuntimeError("Unexpected default route in browser namespace")
    leaf = cgroup_paths()[0]
    maximum = (leaf / "memory.max").read_text().strip()
    if maximum != str(config["memory_mib"] * 1048576):
        raise RuntimeError(
            "Native systemd memory limit is missing or differs from the operator budget"
        )
    if (
        settings.browser_proxy != "http://" + config["host_ip"] + ":3128"
        or settings.bind_host != config["peer_ip"]
    ):
        raise RuntimeError("Native listener or egress route does not match isolated configuration")
    if settings.development or not settings.network_isolated:
        raise RuntimeError("Native operational mode cannot use development/network bypass settings")
    verify_link(config, peer=True, inside=True)


def verify_egress(config):
    """Unprivileged ExecStartPre: host namespace/address and fixed proxy binding."""
    marker = marker_path(config)
    info = marker.stat()
    if marker.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("Isolation attestation is not protected")
    if json.loads(marker.read_text()) != {
        "fingerprint": fingerprint(config),
        "network_inode": Path("/run/netns", config["namespace"]).stat().st_ino,
        # An unprivileged service must not need ptrace access to root PID 1.
        # Compare its own namespace to the root-created protected attestation.
        "host_network_inode": Path("/proc/self/ns/net").stat().st_ino,
    }:
        raise RuntimeError("Native namespace identity/configuration changed")
    if os.getenv("CB_EGRESS_BIND") != config["host_ip"] or os.getenv("CB_EGRESS_PORT") != "3128":
        raise RuntimeError("NATIVE_LINK_CHANGED: egress bind must match the isolated configuration")
    verify_host_ready(config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation", choices=["network-up", "network-down", "verify-network", "verify-egress"]
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    configuration = load_config(args.config)
    {
        "network-up": network_up,
        "network-down": network_down,
        "verify-network": verify_network,
        "verify-egress": verify_egress,
    }[args.operation](configuration)
