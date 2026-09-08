import io
import json
import time

import pytest
from starlette.datastructures import UploadFile
from test_browser import browser as browser
from test_browser import node

from cloud_browser.authentication import AuthRule
from cloud_browser.models import BrowserError
from cloud_browser.service import BrowserService
from cloud_browser.store import Store

pytestmark = pytest.mark.browser


def test_hidden_form_changes_invalidate_revision_without_leaking_values(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("""document.body.innerHTML = `<form action='/submit' method=post>
      <label>Review <input name=review value=hello></label>
      <input type=hidden name=csrf value=internal-hidden-value>
      <input name=offscreen style='position:absolute;top:2000px' value=private-offscreen-value>
      <button>Publish review</button></form>`;""")
    obs, target = node(adapter, sid, tid, "Publish review")
    action = {"type": "click", "node_id": target["node_id"]}
    prepared = adapter.prepare(sid, tid, obs["revision"], action)
    assert prepared["destination_kind"] == "declared_form"
    assert set(prepared["data_sent"]) == {"Review", "csrf", "offscreen"}
    assert "internal-hidden-value" not in json.dumps(obs)
    assert "private-offscreen-value" not in json.dumps(obs)
    state = adapter._tab(sid, tid)
    assert "internal-hidden-value" not in json.dumps(state.data)
    tab.run_js("document.querySelector('[name=csrf]').value='changed-hidden-value'")
    with pytest.raises(BrowserError) as exc:
        adapter.prepare(sid, tid, obs["revision"], action)
    assert exc.value.code == "STALE_NODE"


def test_form_budget_blocks_automatic_actions(browser):
    adapter, sid, tid, _ = browser
    adapter._tab(sid, tid).tab.run_js("document.querySelector('#query').value='x'.repeat(250001)")
    obs, target = node(adapter, sid, tid, "Personal Information")
    with pytest.raises(BrowserError) as exc:
        adapter.prepare(sid, tid, obs["revision"], {"type": "click", "node_id": target["node_id"]})
    assert exc.value.code == "UNSUPPORTED_OPERATION"


def test_same_origin_iframe_text_and_sensitive_child_guard(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("""document.body.innerHTML='<h1>Parent</h1><iframe id=child></iframe>';
      document.querySelector('iframe').contentDocument.body.innerHTML='<h2>Readable child</h2><p>Frame details</p>';""")
    obs = adapter.observe(sid, tid, mode="semantic")
    assert "Readable child" in obs["observation"]["semantic_snapshot"]
    assert obs["observation"]["readable_frames"] == 1
    tab.run_js(
        "document.querySelector('iframe').contentDocument.body.innerHTML='<input type=password value=never-return-this>'"
    )
    for mode in ("semantic", "visual", "interactive"):
        with pytest.raises(BrowserError) as exc:
            adapter.observe(sid, tid, mode=mode)
        assert exc.value.code == "AUTH_REQUIRED"


def test_authentication_only_verified_by_exact_operator_rule(browser):
    adapter, sid, tid, base = browser
    tab = adapter._tab(sid, tid).tab
    adapter.cfg.auth_rules = {
        base: AuthRule(success_selector="#signed-in", failure_selector="#failed")
    }
    adapter.focus(sid, tid)
    assert adapter.resume(sid, tid, base)["authentication"]["authenticated"] is None
    tab.run_js("document.body.innerHTML='<p id=signed-in>Welcome</p>'")
    adapter.focus(sid, tid)
    result = adapter.resume(sid, tid, base)
    assert result["authentication"]["authenticated"] is True
    assert result["authentication"]["verification"] == "operator_rule"
    adapter.focus(sid, tid)
    assert (
        adapter.resume(sid, tid, "https://other.example")["authentication"]["authenticated"] is None
    )
    tab.run_js("document.body.insertAdjacentHTML('beforeend','<p id=failed>Sign-in failed</p>')")
    adapter.focus(sid, tid)
    with pytest.raises(BrowserError) as exc:
        adapter.resume(sid, tid, base)
    assert exc.value.code == "AUTH_FAILED"
    assert adapter._session(sid)["paused"]


async def test_real_file_input_uses_verified_staged_file(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("""document.body.innerHTML='<label>Attach document <input type=file id=file></label><p id=events>0</p>';
      window.fileEvents=0; document.querySelector('#file').onchange=()=>document.querySelector('#events').textContent=++window.fileEvents;""")

    class AdapterWorker:
        async def call(self, method, **kwargs):
            return getattr(adapter, method)(**kwargs)

    store = Store(adapter.cfg.data_dir / "upload-integration.sqlite3")
    service = BrowserService(adapter.cfg, store, AdapterWorker())
    service.sessions[sid] = {"expires": time.time() + 600, "uncertain": False}
    item = await service.stage_upload(
        UploadFile(io.BytesIO(b"controlled fixture file"), filename="fixture.txt")
    )
    try:
        obs, target = node(adapter, sid, tid, "Attach document")
        action = {"type": "upload", "node_id": target["node_id"], "upload_ids": [item["upload_id"]]}
        args = dict(session_id=sid, tab_id=tid, expected_revision=obs["revision"], action=action)
        proposed = await service.call("act", **args)
        assert proposed["status"] == "confirmation_required"
        assert proposed["confirmation"]["files"][0]["sha256"] == item["sha256"]
        assert "path" not in proposed["confirmation"]["files"][0]
        assert tab.run_js("return window.fileEvents") == 0
        token = proposed["confirmation"]["confirmation_token"]
        assert (await service.call("act", **args, confirmation_token=token))[
            "status"
        ] == "confirmation_required"
        assert tab.run_js("return window.fileEvents") == 0
        await service.approve(next(iter(service.pending)), True)
        done = await service.call("act", **args, confirmation_token=token)
        assert done["action_result"]["performed"]
        assert tab.run_js("return document.querySelector('#file').files[0].name") == "fixture.txt"
        assert tab.run_js("return window.fileEvents") == 1
        assert (await service.call("act", **args, confirmation_token=token))["error"][
            "code"
        ] == "CONFIRMATION_USED"
        assert tab.run_js("return window.fileEvents") == 1
    finally:
        service.uploads.close()
        store.close()


@pytest.mark.parametrize("browser", [None, "webmcp-experimental"], indirect=True)
def test_native_webmcp_real_registration_call_and_staleness(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    supported = tab.run_js("return !!(document.modelContext || navigator.modelContext)")
    if not supported:
        # Domain support and page API availability are separate capabilities.
        try:
            listed = adapter.list_page_tools(sid, tid)
            assert listed["page_tools"] == []
        except BrowserError as exc:
            assert exc.code == "UNSUPPORTED_OPERATION"
        pytest.skip("Installed Chromium does not expose native page WebMCP registration")
    tab.run_js("""window.pageToolCalls=0;
      (document.modelContext || navigator.modelContext).registerTool({name:'fixture_search', description:'Controlled test only',
        inputSchema:{type:'object',properties:{query:{type:'string'}},required:['query'],additionalProperties:false},
        execute:async args=>{window.pageToolCalls++; return {query:args.query,count:window.pageToolCalls};}});""")
    time.sleep(0.15)
    listed = adapter.list_page_tools(sid, tid)
    assert any(t["name"] == "fixture_search" for t in listed["page_tools"])
    action = {"type": "page_tool", "tool_name": "fixture_search", "arguments": {"query": "Godot"}}
    assert adapter.prepare(sid, tid, listed["revision"], action)["requires_confirmation"]
    assert tab.run_js("return window.pageToolCalls") == 0
    done = adapter.act(sid, tid, listed["revision"], action)
    assert done["status"] == "ok" and done["page_tool_result"]["untrusted"]
    assert "Godot" in json.dumps(done["page_tool_result"]["output"])
    assert tab.run_js("return window.pageToolCalls") == 1
    tab.run_js("""(document.modelContext || navigator.modelContext).registerTool({
      name:'another_tool',description:'Additional registration invalidates old inventory',execute:async()=>({ok:true})});""")
    time.sleep(0.1)
    with pytest.raises(BrowserError) as exc:
        adapter.prepare(sid, tid, listed["revision"], action)
    assert exc.value.code == "PAGE_TOOL_STALE"
    assert tab.run_js("return window.pageToolCalls") == 1
    adapter.focus(sid, tid)
    adapter.resume(sid, tid)
    with pytest.raises(BrowserError) as exc:
        adapter.prepare(sid, tid, adapter._tab(sid, tid).revision, action)
    assert exc.value.code == "PAGE_TOOL_STALE"
