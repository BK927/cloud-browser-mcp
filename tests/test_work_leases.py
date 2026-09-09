import asyncio
import json
import time

import pytest

from cloud_browser.models import BrowserError


async def test_partial_open_returns_owner_only_recovery_lease(service, monkeypatch):
    service.cfg.max_sessions = 1  # Explicit operator single-work mode remains supported.
    original = service.worker.call

    async def partial(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "open":
            raise BrowserError("BROWSER_ERROR", "Startup observation failed")
        return result

    monkeypatch.setattr(service.worker, "call", partial)
    result = await service.call("open", _principal="owner")
    assert result["status"] == "error" and result["session_id"] and result["lease_id"]
    assert (await service.call("open", _principal="other"))["error"]["code"] == "BROWSER_BUSY"
    closed = await service.call(
        "close",
        _principal="owner",
        session_id=result["session_id"],
        lease_id=result["lease_id"],
        scope="session",
    )
    assert closed["status"] == "ok" and not service.sessions


async def test_expired_close_failure_does_not_transfer_work(service, monkeypatch):
    opened = await service.call("open", _principal="owner")
    sid = opened["session_id"]
    service.sessions[sid]["expires"] = time.time() - 1
    original = service.worker.call

    async def cannot_close(method, **kwargs):
        if method == "close":
            raise BrowserError("BROWSER_ERROR", "Close could not be verified")
        return await original(method, **kwargs)

    monkeypatch.setattr(service.worker, "call", cannot_close)
    other = await service.call("open", _principal="other")
    assert other["error"]["code"] == "CLEANUP_REQUIRED"
    assert sid in service.sessions and service.owners[sid]["lease_id"] == opened["lease_id"]
    assert sid not in json.dumps(other)
    status = await service.call(
        "status", _principal="owner", session_id=sid, lease_id=opened["lease_id"]
    )
    assert status["status"] == "ok" and status["sessions"][0]["work_lease_expired"]


async def test_status_survives_work_expiry_during_human_control(service):
    opened = await service.call("open", _principal="owner")
    args = {k: opened[k] for k in ("session_id", "tab_id", "lease_id")}
    await service.call(
        "auth_request", _principal="owner", **args, site_origin="https://example.com"
    )
    service.sessions[args["session_id"]]["expires"] = time.time() - 1
    status = await service.call(
        "status", _principal="owner", session_id=args["session_id"], lease_id=args["lease_id"]
    )
    assert status["status"] == "ok" and status["sessions"][0]["tabs"] is None
    assert status["sessions"][0]["control"]["automation_paused"]
    assert (await service.call("open", _principal="other"))["error"][
        "code"
    ] == "USER_CONTROL_ACTIVE"


async def test_isolated_leases_even_for_same_oauth_connection(service):
    first = await service.call("open", _principal="connection-a")
    assert first["status"] == "ok"
    sid, lease = first["session_id"], first["lease_id"]
    second = await service.call("open", _principal="connection-a")
    assert second["status"] == "ok" and second["lease_id"] != lease
    assert sid not in json.dumps(second)
    for principal, code, token in (
        ("connection-a", "LEASE_REQUIRED", None),
        ("connection-a", "LEASE_INVALID", "wrong"),
        ("connection-b", "LEASE_INVALID", lease),
    ):
        result = await service.call(
            "list_tabs", _principal=principal, session_id=sid, lease_id=token
        )
        assert result["error"]["code"] == code
        assert sid not in json.dumps(result)
    public = await service.call("status", _principal="connection-a")
    assert public["busy"] and not public["sessions"] and sid not in json.dumps(public)
    owned = await service.call(
        "list_tabs", _principal="connection-a", session_id=sid, lease_id=lease
    )
    assert owned["tabs"]


async def test_simultaneous_open_is_not_a_session_sharing_race(service):
    results = await asyncio.gather(*(service.call("open", _principal="same") for _ in range(2)))
    assert sorted(r["status"] for r in results) == ["ok", "ok"]
    assert len(service.sessions) == 2
    assert len({r["session_id"] for r in results}) == 2
    third = await service.call("open", _principal="same")
    assert third["error"]["code"] == "BROWSER_BUSY"
    assert third["busy_reason"] == "session_capacity"


async def test_cancelled_http_waiter_keeps_dispatch_and_status_fast(service, monkeypatch):
    opened = await service.call("open", _principal="connection")
    sid, tid, lease = (opened[k] for k in ("session_id", "tab_id", "lease_id"))
    original = service.worker.call
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(method, **args):
        if method == "act":
            entered.set()
            await release.wait()
        return await original(method, **args)

    monkeypatch.setattr(service.worker, "call", delayed)
    args = dict(
        _principal="connection",
        lease_id=lease,
        session_id=sid,
        tab_id=tid,
        expected_revision=1,
        action={"type": "scroll", "delta_y": 30},
        operation_id="test-operation-123",
    )
    task = asyncio.create_task(service.call("act", **args))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = await asyncio.wait_for(
        service.call(
            "status",
            _principal="connection",
            lease_id=lease,
            session_id=sid,
            operation_id="test-operation-123",
        ),
        0.5,
    )
    assert status["operation"]["state"] == "running"
    assert status["sessions"][0]["tabs_cached"]
    release.set()
    await asyncio.gather(*list(service.tasks))
    replay = await service.call("act", **args)
    assert replay["replayed"] and replay["status"] == "ok"
    assert service.worker.executions == 1
    assert sid in service.sessions


async def test_result_identifier_cannot_change_arguments(service):
    opened = await service.call("open", _principal="connection")
    args = {k: opened[k] for k in ("session_id", "tab_id", "lease_id")}
    first = await service.call(
        "observe", _principal="connection", **args, operation_id="observe-123"
    )
    assert first["status"] == "ok"
    changed = await service.call(
        "observe", _principal="connection", **args, operation_id="observe-123", mode="visual"
    )
    assert changed["error"]["code"] == "OPERATION_CONFLICT"


async def test_closed_lease_tombstone_is_hashed_and_private(service):
    first = await service.call("open", _principal="connection")
    sid, lease = first["session_id"], first["lease_id"]
    await service.call(
        "close", _principal="connection", lease_id=lease, session_id=sid, scope="session"
    )
    saved = service.store.get("session", sid)
    assert lease not in json.dumps(saved)
    assert saved["reason"] == "explicit_close"
    result = await service.call(
        "list_tabs", _principal="connection", lease_id=lease, session_id=sid
    )
    assert result["error"]["code"] == "SESSION_NOT_FOUND"
    stranger = await service.call(
        "list_tabs", _principal="stranger", lease_id=lease, session_id=sid
    )
    assert stranger["error"]["code"] == "LEASE_INVALID"


async def test_private_reclaim_records_reason_and_releases_exclusive_work(service):
    first = await service.call("open", _principal="connection")
    sid = first["session_id"]
    service.sessions[sid]["expires"] = 0
    result = await service.reclaim_session(sid)
    assert result["session_closed"]
    assert service.store.get("session", sid)["reason"] == "administrator_reclaimed"
    assert (await service.call("open", _principal="another-connection"))["status"] == "ok"
