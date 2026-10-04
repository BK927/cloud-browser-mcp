import socket
from urllib.parse import parse_qs, urlencode, urlsplit

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


@pytest.mark.parametrize("path", ["/board/view/", "/mgallery/board/view/", "/mini/board/view/"])
@pytest.mark.parametrize("article", ["2940536", "123456789012"])
def test_dc_public_article_number_remains_navigable(path, article):
    url = f"https://gall.dcinside.com{path}?id=programming&no={article}#view"
    assert safe_url(url) == url


@pytest.mark.parametrize(
    "base",
    [
        "http://gall.dcinside.com/board/view/",
        "https://gall.dcinside.com:8443/board/view/",
        "https://other.example/board/view/",
        "https://gall.dcinside.com.attacker.example/board/view/",
        "https://gall.dcinside.com/member/login/",
        "https://gall.dcinside.com/board/view",
    ],
)
def test_dc_article_exception_is_origin_and_path_bound(base):
    cleaned = safe_url(base + "?id=programming&no=2940536&access_token=private-token")
    query = parse_qs(urlsplit(cleaned).query)
    assert query["no"] == ["[REDACTED]"]
    assert query["access_token"] == ["[REDACTED]"]
    assert "private-token" not in cleaned


@pytest.mark.parametrize("value", ["", "1234567890123", "-3", "+3", "3.0", "abc", "１２３"])
def test_dc_article_exception_rejects_unbounded_or_non_decimal_values(value):
    cleaned = safe_url("https://gall.dcinside.com/board/view/?" + urlencode({"no": value}))
    assert parse_qs(urlsplit(cleaned).query)["no"] == ["[REDACTED]"]


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
