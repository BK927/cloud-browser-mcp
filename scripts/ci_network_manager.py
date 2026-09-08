#!/usr/bin/env python3
"""Actual NetworkManager regression in a network-none, disposable CI container.

Never run on the operator host. No host mounts, services or network changes.
"""

import json
import os
import pwd
import socket
import subprocess
import time
from pathlib import Path

from cloud_browser import native


def wait_for(predicate, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.2)
    raise RuntimeError("Isolated NetworkManager fixture did not reach expected state")


def main():
    if os.geteuid() != 0 or os.getenv("CB_NETWORK_TEST") != "1" or not Path("/.dockerenv").exists():
        raise RuntimeError("Requires the dedicated disposable CI container")
    interfaces = json.loads(native.run("ip", "-j", "address", "show").stdout)
    if [link["ifname"] for link in interfaces] != ["lo"]:
        raise RuntimeError("Requires --network none; refusing a host or connected network")
    Path("/run/dbus").mkdir(exist_ok=True)
    native.run("dbus-uuidgen", "--ensure")
    config = Path("/run/cb-nm-ci.conf")
    config.write_text(
        "[main]\nplugins=keyfile\ndns=none\nrc-manager=unmanaged\nno-auto-default=*\n"
        "[device-cb-ci]\nmatch-device=interface-name:cb-host0;interface-name:cb-peer0\nmanaged=1\n"
        "[device-other]\nmatch-device=interface-name:ci-other\nmanaged=0\n"
    )
    children = []
    try:
        children.append(
            subprocess.Popen(
                ["dbus-daemon", "--system", "--nofork", "--nopidfile"], stdout=subprocess.DEVNULL
            )
        )
        wait_for(lambda: Path("/run/dbus/system_bus_socket").exists())
        children.append(
            subprocess.Popen(
                [
                    "NetworkManager",
                    "--no-daemon",
                    "--config",
                    str(config),
                    "--pid-file",
                    "/run/cb-nm-ci.pid",
                    "--state-file",
                    "/run/cb-nm-ci.state",
                ],
                stdout=subprocess.DEVNULL,
            )
        )
        wait_for(native.network_manager_active)
        native.run("ip", "link", "add", "ci-other", "type", "dummy")
        native.run("ip", "address", "add", "192.0.2.1/32", "dev", "ci-other")
        native.run("ip", "link", "set", "ci-other", "up")
        cfg = {
            "namespace": "cb-browser",
            "host_interface": "cb-host0",
            "peer_interface": "cb-peer0",
            "network_cidr": "10.203.87.0/30",
            "host_ip": "10.203.87.1",
            "peer_ip": "10.203.87.2",
            "browser_uid": pwd.getpwnam("cb-browser").pw_uid,
        }
        original = native.unmanage_created_interfaces
        observed = []

        def assert_initially_managed(configuration):
            wait_for(lambda: native.nm_device_state("cb-host0") not in (None, 10))
            observed.append(native.nm_device_state("cb-host0"))
            original(configuration)
            assert native.nm_device_state("cb-host0") == 10

        native.unmanage_created_interfaces = assert_initially_managed
        native.network_up(cfg)
        time.sleep(2)  # Let real D-Bus/udev/NM events settle, then revalidate IPs.
        native.verify_network(cfg)
        os.environ.update(CB_EGRESS_BIND=cfg["host_ip"], CB_EGRESS_PORT="3128")
        native.verify_egress(cfg)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((cfg["host_ip"], 3128))
            probe.listen(1)
        # Readiness rejects address loss. Identity-verified cleanup still works.
        native.run("ip", "address", "del", "10.203.87.1/30", "dev", "cb-host0")
        try:
            native.verify_network(cfg)
        except RuntimeError as error:
            assert "NATIVE_LINK_NOT_READY" in str(error)
        else:
            raise AssertionError("Lost host IP passed readiness")
        native.network_down(cfg)
        other = json.loads(native.run("ip", "-j", "address", "show", "dev", "ci-other").stdout)[0]
        assert any(item.get("local") == "192.0.2.1" for item in other["addr_info"])
        assert native.nm_device_state("ci-other") == 10
        assert not native.run("ip", "netns", "list").stdout.strip()
        print(
            json.dumps(
                {
                    "network_manager_regression": "passed",
                    "initial_managed_states": observed,
                    "ip_loss_rejected": True,
                    "owned_cleanup": True,
                    "other_interface_preserved": True,
                }
            )
        )
    finally:
        for child in reversed(children):
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        # This network-none fixture has only generated test state and commands;
        # retain bounded stderr so missing utilities aren't mistaken for policy.
        print((error.stderr or "")[-2000:])
        raise
