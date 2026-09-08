"""Regression coverage for gaps in the original browser contract."""

import httpx
import pytest
from test_service import opened, proposed

from cloud_browser.console import control_app
from cloud_browser.models import BrowserError
from cloud_browser.oauth import Auth


async def test_duplicate_proposal_reuses_one_human_review(service):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    repeated = await service.call("act", **args)
    assert repeated["confirmation"]["confirmation_token"] == token
    assert len(service.pending) == 1
    assert service.worker.executions == 0


async def test_completed_binding_cannot_be_reproposed_when_page_does_not_change(
    service, monkeypatch
):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    original = service.worker.call

    async def unchanged_action(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "act":
            service.worker.sessions[sid][tid]["revision"] = args["expected_revision"]
            result["revision"] = args["expected_revision"]
            result["status"] = "no_change"
        return result

    monkeypatch.setattr(service.worker, "call", unchanged_action)
    await service.approve(next(iter(service.pending)), True)
    assert (await service.call("act", **args, confirmation_token=token))["status"] == "no_change"
    repeated = await service.call("act", **args)
    assert repeated["error"]["code"] == "ACTION_ALREADY_DISPATCHED"
    assert service.worker.executions == 1


async def test_failed_resume_keeps_control_locked_and_uncertainty(service, monkeypatch):
    sid, tid = await opened(service)
    service.sessions[sid]["uncertain"] = True
    await service.call("handoff", session_id=sid, tab_id=tid, reason="Check previous action")
    original = service.worker.call

    async def broken_resume(method, **kwargs):
        if method == "resume":
            raise BrowserError("OBSERVATION_FAILED", "Not ready")
        return await original(method, **kwargs)

    monkeypatch.setattr(service.worker, "call", broken_resume)
    done = await service.complete_handoff(service.leases[sid]["handoff_id"])
    assert done["result"]["error"]["code"] == "OBSERVATION_FAILED"
    assert done["automation_paused"]
    assert service.sessions[sid]["uncertain"]
    assert (await service.call("observe", session_id=sid, tab_id=tid))["error"][
        "code"
    ] == "USER_CONTROL_ACTIVE"


async def test_failed_desktop_disconnect_does_not_resume_browser(service):
    sid, tid = await opened(service)
    await service.call("handoff", session_id=sid, tab_id=tid, reason="Manual task")

    async def failed_disconnect():
        raise RuntimeError("Test connection could not close")

    service.control_disconnectors.add(failed_disconnect)
    done = await service.complete_handoff(service.leases[sid]["handoff_id"])
    assert done["automation_paused"]
    assert done["result"]["error"]["code"] == "CONTROL_DISCONNECT_FAILED"
    assert not any(method == "resume" for method, _ in service.worker.calls)


@pytest.mark.parametrize(
    "method,args", [("navigate", {"operation": "reload"}), ("open", {"new_tab": False})]
)
async def test_uncertainty_blocks_other_navigation_routes(service, method, args):
    sid, tid = await opened(service)
    service.sessions[sid]["uncertain"] = True
    params = {"session_id": sid, **args}
    if method == "navigate":
        params["tab_id"] = tid
    else:
        params["url"] = "https://example.com/"
    count = len(service.worker.calls)
    result = await service.call(method, **params)
    assert result["error"]["code"] == "RESULT_UNCERTAIN"
    assert len(service.worker.calls) == count


async def test_reuse_empty_session_still_checks_new_tab_memory(service, monkeypatch):
    sid, tid = await opened(service)
    await service.call("close", session_id=sid, scope="tab", tab_id=tid)
    monkeypatch.setattr(service, "resources", lambda admission=0: {"can_admit": False})
    result = await service.call("open", session_id=sid, new_tab=False)
    assert result["error"]["code"] == "RESOURCE_PRESSURE"
    assert not service.worker.sessions[sid]


async def test_console_does_not_claim_failed_control_return_succeeded(service, monkeypatch):
    sid, tid = await opened(service)
    await service.call("handoff", session_id=sid, tab_id=tid, reason="Manual task")
    original = service.worker.call

    async def broken_resume(method, **kwargs):
        if method == "resume":
            raise BrowserError(
                "AUTH_REQUIRED", "Sensitive screen still open", "user_action_required"
            )
        return await original(method, **kwargs)

    monkeypatch.setattr(service.worker, "call", broken_resume)
    auth = Auth(service.cfg, service.store)
    service.store.put("control", "test-cookie", {"csrf": "test-csrf"}, 120)
    app = control_app(service.cfg, auth, service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=service.cfg.control_origin,
        cookies={"cb_control": "test-cookie"},
        headers={"Origin": service.cfg.control_origin},
    ) as client:
        result = await client.post(
            "/handoff/" + service.leases[sid]["handoff_id"] + "/complete",
            data={"csrf": "test-csrf"},
        )
    assert result.status_code == 409
    assert "automation remains paused" in result.text
    assert "AUTH_REQUIRED" in result.text
