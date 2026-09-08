"""Opt-in public-site integration: real HTTP MCP + worker + Chromium + private approval."""

import asyncio
import base64
import io
import json
import os
import socket

import httpx
import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from cloud_browser.config import Settings
from cloud_browser.server import create_apps

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("approval_policy", ["strict", "balanced"])
async def test_live_mcp_approval_roundtrip_and_reading(tmp_path, approval_policy):
    executable = os.getenv("CB_TEST_CHROMIUM")
    if not executable or os.getenv("CB_TEST_LIVE") != "true":
        pytest.skip("Opt-in: CB_TEST_CHROMIUM and CB_TEST_LIVE=true")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    cfg = Settings(
        _env_file=None, _env_prefix="CB_ISOLATED_LIVE_TEST_",
        development=True, public_origin=f"http://127.0.0.1:{port}",
        control_origin="http://control.example", data_dir=tmp_path,
        chromium_path=executable, headless=True, browser_proxy="",
        approval_policy=approval_policy,
    )
    public, control, service, auth = create_apps(cfg)
    auth.store.put("grant", "live-test-grant", {"active": True})
    token = auth.issue("live-test-grant")["access_token"]
    # This is an isolated test administrator, never a production console session.
    auth.store.put("control", "test-console-cookie", {"csrf": "test-console-csrf"}, 120)
    server = uvicorn.Server(uvicorn.Config(public, host="127.0.0.1", port=port, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=60) as http:
            async with streamable_http_client(cfg.resource, http_client=http) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()

                    async def call(name, arguments):
                        result = (await client.call_tool(name, arguments)).structured_content
                        assert result is not None
                        return result

                    opened = await call("browser_open", {"url": "https://www.w3.org/WAI/ARIA/apg/patterns/accordion/examples/accordion/"})
                    assert opened["status"] == "ok"
                    args = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
                    observed = await call("browser_observe", args | {"mode": "interactive", "max_chars": 100000})
                    nodes = [json.loads(line) for line in observed["observation"]["interactive_snapshot"].splitlines()]
                    button = next(n for n in nodes if n.get("name") == "Personal Information")
                    assert button["expanded"] == "true"
                    action = args | {"expected_revision": observed["revision"], "action": {"type": "click", "node_id": button["node_id"]}}
                    proposal = await call("browser_act", action)
                    if approval_policy == "balanced":
                        assert proposal["status"] == "ok"
                        assert proposal["action_policy"]["reason"] == "view_control"
                        assert not service.pending
                        acted = proposal
                    else:
                        assert proposal["status"] == "confirmation_required"
                        assert proposal["confirmation"]["destination"] is None
                        confirm = proposal["confirmation"]["confirmation_token"]
                        assert (await call("browser_act", action | {"confirmation_token": confirm}))["status"] == "confirmation_required"
                        review_id = next(iter(service.pending))
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=control), base_url=cfg.control_origin,
                            cookies={"cb_control": "test-console-cookie"}, headers={"Origin": cfg.control_origin},
                        ) as console:
                            denied = await console.post(f"/approval/{review_id}", data={"csrf": "wrong", "decision": "approve"})
                            assert denied.status_code == 409
                            approved = await console.post(f"/approval/{review_id}", data={"csrf": "test-console-csrf", "decision": "approve"})
                            assert approved.status_code == 303
                        status = await call("browser_status", {"session_id": args["session_id"]})
                        assert status["approvals"][0]["state"] == "approved"
                        acted = await call("browser_act", action | {"confirmation_token": confirm})
                    assert acted["status"] == "ok" and acted["action_result"]["performed"]
                    after = await call("browser_observe", args | {"mode": "interactive", "max_chars": 100000})
                    nodes = [json.loads(line) for line in after["observation"]["interactive_snapshot"].splitlines()]
                    assert next(n for n in nodes if n.get("name") == "Personal Information")["expanded"] == "false"

                    docs = await call("browser_open", {"session_id": args["session_id"], "url": "https://docs.python.org/3/tutorial/index.html"})
                    assert docs["status"] == "ok"
                    docs_args = {"session_id": args["session_id"], "tab_id": docs["tab_id"]}
                    read = await call("browser_observe", docs_args | {"mode": "auto", "max_chars": 1000})
                    assert read["observation"]["semantic_snapshot"]
                    assert read["observation"]["interactive_snapshot"]
                    assert "StaticText" not in read["observation"]["semantic_snapshot"]
                    image_result = await client.call_tool("browser_observe", docs_args | {"mode": "visual"})
                    image = next(item for item in image_result.content if item.type == "image")
                    assert Image.open(io.BytesIO(base64.b64decode(image.data))).size == (1024, 768)
                    closed = await call("browser_close", {"session_id": args["session_id"], "scope": "session"})
                    assert closed["status"] == "ok"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()
