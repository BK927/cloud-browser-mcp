import socket

import pytest
from pydantic import TypeAdapter, ValidationError

from cloud_browser.models import Action, BrowserError, Configuration
from cloud_browser.security import public_addresses, safe_url, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/",
        "https://user:pass@example.com/",
        "http://example.com:9222/",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(BrowserError):
        validate_url(url)


def test_dns_mixed_public_private_is_denied(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 443)), (0, 0, 0, "", ("10.0.0.1", 443))],
    )
    with pytest.raises(ValueError):
        public_addresses("example.com", 443)


def test_urls_hide_credentials_query_and_fragment():
    result = safe_url("https://user:password@example.com/a?code=12345&x=secret#token")
    assert not any(secret in result for secret in ("password", "12345", "secret", "#token", "user"))


def test_action_schema_rejects_unadvertised_actions_and_extra_keys():
    schema = TypeAdapter(Action)
    for value in (
        {"type": "eval", "script": "alert(1)"},
        {"type": "click", "node_id": "a", "selector": "button"},
        {"type": "click_at", "x": 1, "y": 2},
    ):
        with pytest.raises(ValidationError):
            schema.validate_python(value)
    with pytest.raises(ValidationError):
        Configuration(viewport_width=1920)
