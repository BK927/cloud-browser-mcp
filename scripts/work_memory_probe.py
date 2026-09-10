"""External-PC single/dual-work memory probe; no credentials/URLs/DOM in output.

An operator supplies short-lived authentication separately. Never runs inside the
measured Pi cgroup, closes foreign work, changes limits, or retries a rejected action.
Use sample_memory.py alongside this client to measure PSS and between-call peaks.
"""

import argparse
import asyncio
import json
import sys
import time
from urllib.parse import urlsplit

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

RESOURCE_KEYS = (
    "host_available_mb",
    "available_mb",
    "cgroup_limit_mb",
    "cgroup_used_mb",
    "cgroup_raw_headroom_mb",
    "cgroup_reclaimable_estimate_mb",
    "cgroup_estimated_headroom_mb",
    "accounting",
    "memory_pressure",
    "pressure_level",
    "soft_reserve_borrowed",
)


class ProbeStop(Exception):
    pass


async def probe(client, *, mode, url, budget_mb=1024, settle_seconds=2, sleep=asyncio.sleep):
    if mode not in ("single", "dual") or not 0 <= settle_seconds <= 10:
        raise ValueError("Invalid bounded workload")
    records, owned, cleanup_errors = [], [], []
    report = {
        "mode": mode,
        "result": "incomplete",
        "records": records,
        "cleanup_errors": cleanup_errors,
    }
    started = time.monotonic()

    async def invoke(name, args, phase, *, cleanup=False):
        # Leave time for owned-work cleanup within a separately issued five-minute token.
        if not cleanup and time.monotonic() - started > 210:
            raise ProbeStop("PROBE_TIME_BUDGET")
        before = time.monotonic()
        try:
            raw = await client.call_tool(name, args)
        except Exception:
            records.append({"phase": phase, "tool": name, "status": "transport_error"})
            raise ProbeStop("TRANSPORT_ERROR") from None
        value = raw.structured_content or {}
        records.append(
            {
                "phase": phase,
                "tool": name,
                "unix_time": time.time(),
                "elapsed_ms": round((time.monotonic() - before) * 1000),
                "status": value.get("status"),
                "error": (value.get("error") or {}).get("code"),
                "images": sum(item.type == "image" for item in raw.content),
                "resource_limited": bool((value.get("observation") or {}).get("resource_limited")),
            }
        )
        return value

    async def sample(phase, *, expected=None):
        await sleep(settle_seconds)
        value = await invoke("browser_status", {}, phase)
        if value.get("status") != "ok":
            raise ProbeStop("STATUS_UNAVAILABLE")
        scheduler, resources = value.get("scheduler") or {}, value.get("resources") or {}
        records.append(
            {
                "phase": phase,
                "unix_time": time.time(),
                "active_sessions": scheduler.get("active_sessions"),
                "resources": {k: resources[k] for k in RESOURCE_KEYS if k in resources},
            }
        )
        if resources.get("cgroup_limit_mb") != budget_mb:
            raise ProbeStop("BUDGET_MISMATCH")
        if scheduler.get("active_sessions") != (len(owned) if expected is None else expected):
            raise ProbeStop("FOREIGN_OR_UNACCOUNTED_WORK")
        if scheduler.get("state") == "user_control" or scheduler.get("automation_paused"):
            raise ProbeStop("HUMAN_CONTROL")

    async def close_owned(work):
        try:
            value = await invoke(
                "browser_close",
                {
                    "session_id": work["session_id"],
                    "lease_id": work["lease_id"],
                    "scope": "session",
                },
                "close_owned",
                cleanup=True,
            )
            if value.get("status") != "ok":
                cleanup_errors.append((value.get("error") or {}).get("code", "CLOSE_FAILED"))
            else:
                owned.remove(work)
        except ProbeStop as exc:
            cleanup_errors.append(str(exc))

    try:
        await sample("baseline", expected=0)
        for index in range(1 if mode == "single" else 2):
            await sample("before_open")
            value = await invoke("browser_open", {"url": url}, f"open_{index + 1}")
            # Partial opens may carry recovery handles: always retain those for cleanup.
            if value.get("session_id") and value.get("lease_id"):
                work = {k: value[k] for k in ("session_id", "lease_id")}
                if value.get("tab_id"):
                    work["tab_id"] = value["tab_id"]
                owned.append(work)
            if value.get("status") != "ok" or not all(
                value.get(k) for k in ("session_id", "lease_id", "tab_id")
            ):
                raise ProbeStop((value.get("error") or {}).get("code", "OPEN_FAILED"))
            configured = await invoke(
                "browser_configure",
                work
                | {
                    "configuration": {
                        "viewport_width": 1024,
                        "viewport_height": 768,
                        "screenshot_quality": 75,
                        "max_chars": 30000,
                        "wait_ms": 500,
                    }
                },
                "baseline_configuration",
            )
            if configured.get("status") != "ok":
                raise ProbeStop("CONFIGURE_FAILED")
            await sample(f"after_open_{index + 1}")
        for index, work in enumerate(owned):
            for observation in ("interactive", "semantic", "visual"):
                value = await invoke(
                    "browser_observe",
                    work | {"mode": observation},
                    f"work_{index + 1}_{observation}",
                )
                if value.get("status") != "ok":
                    raise ProbeStop((value.get("error") or {}).get("code", "OBSERVE_FAILED"))
                if observation == "visual" and not records[-1]["images"]:
                    raise ProbeStop("IMAGE_MISSING")
                await sample(f"after_work_{index + 1}_{observation}")
        report["result"] = "completed"
    except ProbeStop as exc:
        report["result"] = "stopped"
        report["reason"] = str(exc)
    finally:
        # Cleanup failures never skip another owned work or the caller's independent revoke.
        for work in list(reversed(owned)):
            await close_owned(work)
        try:
            await sample("after_close", expected=0)
        except ProbeStop as exc:
            report["after_close_reason"] = str(exc)
            if report["result"] == "completed":
                report["result"] = "unverified_recovery"
        if cleanup_errors:
            report["result"] = "cleanup_required"
    return report


async def main(args):
    if urlsplit(args.endpoint).scheme != "https":
        raise ValueError("Use the verified HTTPS MCP endpoint")
    if sys.stdin.isatty():
        raise ValueError(
            "Use the operator wrapper's protected stdin pipe, not an interactive terminal"
        )
    token = sys.stdin.readline().strip()
    if not token or len(token) > 8192:
        raise ValueError("Provide one temporary bearer token through stdin, not argv")
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=35) as http:
        async with streamable_http_client(args.endpoint, http_client=http) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                report = await probe(client, mode=args.mode, url=args.url, budget_mb=args.budget_mb)
    print(json.dumps({"label": args.label, "repeat": args.repeat, **report}, indent=2))
    return 0 if report["result"] == "completed" else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--url", required=True, help="Same public read-only page for every run")
    parser.add_argument("--mode", choices=("single", "dual"), required=True)
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--budget-mb", type=int, default=1024)
    parser.add_argument("--label", required=True, help="Exact tested commit and install mode")
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(main(args))
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"result": "failed", "reason": "CLIENT_SETUP_OR_INTERRUPTION"}))
        exit_code = 1
    sys.exit(exit_code)
