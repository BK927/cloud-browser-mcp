import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_browser_locale import fake_adapter as locale_adapter

from cloud_browser.models import BrowserError
from cloud_browser.reader_resources import RESOURCE_TYPES, ReaderResources

fake_adapter = locale_adapter


class FetchTab:
    def __init__(self):
        self._driver = self
        self.callbacks = {}
        self.commands = []
        self.dispatched = []
        self.completed = threading.Event()
        self.failure = None

    def set_callback(self, event, callback):
        self.callbacks[event] = callback

    def run_cdp(self, command, **args):
        self.commands.append((command, args))

    def run(self, command, **args):
        self.dispatched.append((command, args, threading.get_ident()))
        if self.failure:
            raise self.failure
        return {}

    def pause(self, kind, request_id="request"):
        self.callbacks["Fetch.requestPaused"](requestId=request_id, resourceType=kind)


@pytest.fixture
def interception():
    resources = ReaderResources()
    try:
        yield resources
    finally:
        resources.close()


@pytest.mark.parametrize(
    "loaded", [None, [], ["images"], ["video"], ["fonts"], ["images", "video", "fonts"]]
)
def test_exact_request_stage_patterns_and_disable(interception, loaded):
    tab = FetchTab()
    if loaded is not None:
        interception.reset(loaded)
    interception.attach(tab)
    enabled = ["images"] if loaded is None else loaded
    expected = [
        {"resourceType": kind, "requestStage": "Request"}
        for name, kind in RESOURCE_TYPES.items()
        if name not in enabled
    ]
    assert tab.commands == (
        [("Fetch.enable", {"patterns": expected})] if expected else [("Fetch.disable", {})]
    )
    assert set(tab.callbacks) == {"Fetch.requestPaused"}
    if loaded is None:
        assert tab.commands == [
            (
                "Fetch.enable",
                {
                    "patterns": [
                        {"resourceType": "Media", "requestStage": "Request"},
                        {"resourceType": "Font", "requestStage": "Request"},
                    ]
                },
            )
        ]


@pytest.mark.parametrize("images", [True, False])
def test_paused_skipped_requests_fail_and_all_other_types_continue(interception, images):
    tab = FetchTab()
    if not images:
        interception.reset([])
    interception.attach(tab)
    caller = threading.get_ident()
    kinds = [
        "Image",
        "Media",
        "Font",
        "Document",
        "Script",
        "Stylesheet",
        "XHR",
        "Fetch",
        "WebSocket",
        "Other",
    ]
    for kind in kinds:
        tab.pause(kind, kind)
    # FIFO sentinel waits for this small fake queue to drain without polling.
    interception.close()
    assert not interception.thread.is_alive()
    assert interception.count["blocked_requests"] == (2 if images else 3)
    for kind, (command, args, thread_id) in zip(kinds, tab.dispatched, strict=True):
        blocked = kind in ("Media", "Font") or (kind == "Image" and not images)
        assert command == ("Fetch.failRequest" if blocked else "Fetch.continueRequest")
        assert args == {"requestId": kind, "_timeout": 1} | (
            {"errorReason": "BlockedByClient"} if blocked else {}
        )
        assert thread_id != caller


def test_callback_never_waits_for_transport_and_uses_current_policy(interception):
    tab = FetchTab()
    interception.reset([])
    entered, release = threading.Event(), threading.Event()
    original = tab.run

    def blocked_transport(command, **args):
        entered.set()
        assert release.wait(timeout=2)
        return original(command, **args)

    tab.run = blocked_transport
    interception.attach(tab)
    tab.pause("Script", "busy")
    assert entered.wait(timeout=2)
    # The callback returns while transport is stalled on a different thread.
    tab.pause("Image", "queued")
    interception.reset(["images"])
    release.set()
    interception.close()
    assert [command for command, _, _ in tab.dispatched] == ["Fetch.continueRequest"] * 2
    assert interception.count["blocked_requests"] == 0


def test_counter_resets_and_queued_old_events_keep_the_old_counter(interception):
    tab = FetchTab()
    interception.reset([])
    entered, release = threading.Event(), threading.Event()
    original = tab.run

    def delayed_ack(command, **args):
        entered.set()
        assert release.wait(timeout=2)
        return original(command, **args)

    tab.run = delayed_ack
    interception.attach(tab)
    previous = interception.count
    tab.pause("Image")
    assert entered.wait(timeout=2)
    interception.reset([])
    release.set()
    interception.close()
    assert previous["blocked_requests"] == 1
    assert interception.count["blocked_requests"] == 0


def test_transport_exception_does_not_kill_callback_or_dispatcher(interception):
    tab = FetchTab()
    interception.reset([])
    interception.attach(tab)
    original = tab.run

    def failing_once(command, **args):
        if args["requestId"] == "closed":
            raise RuntimeError("Target closed")
        return original(command, **args)

    tab.run = failing_once
    tab.pause("Image", "closed")
    tab.pause("Image", "live")
    interception.close()
    assert interception.count["blocked_requests"] == 1
    assert tab.dispatched[0][1]["requestId"] == "live"


@pytest.mark.parametrize("profile", [None, "reader"])
def test_adapter_initializes_new_tabs_and_popups_with_reader_policy(fake_adapter, profile):
    adapter, _, browser = fake_adapter(profile=profile)
    if profile:
        tid = adapter.sessions["ses_test"]["selected"]
        adapter.reader_configure("ses_test", tid, ["images"])
    for name in ("new_tab", "popup"):
        browser.get_tabs.return_value.append(Mock(tab_id=name))
        adapter._sync("ses_test")
    for tab in browser.get_tabs.return_value:
        fetch = [call for call in tab.run_cdp.call_args_list if call.args[0].startswith("Fetch.")]
        callbacks = [
            call
            for call in tab._driver.set_callback.call_args_list
            if call.args[0] == "Fetch.requestPaused"
        ]
        if profile:
            assert len(callbacks) == 1
            assert fetch[-1].args == ("Fetch.enable",)
            assert fetch[-1].kwargs == {
                "patterns": [
                    {"resourceType": "Media", "requestStage": "Request"},
                    {"resourceType": "Font", "requestStage": "Request"},
                ]
            }
        else:
            assert fetch == callbacks == []
            with pytest.raises(BrowserError, match="reader profile"):
                adapter.reader_configure("ses_test", adapter.sessions["ses_test"]["selected"], [])


@pytest.fixture
def screenshot_adapter(fake_adapter):
    adapter, _, _ = fake_adapter(profile="reader")
    tid = adapter.sessions["ses_test"]["selected"]
    data = {
        "url": "https://example.com/",
        "title": "Article",
        "semantic_text": "Article text",
        "viewport": {"width": 1024, "height": 768},
        "interactive_truncated": False,
        "semantic_source": "main",
        "semantic_source_truncated": False,
        "accessibility_source": "dom-fallback",
        "scroll_scan_truncated": False,
        "readable_frames": 0,
        "frame_reading_truncated": False,
        "frames": [],
        "reader_links": [],
        "protected": False,
    }
    adapter._capture_page = Mock(return_value=data)
    adapter._capture_image = Mock(
        return_value=(
            {"screenshot_id": "shot", "width": 1024, "height": 768},
            {"data": "jpeg", "mimeType": "image/jpeg"},
        )
    )
    return SimpleNamespace(adapter=adapter, tid=tid, data=data)


def test_reader_screenshot_uses_existing_capture_path_after_text(screenshot_adapter):
    adapter, tid = screenshot_adapter.adapter, screenshot_adapter.tid
    result = adapter.observe(
        "ses_test", tid, mode="semantic", reader_options={"collect_links": True, "screenshot": True}
    )
    assert result["observation"]["semantic_snapshot"] == "Article text"
    assert result["_image"] == {"data": "jpeg", "mimeType": "image/jpeg"}
    adapter._capture_image.assert_called_once_with(
        adapter.sessions["ses_test"]["tabs"][tid], screenshot_adapter.data, False, False
    )


@pytest.mark.parametrize(
    "code", ["SENSITIVE_SCREEN", "RESOURCE_PRESSURE", "SCREEN_CHANGED", "CAPTURE_TIMEOUT"]
)
def test_reader_capture_refusal_keeps_observed_text(screenshot_adapter, code):
    adapter, tid = screenshot_adapter.adapter, screenshot_adapter.tid
    adapter._capture_image.side_effect = BrowserError(code, "Refused capture", reason="fixture")
    result = adapter.observe(
        "ses_test", tid, mode="semantic", reader_options={"collect_links": True, "screenshot": True}
    )
    assert result["observation"]["semantic_snapshot"] == "Article text" and "_image" not in result
    assert result["observation"]["screenshot_omitted"] == {
        "code": code,
        "message": "Refused capture",
        "reason": "fixture",
    }
