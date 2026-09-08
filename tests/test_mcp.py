import asyncio
import base64
import io
import os
import socket

import httpx2
import pytest
import uvicorn
from conftest import FakeWorker
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from cloud_browser.config import Settings
from cloud_browser.server import create_apps


@pytest.mark.asyncio
@pytest.mark.parametrize("real_browser", [False, pytest.param(True, marks=pytest.mark.browser)])
async def test_actual_streamable_http_sdk_tools_and_image(tmp_path, real_browser):
    executable = os.getenv("CB_TEST_CHROMIUM")
    if real_browser and not executable:
        pytest.skip("Set CB_TEST_CHROMIUM for full HTTP-to-worker-to-browser image test")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    cfg = Settings(
        development=True,
        public_origin=f"http://127.0.0.1:{port}",
        data_dir=tmp_path,
        chromium_path=executable or "/usr/bin/chromium",
        headless=True,
        browser_proxy="",
    )
    public, _, _, auth = create_apps(cfg, worker=None if real_browser else FakeWorker())
    auth.store.put("grant", "test-grant", {"active": True})
    token = auth.issue("test-grant")["access_token"]
    server = uvicorn.Server(
        uvicorn.Config(public, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + token}, timeout=60
        ) as http:
            async with streamable_http_client(cfg.resource, http_client=http) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    tools = (await client.list_tools()).tools
                    assert len(tools) == 12
                    assert {t.name for t in tools} >= {
                        "browser_status",
                        "browser_configure",
                        "browser_act",
                        "browser_list_page_tools",
                        "browser_call_page_tool",
                    }
                    opened = (await client.call_tool("browser_open", {})).structured_content
                    assert opened["status"] == "ok"
                    args = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
                    observed = await client.call_tool("browser_observe", args | {"mode": "visual"})
                    assert observed.structured_content["status"] == "ok"
                    picture = next(item for item in observed.content if item.type == "image")
                    assert picture.mime_type == ("image/jpeg" if real_browser else "image/png")
                    assert Image.open(io.BytesIO(base64.b64decode(picture.data))).size == (
                        (1024, 768) if real_browser else (1, 1)
                    )
                    if real_browser:
                        page_tools = (
                            await client.call_tool("browser_list_page_tools", args)
                        ).structured_content
                        if page_tools["status"] == "ok":
                            assert page_tools["page_tools"] == []
                        else:
                            assert page_tools["error"]["code"] == "UNSUPPORTED_OPERATION"
                        return
                    proposal = await client.call_tool(
                        "browser_act",
                        args
                        | {
                            "expected_revision": 1,
                            "action": {"type": "click", "node_id": "node_1"},
                        },
                    )
                    assert proposal.structured_content["status"] == "confirmation_required"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()
