import json
import os
import time

import pytest

from cloud_browser.artifacts import Artifacts
from cloud_browser.events import Events
from cloud_browser.models import BrowserError
from cloud_browser.page_tools import schema_fingerprint
from cloud_browser.security import safe_url


def test_artifact_isolation_expiry_and_budget(tmp_path):
    first = Artifacts(tmp_path / "one", max_bytes=30, file_bytes=20)
    second = Artifacts(tmp_path / "two")
    item = first.put(b"hello", "report.txt", "text/plain")
    key = item["artifact_id"]
    assert first.get(key)["text"] == "hello"
    assert "storage_name" not in json.dumps(first.list())
    with pytest.raises(BrowserError) as error:
        second.get(key)
    assert error.value.code == "ARTIFACT_NOT_FOUND"
    with pytest.raises(BrowserError):
        first.put(b"a" * 21, "large", "text/plain")
    first.items[key]["expires_at"] = time.time() - 1
    assert first.list() == [] and not list((tmp_path / "one").iterdir())


def test_expired_crash_artifacts_do_not_touch_live_or_unknown_files(tmp_path):
    root = tmp_path / "artifacts"
    old = Artifacts(root / "ses_old")
    live = Artifacts(root / "ses_live")
    old_key = old.put(b"expired", "old.txt", "text/plain")["artifact_id"]
    live_key = live.put(b"active", "live.txt", "text/plain")["artifact_id"]
    recent = old.put(b"recent", "recent.txt", "text/plain")["artifact_id"]
    unknown = old.root / "operator-notes.txt"
    unknown.write_text("preserve")
    stamp = time.time() - 3600
    for file in (old._path(old_key), live._path(live_key), unknown):
        os.utime(file, (stamp, stamp))
    assert Artifacts.reap_orphans(root, {"ses_live"}, 1800) == 1
    assert not old._path(old_key).exists()
    assert live._path(live_key).is_file() and old._path(recent).is_file() and unknown.is_file()
    with pytest.raises(BrowserError) as error:
        old.get(old_key)
    assert error.value.code == "ARTIFACT_NOT_FOUND"


def test_logs_never_capture_console_payload_and_pause_clears():
    events = Events()
    for _ in range(100):
        events.console(type="log", args=[{"value": "password=secret"}])
    assert len(events.read(limit=64)["records"]) == 64
    assert "secret" not in json.dumps(events.read())
    events.pause()
    events.console(type="log")
    assert events.read()["records"] == []


def test_public_query_and_fragment_preserved_secrets_hidden():
    url = safe_url("https://example.com/search?q=Godot&page=2&access_token=secret#installation")
    assert "q=Godot" in url and "page=2" in url and url.endswith("#installation")
    assert "secret" not in url
    assert "oauth" not in safe_url("https://example.com/#access_token=oauth-secret")


def test_schema_fingerprint_is_canonical_and_changes():
    assert schema_fingerprint({"a": 1, "b": 2}) == schema_fingerprint({"b": 2, "a": 1})
    assert schema_fingerprint({"a": 1}) != schema_fingerprint({"a": 2})


async def test_new_tools_obey_auth_lock_and_clipboard_isolation(service):
    first = await service.call("open", _principal="owner")
    args = {k: first[k] for k in ("session_id", "tab_id", "lease_id")}
    await service.call(
        "clipboard",
        _principal="owner",
        session_id=args["session_id"],
        lease_id=args["lease_id"],
        operation="write",
        text="private work",
    )
    assert (
        await service.call(
            "clipboard",
            _principal="other",
            session_id=args["session_id"],
            lease_id=args["lease_id"],
            operation="read",
        )
    )["error"]["code"] == "LEASE_INVALID"
    await service.call(
        "auth_request", _principal="owner", **args, site_origin="https://example.com"
    )
    assert args["session_id"] not in service.clipboards
    for method, options in (
        ("logs", {}),
        ("wait", {"condition": {"type": "dialog"}}),
        ("dialog", {}),
        ("artifacts", {}),
        ("clipboard", {"operation": "read"}),
    ):
        result = await service.call(method, _principal="owner", **args, **options)
        assert result["error"]["code"] == "AUTH_IN_PROGRESS", result
