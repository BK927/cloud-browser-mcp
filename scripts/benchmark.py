"""Operator-only local benchmark. Prints timings/resources, never tokens or page text.

Run inside the browser container or via native_benchmark.py while the listeners run.
It mints a temporary local grant in the operator-owned DB and always revokes it.
"""

import argparse
import asyncio
import json
import platform
import secrets
import time
from urllib.parse import urlsplit

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from cloud_browser.config import Settings
from cloud_browser.oauth import Auth
from cloud_browser.store import Store


async def benchmark(args):
    cfg = Settings()
    store = Store(cfg.data_dir / "state.sqlite3")
    auth = Auth(cfg, store)
    grant = secrets.token_urlsafe(32)
    store.put("grant", grant, {"active": True}, 600)
    token = auth.issue(grant)["access_token"]
    records = []
    sid = None
    lease_id = None
    control_token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    host = cfg.bind_host if cfg.bind_host not in ("0.0.0.0", "::") else "127.0.0.1"
    phase = "idle"
    headers = {"Authorization": "Bearer " + token, "Host": urlsplit(cfg.public_origin).netloc}
    try:
        async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
            async with streamable_http_client(
                f"http://{host}:{cfg.public_port}/mcp", http_client=http
            ) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()

                    async def invoke(name, parameters):
                        nonlocal lease_id
                        if lease_id and parameters.get("session_id"):
                            parameters = parameters | {"lease_id": lease_id}
                        start = time.perf_counter()
                        started_at = time.time()
                        result = await client.call_tool(name, parameters)
                        value = result.structured_content or {
                            "status": "error",
                            "error": {"code": "NO_STRUCTURED_RESULT"},
                        }
                        if value and value.get("lease_id"):
                            lease_id = value["lease_id"]
                        records.append(
                            {
                                "tool": name,
                                "phase": phase,
                                "started_at": started_at,
                                "finished_at": time.time(),
                                "elapsed_ms": round((time.perf_counter() - start) * 1000),
                                "status": value.get("status"),
                                "error": (value.get("error") or {}).get("code"),
                                "image_base64_bytes": sum(
                                    len(item.data)
                                    for item in result.content
                                    if item.type == "image"
                                ),
                            }
                        )
                        return value

                    async def settle(name):
                        nonlocal phase
                        phase = name
                        start = time.time()
                        await asyncio.sleep(args.settle_seconds)
                        value = await invoke("browser_status", {"session_id": sid} if sid else {})
                        records.append(
                            {
                                "phase": name,
                                "started_at": start,
                                "finished_at": time.time(),
                                "resources": value.get("resources"),
                            }
                        )

                    async def finish_control(hid):
                        # Operator-run synthetic benchmark only. No login secrets
                        # or real external actions are entered or approved.
                        store.put("control", control_token, {"csrf": csrf}, 120)
                        async with httpx2.AsyncClient(timeout=30) as private:
                            result = await private.post(
                                f"http://{host}:{cfg.control_port}/handoff/{hid}/complete",
                                headers={
                                    "Host": urlsplit(cfg.control_origin).netloc,
                                    "Origin": cfg.control_origin,
                                    "Cookie": "cb_control=" + control_token,
                                },
                                data={"csrf": csrf},
                            )
                        if result.status_code != 200:
                            raise RuntimeError(
                                "Synthetic manual control did not return; finish/cancel it privately before another run"
                            )

                    try:
                        initial = await invoke("browser_status", {})
                        if initial.get("sessions") or initial.get("busy"):
                            raise RuntimeError(
                                "Existing browser session: finish it before an isolated benchmark"
                            )
                        if (
                            args.require_budget_mb
                            and (initial.get("resources") or {}).get("cgroup_limit_mb")
                            != args.require_budget_mb
                        ):
                            raise RuntimeError(
                                "Actual cgroup budget differs from required comparison budget; do not compare unlike limits"
                            )
                        await settle("idle")
                        phase = "open_heavy"
                        opened = await invoke("browser_open", {"url": args.url})
                        if opened["status"] != "ok":
                            raise RuntimeError(
                                "Could not start benchmark session; see result status"
                            )
                        sid, tid = opened["session_id"], opened["tab_id"]
                        await invoke(
                            "browser_configure",
                            {
                                "session_id": sid,
                                "tab_id": tid,
                                "configuration": {"viewport_width": 1024, "viewport_height": 768},
                            },
                        )
                        await settle("single_page")
                        for _ in range(args.rounds):
                            for mode in ("interactive", "visual"):
                                phase = (
                                    "text_observation" if mode == "interactive" else "image_capture"
                                )
                                await invoke(
                                    "browser_observe",
                                    {"session_id": sid, "tab_id": tid, "mode": mode},
                                )
                        phase = "long_observation"
                        await invoke(
                            "browser_observe",
                            {
                                "session_id": sid,
                                "tab_id": tid,
                                "mode": "semantic",
                                "max_chars": 100000,
                            },
                        )
                        phase = "full_page_image"
                        await invoke(
                            "browser_observe",
                            {"session_id": sid, "tab_id": tid, "mode": "visual", "full_page": True},
                        )
                        for _ in range(args.extra_tabs):
                            phase = "add_tab"
                            result = await invoke("browser_open", {"session_id": sid})
                            if result["status"] != "ok":
                                break
                        await settle("additional_tabs")
                        if args.manual_control:
                            phase = "start_manual_control"
                            manual = await invoke(
                                "browser_handoff",
                                {
                                    "session_id": sid,
                                    "tab_id": tid,
                                    "reason": "Operator-run performance benchmark; no credentials or external actions",
                                },
                            )
                            hid = (manual.get("handoff") or {}).get("handoff_id")
                            if hid:
                                try:
                                    await settle("manual_control")
                                finally:
                                    await finish_control(hid)
                                await settle("after_manual_control")
                        final = await invoke("browser_status", {"session_id": sid})
                        records.append(
                            {
                                "resources_before": initial.get("resources"),
                                "resources_after": final.get("resources"),
                            }
                        )
                    finally:
                        if sid:
                            phase = "close_session"
                            closed = await invoke(
                                "browser_close", {"session_id": sid, "scope": "session"}
                            )
                            if closed["status"] == "ok":
                                sid = None
                                await settle("after_close")
    finally:
        store.delete("grant", grant)
        store.delete("control", control_token)
        store.close()
        print(
            json.dumps(
                {
                    "platform": platform.platform(),
                    "machine": platform.machine(),
                    "label": args.label,
                    "workload": {
                        "url": args.url,
                        "viewport": [1024, 768],
                        "rounds": args.rounds,
                        "extra_tabs": args.extra_tabs,
                        "manual_control": args.manual_control,
                        "settle_seconds": args.settle_seconds,
                        "require_budget_mb": args.require_budget_mb,
                    },
                    "measurements": records,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default=None, help="Optional public test-page URL; blank page otherwise"
    )
    parser.add_argument("--rounds", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--extra-tabs", type=int, choices=range(0, 21), default=0)
    parser.add_argument("--label", default="operator-benchmark")
    parser.add_argument("--settle-seconds", type=int, choices=range(1, 31), default=5)
    parser.add_argument(
        "--manual-control",
        action="store_true",
        help="Start and finish synthetic handoff through the private local console; never approves a real action",
    )
    parser.add_argument("--require-budget-mb", type=int, default=1024)
    asyncio.run(benchmark(parser.parse_args()))
