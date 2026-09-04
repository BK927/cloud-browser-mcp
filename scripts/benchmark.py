"""Operator-only local benchmark. Prints timings/resources, never tokens or page text.

Run inside the browser container while the two application listeners are running.
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
    headers = {"Authorization": "Bearer " + token, "Host": urlsplit(cfg.public_origin).netloc}
    try:
        async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
            async with streamable_http_client(
                f"http://127.0.0.1:{cfg.public_port}/mcp", http_client=http
            ) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()

                    async def invoke(name, parameters):
                        start = time.perf_counter()
                        result = await client.call_tool(name, parameters)
                        value = result.structured_content
                        records.append(
                            {
                                "tool": name,
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

                    try:
                        initial = await invoke("browser_status", {})
                        if initial.get("sessions"):
                            raise RuntimeError(
                                "Existing browser session: finish it before an isolated benchmark"
                            )
                        opened = await invoke("browser_open", {"url": args.url})
                        if opened["status"] != "ok":
                            raise RuntimeError(
                                "Could not start benchmark session; see result status"
                            )
                        sid, tid = opened["session_id"], opened["tab_id"]
                        for _ in range(args.rounds):
                            for mode in ("interactive", "visual"):
                                await invoke(
                                    "browser_observe",
                                    {"session_id": sid, "tab_id": tid, "mode": mode},
                                )
                        await invoke(
                            "browser_observe",
                            {"session_id": sid, "tab_id": tid, "mode": "visual", "full_page": True},
                        )
                        for _ in range(args.extra_tabs):
                            result = await invoke("browser_open", {"session_id": sid})
                            if result["status"] != "ok":
                                break
                        final = await invoke("browser_status", {"session_id": sid})
                        records.append(
                            {
                                "resources_before": initial.get("resources"),
                                "resources_after": final.get("resources"),
                            }
                        )
                    finally:
                        if sid:
                            await invoke("browser_close", {"session_id": sid, "scope": "session"})
    finally:
        store.delete("grant", grant)
        store.close()
        print(
            json.dumps(
                {
                    "platform": platform.platform(),
                    "machine": platform.machine(),
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
    asyncio.run(benchmark(parser.parse_args()))
