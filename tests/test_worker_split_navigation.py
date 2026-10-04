"""Actual IPC/Chromium test; local-URL override exists only in this test child."""

import asyncio
import http.server
import multiprocessing
import os
import threading
import time

import pytest
from conftest import fake_worker_resources

from cloud_browser.models import BrowserError
from cloud_browser.service import BrowserService
from cloud_browser.store import Store
from cloud_browser.worker import Worker, worker_main


def fixture_worker_main(connection, configuration, base):
    import cloud_browser.drission

    def controlled_url(url, **kwargs):
        if not url.startswith(base + "/"):
            raise BrowserError("INVALID_URL", "Controlled worker fixture only")

    cloud_browser.drission.validate_url = controlled_url
    worker_main(connection, configuration)


class SlowResources(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/delay.js"):
            time.sleep(48)
            body = b"window.fixtureResourceLoaded=true"
        elif self.path == "/slow.html":
            body = b"<title>Slow fixture</title><h1>Visible is not complete</h1><script src=/delay.js></script>"
        else:
            body = b"<title>Fast fixture</title><h1>Independent public work</h1><button>Local section</button>"
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Type",
                "text/javascript" if self.path.startswith("/delay.js") else "text/html",
            )
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@pytest.mark.browser
async def test_loading_over_45_seconds_keeps_actual_worker_and_other_work_responsive(
    cfg, monkeypatch, record_property
):
    executable = os.getenv("CB_TEST_CHROMIUM")
    if not executable:
        pytest.skip("Set CB_TEST_CHROMIUM for real IPC/navigation")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowResources)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr("cloud_browser.service.validate_url", lambda url, **kw: None)
    cfg = cfg.model_copy(
        update={"chromium_path": executable, "headless": True, "browser_proxy": ""}
    )
    worker = Worker(cfg)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker.process = context.Process(
        target=fixture_worker_main, args=(child, cfg.model_dump(mode="json"), base), daemon=True
    )
    worker.process.start()
    child.close()
    worker.connection = parent
    store = Store(cfg.data_dir / "split-worker.sqlite")
    service = BrowserService(cfg, store, worker)
    service.resources = fake_worker_resources

    async def completed_open():
        opened = await service.call("open", _principal="fixture", url=base + "/fast.html")
        if not opened.get("navigation", {}).get("pending"):
            return opened
        for _ in range(120):
            state = await service.call(
                "status",
                _principal="fixture",
                session_id=opened["session_id"],
                lease_id=opened["lease_id"],
                operation_id=opened["operation_id"],
            )
            if state["operation"]["state"] == "completed":
                return state["operation"]["result"]
            await asyncio.sleep(0.5)
        raise AssertionError("Controlled startup did not finish")

    try:
        first = await completed_open()
        assert first["status"] == "ok", first
        owned = {
            "_principal": "fixture",
            **{k: first[k] for k in ("session_id", "tab_id", "lease_id")},
        }
        second = await completed_open()
        other = {
            "_principal": "fixture",
            **{k: second[k] for k in ("session_id", "tab_id", "lease_id")},
        }
        worker_pid = worker.process.pid
        short = await service.call(
            "navigate", **owned, operation="goto", url=base + "/slow.html", timeout_ms=1000
        )
        assert short["error"]["code"] == "NAVIGATION_TIMEOUT"
        assert short["navigation"]["phase"] in ("loading", "command_response")
        assert len(service.sessions) == 2 and worker.process.pid == worker_pid
        started = time.monotonic()
        pending = await service.call(
            "navigate",
            **owned,
            operation="goto",
            url=base + "/slow.html",
            timeout_ms=60000,
            operation_id="slow-resource-once",
        )
        assert pending["status"] == "no_change" and pending["navigation"]["pending"]
        assert time.monotonic() - started < 5.5
        start = time.monotonic()
        status = await service.call(
            "status",
            **{k: v for k, v in owned.items() if k != "tab_id"},
            operation_id=pending["operation_id"],
        )
        status_time = time.monotonic() - start
        assert status_time < 1 and status["operation"]["state"] == "running"
        start = time.monotonic()
        observed = await service.call("observe", **other, mode="semantic", max_chars=1000)
        observe_time = time.monotonic() - start
        assert observed["status"] == "ok" and observe_time < 5
        assert "Independent public work" in observed["observation"]["semantic_snapshot"]
        start = time.monotonic()
        assert (await service.call("close", **other, scope="session"))["status"] == "ok"
        cleanup_time = time.monotonic() - start
        assert cleanup_time < 5
        for _ in range(130):
            status = await service.call(
                "status",
                **{k: v for k, v in owned.items() if k != "tab_id"},
                operation_id=pending["operation_id"],
            )
            if status["operation"]["state"] == "completed":
                break
            await asyncio.sleep(0.5)
        final = status["operation"]["result"]
        assert final["status"] == "ok" and final["navigation"]["phase"] == "completed"
        assert final["navigation"]["elapsed_ms"] > 45000
        assert worker.process.is_alive() and worker.process.pid == worker_pid
        replay = await service.call(
            "navigate",
            **owned,
            operation="goto",
            url=base + "/slow.html",
            timeout_ms=60000,
            operation_id="slow-resource-once",
        )
        assert replay["replayed"] and replay["request_id"] == final["request_id"]
        for name, value in (
            ("status_seconds", status_time),
            ("other_observation_seconds", observe_time),
            ("other_cleanup_seconds", cleanup_time),
            ("navigation_seconds", final["navigation"]["elapsed_ms"] / 1000),
        ):
            record_property(name, value)
    finally:
        await service.shutdown()
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(2)
