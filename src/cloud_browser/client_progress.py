"""Owned, bounded status polling for operator harnesses; never redispatch a tool.

The standalone benchmark bundle may copy this file beside benchmark.py when
measuring an older installed server. Synchronous responses remain unchanged.
"""

import asyncio
import time


async def finish_navigation(
    value, poll, *, clock=time.monotonic, sleep=asyncio.sleep, max_wait_seconds=None
):
    if not value.get("navigation", {}).get("pending"):
        return value
    identity = {key: value.get(key) for key in ("session_id", "lease_id", "operation_id")}

    def fail(code):
        return value | {"status": "error", "error": {"code": code}}

    if value.get("status") != "no_change" or not all(identity.values()):
        return fail("NAVIGATION_PROGRESS_INVALID")
    timeout = value["navigation"].get("timeout_ms", 60000)
    if type(timeout) is not int or not 1000 <= timeout <= 300000:
        return fail("NAVIGATION_PROGRESS_INVALID")
    # A cold startup has its own bounded worker budget before navigation starts.
    deadline = clock() + timeout / 1000 + (45 if not value.get("tab_id") else 0) + 5
    if max_wait_seconds is not None:
        deadline = min(deadline, clock() + max(0, max_wait_seconds))
    while clock() < deadline:
        await sleep(0.5)
        if clock() >= deadline:
            break
        try:
            state = await asyncio.wait_for(poll(identity), timeout=min(5, deadline - clock()))
        except Exception:
            return fail("NAVIGATION_PROGRESS_TRANSPORT_ERROR")
        if state.get("status") != "ok":
            return fail((state.get("error") or {}).get("code", "NAVIGATION_PROGRESS_UNAVAILABLE"))
        operation = state.get("operation", {})
        if operation.get("state") == "completed":
            result = operation.get("result")
            if (
                not isinstance(result, dict)
                or result.get("navigation", {}).get("pending")
                or result.get("session_id") not in (None, identity["session_id"])
                or result.get("lease_id") not in (None, identity["lease_id"])
                or result.get("operation_id") not in (None, identity["operation_id"])
                or (result.get("status") == "ok" and not result.get("tab_id"))
            ):
                return fail("NAVIGATION_PROGRESS_INVALID")
            # Preserve issued cleanup handles even for a terminal error result.
            return result | {key: val for key, val in identity.items() if result.get(key) is None}
        if operation.get("state") != "running":
            return fail("NAVIGATION_PROGRESS_UNAVAILABLE")
    return fail("NAVIGATION_PROGRESS_TIMEOUT")
