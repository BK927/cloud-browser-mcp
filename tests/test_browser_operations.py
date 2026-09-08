import json
import time

import pytest
from test_browser import browser as browser

from cloud_browser.models import BrowserError

pytestmark = pytest.mark.browser


def nodes(adapter, sid, tid, **options):
    result = adapter.observe(sid, tid, mode="interactive", max_chars=100000, **options)
    return result, [
        json.loads(line) for line in result["observation"]["interactive_snapshot"].splitlines()
    ]


def test_sequential_input_modifiers_and_multiple_select(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.innerHTML='<input aria-label=Draft><select multiple aria-label=Colours><option value=red>Red</option><option value=blue>Blue</option></select>'"
    )
    seen, items = nodes(adapter, sid, tid)
    adapter.act(
        sid,
        tid,
        seen["revision"],
        {"type": "type", "node_id": items[0]["node_id"], "text": "한글 abc", "interval_ms": 1},
    )
    assert tab.run_js("return document.querySelector('input').value") == "한글 abc"
    seen, items = nodes(adapter, sid, tid)
    adapter.act(
        sid,
        tid,
        seen["revision"],
        {
            "type": "keypress",
            "node_id": items[0]["node_id"],
            "keys": ["A"],
            "modifiers": ["CONTROL"],
        },
    )
    seen, items = nodes(adapter, sid, tid)
    adapter.act(
        sid,
        tid,
        seen["revision"],
        {"type": "type", "node_id": items[0]["node_id"], "text": "replacement"},
    )
    assert tab.run_js("return document.querySelector('input').value") == "replacement"
    seen, items = nodes(adapter, sid, tid)
    adapter.act(
        sid,
        tid,
        seen["revision"],
        {"type": "select_multiple", "node_id": items[1]["node_id"], "values": ["red", "blue"]},
    )
    assert tab.run_js(
        "return [...document.querySelector('select').selectedOptions].map(o=>o.value)"
    ) == ["red", "blue"]


def test_scoped_query_wait_and_global_clipboard_rejection(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.innerHTML='<section id=one><input aria-label=Draft><button>Alpha</button></section><section><button>Beta</button></section>'"
    )
    seen, items = nodes(
        adapter, sid, tid, query={"scope": "#one", "role": "button", "name": "Alpha"}
    )
    assert len(items) == 1 and items[0]["name"] == "Alpha"
    assert adapter.wait(sid, tid, {"type": "element", "query": {"selector": "#one button"}}, 100)[
        "wait"
    ]["matched"]
    assert adapter.wait(sid, tid, {"type": "element", "query": {"selector": ".missing"}}, 100)[
        "wait"
    ]["timed_out"]
    seen, items = nodes(adapter, sid, tid, query={"selector": "input"})
    with pytest.raises(BrowserError) as error:
        adapter.prepare(
            sid,
            tid,
            seen["revision"],
            {
                "type": "keypress",
                "node_id": items[0]["node_id"],
                "keys": ["V"],
                "modifiers": ["CONTROL"],
            },
        )
    assert error.value.code == "POLICY_BLOCKED"


def test_right_middle_and_pointer_drag(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("""document.body.innerHTML='<button style="width:120px;height:50px">Mouse target</button><div id=source tabindex=0 style="position:fixed;left:20px;top:150px;width:100px;height:70px">Source</div><div id=dest tabindex=0 style="position:fixed;left:300px;top:150px;width:100px;height:70px">Destination</div>';
      window.mouse=[];document.querySelector('button').oncontextmenu=e=>{e.preventDefault();window.mouse.push('right')};document.querySelector('button').onauxclick=e=>{if(e.button===1)window.mouse.push('middle')};
      window.dragged=false;document.querySelector('#source').onpointerdown=()=>window.dragged=true;document.querySelector('#dest').onpointerup=()=>{if(window.dragged)document.querySelector('#dest').textContent='Dropped'};""")
    for action in ("right_click", "middle_click"):
        seen, items = nodes(adapter, sid, tid)
        adapter.act(sid, tid, seen["revision"], {"type": action, "node_id": items[0]["node_id"]})
    assert tab.run_js("return window.mouse") == ["right", "middle"]
    seen, items = nodes(adapter, sid, tid)
    source = next(n for n in items if n["name"] == "Source")
    dest = next(n for n in items if n["name"] == "Destination")
    adapter.act(
        sid,
        tid,
        seen["revision"],
        {"type": "drag", "node_id": source["node_id"], "target_node_id": dest["node_id"]},
    )
    assert tab.run_js("return document.querySelector('#dest').textContent") == "Dropped"


def test_dialog_query_response_and_bounded_logs(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.innerHTML='<button onclick=\"window.choice=confirm(String(42))\">Ask</button>'"
    )
    seen, items = nodes(adapter, sid, tid)
    result = adapter.act(
        sid, tid, seen["revision"], {"type": "click", "node_id": items[0]["node_id"]}
    )
    assert result["dialog"]["type"] == "confirm"
    info = adapter.dialog_info(sid, tid)
    assert tab._has_alert and info["dialog"]["message"] == "42"
    action = {"type": "dialog", "operation": "dismiss", "dialog_id": info["dialog"]["dialog_id"]}
    assert adapter.prepare(sid, tid, info["revision"], action)["requires_confirmation"]
    adapter.act(sid, tid, info["revision"], action)
    assert tab.run_js("return window.choice") is False
    tab.run_js("for(let i=0;i<80;i++)console.log('ghp_'+ 'x'.repeat(30))")
    time.sleep(0.1)
    logs = adapter.logs(sid, tid, limit=64)["logs"]
    assert 0 < len(logs["records"]) <= 64 and "ghp_" not in json.dumps(logs)


def test_isolated_download_and_safe_exports(browser):
    adapter, sid, tid, base = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.innerHTML='<h1>Export article</h1><a download=report.txt href=\"'+arguments[0]+'/download.txt\">Download report</a>'",
        base,
    )
    seen, items = nodes(adapter, sid, tid)
    action = {"type": "click", "node_id": items[0]["node_id"]}
    assert adapter.prepare(sid, tid, seen["revision"], action)["requires_confirmation"]
    adapter.act(sid, tid, seen["revision"], action)
    assert adapter.wait(sid, tid, {"type": "download", "state": "completed"}, 3000)["wait"][
        "matched"
    ]
    files = adapter.artifacts(sid)["artifacts"]
    assert len(files) == 1 and files[0]["state"] == "completed"
    assert "isolated download" in adapter.artifacts(sid, "get", files[0]["artifact_id"])["text"]
    exported = adapter.artifacts(sid, "export", tab_id=tid, format="html")["artifact"]
    text = adapter.artifacts(sid, "get", exported["artifact_id"])["text"]
    assert "Export article" in text and "<script" not in text and "<a " not in text
    image = adapter.artifacts(sid, "export", tab_id=tid, format="image")["artifact"]
    assert adapter.artifacts(sid, "get", image["artifact_id"])["_image"]["mimeType"] == "image/jpeg"
    adapter.artifacts(sid, "clear")
    assert adapter.artifacts(sid)["artifacts"] == []
