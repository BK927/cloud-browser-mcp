"""Completion validation and bounded observation recovery never repeat input."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_service import opened, proposed

from cloud_browser.drission import DrissionAdapter, TabState
from cloud_browser.models import BrowserError


@pytest.mark.parametrize("field", ["selector", "scope"])
async def test_invalid_completion_css_is_rejected_in_prepare_before_dispatch(
    service, monkeypatch, field
):
    sid, tid = await opened(service)
    adapter = object.__new__(DrissionAdapter)
    state = TabState(Mock(_frame_id="frame-main"), revision=1)
    adapter._tab = Mock(return_value=state)
    registry = SimpleNamespace(pinned=set(), get=Mock(return_value=None))
    adapter._registry_for = Mock(return_value=(state, registry))
    adapter._capture_page = Mock(return_value={})
    adapter._guard_page = Mock()

    def cdp(command, **parameters):
        if command == "Page.createIsolatedWorld":
            assert parameters["worldName"] == "cloud-browser-observer"
            return {"executionContextId": 42}
        assert command == "Runtime.evaluate"
        assert parameters["contextId"] == 42 and parameters["returnByValue"]
        assert "document.querySelector" in parameters["expression"]
        assert "SyntaxError" in parameters["expression"]
        assert '"["' in parameters["expression"]
        return {"result": {"value": False}}

    state.tab.run_cdp.side_effect = cdp
    original = service.worker.call

    async def call(method, **args):
        if method == "prepare":
            service.worker.calls.append((method, args))
            return adapter.prepare(**args)
        return await original(method, **args)

    monkeypatch.setattr(service.worker, "call", call)
    result = await service.call(
        "act",
        session_id=sid,
        tab_id=tid,
        expected_revision=1,
        action={"type": "click", "node_id": "node_1"},
        completion={"type": "element", "query": {field: "["}},
    )
    assert result["error"]["code"] == "INVALID_SELECTOR"
    adapter._capture_page.assert_called_once_with(state)
    assert not any(method == "act" for method, _ in service.worker.calls)
    assert not service.sessions[sid]["uncertain"]
    assert not service.pending
    assert service.worker.executions == 0


@pytest.mark.parametrize(
    "completion", [{"type": "element", "query": {"selector": 1}}, {"type": "url"}]
)
async def test_invalid_completion_input_is_rejected_before_prepare(service, completion):
    sid, tid = await opened(service)
    result = await service.call(
        "act",
        session_id=sid,
        tab_id=tid,
        expected_revision=1,
        action={"type": "click", "node_id": "node_1"},
        completion=completion,
    )
    assert result["error"]["code"] == "INVALID_INPUT"
    assert not any(method in ("prepare", "act") for method, _ in service.worker.calls)
    assert not service.sessions[sid]["uncertain"]


@pytest.fixture
def waiting_adapter():
    adapter = object.__new__(DrissionAdapter)
    state = TabState(Mock(url="https://example.com/"))
    adapter._tab = Mock(return_value=state)
    adapter._cached_result = lambda sid, tid, state, **extra: extra
    return adapter


@pytest.mark.parametrize("wait_state", ["absent", "hidden"])
@pytest.mark.parametrize(
    "reason,partial",
    [
        ("FRAME_NOT_VISIBLE", False),
        ("PROTECTED_PARENT", True),
        ("SENSITIVE_FRAME", True),
        ("FRAME_UNAVAILABLE", True),
        ("FRAME_BUDGET", True),
        (None, True),
    ],
)
def test_negative_wait_classifies_unreadable_frame_reasons(
    waiting_adapter, wait_state, reason, partial
):
    waiting_adapter.observe = Mock(
        return_value={
            "observation": {
                "interactive_snapshot": "",
                "frame_reading_truncated": True,
                "frames": [{"readable": False, "reason": reason}],
            }
        }
    )
    result = waiting_adapter.wait(
        "sid", "tid", {"type": "element", "query": {"selector": ".spinner"}, "state": wait_state}, 0
    )
    assert result["wait"]["partial"] is partial
    assert result["wait"]["matched"] is not partial
    assert result["wait"]["timed_out"] is partial


@pytest.mark.parametrize("wait_state", ["present", "visible", "enabled"])
def test_positive_wait_keeps_existing_hidden_frame_partial_semantics(waiting_adapter, wait_state):
    waiting_adapter.observe = Mock(
        return_value={
            "observation": {
                "interactive_snapshot": '{"node_id":"node_1","disabled":false}',
                "frame_reading_truncated": True,
                "frames": [{"readable": False, "reason": "FRAME_NOT_VISIBLE"}],
            }
        }
    )
    result = waiting_adapter.wait(
        "sid", "tid", {"type": "element", "query": {"selector": ".spinner"}, "state": wait_state}, 0
    )
    assert result["wait"]["matched"] and result["wait"]["partial"]


@pytest.mark.parametrize("wait_state", ["absent", "hidden"])
@pytest.mark.parametrize("truncated", ["truncated", "interactive_truncated", "unknown_frames"])
def test_negative_wait_stays_partial_for_other_truncation(waiting_adapter, wait_state, truncated):
    observation = {
        "interactive_snapshot": "",
        "frame_reading_truncated": True,
        "frames": [{"readable": False, "reason": "FRAME_NOT_VISIBLE"}],
    }
    if truncated == "unknown_frames":
        observation["frames"] = []
    else:
        observation[truncated] = True
    waiting_adapter.observe = Mock(return_value={"observation": observation})
    result = waiting_adapter.wait(
        "sid", "tid", {"type": "element", "query": {"selector": ".spinner"}, "state": wait_state}, 0
    )
    assert result["wait"]["partial"] and not result["wait"]["matched"]


@pytest.mark.parametrize(
    "code", ["BROWSER_ERROR", "OBSERVATION_FAILED", "NAVIGATION_IN_PROGRESS", "SCREEN_CHANGED"]
)
async def test_transient_completion_error_then_match_succeeds(service, monkeypatch, code):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    await service.approve(next(iter(service.pending)), True)
    wait = AsyncMock(
        side_effect=[
            BrowserError(code, "Page changed during observation"),
            {"wait": {"matched": True, "partial": False, "timed_out": False}},
        ]
    )
    monkeypatch.setattr(service, "_wait", wait)
    result = await service.call(
        "act",
        **args,
        confirmation_token=token,
        completion={"type": "element", "query": {"selector": "#done"}},
        completion_timeout_ms=1000,
    )
    assert result["status"] == "ok" and result["completion"]["matched"]
    assert not service.sessions[sid]["uncertain"]
    assert wait.await_count == 2
    first, second = wait.await_args_list
    assert 0 <= second.args[3] < first.args[3] <= 1000
    assert service.worker.executions == 1
    assert (await service.call("act", **args, confirmation_token=token))["error"][
        "code"
    ] == "CONFIRMATION_USED"


@pytest.mark.parametrize("partial", [False, True])
async def test_transient_completion_error_then_timeout_keeps_definitive_vs_unknown_result(
    service, monkeypatch, partial
):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    await service.approve(next(iter(service.pending)), True)
    wait = AsyncMock(
        side_effect=[
            BrowserError("BROWSER_ERROR", "Context lost during navigation"),
            {"wait": {"matched": False, "partial": partial, "timed_out": True}},
        ]
    )
    monkeypatch.setattr(service, "_wait", wait)
    result = await service.call(
        "act",
        **args,
        confirmation_token=token,
        completion={"type": "element", "query": {"selector": "#done"}},
        completion_timeout_ms=1000,
    )
    assert result["error"]["code"] == ("RESULT_UNCERTAIN" if partial else "ACTION_GOAL_NOT_MET")
    assert service.sessions[sid]["uncertain"] is partial
    assert result["action_result"]["performed"]
    assert wait.await_count == 2 and service.worker.executions == 1


async def test_repeated_transient_completion_error_exhausts_original_deadline(service, monkeypatch):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    await service.approve(next(iter(service.pending)), True)
    now = 100.0
    monkeypatch.setattr("cloud_browser.service.time.monotonic", lambda: now)

    async def sleep(delay):
        nonlocal now
        now += delay

    monkeypatch.setattr("cloud_browser.service.asyncio.sleep", sleep)
    wait = AsyncMock(side_effect=BrowserError("BROWSER_ERROR", "Context repeatedly lost"))
    monkeypatch.setattr(service, "_wait", wait)
    result = await service.call(
        "act",
        **args,
        confirmation_token=token,
        completion={"type": "url", "value": "https://example.com/done"},
        completion_timeout_ms=500,
    )
    assert now == 100.5
    assert wait.await_count == 3
    assert result["error"]["code"] == "RESULT_UNCERTAIN"
    assert result["completion"]["error"]["code"] == "BROWSER_ERROR"
    assert result["action_result"]["performed"]
    assert service.sessions[sid]["uncertain"]
    assert service.worker.executions == 1
