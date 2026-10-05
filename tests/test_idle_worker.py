import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from test_reader import reader as reader

from cloud_browser.models import BrowserError


async def close_work(service):
    opened = await service.call("open")
    closed = await service.call("close", session_id=opened["session_id"], scope="session")
    assert closed["status"] == "ok"
    return opened


async def test_idle_worker_stops_once_after_grace_and_retains_history(service):
    shutdown = AsyncMock()
    service.worker.shutdown = shutdown
    opened = await close_work(service)
    assert service.worker_idle_since is not None
    await service._reap_idle_worker()
    shutdown.assert_not_awaited()
    service.worker_idle_since -= service.cfg.worker_idle_timeout
    await service._reap_idle_worker()
    await service._reap_idle_worker()
    shutdown.assert_awaited_once()
    assert service.store.get("session", opened["session_id"])["state"] == "closed"
    assert (await service.call("open"))["status"] == "ok"


async def test_open_during_grace_disarms_idle_timer(service):
    service.worker.shutdown = AsyncMock()
    await close_work(service)
    service.worker_idle_since -= 100
    assert (await service.call("open"))["status"] == "ok"
    assert service.worker_idle_since is None
    await service._reap_idle_worker()
    service.worker.shutdown.assert_not_awaited()


@pytest.mark.parametrize(
    "guard",
    [
        "session",
        "queued",
        "running",
        "control",
        "cleanup",
        "worker_cleanup",
        "navigation",
        "disabled",
    ],
)
async def test_idle_reaper_never_interrupts_work_or_control(service, guard):
    service.worker.shutdown = AsyncMock()
    service.worker_idle_since = time.monotonic() - 100
    if guard == "session":
        service.sessions["uncertain"] = {"uncertain": True}
    elif guard == "queued":
        service.queued[None] = 1
    elif guard == "running":
        service.running = (None, "open")
    elif guard == "control":
        service.leases["human"] = {"state": "active"}
    elif guard == "cleanup":
        service.cleanup_required = True
    elif guard == "worker_cleanup":
        service.worker.cleanup_failed = True
    elif guard == "navigation":
        service.navigations["pending"] = {"tab_id": "tab"}
    else:
        service.cfg.worker_idle_timeout = 0
    await service._reap_idle_worker()
    service.worker.shutdown.assert_not_awaited()


async def test_closing_one_of_two_works_does_not_arm_timer(service):
    a = await service.call("open")
    b = await service.call("open")
    await service.call("close", session_id=a["session_id"], scope="session")
    assert service.worker_idle_since is None
    assert b["session_id"] in service.sessions


async def test_failed_close_cannot_arm_idle_reaper(service):
    opened = await service.call("open")
    service.worker.call = AsyncMock(side_effect=BrowserError("RESULT_UNCERTAIN", "test"))
    result = await service.call("close", session_id=opened["session_id"], scope="session")
    assert result["status"] == "error"
    assert service.worker_idle_since is None
    assert opened["session_id"] in service.sessions


async def test_reader_browser_keeps_worker_live_and_disarms_timer(reader):
    service, _ = reader
    service.worker.shutdown = AsyncMock()
    await close_work(service)
    assert service.worker_idle_since is not None
    result = await service.call("read", _principal="alice", url="https://example.com/")
    assert result["status"] == "ok"
    assert service.reader_sid in service.sessions
    assert service.reader_sid in service.worker.sessions
    assert service.worker_idle_since is None
    await close_work(service)
    assert service.worker_idle_since is None
    # Even a stale armed timer cannot override a live reader browser.
    service.worker_idle_since = time.monotonic() - 100
    async with service.lock:
        await service._reap_idle_worker()
    service.worker.shutdown.assert_not_awaited()


async def test_read_in_progress_keeps_worker_live_without_a_session(service):
    service.worker.shutdown = AsyncMock()
    await close_work(service)
    service.worker_idle_since -= service.cfg.worker_idle_timeout
    async with service.reader_lock:
        assert not service.sessions and not service.queued and not service.navigations
        async with service.lock:
            await service._reap_idle_worker()
        service.worker.shutdown.assert_not_awaited()
        assert service.worker_idle_since is not None
    async with service.lock:
        await service._reap_idle_worker()
    service.worker.shutdown.assert_awaited_once()


async def test_cached_read_survives_reader_close_and_idle_worker_shutdown(reader):
    service, state = reader
    result = await service.call("read", _principal="alice", url="https://example.com/")
    assert result["status"] == "ok"
    rid = result["read"]["read_id"]
    cached = service.read_cache[rid]
    service.worker.shutdown = AsyncMock(wraps=service.worker.shutdown)
    service.sessions[service.reader_sid]["expires"] = time.time() - 1
    async with service.lock:
        await service._reap_expired()
        assert service.reader_sid is None and not service.sessions
        assert service.worker_idle_since is not None
        service.worker_idle_since -= service.cfg.worker_idle_timeout
        await service._reap_idle_worker()
    service.worker.shutdown.assert_awaited_once()
    assert not service.worker.sessions
    assert service.read_cache[rid] is cached
    count = len(service.worker.calls)
    sliced = await service.call("read", _principal="alice", read_id=rid, offset=1000)
    assert sliced["status"] == "ok" and sliced["read"]["text"] == state["text"][1000:]
    assert len(service.worker.calls) == count
    reopened = await service.call("read", _principal="alice", url="https://example.com/")
    assert reopened["status"] == "ok" and reopened["read"]["text"] == state["text"]
    assert service.worker_idle_since is None


async def test_background_sweep_reclaims_and_latches_cleanup_failure(service):
    await close_work(service)
    service.cfg.session_sweep_interval = 0.01
    service.cfg.worker_idle_timeout = 0.01
    service.worker_idle_since -= 1

    async def fail():
        service.worker.cleanup_failed = True
        raise BrowserError("CLEANUP_FAILED", "test")

    shutdown = AsyncMock(side_effect=fail)
    service.worker.shutdown = shutdown
    service.start()
    try:
        for _ in range(100):
            if service.cleanup_required:
                break
            await asyncio.sleep(0.01)
        assert service.cleanup_required
        await asyncio.sleep(0.05)
        assert service.cleanup_required
        shutdown.assert_awaited_once()
    finally:
        service.sweeper.cancel()
        await asyncio.gather(service.sweeper, return_exceptions=True)
        service.sweeper = None


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_cancelled_idle_reaper_holds_lock_until_cleanup_settles(service, cleanup_fails):
    await close_work(service)
    service.worker_idle_since -= service.cfg.worker_idle_timeout
    started, release = asyncio.Event(), asyncio.Event()

    async def cleanup():
        started.set()
        await release.wait()
        if cleanup_fails:
            service.worker.cleanup_failed = True
            raise BrowserError("CLEANUP_FAILED", "test")

    async def reap():
        async with service.lock:
            await service._reap_idle_worker()

    service.worker.shutdown = AsyncMock(side_effect=cleanup)
    reaper = asyncio.create_task(reap())
    waiter = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        reaper.cancel()
        waiter = asyncio.create_task(service.lock.acquire())
        await asyncio.sleep(0)
        assert service.lock.locked() and not reaper.done() and not waiter.done()
        release.set()
        result = await asyncio.wait_for(asyncio.gather(reaper, return_exceptions=True), 1)
        assert isinstance(result[0], asyncio.CancelledError)
        assert reaper.cancelled()
        assert service.cleanup_required is cleanup_fails
        assert await asyncio.wait_for(waiter, 1)
        service.worker.shutdown.assert_awaited_once()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(reaper, return_exceptions=True), 1)
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        if service.lock.locked():
            service.lock.release()


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_shutdown_waits_for_idle_cleanup_before_queued_open(service, cleanup_fails):
    await close_work(service)
    service.cfg.session_sweep_interval = 0.01
    service.worker_idle_since -= service.cfg.worker_idle_timeout
    started = asyncio.Event()
    release = asyncio.Event()
    order = []
    calls = 0
    worker_call = service.worker.call

    async def delayed_cleanup():
        nonlocal calls
        calls += 1
        if calls == 1:
            order.append("cleanup_started")
            started.set()
            await release.wait()
            order.append("cleanup_finished")
            if cleanup_fails:
                service.worker.cleanup_failed = True
                raise BrowserError("CLEANUP_FAILED", "test")
        else:
            order.append("final_shutdown")
        service.worker.sessions.clear()

    async def checked_call(method, **args):
        if method == "open":
            order.append("open_dispatched")
            assert "cleanup_finished" in order
            if getattr(service.worker, "cleanup_failed", False):
                raise BrowserError("CLEANUP_FAILED", "test")
        return await worker_call(method, **args)

    service.worker.shutdown = delayed_cleanup
    service.worker.call = checked_call
    service.start()
    opened = stopping = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        opened = asyncio.create_task(service.call("open"))
        for _ in range(100):
            if service.queued:
                break
            await asyncio.sleep(0)
        assert service.queued
        sweeper = service.sweeper
        stopping = asyncio.create_task(service.shutdown())
        await asyncio.sleep(0)
        # Main first acquires the command lock to cancel pending navigations.
        # Server shutdown must wait for idle cleanup before reaching that step.
        assert not sweeper.cancelling()
        assert service.lock.locked()
        assert not opened.done() and not stopping.done()
        assert order == ["cleanup_started"]
        release.set()
        result, _ = await asyncio.wait_for(asyncio.gather(opened, stopping), 1)
        assert order == ["cleanup_started", "cleanup_finished", "open_dispatched", "final_shutdown"]
        assert sweeper.cancelled()
        assert service.sweeper is None
        assert result["status"] == ("error" if cleanup_fails else "ok")
        if cleanup_fails:
            assert result["error"]["code"] == "CLEANUP_FAILED"
            assert service.cleanup_required and service.worker.cleanup_failed
    finally:
        release.set()
        pending = [task for task in (opened, stopping) if task is not None]
        if service.sweeper:
            service.sweeper.cancel()
            pending.append(service.sweeper)
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 1)
