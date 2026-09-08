import asyncio
import json

import pytest


async def test_exclusive_lease_even_for_same_oauth_connection(service):
    first = await service.call("open", _principal="connection-a")
    assert first["status"] == "ok"
    sid, lease = first["session_id"], first["lease_id"]
    second = await service.call("open", _principal="connection-a")
    assert second["error"]["code"] == "BROWSER_BUSY"
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
    assert sorted(r["status"] for r in results) == ["error", "ok"]
    assert len(service.sessions) == 1


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
    await service.call("close", _principal="connection", lease_id=lease, session_id=sid, scope="session")
    saved = service.store.get("session", sid)
    assert lease not in json.dumps(saved)
    assert saved["reason"] == "explicit_close"
    result = await service.call("list_tabs", _principal="connection", lease_id=lease, session_id=sid)
    assert result["error"]["code"] == "SESSION_NOT_FOUND"
    stranger = await service.call("list_tabs", _principal="stranger", lease_id=lease, session_id=sid)
    assert stranger["error"]["code"] == "LEASE_INVALID"


async def test_private_reclaim_records_reason_and_releases_exclusive_work(service):
    first = await service.call("open", _principal="connection")
    sid = first["session_id"]
    service.sessions[sid]["expires"] = 0
    result = await service.reclaim_session(sid)
    assert result["session_closed"]
    assert service.store.get("session", sid)["reason"] == "administrator_reclaimed"
    assert (await service.call("open", _principal="another-connection"))["status"] == "ok"
