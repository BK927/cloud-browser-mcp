"""Private control revokes consent, including actions without a DOM node binding."""

import copy

import pytest

from cloud_browser.models import BrowserError


async def opened(service, principal="connection"):
    result = await service.call("open", _principal=principal)
    assert result["status"] == "ok", result
    return {key: result[key] for key in ("session_id", "tab_id", "lease_id")} | {
        "_principal": principal
    }


async def proposed(service, work, revision=1):
    args = work | {
        "revision": revision,
        "tool_name": "publish_note",
        "arguments": {"text": "Public test note"},
    }
    result = await service.call("call_page_tool", **args)
    assert result["status"] == "confirmation_required", result
    token = result["confirmation"]["confirmation_token"]
    review_id = next(key for key, item in service.pending.items() if item["token"] == token)
    return args, token, review_id


async def start_control(service, work, kind):
    options = (
        {"site_origin": "https://example.com"}
        if kind == "auth_request"
        else {"reason": "Test manual control"}
    )
    result = await service.call(kind, **work, **options)
    assert result["status"] == "user_action_required", result
    return result["auth" if kind == "auth_request" else "handoff"]["handoff_id"]


@pytest.mark.parametrize("kind", ["handoff", "auth_request"])
@pytest.mark.parametrize("decision", [None, True, False])
async def test_control_revokes_all_outstanding_decisions_and_requires_new_consent(
    service, kind, decision
):
    work = await opened(service)
    args, token, review_id = await proposed(service, work)
    if decision is not None:
        await service.approve(review_id, decision)
    hid = await start_control(service, work, kind)
    sid = work["session_id"]
    assert service.sessions[sid]["approval_epoch"] == 1
    assert service.store.get("approval", token) is None
    assert review_id not in service.pending
    status_args = {key: value for key, value in work.items() if key != "tab_id"}
    assert (await service.call("status", **status_args))["approvals"] == []
    with pytest.raises(BrowserError, match="expired") as rejected:
        await service.approve(review_id, True)
    assert rejected.value.code == "CONFIRMATION_STALE"

    done = await service.complete_handoff(hid)
    assert done["state"] == "completed"
    assert service.sessions[sid]["approval_epoch"] == 2
    assert service.worker.executions == 0
    stale = await service.call(
        "call_page_tool", **(args | {"revision": 2}), confirmation_token=token
    )
    assert stale["error"]["code"] == "CONFIRMATION_STALE"
    assert service.worker.executions == 0

    fresh_args, fresh_token, fresh_review = await proposed(service, work, revision=2)
    assert fresh_token != token
    await service.approve(fresh_review, True)
    result = await service.call("call_page_tool", **fresh_args, confirmation_token=fresh_token)
    assert result["status"] == "ok"
    assert service.worker.executions == 1


async def test_epoch_rejects_persisted_approval_missing_from_pending(service):
    work = await opened(service)
    args, token, review_id = await proposed(service, work)
    await service.approve(review_id, True)
    service.pending.pop(review_id)
    old_record = copy.deepcopy(service.store.get("approval", token))
    hid = await start_control(service, work, "handoff")
    assert service.store.get("approval", token) == old_record
    await service.complete_handoff(hid)
    calls = len(service.worker.calls)
    result = await service.call(
        "call_page_tool", **(args | {"revision": 2}), confirmation_token=token
    )
    assert result["error"]["code"] == "CONFIRMATION_STALE"
    assert len(service.worker.calls) == calls
    assert service.worker.executions == 0


async def test_revocation_preserves_other_work_approval_owner_and_lease(service):
    service.cfg.managed_display = True
    first, second = await opened(service), await opened(service)
    first_args, first_token, first_review = await proposed(service, first)
    second_args, second_token, second_review = await proposed(service, second)
    await service.approve(first_review, True)
    await service.approve(second_review, True)
    second_record = copy.deepcopy(service.store.get("approval", second_token))
    second_owner = copy.deepcopy(service.owners[second["session_id"]])
    hid = await start_control(service, first, "auth_request")
    assert first_review not in service.pending
    assert second_review in service.pending
    assert service.store.get("approval", second_token) == second_record
    assert service.sessions[second["session_id"]]["approval_epoch"] == 0
    await service.complete_handoff(hid)
    assert service.store.get("approval", second_token) == second_record
    assert service.sessions[second["session_id"]]["approval_epoch"] == 0
    assert service.owners[second["session_id"]] == second_owner
    stolen = await service.call(
        "call_page_tool",
        **(second_args | {"lease_id": first["lease_id"]}),
        confirmation_token=second_token,
    )
    assert stolen["error"]["code"] == "LEASE_INVALID"
    result = await service.call("call_page_tool", **second_args, confirmation_token=second_token)
    assert result["status"] == "ok"
    stale = await service.call(
        "call_page_tool", **(first_args | {"revision": 2}), confirmation_token=first_token
    )
    assert stale["error"]["code"] == "CONFIRMATION_STALE"
    assert service.worker.executions == 1


@pytest.mark.parametrize("kind", ["handoff", "auth_request"])
async def test_private_control_start_failure_never_restores_old_consent(service, monkeypatch, kind):
    work = await opened(service)
    args, token, review_id = await proposed(service, work)
    await service.approve(review_id, True)
    original = service.worker.call

    async def fail_focus(method, **kwargs):
        if method == "focus":
            raise BrowserError("HANDOFF_UNAVAILABLE", "Bridge startup failed")
        return await original(method, **kwargs)

    monkeypatch.setattr(service.worker, "call", fail_focus)
    options = (
        {"site_origin": "https://example.com"}
        if kind == "auth_request"
        else {"reason": "Test failure"}
    )
    failed = await service.call(kind, **work, **options)
    assert failed["error"]["code"] == "HANDOFF_UNAVAILABLE"
    assert service.store.get("approval", token) is None
    assert review_id not in service.pending
    assert service.sessions[work["session_id"]]["approval_epoch"] == 1
    lease = service.leases[work["session_id"]]
    assert lease["state"] == "active"
    monkeypatch.setattr(service.worker, "call", original)
    assert (await service.complete_handoff(lease["handoff_id"]))["state"] == "completed"
    result = await service.call(
        "call_page_tool", **(args | {"revision": 2}), confirmation_token=token
    )
    assert result["error"]["code"] == "CONFIRMATION_STALE"
    assert service.worker.executions == 0


@pytest.mark.parametrize("failure", ["disconnect", "resume"])
async def test_failed_return_keeps_automation_paused_and_revocation_permanent(
    service, monkeypatch, failure
):
    work = await opened(service)
    _, token, review_id = await proposed(service, work)
    await service.approve(review_id, True)
    hid = await start_control(service, work, "handoff")
    original = service.worker.call

    async def fail_resume(method, **kwargs):
        if method == "resume":
            raise BrowserError("BROWSER_ERROR", "Resume failed")
        return await original(method, **kwargs)

    async def fail_disconnect():
        raise RuntimeError("Private connection could not close")

    if failure == "resume":
        monkeypatch.setattr(service.worker, "call", fail_resume)
    else:
        service.control_disconnectors.add(fail_disconnect)
    failed = await service.complete_handoff(hid)
    assert failed["state"] == "active"
    assert failed["automation_paused"]
    assert service.sessions[work["session_id"]]["approval_epoch"] == 2
    assert service.store.get("approval", token) is None
    assert review_id not in service.pending
    service.control_disconnectors.clear()
    monkeypatch.setattr(service.worker, "call", original)
    assert (await service.complete_handoff(hid))["state"] == "completed"
    assert service.sessions[work["session_id"]]["approval_epoch"] == 3
    assert service.store.get("approval", token) is None
    assert service.worker.executions == 0


@pytest.mark.parametrize("failure", ["origin", "tab", "disabled", "lease", "principal"])
async def test_rejected_control_request_cannot_revoke_existing_approval(service, failure):
    work = await opened(service)
    _, token, review_id = await proposed(service, work)
    await service.approve(review_id, True)
    record = copy.deepcopy(service.store.get("approval", token))
    args = work | {"site_origin": "https://example.com"}
    if failure == "origin":
        args["site_origin"] = "https://other.example"
    elif failure == "tab":
        args["tab_id"] = "closed_test_tab"
    elif failure == "disabled":
        service.cfg.manual_control_enabled = False
    elif failure == "lease":
        args["lease_id"] = "wrong_test_lease"
    else:
        args["_principal"] = "other_connection"
    failed = await service.call("auth_request", **args)
    assert failed["status"] == "error"
    assert service.sessions[work["session_id"]]["approval_epoch"] == 0
    assert service.store.get("approval", token) == record
    assert review_id in service.pending
    assert not service.leases


async def test_control_transition_never_erases_duplicate_dispatch_protection(service):
    work = await opened(service)
    args, token, review_id = await proposed(service, work)
    await service.approve(review_id, True)
    assert (await service.call("call_page_tool", **args, confirmation_token=token))[
        "status"
    ] == "ok"
    hid = await start_control(service, work, "handoff")
    await service.complete_handoff(hid)
    used = await service.call("call_page_tool", **args, confirmation_token=token)
    assert used["error"]["code"] == "CONFIRMATION_USED"
    automatic_repeat = await service.call("call_page_tool", **args)
    assert automatic_repeat["error"]["code"] == "ACTION_ALREADY_DISPATCHED"
    assert service.worker.executions == 1
