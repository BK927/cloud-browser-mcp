import asyncio
import time

from cloud_browser.service import BrowserService


async def opened(service):
    result = await service.call("open")
    assert result["status"] == "ok", result
    return result["session_id"], result["tab_id"]


async def proposed(service, sid, tid):
    args = dict(
        session_id=sid,
        tab_id=tid,
        expected_revision=1,
        action={"type": "click", "node_id": "node_1"},
    )
    result = await service.call("act", **args)
    assert result["status"] == "confirmation_required", result
    return args, result["confirmation"]["confirmation_token"]


async def test_approval_is_human_gated_and_single_use(service):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    echo = await service.call("act", **args, confirmation_token=token)
    assert echo["status"] == "confirmation_required"
    assert service.worker.executions == 0
    await service.approve(next(iter(service.pending)), True)
    results = await asyncio.gather(
        *(service.call("act", **args, confirmation_token=token) for _ in range(2))
    )
    assert sum(r["status"] == "ok" for r in results) == 1
    assert results[1]["error"]["code"] == "CONFIRMATION_USED"
    assert service.worker.executions == 1


async def test_changed_action_and_revision_cannot_use_approval(service):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    await service.approve(next(iter(service.pending)), True)
    altered = args | {"action": {"type": "click", "node_id": "node_2"}}
    assert (await service.call("act", **altered, confirmation_token=token))["error"][
        "code"
    ] == "CONFIRMATION_STALE"
    service.worker.sessions[sid][tid]["revision"] = 2
    assert (await service.call("act", **args, confirmation_token=token))["error"][
        "code"
    ] == "CONFIRMATION_STALE"
    assert service.worker.executions == 0


async def test_uncertain_does_not_retry_or_reuse(service):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    await service.approve(next(iter(service.pending)), True)
    service.worker.uncertain = True
    for _ in range(2):
        result = await service.call("act", **args, confirmation_token=token)
        assert result["error"]["code"] == "RESULT_UNCERTAIN"
    assert service.worker.executions == 1


async def test_auth_locks_entire_session_and_does_not_claim_success(service):
    sid, tid = await opened(service)
    second = await service.call("open", session_id=sid)
    result = await service.call(
        "auth_request", session_id=sid, tab_id=tid, site_origin="https://example.com"
    )
    assert result["status"] == "user_action_required"
    count = len(service.worker.calls)
    for method, args in (
        ("observe", {"tab_id": second["tab_id"]}),
        ("list_tabs", {}),
        ("configure", {"tab_id": tid, "options": {}}),
    ):
        result = await service.call(method, session_id=sid, **args)
        assert result["error"]["code"] == "AUTH_IN_PROGRESS"
    status = await service.call("status", session_id=sid)
    assert status["sessions"][0]["tabs"] is None
    assert len(service.worker.calls) == count
    lease = service.leases[sid]
    lease["expires"] = time.time() - 1
    assert (await service.call("observe", session_id=sid, tab_id=tid))["error"][
        "code"
    ] == "AUTH_IN_PROGRESS"
    done = await service.complete_handoff(lease["handoff_id"])
    assert done["authenticated"] is None
    assert done["verification"] == "unverified"
    assert (await service.call("observe", session_id=sid, tab_id=tid))["revision"] == 2


async def test_closed_tab_during_handoff(service):
    sid, tid = await opened(service)
    await service.call("handoff", session_id=sid, tab_id=tid, reason="Choose map location")
    del service.worker.sessions[sid][tid]
    done = await service.complete_handoff(service.leases[sid]["handoff_id"])
    assert done["result"]["error"]["code"] == "TAB_NOT_FOUND"


async def test_session_tombstones_survive_restart(service):
    sid, _ = await opened(service)
    restarted = BrowserService(service.cfg, service.store, service.worker)
    result = await restarted.call("open", session_id=sid)
    assert result["error"]["code"] == "SESSION_EXPIRED"
    assert restarted.sessions == {}


async def test_no_fixed_three_tab_limit(service):
    sid, _ = await opened(service)
    for _ in range(4):
        assert (await service.call("open", session_id=sid))["status"] == "ok"
    assert len((await service.call("list_tabs", session_id=sid))["tabs"]) == 5


async def test_memory_pressure_preserves_existing_tabs(service, monkeypatch):
    sid, _ = await opened(service)
    monkeypatch.setattr(
        service, "resources", lambda admission=0: {"can_admit": False, "available_mb": 20}
    )
    result = await service.call("open", session_id=sid)
    assert result["error"]["code"] == "RESOURCE_PRESSURE"
    assert len(service.worker.sessions[sid]) == 1


async def test_close_is_not_silent_success(service):
    sid, tid = await opened(service)
    assert (await service.call("close", session_id=sid, scope="tab", tab_id=tid))["status"] == "ok"
    assert (await service.call("close", session_id=sid, scope="tab", tab_id=tid))["error"][
        "code"
    ] == "TAB_NOT_FOUND"
    await service.call("close", session_id=sid, scope="session")
    assert (await service.call("close", session_id=sid, scope="session"))["error"][
        "code"
    ] == "SESSION_NOT_FOUND"


async def test_memory_denial_does_not_allocate_phantom_session(service, monkeypatch):
    monkeypatch.setattr(service, "resources", lambda admission=0: {"can_admit": False})
    assert (await service.call("open", new_tab=False))["error"]["code"] == "RESOURCE_PRESSURE"
    assert not service.sessions
    assert not service.worker.calls


async def test_cancelled_rpc_invalidates_session(service, monkeypatch):
    sid, tid = await opened(service)

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(service.worker, "call", cancel)
    import pytest

    with pytest.raises(asyncio.CancelledError):
        await service.call("observe", session_id=sid, tab_id=tid)
    assert not service.sessions
    assert service.store.get("session", sid)["state"] == "expired"
