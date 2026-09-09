"""Controlled MCP lifecycle probe; execute only in a disposable native/container CI.

Runs as the API user. It uses a temporary local grant, not a production OAuth
reset, and never outputs a token or page text. It intentionally kills only this
fresh test's worker after identifying its parent and exact cgroup.
"""

import asyncio
import json
import os
import secrets
import signal
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
import psutil
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from cloud_browser.config import Settings
from cloud_browser.oauth import Auth
from cloud_browser.runtime import DataLock
from cloud_browser.store import Store


def processes():
    group = Path("/proc/self/cgroup").read_text()
    result = []
    for proc in psutil.process_iter(["pid", "ppid", "name"]):
        try:
            if (
                Path(f"/proc/{proc.pid}/cgroup").read_text() == group
                and proc.status() != psutil.STATUS_ZOMBIE
            ):
                result.append(proc)
        except (OSError, psutil.Error):
            pass
    return result


async def no_runtime():
    for _ in range(40):
        live = [
            p.name()
            for p in processes()
            if any(
                word in p.name().lower()
                for word in ("chrome", "chromium", "xvfb", "x11vnc", "websockify")
            )
        ]
        if not live:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("Test runtime processes were not reclaimed: " + str(live))


async def main():
    cfg = Settings()
    if cfg.public_origin != "https://ci-mcp.example":
        raise RuntimeError("Refusing to run a crash probe against a non-CI instance")
    try:
        with DataLock(cfg.data_dir):
            raise AssertionError("Service did not lock its data directory")
    except RuntimeError as exc:
        assert "already owned" in str(exc)
    store = Store(cfg.data_dir / "state.sqlite3")
    auth = Auth(cfg, store)
    grant, control, csrf = (secrets.token_urlsafe(32) for _ in range(3))
    store.put("grant", grant, {"active": True}, 300)
    store.put("control", control, {"csrf": csrf}, 300)
    token = auth.issue(grant)["access_token"]
    host = cfg.bind_host if cfg.bind_host != "0.0.0.0" else "127.0.0.1"
    leases = {}
    try:
        async with httpx2.AsyncClient(
            headers={
                "Host": urlsplit(cfg.public_origin).netloc,
                "Authorization": "Bearer " + token,
            },
            timeout=60,
        ) as http:
            async with streamable_http_client(
                f"http://{host}:{cfg.public_port}/mcp", http_client=http
            ) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()

                    async def call(name, **arguments):
                        if arguments.get("session_id") in leases:
                            arguments["lease_id"] = leases[arguments["session_id"]]
                        result = await client.call_tool("browser_" + name, arguments)
                        value = result.structured_content
                        assert value, (name, "missing structured result")
                        if value.get("lease_id"):
                            leases[value["session_id"]] = value["lease_id"]
                        assert value["status"] in (
                            "ok",
                            "no_change",
                            "user_action_required",
                            "error",
                        ), (name, value.get("status"), value.get("error"))
                        return value, result

                    status, _ = await call("status")
                    assert not status["sessions"] and not status.get("busy")
                    assert status["resources"]["cgroup_limit_mb"] == 1024
                    await no_runtime()
                    opened, _ = await call("open")
                    assert opened["status"] == "ok", opened.get("error")
                    sid, tid = opened["session_id"], opened["tab_id"]
                    seen, raw = await call("observe", session_id=sid, tab_id=tid, mode="visual")
                    assert seen["status"] == "ok", seen.get("error")
                    assert any(c.type == "image" for c in raw.content)
                    assert any(p.name() == "Xvfb" for p in processes())
                    sandboxed = []
                    for proc in processes():
                        if "chrome" in proc.name().lower() or "chromium" in proc.name().lower():
                            try:
                                fields = dict(
                                    line.split(":", 1)
                                    for line in Path(f"/proc/{proc.pid}/status")
                                    .read_text()
                                    .splitlines()
                                    if ":" in line
                                )
                                sandboxed.append(
                                    fields.get("NoNewPrivs", "").strip() == "1"
                                    and fields.get("Seccomp", "").strip() == "2"
                                )
                            except OSError:
                                pass
                    assert any(sandboxed), (
                        "No Chromium child with no-new-privileges and seccomp filtering"
                    )
                    assert not any("x11vnc" in p.name() for p in processes())
                    # Cold start/capture can temporarily charge dirty cache or
                    # register PSI stalls. Only retry a resource denial proven
                    # to precede session allocation; never retry an uncertain open.
                    for attempt in range(10):
                        other, _ = await call("open")
                        if other["status"] == "ok":
                            break
                        if (other.get("error") or {}).get(
                            "code"
                        ) != "RESOURCE_PRESSURE" or other.get("session_id"):
                            break
                        resources = other.get("resources") or {}
                        print(
                            json.dumps(
                                {
                                    "phase": "second_work_admission",
                                    "attempt": attempt + 1,
                                    "resources": {
                                        key: resources.get(key)
                                        for key in (
                                            "available_mb",
                                            "host_available_mb",
                                            "cgroup_used_mb",
                                            "cgroup_raw_headroom_mb",
                                            "cgroup_reclaimable_estimate_mb",
                                            "memory_pressure",
                                            "required_headroom_mb",
                                            "pressure_level",
                                        )
                                    },
                                }
                            ),
                            flush=True,
                        )
                        await asyncio.sleep(2)
                    assert other["status"] == "ok", (other.get("error"), other.get("resources"))
                    assert other["session_id"] != sid
                    assert sum(p.name() == "Xvfb" for p in processes()) == 2
                    blocked, _ = await call("open")
                    assert blocked["error"]["code"] == "BROWSER_BUSY"
                    assert blocked["busy_reason"] == "session_capacity"
                    manual, _ = await call(
                        "handoff",
                        session_id=sid,
                        tab_id=tid,
                        reason="Disposable CI control lifecycle test",
                    )
                    assert manual["status"] == "user_action_required", manual.get("error")
                    assert any("x11vnc" in p.name() for p in processes())
                    # Opening work B changed the launch environment; VNC must
                    # still bind to work A's display, never the newest display.
                    for proc in processes():
                        if "x11vnc" in proc.name():
                            argv = proc.cmdline()
                            assert argv[argv.index("-display") + 1] == f":{cfg.display_number}"
                    paused, _ = await call("observe", session_id=sid, tab_id=tid)
                    assert paused["error"]["code"] == "USER_CONTROL_ACTIVE"
                    paused_other, _ = await call(
                        "observe", session_id=other["session_id"], tab_id=other["tab_id"]
                    )
                    assert paused_other["error"]["code"] == "USER_CONTROL_ACTIVE"
                    hid = manual["handoff"]["handoff_id"]
                    async with httpx2.AsyncClient(timeout=30) as private:
                        response = await private.post(
                            f"http://{host}:{cfg.control_port}/handoff/{hid}/complete",
                            headers={
                                "Host": urlsplit(cfg.control_origin).netloc,
                                "Origin": cfg.control_origin,
                                "Cookie": "cb_control=" + control,
                            },
                            data={"csrf": csrf},
                        )
                        assert response.status_code == 200, "Control completion failed"
                    assert not any(
                        "x11vnc" in p.name() or "websockify" in p.name() for p in processes()
                    )
                    closed, _ = await call("close", session_id=sid, scope="session")
                    assert closed["status"] == "ok"
                    other_seen, _ = await call(
                        "observe",
                        session_id=other["session_id"],
                        tab_id=other["tab_id"],
                        mode="interactive",
                    )
                    assert other_seen["status"] == "ok"
                    await call("close", session_id=other["session_id"], scope="session")
                    await no_runtime()
                    opened, _ = await call("open")
                    assert opened["status"] == "ok", opened.get("error")
                    sid, tid = opened["session_id"], opened["tab_id"]
                    candidates = []
                    for proc in processes():
                        try:
                            if (
                                "spawn_main" in " ".join(proc.cmdline())
                                and proc.uids().effective == os.geteuid()
                            ):
                                parent = proc.parent()
                                if parent and any(
                                    "cloud_browser.cli" in arg or arg.endswith("/cloud-browser")
                                    for arg in parent.cmdline()
                                ):
                                    candidates.append(proc)
                        except psutil.Error:
                            pass
                    assert len(candidates) == 1, "Could not identify exactly the CI API worker"
                    candidates[0].send_signal(signal.SIGKILL)
                    await asyncio.sleep(0.2)
                    expired, _ = await call("observe", session_id=sid, tab_id=tid)
                    assert expired["error"]["code"] == "SESSION_EXPIRED", expired.get("error")
                    await no_runtime()
                    reopened, _ = await call("open")
                    assert reopened["status"] == "ok", reopened.get("error")
                    await call("close", session_id=reopened["session_id"], scope="session")
                    await no_runtime()
        print(
            json.dumps(
                {
                    "mcp_image": True,
                    "data_lock": True,
                    "on_demand_display": True,
                    "control_pause_resume": True,
                    "two_isolated_works": True,
                    "worker_crash_cleanup": True,
                }
            )
        )
    finally:
        store.delete("grant", grant)
        store.delete("control", control)
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
