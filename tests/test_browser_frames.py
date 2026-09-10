import base64
import io
import json
import time

import pytest
from PIL import Image
from test_browser import browser as browser

from cloud_browser.models import BrowserError

pytestmark = pytest.mark.browser


def test_frame_observation_action_and_replacement(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("""document.body.innerHTML='<h1>Parent</h1><iframe id=child style="width:400px;height:250px"></iframe>';
      document.querySelector('iframe').contentDocument.body.innerHTML='<h2>Child article</h2><label>Child search <input type=search></label><button onclick="this.textContent=String(42)">Child button</button>';""")
    seen = adapter.observe(sid, tid, max_chars=100000)
    frames = seen["observation"]["frames"]
    assert len(frames) == 1 and frames[0]["readable"], frames
    assert "Child article" in seen["observation"]["semantic_snapshot"]
    nodes = [json.loads(line) for line in seen["observation"]["interactive_snapshot"].splitlines()]
    button = next(n for n in nodes if n["name"] == "Child button")
    assert button["frame_id"] == frames[0]["frame_id"]
    result = adapter.act(
        sid, tid, seen["revision"], {"type": "click", "node_id": button["node_id"]}
    )
    assert result["action_result"]["performed"]
    assert (
        tab.run_js(
            "return document.querySelector('iframe').contentDocument.querySelector('button').textContent"
        )
        == "42"
    )
    tab.run_js("document.querySelector('iframe').srcdoc='<p>New document</p>'")
    time.sleep(0.1)
    with pytest.raises(BrowserError) as error:
        adapter.prepare(sid, tid, seen["revision"], {"type": "click", "node_id": button["node_id"]})
    assert error.value.code == "STALE_NODE"


def test_normal_frame_visible_sensitive_frame_masked(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("""document.body.innerHTML='<iframe id=safe style="position:fixed;left:20px;top:20px;width:150px;height:100px;border:0"></iframe><iframe id=secret style="position:fixed;left:220px;top:20px;width:150px;height:100px;border:0"></iframe>';
      document.querySelector('#safe').contentDocument.body.innerHTML='<body style="background:lime">Visible frame</body>';
      document.querySelector('#secret').contentDocument.body.innerHTML='<body style="background:red"><input type=password value=must-not-appear></body>';""")
    seen = adapter.observe(sid, tid, mode="visual")
    assert any(f["reason"] == "SENSITIVE_FRAME" for f in seen["observation"]["frames"])
    assert "must-not-appear" not in json.dumps(seen)
    image = Image.open(io.BytesIO(base64.b64decode(seen["_image"]["data"])))
    assert image.getpixel((260, 60)) == (0, 0, 0)
    assert image.getpixel((80, 60))[1] > 150


def test_cross_origin_frame_input_uses_its_own_context(browser):
    adapter, sid, tid, base = browser
    tab = adapter._tab(sid, tid).tab
    cross_origin = base.replace("127.0.0.1", "localhost") + "/frame-child.html"
    tab.run_js(
        "document.body.innerHTML='<iframe style=width:600px;height:400px></iframe>';document.querySelector('iframe').src=arguments[0]",
        cross_origin,
    )
    # Cross-origin target attachment can follow load by more than 300 ms. Wait
    # read-only for the actual child control; never repeat the eventual input.
    ready = adapter.wait(
        sid,
        tid,
        {
            "type": "element",
            "query": {"role": "searchbox", "name": "Frame query"},
            "state": "visible",
        },
        5000,
    )
    assert ready["wait"]["matched"], ready["wait"]
    seen = adapter.observe(sid, tid, max_chars=100000)
    assert seen["observation"]["frames"][0]["readable"], seen["observation"]["frames"]
    nodes = [json.loads(line) for line in seen["observation"]["interactive_snapshot"].splitlines()]
    query = next(n for n in nodes if n["name"] == "Frame query")
    result = adapter.act(
        sid,
        tid,
        seen["revision"],
        {"type": "fill", "node_id": query["node_id"], "text": "cross-frame value"},
    )
    assert result["action_result"]["performed"]
    after = adapter.observe(sid, tid, max_chars=100000)
    assert "cross-frame value" in after["observation"]["interactive_snapshot"]


def test_legacy_frameset_has_distinct_frames_and_nodes(browser):
    adapter, sid, tid, base = browser
    adapter.navigate(sid, tid, "goto", base + "/frameset.html")
    seen = adapter.observe(sid, tid, max_chars=100000)
    frames = seen["observation"]["frames"]
    assert len(frames) == 2 and all(f["readable"] for f in frames), frames
    nodes = [json.loads(line) for line in seen["observation"]["interactive_snapshot"].splitlines()]
    buttons = [n for n in nodes if n["name"] == "Frame action"]
    assert len(buttons) == 2 and buttons[0]["node_id"] != buttons[1]["node_id"]
    result = adapter.act(
        sid, tid, seen["revision"], {"type": "click", "node_id": buttons[1]["node_id"]}
    )
    assert result["action_result"]["performed"]


def test_nested_frame_click_and_ancestor_occlusion(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("""document.body.innerHTML='<iframe style="width:600px;height:400px"></iframe>';
      const d=document.querySelector('iframe').contentDocument;
      d.body.innerHTML='<iframe style="width:400px;height:250px"></iframe>';
      d.querySelector('iframe').contentDocument.body.innerHTML='<button onclick="this.textContent=String(77)">Nested button</button>';""")
    seen = adapter.observe(sid, tid, max_chars=100000)
    frames = seen["observation"]["frames"]
    assert len(frames) == 2 and frames[1]["parent_frame_id"] == frames[0]["frame_id"]
    nodes = [json.loads(line) for line in seen["observation"]["interactive_snapshot"].splitlines()]
    button = next(n for n in nodes if n["name"] == "Nested button")
    adapter.act(sid, tid, seen["revision"], {"type": "click", "node_id": button["node_id"]})
    assert "77" in adapter.observe(sid, tid)["observation"]["interactive_snapshot"]
    tab.run_js(
        "document.body.insertAdjacentHTML('beforeend','<div style=position:fixed;inset:0;z-index:999;background:white>Cover</div>')"
    )
    current = adapter.observe(sid, tid)
    node = next(
        json.loads(line)
        for line in current["observation"]["interactive_snapshot"].splitlines()
        if json.loads(line)["name"] == "77"
    )
    with pytest.raises(BrowserError) as error:
        adapter.act(sid, tid, current["revision"], {"type": "click", "node_id": node["node_id"]})
    assert error.value.code == "NODE_NOT_ACTIONABLE"


def test_dynamic_pixels_do_not_stale_same_dom_coordinate_target(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("""document.body.innerHTML='<button aria-expanded=true style="position:fixed;left:20px;top:20px;width:160px;height:60px" onclick="this.setAttribute(String.fromCharCode(97,114,105,97,45,101,120,112,97,110,100,101,100),false)">Panel</button><canvas style="position:fixed;left:300px;top:10px" width=200 height=200></canvas>';
      const c=document.querySelector('canvas').getContext('2d');
      window.paint=setInterval(()=>{c.fillStyle='rgb('+Math.floor(Math.random()*255)+',0,0)';c.fillRect(0,0,200,200)},16);""")
    shot = adapter.observe(sid, tid, mode="visual")
    action = {
        "type": "click_at",
        "x": 80,
        "y": 40,
        "screenshot_id": shot["observation"]["screenshot"]["screenshot_id"],
    }
    time.sleep(0.05)
    prepared = adapter.prepare(sid, tid, shot["revision"], action)
    assert prepared["resolved_node_id"]
    adapter.act(sid, tid, shot["revision"], action)
    assert (
        tab.run_js("return document.querySelector('button').getAttribute('aria-expanded')")
        == "false"
    )
    tab.run_js("clearInterval(window.paint)")
