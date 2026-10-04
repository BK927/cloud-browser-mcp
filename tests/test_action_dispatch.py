"""Dispatch evidence, rather than exception type, decides retry safety."""

import hashlib
import json
from unittest.mock import Mock

import pytest
from test_service import opened, proposed

from cloud_browser.drission import DrissionAdapter, TabState
from cloud_browser.models import BrowserError


@pytest.fixture
def action_adapter(cfg):
    adapter = object.__new__(DrissionAdapter)
    adapter.cfg = cfg
    state = TabState(
        Mock(url="https://example.com/", _frame_id="frame-main"),
        revision=1,
        data={"url": "https://example.com/", "title": "Example"},
    )
    state.options["wait_ms"] = 0
    adapter._tab = Mock(return_value=state)
    adapter.prepare = Mock()
    adapter._session = Mock(return_value={"tabs": {"tab_test": state}})
    adapter._navigation_marker = Mock(
        return_value={"url": state.tab.url, "document": "doc", "sequence": 0}
    )
    adapter._node_target = Mock(return_value=(state, (7, {})))
    adapter.Element = Mock()
    adapter._target_value = Mock(
        side_effect=lambda owner, backend, script: (
            {"x": 10, "y": 10, "width": 20, "height": 20}
            if "getBoundingClientRect" in script
            else True
        )
    )
    adapter._deep_target_hit = Mock(return_value=True)
    adapter._frame_point = Mock(side_effect=lambda owner, x, y: (x, y))
    adapter._sync = Mock()
    adapter._capture_page = Mock(return_value=state.data)
    adapter._verify_target_goal = Mock(return_value=None)

    def result(sid, tid, **extra):
        return {
            "session_id": sid,
            "tab_id": tid,
            "revision": state.revision,
            "page": state.data.copy(),
            **extra,
        }

    adapter._result = result
    adapter._cached_result = lambda sid, tid, state, **extra: result(sid, tid, **extra)
    return adapter


def connect_action(service, adapter, monkeypatch, *, automatic=False):
    original = service.worker.call

    def input_count():
        return sum(
            call.args[0].startswith("Input.")
            for call in adapter._tab.return_value.tab.run_cdp.call_args_list
        )

    async def call(method, **args):
        if method == "act":
            service.worker.calls.append((method, args))
            before = input_count()
            try:
                return adapter.act(**args)
            finally:
                if input_count() > before:
                    service.worker.executions += 1
        result = await original(method, **args)
        if automatic and method == "prepare":
            result["requires_confirmation"] = False
        return result

    monkeypatch.setattr(service.worker, "call", call)


def execution_binding(args):
    return hashlib.sha256(
        json.dumps(
            [args["session_id"], args["tab_id"], args["expected_revision"], args["action"]],
            sort_keys=True,
        ).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    "failure,code",
    [
        ("focus", "NODE_NOT_ACTIONABLE"),
        ("focus_verification", "NODE_NOT_ACTIONABLE"),
        ("state", "BROWSER_ERROR"),
        ("prepare", "BROWSER_ERROR"),
        ("target", "BROWSER_ERROR"),
        ("element", "BROWSER_ERROR"),
        ("inside", "BROWSER_ERROR"),
        ("stale_prepare", "STALE_NODE"),
    ],
)
async def test_pre_dispatch_errors_do_not_lock_and_same_approval_can_retry(
    service, action_adapter, monkeypatch, failure, code
):
    sid, tid = await opened(service)
    connect_action(service, action_adapter, monkeypatch)
    args = {
        "session_id": sid,
        "tab_id": tid,
        "expected_revision": 1,
        "action": {"type": "keypress", "node_id": "node_1", "keys": ["ENTER"]},
    }
    proposal = await service.call("act", **args)
    token = proposal["confirmation"]["confirmation_token"]
    review_id = next(iter(service.pending))
    await service.approve(review_id, True)
    if failure == "focus":
        failing = action_adapter._tab.return_value.tab.run_cdp
    elif failure in ("focus_verification", "inside"):
        failing = action_adapter._target_value
    elif failure == "element":
        failing = action_adapter.Element
    elif failure == "target":
        failing = action_adapter._node_target
    elif failure == "state":
        failing = action_adapter._tab
    else:
        failing = action_adapter.prepare
    original_effect = failing.side_effect
    failing.side_effect = (
        BrowserError("STALE_NODE", "Target replaced", reason="replaced")
        if failure == "stale_prepare"
        else RuntimeError("Injected pre-dispatch failure")
    )
    if failure == "focus_verification":
        failing.side_effect = None
        failing.return_value = False
    result = await service.call("act", **args, confirmation_token=token)
    assert result["error"]["code"] == code
    assert result["action_result"]["performed"] is False
    if failure == "stale_prepare":
        assert result["reason"] == "replaced"
    assert not service.sessions[sid]["uncertain"]
    assert service.worker.executions == 0
    assert service.store.get("execution", execution_binding(args)) is None
    assert service.store.get("approval", token)["state"] == "approved"
    assert review_id in service.pending
    failing.side_effect = original_effect
    result = await service.call("act", **args, confirmation_token=token)
    assert result["status"] in ("ok", "no_change")
    assert result["action_result"]["performed"] is True
    assert service.worker.executions == 1
    assert (await service.call("act", **args, confirmation_token=token))["error"][
        "code"
    ] == "CONFIRMATION_USED"


async def test_covered_target_restores_approval_without_renewing_ttl(
    service, action_adapter, monkeypatch
):
    sid, tid = await opened(service)
    connect_action(service, action_adapter, monkeypatch)
    args, token = await proposed(service, sid, tid)
    review_id = next(iter(service.pending))
    await service.approve(review_id, True)
    expires = service.store.expires_at("approval", token)
    pending = service.pending[review_id].copy()
    now = expires - 30
    monkeypatch.setattr("cloud_browser.service.time.time", lambda: now)

    def covered(*args):
        nonlocal now
        now += 5
        return False

    action_adapter._deep_target_hit.side_effect = covered
    result = await service.call("act", **args, confirmation_token=token)
    assert result["error"]["code"] == "NODE_NOT_ACTIONABLE"
    assert result["action_result"]["performed"] is False
    assert service.store.expires_at("approval", token) == expires
    assert service.pending[review_id] == pending
    assert service.store.get("approval", token)["state"] == "approved"
    assert not service.sessions[sid]["uncertain"]
    action_adapter._deep_target_hit.side_effect = None
    result = await service.call("act", **args, confirmation_token=token)
    assert result["action_result"]["performed"] is True
    assert service.worker.executions == 1
    assert not service.pending
    assert (await service.call("act", **args, confirmation_token=token))["error"][
        "code"
    ] == "CONFIRMATION_USED"


async def test_pre_dispatch_rollback_does_not_revive_expired_approval(
    service, action_adapter, monkeypatch
):
    sid, tid = await opened(service)
    connect_action(service, action_adapter, monkeypatch)
    args, token = await proposed(service, sid, tid)
    await service.approve(next(iter(service.pending)), True)
    expires = service.store.expires_at("approval", token)
    now = expires - 1
    monkeypatch.setattr("cloud_browser.service.time.time", lambda: now)

    def covered(*args):
        nonlocal now
        now = expires + 1
        return False

    action_adapter._deep_target_hit.side_effect = covered
    result = await service.call("act", **args, confirmation_token=token)
    assert result["action_result"]["performed"] is False
    assert service.store.get("execution", execution_binding(args)) is None
    assert service.store.get("approval", token)["state"] == "consumed"
    assert not service.pending
    assert service.worker.executions == 0
    assert (await service.call("act", **args, confirmation_token=token))["error"][
        "code"
    ] == "CONFIRMATION_USED"


@pytest.mark.parametrize("failure", ["input", "observation"])
async def test_post_dispatch_failure_keeps_lock_and_consumed_approval(
    service, action_adapter, monkeypatch, failure
):
    sid, tid = await opened(service)
    connect_action(service, action_adapter, monkeypatch)
    args, token = await proposed(service, sid, tid)
    await service.approve(next(iter(service.pending)), True)
    if failure == "input":

        def dispatch(command, **parameters):
            if command == "Input.dispatchMouseEvent" and parameters["type"] == "mousePressed":
                raise RuntimeError("Lost input reply")
            return {}

        action_adapter._tab.return_value.tab.run_cdp.side_effect = dispatch
    else:
        action_adapter._capture_page.side_effect = BrowserError(
            "OBSERVATION_FAILED", "Page context gone after input"
        )
    result = await service.call("act", **args, confirmation_token=token)
    assert result["error"]["code"] == "RESULT_UNCERTAIN"
    assert result["action_result"]["performed"] is True
    assert service.sessions[sid]["uncertain"]
    assert service.store.get("approval", token)["state"] == "consumed"
    assert service.store.get("execution", execution_binding(args))
    assert not service.pending
    assert (await service.call("act", **args, confirmation_token=token))["error"][
        "code"
    ] == "RESULT_UNCERTAIN"
    assert service.worker.executions == 1


async def test_balanced_pre_dispatch_failure_removes_only_its_execution(
    service, action_adapter, monkeypatch
):
    sid, tid = await opened(service)
    service.cfg.approval_policy = "balanced"
    connect_action(service, action_adapter, monkeypatch, automatic=True)
    args = {
        "session_id": sid,
        "tab_id": tid,
        "expected_revision": 1,
        "action": {"type": "click", "node_id": "node_1"},
    }
    service.store.put("execution", "unrelated", {"dispatched": True})
    action_adapter._deep_target_hit.return_value = False
    failed = await service.call("act", **args)
    assert failed["error"]["code"] == "NODE_NOT_ACTIONABLE"
    assert service.store.get("execution", execution_binding(args)) is None
    assert service.store.get("execution", "unrelated") == {"dispatched": True}
    assert not service.sessions[sid]["uncertain"]
    action_adapter._deep_target_hit.return_value = True
    succeeded = await service.call("act", **args)
    assert succeeded["action_result"]["performed"]
    assert (await service.call("act", **args))["error"]["code"] == "ACTION_ALREADY_DISPATCHED"
    assert service.worker.executions == 1


@pytest.mark.parametrize(
    "code,performed",
    [("BROWSER_ERROR", None), ("BROWSER_ERROR", True), ("RESULT_UNCERTAIN", False)],
)
async def test_rollback_requires_explicit_false_and_non_uncertain_code(
    service, monkeypatch, code, performed
):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    await service.approve(next(iter(service.pending)), True)
    original = service.worker.call

    async def failure(method, **arguments):
        if method == "act":
            raise BrowserError(
                code,
                "Unknown or dispatched failure",
                **({"action_result": {"performed": performed}} if performed is not None else {}),
            )
        return await original(method, **arguments)

    monkeypatch.setattr(service.worker, "call", failure)
    assert (await service.call("act", **args, confirmation_token=token))["error"]["code"] == code
    assert service.store.get("approval", token)["state"] == "consumed"
    assert service.store.get("execution", execution_binding(args))
    assert not service.pending
