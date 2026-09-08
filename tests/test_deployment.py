import hashlib
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cloud_browser.config import Settings

ROOT = Path(__file__).parents[1]


def test_compose_isolation_and_no_raw_port_exposure():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert compose["networks"]["browser_internal"]["internal"] is True
    browser = compose["services"]["browser"]
    assert browser["networks"] == ["browser_internal"]
    assert not browser.get("ports")
    ingress = compose["services"]["ingress"]
    assert ingress["networks"] == ["browser_internal", "ingress_host"]
    assert compose["networks"]["ingress_host"] == {}
    assert all(p.startswith("127.0.0.1:") for p in ingress["ports"])
    assert len(ingress["ports"]) == 2
    assert not any("5900" in p or "6080" in p or "9222" in p for p in ingress["ports"])
    assert not ingress.get("volumes")
    assert not ingress.get("env_file")
    assert ingress["cap_drop"] == ["ALL"]
    assert ingress["read_only"] is True
    assert ingress["security_opt"] == ["no-new-privileges:true"]
    assert not browser.get("privileged")
    assert browser["cap_add"] == ["NET_ADMIN"]
    assert browser["security_opt"] == ["seccomp=./deploy/seccomp/chromium.json"]
    assert "no-sandbox" not in (ROOT / "deploy/chromium-launcher").read_text()
    assert not compose["services"]["egress"].get("ports")


def test_seccomp_only_adds_exact_chromium_namespace_calls():
    source = ROOT / "deploy/seccomp/docker-29.8.0.json"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        "de1f5327ca42b80be02daba8d39c0d087a530dc3c16f7028170fe068c9d66e61"
    )
    base = json.loads(source.read_text())
    profile = json.loads((ROOT / "deploy/seccomp/chromium.json").read_text())
    added = profile["syscalls"][len(base["syscalls"]) :]
    assert len(added) == 6
    expected = [
        ("clone", v) for v in (0x10000011, 0x30000011, 0x50000011, 0x70000011, 0x20000011)
    ] + [("unshare", 0x10000000)]
    for rule, (name, flags) in zip(added, expected, strict=True):
        assert rule["names"] == [name]
        assert rule["action"] == "SCMP_ACT_ALLOW"
        assert rule["includes"] == {"arches": ["amd64", "arm64"]}
        assert rule["args"] == [{"index": 0, "value": flags, "op": "SCMP_CMP_EQ"}]
    profile["syscalls"] = profile["syscalls"][: len(base["syscalls"])]
    assert profile == base  # mount/setns/clone3 and every other rule are unchanged.


def test_namespace_profile_does_not_allow_unrelated_namespace_flags():
    profile = json.loads((ROOT / "deploy/seccomp/chromium.json").read_text())
    added = profile["syscalls"][-6:]
    for syscall, flags in (
        ("unshare", 0),
        ("unshare", 0x20000),
        ("unshare", 0x70000000),
        ("clone", 0x10020011),
        ("clone", 0x14000011),
        ("clone", 0x18000011),
        ("clone3", 0),
        ("setns", 0),
        ("mount", 0),
    ):
        assert not any(
            syscall in rule["names"] and flags == rule["args"][0]["value"] for rule in added
        )


def test_seccomp_retains_docker29_af_alg_and_vsock_blocks():
    profile = json.loads((ROOT / "deploy/seccomp/chromium.json").read_text())
    rules = [r for r in profile["syscalls"] if "socket" in r["names"]]
    assert [r["args"] for r in rules] == [
        [{"index": 0, "value": 38, "op": "SCMP_CMP_LT"}],
        [{"index": 0, "value": 39, "op": "SCMP_CMP_EQ"}],
        [{"index": 0, "value": 40, "op": "SCMP_CMP_GT"}],
    ]


def test_production_rejects_incomplete_or_insecure_settings():
    for args in (
        {},
        {"public_origin": "http://example.com"},
        {"network_isolated": False},
        {"public_port": 8001},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **args)


def test_callback_wildcard_and_same_origin_are_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, development=True, oauth_redirect_uris=["https://chatgpt.com/*"])
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            development=True,
            public_origin="https://example.com",
            control_origin="https://example.com",
        )


@pytest.mark.parametrize(
    "callback",
    [
        "https:",
        "https:///callback",
        "https://@callback.example/",
        "https://callback.example/#",
        "https://callback.example:0/",
        "https://callback.example:99999/",
        "https://callback.example/; form-action *",
        "https://callback.example/\r\nX-Evil: yes",
        "https://callback.example\\other/",
    ],
)
def test_callback_requires_concrete_safe_https_url(callback):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, development=True, oauth_redirect_uris=[callback])
