"""A human approval gets its own window and remains discoverable to the client."""

from datetime import datetime

import httpx
import pytest
from test_service import opened, proposed

from cloud_browser.console import control_app
from cloud_browser.oauth import Auth


async def test_late_approval_renews_window_and_resend_reports_approved(service, monkeypatch):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    review_id = next(iter(service.pending))
    item = service.pending[review_id]
    original_expires = item["expires"]
    repeated = await service.call("act", **args)
    assert repeated["confirmation"]["approval_state"] == "pending"
    now = original_expires - 1
    monkeypatch.setattr("cloud_browser.service.time.time", lambda: now)
    await service.approve(review_id, True)
    new_expires = now + service.cfg.approval_ttl
    assert item["expires"] == new_expires
    assert service.store.expires_at("approval", token) == new_expires
    assert datetime.fromisoformat(item["confirmation"]["expires_at"]).timestamp() == pytest.approx(
        new_expires, abs=0.000001
    )
    now = original_expires + 10
    repeated = await service.call("act", **args)
    assert repeated["status"] == "confirmation_required"
    assert repeated["confirmation"]["confirmation_token"] == token
    assert repeated["confirmation"]["approval_state"] == "approved"
    assert repeated["confirmation"]["expires_at"] == item["confirmation"]["expires_at"]
    assert repeated["notices"] == [
        "The user approved this exact action. Call browser_act again with the same arguments plus confirmation_token before expires_at."
    ]
    assert service.worker.executions == 0
    assert len(service.pending) == 1
    executed = await service.call("act", **args, confirmation_token=token)
    assert executed["status"] == "ok"
    assert (await service.call("act", **args, confirmation_token=token))["error"][
        "code"
    ] == "CONFIRMATION_USED"
    assert service.worker.executions == 1


async def test_late_denial_keeps_original_expiry(service, monkeypatch):
    sid, tid = await opened(service)
    args, token = await proposed(service, sid, tid)
    review_id = next(iter(service.pending))
    item = service.pending[review_id]
    original_expires = item["expires"]
    now = original_expires - 1
    monkeypatch.setattr("cloud_browser.service.time.time", lambda: now)
    await service.approve(review_id, False)
    assert item["expires"] == original_expires
    assert service.store.expires_at("approval", token) == original_expires
    assert (await service.call("act", **args, confirmation_token=token))["error"][
        "code"
    ] == "CONFIRMATION_DENIED"
    now = original_expires + 1
    assert service.store.get("approval", token) is None


async def test_only_console_index_auto_refreshes_without_changing_csp(service):
    sid, tid = await opened(service)
    await proposed(service, sid, tid)
    auth = Auth(service.cfg, service.store)
    service.store.put("control", "test-cookie", {"csrf": "test-csrf"}, 120)
    app = control_app(service.cfg, auth, service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=service.cfg.control_origin,
        cookies={"cb_control": "test-cookie"},
        headers={"Origin": service.cfg.control_origin},
    ) as client:
        index = await client.get("/")
        login = await client.get("/login")
        posted = await client.post(
            "/approval/" + next(iter(service.pending)),
            data={"csrf": "test-csrf", "decision": "approve"},
        )
    assert index.status_code == 200
    assert '<meta http-equiv="refresh" content="10">' in index.text
    assert "refreshes automatically every 10 seconds" in index.text
    assert "<script" not in index.text
    assert 'http-equiv="refresh"' not in login.text
    assert 'http-equiv="refresh"' not in posted.text
    assert posted.status_code == 303
    # Existing same-origin script files support noVNC; inline scripts/eval stay forbidden.
    csp = index.headers["Content-Security-Policy"]
    assert csp == (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    )
    assert csp == login.headers["Content-Security-Policy"]
    assert csp == posted.headers["Content-Security-Policy"]
