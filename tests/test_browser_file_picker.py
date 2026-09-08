import io
import json
import time

import pytest
from starlette.datastructures import UploadFile
from test_browser import browser as browser

from cloud_browser.models import BrowserError
from cloud_browser.security import origin
from cloud_browser.uploads import Uploads

pytestmark = pytest.mark.browser


async def test_hidden_file_picker_is_exact_staged_upload_target(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        """document.body.innerHTML='<input id=file type=file hidden><button onclick=document.querySelector(\"#file\").click()>Choose file</button>'"""
    )
    seen = adapter.observe(sid, tid, mode="interactive")
    button = json.loads(seen["observation"]["interactive_snapshot"].splitlines()[0])
    adapter.act(sid, tid, seen["revision"], {"type": "click", "node_id": button["node_id"]})
    time.sleep(0.1)
    seen = adapter.observe(sid, tid, mode="interactive")
    picker = seen["observation"]["file_chooser"]
    assert picker and picker.get("node_id"), picker
    staging = Uploads(adapter.cfg)
    try:
        item = await staging.stage(UploadFile(filename="fixture.txt", file=io.BytesIO(b"hello")))
        action = {
            "type": "upload",
            "node_id": picker["node_id"],
            "upload_ids": [item["upload_id"]],
            "_uploads": staging.resolve([item["upload_id"]]),
        }
        assert adapter.prepare(sid, tid, seen["revision"], action)["requires_confirmation"]
        adapter.act(sid, tid, seen["revision"], action)
        assert tab.run_js("return document.querySelector('#file').files[0].name") == "fixture.txt"
        assert adapter.observe(sid, tid)["observation"]["file_chooser"] is None
    finally:
        staging.close()


@pytest.mark.parametrize("browser", ["webmcp-experimental"], indirect=True)
def test_operator_pinned_webmcp_read_requires_exact_origin_and_schema(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    if not tab.run_js("return !!(document.modelContext || navigator.modelContext)"):
        pytest.skip("Native page WebMCP registration unavailable")
    tab.run_js(
        "(document.modelContext||navigator.modelContext).registerTool({name:'read_fixture',description:'Controlled test',inputSchema:{type:'object',properties:{}},execute:async()=>({ok:true})})"
    )
    time.sleep(0.1)
    listed = adapter.list_page_tools(sid, tid)
    tool = next(t for t in listed["page_tools"] if t["name"] == "read_fixture")
    action = {"type": "page_tool", "tool_name": "read_fixture", "arguments": {}}
    assert adapter.prepare(sid, tid, listed["revision"], action)["requires_confirmation"]
    adapter.cfg.webmcp_read_allowlist = {origin(tab.url): {"read_fixture": tool["schema_sha256"]}}
    assert not adapter.prepare(sid, tid, listed["revision"], action)["requires_confirmation"]
    adapter.cfg.webmcp_read_allowlist = {
        "https://wrong.example": {"read_fixture": tool["schema_sha256"]}
    }
    assert adapter.prepare(sid, tid, listed["revision"], action)["requires_confirmation"]


def test_sensitive_dialog_requires_manual_authentication(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("setTimeout(()=>prompt('Password'),50)")
    time.sleep(0.2)
    info = adapter.dialog_info(sid, tid)
    assert info["dialog"]["sensitive"]
    with pytest.raises(BrowserError) as error:
        adapter.prepare(
            sid,
            tid,
            info["revision"],
            {
                "type": "dialog",
                "operation": "accept",
                "dialog_id": info["dialog"]["dialog_id"],
                "text": "never",
            },
        )
    assert error.value.code == "SENSITIVE_INPUT"
    tab.handle_alert(accept=False, timeout=0.1)
