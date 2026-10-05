from types import SimpleNamespace

import pytest

from cloud_browser.capture_budget import DeadlineTab
from cloud_browser.capture_consistency import changed, may_retry
from cloud_browser.models import BrowserError


def test_only_known_presentation_changes_retry():
    assert may_retry(["viewport", "public_frame_geometry"])
    for reason in (
        "document",
        "frame_document",
        "protected_geometry",
        "privacy_history",
        "privacy_unbounded",
        "unknown",
    ):
        assert not may_retry(["scroll", reason])
    assert not may_retry([])
    assert changed({"scroll": 1, "privacy_history": 3}, {"scroll": 2, "privacy_history": 3}) == [
        "scroll"
    ]


def test_capture_verification_uses_shared_deadline_not_per_command_budget(monkeypatch):
    clock = [10]
    monkeypatch.setattr("cloud_browser.capture_budget.time.monotonic", lambda: clock[0])
    calls = []
    proxy = DeadlineTab(SimpleNamespace(run_cdp=lambda command, **args: calls.append(args)), 25)
    proxy.run_cdp("Runtime.evaluate", _timeout=30)
    assert calls[-1]["_timeout"] == 15
    clock[0] = 24.5
    proxy.run_cdp("Page.getFrameTree")
    assert calls[-1]["_timeout"] == 0.5
    clock[0] = 25
    with pytest.raises(BrowserError) as error:
        proxy.run_cdp("Page.getFrameTree")
    assert error.value.code == "CAPTURE_TIMEOUT" and len(calls) == 2
