"""Dispatched failures retain their result and cannot dispatch a second time."""

from unittest.mock import AsyncMock

import pytest
from test_service import opened, proposed

from cloud_browser.models import BrowserError


@pytest.mark.parametrize("failed,partial", [(False, False), (False, True), (True, False)])
async def test_explicit_completion_failure_is_not_success_or_retry(
    service, monkeypatch, failed, partial
):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    await service.approve(next(iter(service.pending)), True)
    wait = AsyncMock(
        side_effect=BrowserError("TAB_NOT_FOUND", "Target closed") if failed else None,
        return_value={"wait": {"matched": False, "timed_out": True, "partial": partial}},
    )
    monkeypatch.setattr(service, "_wait", wait)
    result = await service.call(
        "act",
        **args,
        confirmation_token=token,
        completion={"type": "url", "value": "https://example.com/done"},
    )
    assert result["status"] == "error"
    uncertain = failed or partial
    assert result["error"]["code"] == ("RESULT_UNCERTAIN" if uncertain else "ACTION_GOAL_NOT_MET")
    assert result["action_result"]["performed"]
    assert not result["error"]["retryable"]
    again = await service.call("act", **args, confirmation_token=token)
    assert again["error"]["code"] == ("RESULT_UNCERTAIN" if uncertain else "CONFIRMATION_USED")
    assert service.worker.executions == 1


async def test_goal_error_preserves_worker_details(service, monkeypatch):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    await service.approve(next(iter(service.pending)), True)
    original = service.worker.call

    async def fail_goal(method, **arguments):
        result = await original(method, **arguments)
        if method == "act":
            raise BrowserError(
                "ACTION_GOAL_NOT_MET",
                "Requested target value was not reached",
                revision=result["revision"],
                page=result["page"],
                action_result={"performed": True, "target_state_verified": False},
            )
        return result

    monkeypatch.setattr(service.worker, "call", fail_goal)
    result = await service.call("act", **args, confirmation_token=token)
    assert result["status"] == "error" and result["revision"] == 2
    assert result["page"]["url"] == "https://example.com/"
    assert result["action_result"] == {"performed": True, "target_state_verified": False}
    assert result["error"]["suggested_tool"] == "browser_observe"
    assert not result["error"]["retryable"]
    assert (await service.call("act", **args, confirmation_token=token))["error"][
        "code"
    ] == "CONFIRMATION_USED"
    assert service.worker.executions == 1
