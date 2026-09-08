import time

import pytest
from test_service import opened, proposed

from cloud_browser.models import BrowserError


async def test_denial_is_visible_and_cannot_be_used_as_approval(service):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    status = await service.call("status", session_id=sid)
    assert status["approvals"][0]["state"] == "pending"
    assert "confirmation_token" not in status["approvals"][0]
    await service.approve(next(iter(service.pending)), False)
    status = await service.call("status", session_id=sid)
    assert status["approvals"][0]["state"] == "denied"
    rejected = await service.call("act", **args, confirmation_token=token)
    assert rejected["status"] == "blocked"
    assert rejected["error"]["code"] == "CONFIRMATION_DENIED"
    assert service.worker.executions == 0


async def test_approval_does_not_invent_a_destination(service):
    sid, tid = await opened(service)
    await proposed(service, sid, tid)
    review = next(iter(service.pending.values()))["confirmation"]
    assert review["current_page"] == "https://example.com/"
    assert review["destination"] is None
    assert review["destination_kind"] == "unknown"
    assert review["destination_verified"] is False


async def test_approved_state_is_reported_without_consuming_approval(service):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    await service.approve(next(iter(service.pending)), True)
    assert (await service.call("status", session_id=sid))["approvals"][0]["state"] == "approved"
    assert service.worker.executions == 0
    assert (await service.call("act", **args, confirmation_token=token))["status"] == "ok"
    assert not (await service.call("status", session_id=sid))["approvals"]


async def test_expired_control_can_only_be_renewed_privately_and_stays_locked(service):
    sid, tid = await opened(service)
    await service.call(
        "auth_request", session_id=sid, tab_id=tid, site_origin="https://example.com"
    )
    lease = service.leases[sid]
    lease["expires"] = time.time() - 1
    status = await service.call("status", session_id=sid)
    control = status["sessions"][0]["control"]
    assert control["control_access_expired"] and control["automation_paused"]
    count = len(service.worker.calls)
    result = await service.renew_handoff(lease["handoff_id"])
    assert not result["control_access_expired"] and result["automation_paused"]
    assert len(service.worker.calls) == count
    assert (await service.call("observe", session_id=sid, tab_id=tid))["error"][
        "code"
    ] == "AUTH_IN_PROGRESS"
    assert result["authenticated"] is None


async def test_cancelling_control_closes_session_without_observing_credentials(service):
    sid, tid = await opened(service)
    await proposed(service, sid, tid)
    await service.call("handoff", session_id=sid, tab_id=tid, reason="Manual test")
    hid = service.leases[sid]["handoff_id"]
    count = len(service.worker.calls)
    result = await service.cancel_handoff(hid)
    assert result["session_closed"]
    assert [method for method, _ in service.worker.calls[count:]] == ["close"]
    assert sid not in service.sessions and sid not in service.leases
    assert not service.pending
    assert (await service.call("observe", session_id=sid, tab_id=tid))["error"][
        "code"
    ] == "SESSION_NOT_FOUND"
    with pytest.raises(BrowserError):
        await service.renew_handoff(hid)


async def test_failed_cancel_does_not_drop_or_unlock_a_live_session(service, monkeypatch):
    sid, tid = await opened(service)
    await service.call("handoff", session_id=sid, tab_id=tid, reason="Manual test")
    hid = service.leases[sid]["handoff_id"]
    original = service.worker.call

    async def close_failure(method, **kwargs):
        if method == "close":
            raise BrowserError("BROWSER_ERROR", "Test close failure")
        return await original(method, **kwargs)

    monkeypatch.setattr(service.worker, "call", close_failure)
    with pytest.raises(BrowserError):
        await service.cancel_handoff(hid)
    assert sid in service.sessions and service.leases[sid]["state"] == "active"
    assert (await service.call("observe", session_id=sid, tab_id=tid))["error"][
        "code"
    ] == "USER_CONTROL_ACTIVE"
    monkeypatch.setattr(service.worker, "call", original)
    assert (await service.cancel_handoff(hid))["session_closed"]


async def test_capabilities_are_explicit(service):
    status = await service.call("status")
    assert status["capabilities"]["image_content"]
    assert status["capabilities"]["approval_policy"] == "strict-per-action"
    assert not status["capabilities"]["authentication_verification"]
    assert status["capabilities"]["file_upload_automation"]
    assert status["capabilities"]["file_upload_scope"] == "private-staged-files-only"


async def test_bridge_start_failure_does_not_unlock_collection(service, monkeypatch):
    sid, tid = await opened(service)
    original = service.worker.call

    async def fail(method, **kwargs):
        if method == "focus":
            raise BrowserError("HANDOFF_UNAVAILABLE", "Bridge failed after pausing collection")
        return await original(method, **kwargs)

    monkeypatch.setattr(service.worker, "call", fail)
    failed = await service.call("handoff", session_id=sid, tab_id=tid, reason="Test")
    assert failed["error"]["code"] == "HANDOFF_UNAVAILABLE"
    assert service.leases[sid]["start_error"] == "HANDOFF_UNAVAILABLE"
    blocked = await service.call("observe", session_id=sid, tab_id=tid)
    assert blocked["error"]["code"] == "USER_CONTROL_ACTIVE"
    assert (await service.cancel_handoff(service.leases[sid]["handoff_id"]))["session_closed"]
