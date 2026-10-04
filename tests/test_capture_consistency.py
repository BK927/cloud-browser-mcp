from types import SimpleNamespace

import pytest

from cloud_browser.capture_budget import DeadlineTab
from cloud_browser.capture_consistency import changed, may_retry
from cloud_browser.drission import DrissionAdapter, TabState
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


def test_frame_lifetime_monitor_preserves_sdk_handlers_and_pauses_privately():
    calls = []
    event = "Page.frameNavigated"

    def sdk_handler(**payload):
        calls.append(payload)

    class Driver:
        event_handlers = {event: sdk_handler}

        def set_callback(self, name, callback):
            if callback:
                self.event_handlers[name] = callback
            else:
                self.event_handlers.pop(name, None)

    driver = Driver()
    state = TabState(SimpleNamespace(_driver=driver))
    state.events.enabled = True
    DrissionAdapter._watch_frame_lifetimes(state)
    installed = driver.event_handlers[event]
    DrissionAdapter._watch_frame_lifetimes(state)
    assert driver.event_handlers[event] is installed  # no wrapper stacking
    installed(frame={"id": "main"}, type="BackForwardCacheRestore")
    assert state.frame_lifetime_epoch == 1 and len(calls) == 1
    state.events.pause()
    DrissionAdapter._pause_frame_lifetimes(state)
    assert driver.event_handlers[event] is sdk_handler
    sdk_handler(frame={"id": "private"})
    assert state.frame_lifetime_epoch == 1 and len(calls) == 2
    assert not state.frame_callbacks
