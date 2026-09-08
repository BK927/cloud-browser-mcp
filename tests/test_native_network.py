import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_browser import native


@pytest.fixture
def cfg():
    return {
        "namespace": "cb-browser",
        "host_interface": "cb-host0",
        "peer_interface": "cb-peer0",
        "network_cidr": "10.203.87.0/30",
        "host_ip": "10.203.87.1",
        "peer_ip": "10.203.87.2",
        "browser_uid": 999,
    }


def reply(stdout="", code=0):
    return SimpleNamespace(stdout=stdout, returncode=code)


def link(address="10.203.87.1", prefix=30, flags=None, kind="veth", extra=None):
    return json.dumps(
        [
            {
                "linkinfo": {"info_kind": kind},
                "flags": ["UP"] if flags is None else flags,
                "addr_info": [
                    {"family": "inet", "local": address, "prefixlen": prefix},
                    *(extra or []),
                ],
            }
        ]
    )


@pytest.mark.parametrize(
    "present,output,code,expected",
    [
        (True, "running\n", 0, True),
        (True, "not running\n", 0, False),
        (True, "", 8, False),
        (False, "", 3, False),
    ],
)
def test_manager_detection(monkeypatch, present, output, code, expected):
    monkeypatch.setattr(native.shutil, "which", lambda _: "/usr/bin/nmcli" if present else None)
    monkeypatch.setattr(native, "run", lambda *args, **kwargs: reply(output, code))
    assert native.network_manager_active() is expected


@pytest.mark.parametrize(
    "present,output,code",
    [
        (False, "", 0),
        (True, "unexpected", 0),
        (True, "", 1),
    ],
)
def test_unknown_manager_state_fails_closed(monkeypatch, present, output, code):
    monkeypatch.setattr(native.shutil, "which", lambda _: "/usr/bin/nmcli" if present else None)
    monkeypatch.setattr(native, "run", lambda *args, **kwargs: reply(output, code))
    with pytest.raises(RuntimeError, match="NATIVE_MANAGER_UNVERIFIED"):
        native.network_manager_active()


def test_unmanage_only_new_veth_pair_and_wait_for_discovery(monkeypatch, cfg):
    calls = []
    attempts = {}

    def run(*args, **kwargs):
        calls.append(args)
        if args[0] == "ip":
            return reply(link())
        if "set" in args:
            name = args[-3]
            attempts[name] = attempts.get(name, 0) + 1
            return reply(code=10 if attempts[name] == 1 else 0)
        return reply("10 (unmanaged)\n")

    monkeypatch.setattr(native, "network_manager_active", lambda: True)
    monkeypatch.setattr(native, "run", run)
    monkeypatch.setattr(native.time, "sleep", lambda _: None)
    native.unmanage_created_interfaces(cfg)
    mutations = [args for args in calls if "set" in args]
    assert mutations == [
        ("nmcli", "--wait", "2", "device", "set", name, "managed", "no")
        for name in ("cb-host0", "cb-host0", "cb-peer0", "cb-peer0")
    ]
    assert not any("connection" in args or "reload" in args for args in calls)


@pytest.mark.parametrize("kind,code", [("bridge", 0), ("veth", 1)])
def test_foreign_link_or_failed_handoff_is_not_ignored(monkeypatch, cfg, kind, code):
    monkeypatch.setattr(native, "network_manager_active", lambda: True)
    monkeypatch.setattr(
        native,
        "run",
        lambda *args, **kwargs: reply(link(kind=kind)) if args[0] == "ip" else reply(code=code),
    )
    with pytest.raises(RuntimeError, match="NATIVE_"):
        native.unmanage_created_interfaces(cfg)


def test_handoff_timeout_is_bounded_and_does_not_repeat_successful_set(monkeypatch, cfg):
    mutations = []

    def run(*args, **kwargs):
        if args[0] == "ip":
            return reply(link())
        if "set" in args:
            mutations.append(args)
            return reply()
        return reply("30 (disconnected)")

    ticks = iter([0, 1, 9])
    monkeypatch.setattr(native, "network_manager_active", lambda: True)
    monkeypatch.setattr(native, "run", run)
    monkeypatch.setattr(native.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(native.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="did not become unmanaged"):
        native.unmanage_created_interfaces(cfg)
    assert len(mutations) == 1


@pytest.mark.parametrize(
    "payload",
    [
        link(address="10.203.87.3"),
        link(prefix=24),
        link(flags=[]),
        link(kind="dummy"),
        link(extra=[{"family": "inet", "local": "192.0.2.1", "prefixlen": 24}]),
        "[]",
    ],
)
def test_missing_changed_address_or_link_fails_readiness(monkeypatch, cfg, payload):
    monkeypatch.setattr(native, "run", lambda *args, **kwargs: reply(payload))
    with pytest.raises(RuntimeError, match="NATIVE_LINK_"):
        native.verify_link(cfg)


def test_peer_is_checked_in_correct_namespace(monkeypatch, cfg):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return reply(link(address=cfg["peer_ip"]))

    monkeypatch.setattr(native, "run", run)
    native.verify_link(cfg, peer=True)
    assert calls[-1][:4] == ("ip", "netns", "exec", "cb-browser")
    native.verify_link(cfg, peer=True, inside=True)
    assert calls[-1][:2] == ("ip", "-j")


def test_address_check_does_not_accept_managed_host(monkeypatch, cfg):
    monkeypatch.setattr(native, "network_manager_active", lambda: True)
    monkeypatch.setattr(native, "nm_device_state", lambda _: 30)
    with pytest.raises(RuntimeError, match="host veth is not unmanaged"):
        native.verify_host_ready(cfg)


def test_network_handoff_precedes_address_assignment(monkeypatch, cfg, tmp_path):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args == ("ip", "-j", "address", "show"):
            return reply("[]")
        if args[:3] == ("iptables", "-S", "CB_NATIVE_INGRESS"):
            return reply(code=1)
        return reply()

    monkeypatch.setattr(native, "run", run)
    monkeypatch.setattr(native.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(native, "marker_path", lambda _: tmp_path / "marker.json")
    monkeypatch.setattr(
        native, "Path", lambda *args: SimpleNamespace(stat=lambda: SimpleNamespace(st_ino=123))
    )
    monkeypatch.setattr(native, "unmanage_created_interfaces", lambda _: calls.append(("handoff",)))
    monkeypatch.setattr(native, "verify_network", lambda *args, **kwargs: calls.append(("verify",)))
    native.network_up(cfg)
    handoff = calls.index(("handoff",))
    assert handoff < calls.index(("ip", "address", "add", "10.203.87.1/30", "dev", "cb-host0"))
    assert handoff < calls.index(("ip", "link", "set", "cb-peer0", "netns", "cb-browser"))
    assert calls[-1] == ("verify",)


def test_owned_network_can_be_removed_even_after_ip_loss(monkeypatch, cfg, tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("fixture")
    checks, commands = [], []
    monkeypatch.setattr(native.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(native, "marker_path", lambda _: marker)
    monkeypatch.setattr(native, "verify_network", lambda config, **kwargs: checks.append(kwargs))
    monkeypatch.setattr(native, "run", lambda *args, **kwargs: commands.append(args))
    native.network_down(cfg)
    assert checks == [{"ready": False}]  # identity/firewall checks still run in production.
    assert ("ip", "link", "delete", "cb-host0") in commands
    assert not marker.exists()


def test_native_egress_has_readiness_gate_and_no_global_nm_mutation():
    source = Path(__file__).parents[1]
    text = (source / "scripts/native_install.py").read_text()
    assert "ExecStartPre={python} -I -m cloud_browser.native verify-egress" in text
    text = (source / "src/cloud_browser/native.py").read_text()
    assert '"connection", "reload"' not in text and '"networking", "off"' not in text


@pytest.mark.parametrize("own_inode", [456, 789])
def test_unprivileged_egress_uses_root_attestation_not_pid1(monkeypatch, cfg, tmp_path, own_inode):
    marker = tmp_path / "marker.json"
    marker.write_text(
        json.dumps(
            {
                "fingerprint": native.fingerprint(cfg),
                "network_inode": 123,
                "host_network_inode": 456,
            }
        )
    )

    def path(*args):
        assert args != ("/proc/1/ns/net",), "Must not require ptrace access to root PID 1"
        value = own_inode if args == ("/proc/self/ns/net",) else 123
        return SimpleNamespace(stat=lambda: SimpleNamespace(st_ino=value))

    monkeypatch.setattr(native, "Path", path)
    monkeypatch.setattr(native, "marker_path", lambda _: marker)
    monkeypatch.setattr(native, "verify_host_ready", lambda _: None)
    monkeypatch.setenv("CB_EGRESS_BIND", cfg["host_ip"])
    monkeypatch.setenv("CB_EGRESS_PORT", "3128")
    if own_inode == 456:
        native.verify_egress(cfg)
    else:
        with pytest.raises(RuntimeError, match="identity/configuration changed"):
            native.verify_egress(cfg)
