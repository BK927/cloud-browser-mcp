import asyncio
import io
import json
import time
from unittest.mock import Mock

import pytest
from starlette.datastructures import UploadFile

from cloud_browser.models import BrowserError
from cloud_browser.resources import admission_state, pressure_at


def own(opened):
    return {k: opened[k] for k in ("session_id", "tab_id", "lease_id")} | {
        "_principal": "connection"
    }


async def test_idle_work_does_not_block_other_conversation_or_leak_tabs(service):
    first = await service.call("open", _principal="connection")
    global_status = await service.call("status", _principal="connection")
    assert not global_status["busy"]
    assert global_status["scheduler"]["state"] == "available"
    second = await service.call("open", _principal="connection")
    assert first["session_id"] != second["session_id"]
    cross = await service.call("observe", **(own(first) | {"tab_id": second["tab_id"]}))
    assert cross["error"]["code"] == "TAB_NOT_FOUND"
    stolen = await service.call(
        "list_tabs",
        _principal="connection",
        session_id=second["session_id"],
        lease_id=first["lease_id"],
    )
    assert stolen["error"]["code"] == "LEASE_INVALID"
    for opened in (first, second):
        seen = await service.call("observe", **own(opened))
        assert seen["status"] == "ok"
    status = await service.call("status", _principal="connection")
    assert status["scheduler"]["state"] == "session_capacity"
    assert not status["scheduler"]["can_open_session"]
    assert status["scheduler"]["owned_commands_can_queue"]
    assert all(o["session_id"] not in json.dumps(status) for o in (first, second))


async def test_idle_expiry_is_not_renewed_by_status_but_owned_work_renews(service):
    first = await service.call("open", _principal="connection")
    sid = first["session_id"]
    service.sessions[sid]["expires"] = time.time() + 5
    before = service.sessions[sid]["expires"]
    await service.call("status", **{k: v for k, v in own(first).items() if k != "tab_id"})
    assert service.sessions[sid]["expires"] == before
    await service.call("observe", **own(first))
    assert service.sessions[sid]["expires"] > before + 100
    service.sessions[sid]["expires"] = time.time() - 1
    status = await service.call("status", _principal="connection")
    assert status["scheduler"]["state"] == "cleanup_pending"
    await service._reap_expired()
    assert not service.sessions and not service.tab_cache and not service.owners
    assert service.store.get("session", sid)["reason"] == "idle_lease_expired"


async def test_background_reaper_and_owned_expired_close(service):
    first = await service.call("open", _principal="connection")
    service.sessions[first["session_id"]]["expires"] = 0
    service.cfg.session_sweep_interval = 0.01
    service.start()
    try:
        for _ in range(100):
            if not service.sessions:
                break
            await asyncio.sleep(0.01)
        assert not service.sessions
        second = await service.call("open", _principal="connection")
        service.sessions[second["session_id"]]["expires"] = 0
        result = await service.call("close", **own(second), scope="session")
        assert result["status"] == "ok"
    finally:
        await service.shutdown()


async def test_manual_control_pause_does_not_touch_other_worker_or_downloads(service):
    service.cfg.managed_display = True
    first = await service.call("open", _principal="connection")
    second = await service.call("open", _principal="connection")
    start = len(service.worker.calls)
    auth = await service.call("auth_request", **own(first), site_origin="https://example.com")
    assert auth["status"] == "user_action_required"
    assert all(
        args["session_id"] == first["session_id"] for _, args in service.worker.calls[start:]
    )
    count = len(service.worker.calls)
    assert (await service.call("observe", **own(second)))["error"]["code"] == "USER_CONTROL_ACTIVE"
    await service._reap_expired()
    assert len(service.worker.calls) == count
    status = await service.call("status", **{k: v for k, v in own(second).items() if k != "tab_id"})
    assert status["sessions"][0]["tabs"] is None
    service.sessions[first["session_id"]]["expires"] = 0
    done = await service.complete_handoff(auth["auth"]["handoff_id"])
    assert done["state"] == "completed" and done["authenticated"] is None
    assert (await service.call("observe", **own(second)))["status"] == "ok"


async def test_queue_serializes_work_and_times_out_without_dispatch(service, monkeypatch):
    first, second = [await service.call("open", _principal="connection") for _ in range(2)]
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.worker.call

    async def hold(method, **args):
        if method == "observe" and args["session_id"] == first["session_id"]:
            entered.set()
            await release.wait()
        return await original(method, **args)

    monkeypatch.setattr(service.worker, "call", hold)
    service.cfg.command_queue_timeout = 0.02
    running = asyncio.create_task(service.call("observe", **own(first)))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        status = await asyncio.wait_for(service.call("status", _principal="connection"), 0.5)
        assert status["scheduler"]["running_commands"] == 1
        result = await service.call("observe", **own(second))
        assert result["busy_reason"] == "queue_timeout"
        assert not any(
            m == "observe" and a["session_id"] == second["session_id"]
            for m, a in service.worker.calls
        )
    finally:
        release.set()
        await running
    assert (await service.call("observe", **own(second)))["status"] == "ok"


async def test_private_files_require_explicit_work_and_do_not_cross_leases(service):
    first, second = [await service.call("open", _principal="connection") for _ in range(2)]
    with pytest.raises(BrowserError, match="Choose the work"):
        await service.stage_upload(UploadFile(io.BytesIO(b"fixture"), filename="fixture.txt"))
    upload = await service.stage_upload(
        UploadFile(io.BytesIO(b"fixture"), filename="fixture.txt"), first["session_id"]
    )
    status = await service.call("status", **{k: v for k, v in own(second).items() if k != "tab_id"})
    assert upload["upload_id"] not in json.dumps(status)
    wrong = await service.call(
        "act",
        **own(second),
        expected_revision=1,
        action={"type": "upload", "node_id": "node_1", "upload_ids": [upload["upload_id"]]},
    )
    assert wrong["error"]["code"] == "UPLOAD_NOT_FOUND"


def test_adaptive_policy_borrows_only_soft_reserve_and_honors_host_and_psi(cfg):
    state = {"available_mb": 180, "host_available_mb": 800, "can_admit": False}
    small = admission_state(state, cfg, cost_mb=44, operation="capture")
    assert small["can_admit"] and small["soft_reserve_borrowed"]
    assert not admission_state(state, cfg, cost_mb=192)["can_admit"]
    assert not admission_state(state | {"host_available_mb": 220}, cfg, cost_mb=44)["can_admit"]
    assert not admission_state(state | {"memory_pressure": {"full": 11, "some": 20}}, cfg)[
        "can_admit"
    ]
    cfg.memory_policy = "strict"
    assert not admission_state(state, cfg, cost_mb=44)["can_admit"]


def test_psi_unknown_is_not_zero_and_nonfinite_is_rejected(tmp_path):
    path = tmp_path / "pressure"
    assert pressure_at(path) is None
    path.write_text("some avg10=2.00 avg60=0 total=10\nfull avg10=0.30 total=2\n")
    assert pressure_at(path) == {"some": 2, "full": 0.3}
    path.write_text("some avg10=NaN\nfull avg10=0\n")
    assert pressure_at(path) is None


def test_existing_operator_reserve_below_new_default_floor_is_preserved(cfg):
    cfg.memory_reserve_mb = 64
    state = admission_state({"available_mb": 100, "host_available_mb": 800}, cfg)
    assert state["required_headroom_mb"] == 64
    assert state["can_admit"]


def test_capacity_logs_are_bounded_correlated_and_payload_free(monkeypatch):
    from cloud_browser import operation_diagnostics as diag

    logger = Mock()
    monkeypatch.setattr(diag, "_logger", logger)
    diag._events.clear()
    for _ in range(130):
        diag.log_capacity(
            {
                "request_id": "req_fixture",
                "error": {"code": "BROWSER_BUSY", "message": "secret"},
                "session_id": "secret",
                "busy_reason": "secret",
            }
        )
    assert logger.info.call_count == 120
    assert "secret" not in str(logger.info.call_args_list)
    assert "req_fixture" in str(logger.info.call_args_list)
    diag._events.clear()
