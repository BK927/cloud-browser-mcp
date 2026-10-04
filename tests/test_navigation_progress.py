import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cloud_browser.drission import DrissionAdapter
from cloud_browser.models import BrowserError
from cloud_browser.navigation_job import NavigationJob


class ProtocolTab:
    def __init__(self):
        self.calls = []
        self.loader = "new"
        self.ready = "loading"

    def run_cdp(self, command, **args):
        self.calls.append((command, args))
        if command == "Page.navigate":
            return {"loaderId": "new"}
        if command == "Page.getFrameTree":
            return {
                "frameTree": {
                    "frame": {"id": "main", "loaderId": self.loader, "url": "https://example.com/"}
                }
            }
        if command == "Page.createIsolatedWorld":
            return {"executionContextId": 5}
        if command == "Runtime.evaluate":
            return {
                "result": {
                    "value": self.ready if args["expression"] == "document.readyState" else "Title"
                }
            }
        return {}


def job(clock, timeout=300000):
    tab = ProtocolTab()
    task = NavigationJob(
        tab, "Page.navigate", {"url": "https://example.com/"}, timeout, clock=lambda: clock[0]
    )
    assert task.ack.wait(1)
    before = {
        "document": "main:old",
        "sequence": 0,
        "state": SimpleNamespace(same_document_sequence=0),
    }
    return task, tab, before


def test_five_minute_boundary_never_turns_visible_loading_into_success():
    clock = [10]
    task, tab, before = job(clock)
    clock[0] = 309.999
    assert not task.poll(before=before)
    assert task.phase == "loading"
    clock[0] = 310
    with pytest.raises(BrowserError) as error:
        task.poll(before=before)
    assert error.value.code == "NAVIGATION_TIMEOUT"
    assert error.value.details["navigation"]["phase"] == "loading"
    assert error.value.details["current_page"]["url"] == "https://example.com/"
    assert len([c for c, _ in tab.calls if c == "Page.navigate"]) == 1


def test_old_complete_document_and_changed_final_document_do_not_complete():
    task, tab, before = job([0], 1000)
    tab.loader, tab.ready = "old", "complete"
    assert not task.poll(before=before)
    assert task.phase == "document_transition"
    tab.loader = "new"
    assert task.poll(before=before)
    assert task.page["title"] == "Title"


def test_ack_failure_does_not_dispatch_twice_or_probe_dom():
    class Failed(ProtocolTab):
        def run_cdp(self, command, **args):
            self.calls.append((command, args))
            raise TimeoutError

    tab = Failed()
    task = NavigationJob(tab, "Page.navigate", {}, 1000)
    assert task.ack.wait(1)
    with pytest.raises(BrowserError) as error:
        task.poll(before={})
    assert error.value.code == "NAVIGATION_TIMEOUT"
    assert error.value.details["navigation"]["phase"] == "command_response"
    assert len(tab.calls) == 1


def install_pending_worker(service, monkeypatch):
    original = service.worker.call
    complete = asyncio.Event()
    dispatched = []

    async def invoke(method, **args):
        if method == "navigation_begin":
            dispatched.append(args)
        if method in ("navigation_begin", "navigation_poll"):
            return {
                "session_id": args["session_id"],
                "tab_id": args["tab_id"],
                "revision": 2,
                "page": {"url": "https://example.com/", "title": "Example"},
                "status": "ok"
                if method == "navigation_poll" and complete.is_set()
                else "no_change",
                "navigation": {
                    "operation": "goto",
                    "redirected": False,
                    "navigation_occurred": complete.is_set(),
                    "pending": not complete.is_set(),
                    "phase": "completed" if complete.is_set() else "loading",
                    "timeout_ms": 60000,
                },
            }
        if method == "navigation_cancel":
            return {"session_id": args["session_id"], "tab_id": args["tab_id"]}
        return await original(method, **args)

    monkeypatch.setattr(service.worker, "call", invoke)
    return complete, dispatched


def owned(result):
    return {"_principal": "p", **{key: result[key] for key in ("session_id", "lease_id", "tab_id")}}


async def test_pending_open_has_lease_fast_status_and_other_work_cleanup(service, monkeypatch):
    complete, dispatched = install_pending_worker(service, monkeypatch)
    first = await service.call(
        "open", _principal="p", url="https://example.com/", timeout_ms=300000
    )
    assert first["status"] == "no_change" and first["navigation"]["pending"]
    args = owned(first)
    start = time.monotonic()
    status = await service.call(
        "status",
        **{k: v for k, v in args.items() if k != "tab_id"},
        operation_id=first["operation_id"],
    )
    assert time.monotonic() - start < 1
    assert status["operation"]["navigation"]["phase"] == "loading"
    second = await service.call("open", _principal="p")
    start = time.monotonic()
    assert (await service.call("observe", **owned(second)))["status"] == "ok"
    assert time.monotonic() - start < 5
    global_status = await service.call("status", _principal="p")
    assert first["operation_id"] not in json.dumps(global_status)
    assert first["session_id"] not in json.dumps(global_status)
    reject = await service.call(
        "act", **args, expected_revision=1, action={"type": "scroll", "delta_y": 1}
    )
    assert reject["error"]["code"] == "NAVIGATION_IN_PROGRESS"
    assert dispatched[0]["timeout_ms"] == 300000
    start = time.monotonic()
    assert (await service.call("close", **args, scope="session"))["status"] == "ok"
    assert time.monotonic() - start < 5
    assert not service.navigations
    done = await service.call(
        "status",
        **{k: v for k, v in args.items() if k != "tab_id"},
        operation_id=first["operation_id"],
    )
    assert done["operation"]["result"]["error"]["code"] == "NAVIGATION_CANCELLED"
    assert (await service.call("observe", **owned(second)))["status"] == "ok"
    await service.shutdown()


async def test_navigation_replay_conflict_and_http_cancel_keep_one_dispatch(service, monkeypatch):
    complete, dispatched = install_pending_worker(service, monkeypatch)
    first = await service.call("open", _principal="p")
    args = owned(first) | {
        "operation": "goto",
        "url": "https://example.com/",
        "operation_id": "retry-navigation-1",
    }
    waiter = asyncio.create_task(service.call("navigate", **args))
    for _ in range(100):
        if dispatched:
            break
        await asyncio.sleep(0.01)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    replay = await service.call("navigate", **args)
    assert replay["replayed"] and replay["navigation"]["pending"]
    conflict = await service.call("navigate", **(args | {"url": "https://example.com/different"}))
    assert conflict["error"]["code"] == "OPERATION_CONFLICT"
    complete.set()
    for _ in range(100):
        if not service.navigations:
            break
        await asyncio.sleep(0.01)
    done = await service.call("navigate", **args)
    assert done["status"] == "ok" and done["replayed"]
    assert len(dispatched) == 1
    await service.shutdown()


async def test_timeout_precedence_and_ceiling_reject_before_creating_session(service):
    service.cfg.navigation_max_timeout = 120
    denied = await service.call("open", _principal="p", timeout_ms=120001)
    assert denied["error"]["code"] == "INVALID_INPUT"
    assert not service.sessions and not service.worker.calls
    assert service._navigation_timeout() == 60000
    service.configurations = {"s": {"t": {"navigation_timeout_ms": 90000}}}
    assert service._navigation_timeout("s", "t") == 90000
    assert service._navigation_timeout("s", "t", 120000) == 120000
    with pytest.raises(BrowserError):
        service._navigation_timeout("s", "t", True)


async def test_private_control_cancels_navigation_then_stops_all_polling(service, monkeypatch):
    _, dispatched = install_pending_worker(service, monkeypatch)
    first = await service.call("open", _principal="p", url="https://example.com/")
    handoff = await service.call("auth_request", **owned(first), site_origin="https://example.com")
    assert handoff["status"] == "user_action_required", handoff
    count = len(service.worker.calls)
    await asyncio.sleep(0.6)
    assert len(service.worker.calls) == count
    assert len(dispatched) == 1 and not service.navigations
    await service.shutdown()


async def test_cold_browser_initialization_returns_owned_progress_within_five_seconds(
    service, monkeypatch
):
    original = service.worker.call
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow_open(method, **args):
        if method == "open":
            calls.append(args)
            entered.set()
            await release.wait()
        return await original(method, **args)

    monkeypatch.setattr(service.worker, "call", slow_open)
    start = time.monotonic()
    task = asyncio.create_task(service.call("open", _principal="p"))
    await entered.wait()
    first = await task
    assert time.monotonic() - start < 5.5
    assert first["navigation"]["pending"] and first["tab_id"] is None
    identity = {"_principal": "p", "session_id": first["session_id"], "lease_id": first["lease_id"]}
    try:
        status = await asyncio.wait_for(
            service.call("status", **identity, operation_id=first["operation_id"]), 1
        )
        assert status["operation"]["state"] == "running"
        denied = await service.call(
            "status", **(identity | {"_principal": "another"}), operation_id=first["operation_id"]
        )
        assert denied["error"] and "operation" not in denied
    finally:
        release.set()
    for _ in range(100):
        status = await service.call("status", **identity, operation_id=first["operation_id"])
        if status["operation"]["state"] == "completed":
            break
        await asyncio.sleep(0.01)
    final = status["operation"]["result"]
    assert not status["operation"].get("navigation", {}).get("pending")
    assert final["status"] == "ok" and final["tab_id"]
    assert final["lease_id"] == first["lease_id"] and len(calls) == 1
    await service.shutdown()


async def test_one_browser_disconnect_does_not_expire_other_work(service, monkeypatch):
    first, second = [await service.call("open", _principal="p") for _ in range(2)]
    original = service.worker.call

    async def disconnected(method, **args):
        if args["session_id"] == first["session_id"]:
            raise BrowserError("SESSION_EXPIRED", "Browser disconnected", failure_scope="session")
        return await original(method, **args)

    monkeypatch.setattr(service.worker, "call", disconnected)
    failure = await service.call("observe", **owned(first))
    assert failure["error"]["code"] == "SESSION_EXPIRED"
    assert first["session_id"] not in service.sessions
    assert (await service.call("observe", **owned(second)))["status"] == "ok"
    await service.shutdown()


async def test_active_navigation_outlives_idle_ttl_and_can_still_be_cleaned(service, monkeypatch):
    install_pending_worker(service, monkeypatch)
    first = await service.call("open", _principal="p", url="https://example.com/")
    service.sessions[first["session_id"]]["expires"] = time.time() - 1
    assert service._session(first["session_id"])
    status = await service.call(
        "status", **{k: v for k, v in owned(first).items() if k != "tab_id"}
    )
    assert status["navigations"] and first["session_id"] in service.sessions
    closed = await service.call("close", **owned(first), scope="session")
    assert closed["status"] == "ok" and not service.navigations
    await service.shutdown()


async def test_delayed_progress_probe_does_not_abandon_dispatched_navigation(service, monkeypatch):
    complete, dispatched = install_pending_worker(service, monkeypatch)
    first = await service.call("open", _principal="p", url="https://example.com/")
    nav = service.navigations[first["session_id"]]
    original_lock = service._command_lock
    delayed = asyncio.Event()

    @asynccontextmanager
    async def unavailable_once():
        if not delayed.is_set():
            delayed.set()
            raise BrowserError("BROWSER_BUSY", "Progress probe queued", busy_reason="queue_timeout")
        async with original_lock():
            yield

    monkeypatch.setattr(service, "_command_lock", unavailable_once)
    await asyncio.wait_for(delayed.wait(), 2)
    assert not nav["done"].is_set() and first["session_id"] in service.navigations
    complete.set()
    await asyncio.wait_for(nav["done"].wait(), 2)
    assert nav["result"]["status"] == "ok" and len(dispatched) == 1
    await service.shutdown()


@pytest.mark.parametrize("running", [True, False])
def test_disconnected_browser_is_work_local_only_with_exit_proof(running):
    adapter = object.__new__(DrissionAdapter)
    broken = Mock()
    broken.get_tabs.side_effect = OSError("Broken connection")
    process = Mock()
    process.is_running.return_value = running
    process.status.return_value = "running"
    artifact, display = Mock(), Mock()
    other = {"sentinel": True}
    adapter.sessions = {
        "one": {"browser": broken, "process_ref": process, "artifacts": artifact},
        "other": other,
    }
    adapter.runtimes = {"one": display, "other": Mock()}
    with pytest.raises(BrowserError) as error:
        adapter._sync("one")
    assert error.value.details["failure_scope"] == ("worker" if running else "session")
    assert adapter.sessions["other"] is other
    adapter.runtimes["other"].close.assert_not_called()
    assert artifact.close.called == (not running)
    assert display.close.called == (not running)
