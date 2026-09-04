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
    assert all(p.startswith("127.0.0.1:") for p in browser["ports"])
    assert len(browser["ports"]) == 2
    assert not any("5900" in p or "6080" in p or "9222" in p for p in browser["ports"])
    assert not browser.get("privileged")
    assert "no-sandbox" not in (ROOT / "deploy/chromium-launcher").read_text()
    assert not compose["services"]["egress"].get("ports")


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
