import asyncio
from unittest.mock import AsyncMock

import pytest

from cloud_browser.client_progress import finish_navigation


def pending():
    return {
        "status": "no_change",
        "session_id": "s",
        "lease_id": "l",
        "tab_id": "t",
        "operation_id": "o",
        "navigation": {"pending": True, "timeout_ms": 1000},
    }


async def test_synchronous_old_server_needs_no_poll():
    poll = AsyncMock()
    value = {"status": "ok"}
    assert await finish_navigation(value, poll) is value
    poll.assert_not_awaited()


async def test_owned_progress_finishes_without_redispatch():
    final = {"status": "ok", "session_id": "s", "tab_id": "t"}
    poll = AsyncMock(
        side_effect=[
            {"status": "ok", "operation": {"state": "running"}},
            {"status": "ok", "operation": {"state": "completed", "result": final}},
        ]
    )
    result = await finish_navigation(pending(), poll, sleep=AsyncMock())
    assert result["status"] == "ok" and result["lease_id"] == "l"
    assert all(
        call.args == ({"session_id": "s", "lease_id": "l", "operation_id": "o"},)
        for call in poll.call_args_list
    )


@pytest.mark.parametrize(
    "result", [None, {"status": "ok"}, {"status": "ok", "session_id": "foreign", "tab_id": "t"}]
)
async def test_bad_saved_result_never_counts_as_completion(result):
    poll = AsyncMock(
        return_value={"status": "ok", "operation": {"state": "completed", "result": result}}
    )
    value = await finish_navigation(pending(), poll, sleep=AsyncMock())
    assert value["error"]["code"] == "NAVIGATION_PROGRESS_INVALID" and value["session_id"] == "s"


async def test_progress_timeout_retains_cleanup_handles():
    clock = [0]

    async def sleep(seconds):
        clock[0] += seconds

    poll = AsyncMock(return_value={"status": "ok", "operation": {"state": "running"}})
    result = await finish_navigation(pending(), poll, clock=lambda: clock[0], sleep=sleep)
    assert result["error"]["code"] == "NAVIGATION_PROGRESS_TIMEOUT"
    assert result["session_id"] == "s" and result["lease_id"] == "l" and clock[0] == 6


async def test_terminal_error_with_null_identity_keeps_partial_open_cleanup():
    poll = AsyncMock(
        return_value={
            "status": "ok",
            "operation": {
                "state": "completed",
                "result": {
                    "status": "error",
                    "session_id": None,
                    "lease_id": None,
                    "error": {"code": "NAVIGATION_TIMEOUT"},
                },
            },
        }
    )
    result = await finish_navigation(pending() | {"tab_id": None}, poll, sleep=AsyncMock())
    assert result["error"]["code"] == "NAVIGATION_TIMEOUT"
    assert result["session_id"] == "s" and result["lease_id"] == "l"


async def test_caller_budget_leaves_time_for_owned_cleanup():
    poll = AsyncMock()
    result = await finish_navigation(pending(), poll, max_wait_seconds=0)
    assert result["error"]["code"] == "NAVIGATION_PROGRESS_TIMEOUT"
    poll.assert_not_awaited()


async def test_stalled_status_cannot_outlive_the_client_budget():
    async def stalled(identity):
        await asyncio.Event().wait()

    result = await asyncio.wait_for(
        finish_navigation(pending(), stalled, sleep=AsyncMock(), max_wait_seconds=0.02), 1
    )
    assert result["error"]["code"] == "NAVIGATION_PROGRESS_TRANSPORT_ERROR"
    assert result["lease_id"] == "l"
