import base64
import functools
import http.server
import io
import json
import os
import threading
from pathlib import Path

import pytest
from PIL import Image

from cloud_browser.config import Settings
from cloud_browser.drission import DrissionAdapter
from cloud_browser.models import BrowserError

pytestmark = pytest.mark.browser


@pytest.fixture
def browser(tmp_path, monkeypatch, request):
    executable = os.getenv("CB_TEST_CHROMIUM")
    if not executable:
        pytest.skip("Set CB_TEST_CHROMIUM to a Chromium executable for real browser tests")
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(Path(__file__).parent / "fixtures")
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def fixture_url_only(url, *, dns_proxy=None):
        assert dns_proxy is None  # The local fixture never claims production isolation.
        if not url.startswith(base + "/"):
            raise BrowserError("INVALID_URL", "Test fixture only")

    # Narrow test-only override, never available through configuration or MCP.
    monkeypatch.setattr("cloud_browser.drission.validate_url", fixture_url_only)
    adapter = DrissionAdapter(
        Settings(
            development=True,
            data_dir=tmp_path,
            headless=True,
            chromium_path=executable,
            browser_proxy="",
        )
    )
    if getattr(request, "param", None) == "webmcp-experimental":
        adapter.cfg.webmcp_testing = True
    adapter.open("ses_test", base + "/browser.html")
    tid = adapter.list_tabs("ses_test")["selected_tab_id"]
    yield adapter, "ses_test", tid, base
    adapter.shutdown()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def node(adapter, sid, tid, name):
    result = adapter.observe(sid, tid, mode="interactive")
    nodes = [
        json.loads(line)
        for line in result["observation"]["interactive_snapshot"].splitlines()
        if line
    ]
    return result, next(n for n in nodes if n["name"] == name)


def test_real_accordion_inputs_select_check_and_scroll(browser):
    adapter, sid, tid, _ = browser
    observation, target = node(adapter, sid, tid, "Personal Information")
    assert target["expanded"] == "true"
    result = adapter.act(
        sid, tid, observation["revision"], {"type": "click", "node_id": target["node_id"]}
    )
    assert result["action_result"]["performed"]
    assert node(adapter, sid, tid, "Personal Information")[1]["expanded"] == "false"
    for name, action, expected in (
        ("Search", {"type": "fill", "text": "Godot"}, ("value", "Godot")),
        ("Order", {"type": "select", "value": "recent"}, ("value", "recent")),
        ("Consent", {"type": "check", "checked": True}, ("checked", True)),
    ):
        obs, target = node(adapter, sid, tid, name)
        adapter.act(sid, tid, obs["revision"], action | {"node_id": target["node_id"]})
        assert node(adapter, sid, tid, name)[1][expected[0]] == expected[1]
    old = adapter.observe(sid, tid, mode="interactive")
    result = adapter.act(
        sid, tid, old["revision"], {"type": "scroll", "delta_x": 0, "delta_y": 600}
    )
    assert result["revision"] > old["revision"]


def test_real_stale_replaced_dom_and_screenshot(browser):
    adapter, sid, tid, _ = browser
    obs, target = node(adapter, sid, tid, "Personal Information")
    state = adapter._tab(sid, tid)
    state.tab.run_js(
        "document.querySelector('#accordion').outerHTML=document.querySelector('#accordion').outerHTML"
    )
    with pytest.raises(BrowserError, match="target changed"):
        adapter.act(sid, tid, obs["revision"], {"type": "click", "node_id": target["node_id"]})
    shot = adapter.observe(sid, tid, mode="visual")
    assert shot["_image"]["mimeType"] == "image/jpeg"
    sid_image = shot["observation"]["screenshot"]["screenshot_id"]
    adapter.configure(sid, tid, {"viewport_width": 800, "viewport_height": 600})
    current = adapter.observe(sid, tid, mode="interactive")
    with pytest.raises(BrowserError) as exc:
        adapter.prepare(
            sid,
            tid,
            current["revision"],
            {"type": "click_at", "x": 10, "y": 10, "screenshot_id": sid_image},
        )
    assert exc.value.code == "STALE_SCREENSHOT"


def test_real_cursor_and_protected_screen(browser):
    adapter, sid, tid, _ = browser
    obs = adapter.observe(sid, tid, max_chars=256)
    cursor = obs["observation"]["next_cursor"]
    assert cursor
    assert adapter.observe(sid, tid, cursor=cursor)["revision"] == obs["revision"]
    state = adapter._tab(sid, tid)
    state.tab.run_js(
        "document.body.insertAdjacentHTML('afterbegin','<input type=password value=secret>')"
    )
    with pytest.raises(BrowserError) as exc:
        adapter.observe(sid, tid, mode="visual")
    assert exc.value.code == "AUTH_REQUIRED"


def test_real_popup_and_closed_tab(browser):
    adapter, sid, tid, _ = browser
    obs, target = node(adapter, sid, tid, "Open new tab")
    result = adapter.act(sid, tid, obs["revision"], {"type": "click", "node_id": target["node_id"]})
    added = result["action_result"]["new_tab_ids"]
    assert len(added) == 1
    assert len(adapter.list_tabs(sid)["tabs"]) == 2
    adapter.close(sid, "tab", added[0])
    with pytest.raises(BrowserError) as exc:
        adapter.observe(sid, added[0])
    assert exc.value.code == "TAB_NOT_FOUND"


def test_real_keypress_reload_and_no_change(browser):
    adapter, sid, tid, _ = browser
    obs, target = node(adapter, sid, tid, "Personal Information")
    adapter.act(
        sid,
        tid,
        obs["revision"],
        {"type": "keypress", "node_id": target["node_id"], "keys": ["ENTER"]},
    )
    assert node(adapter, sid, tid, "Personal Information")[1]["expanded"] == "false"
    loaded = adapter.navigate(sid, tid, "reload")
    assert loaded["revision"] > obs["revision"]
    assert node(adapter, sid, tid, "Personal Information")[1]["expanded"] == "true"
    obs, target = node(adapter, sid, tid, "Consent")
    unchanged = adapter.act(
        sid, tid, obs["revision"], {"type": "check", "node_id": target["node_id"], "checked": False}
    )
    assert unchanged["status"] == "no_change"
    assert not unchanged["action_result"]["performed"]
    assert adapter.navigate(sid, tid, "forward")["status"] == "no_change"


def test_real_capture_budget_and_stale_cursor(browser):
    adapter, sid, tid, _ = browser
    obs = adapter.observe(sid, tid, max_chars=256)
    cursor = obs["observation"]["next_cursor"]
    adapter.configure(sid, tid, {"viewport_width": 800, "viewport_height": 600})
    with pytest.raises(BrowserError) as exc:
        adapter.observe(sid, tid, cursor=cursor)
    assert exc.value.code == "CURSOR_STALE"
    adapter.cfg.max_capture_pixels = 786432
    with pytest.raises(BrowserError) as exc:
        adapter.observe(sid, tid, mode="visual", full_page=True)
    assert exc.value.code == "RESOURCE_PRESSURE"


def test_real_coordinate_prefers_dom_and_pixel_probe(browser):
    adapter, sid, tid, base = browser
    shot = adapter.observe(sid, tid, mode="visual")
    _, target = node(adapter, sid, tid, "Personal Information")
    rect = target["rect"]
    with pytest.raises(BrowserError) as exc:
        adapter.prepare(
            sid,
            tid,
            shot["revision"],
            {
                "type": "click_at",
                "x": int(rect["x"] + rect["width"] / 2),
                "y": int(rect["y"] + rect["height"] / 2),
                "screenshot_id": shot["observation"]["screenshot"]["screenshot_id"],
            },
        )
    assert exc.value.code == "DOM_TARGET_AVAILABLE"
    adapter.navigate(sid, tid, "goto", base + "/visual-probe.html")
    assert not adapter._tab(sid, tid).data["text"]
    shot = adapter.observe(sid, tid, mode="visual")
    image = Image.open(io.BytesIO(base64.b64decode(shot["_image"]["data"])))
    assert image.size == (1024, 768)
    blue = image.getpixel((230, 150))
    assert blue[2] > 180 and blue[0] < 70


def test_real_screenshot_tracks_scrolled_viewport(browser):
    adapter, sid, tid, _ = browser
    state = adapter._tab(sid, tid)
    state.tab.run_js(
        "document.body.insertAdjacentHTML('beforeend','<div style=\"position:absolute;top:0;left:750px;width:200px;height:100px;background:red\"></div>')"
    )
    first = adapter.observe(sid, tid, mode="visual")
    image = Image.open(io.BytesIO(base64.b64decode(first["_image"]["data"])))
    assert image.getpixel((800, 20))[0] > 230 and image.getpixel((800, 20))[1] < 30
    adapter.act(sid, tid, first["revision"], {"type": "scroll", "delta_x": 0, "delta_y": 600})
    after = adapter.observe(sid, tid, mode="visual")
    image = Image.open(io.BytesIO(base64.b64decode(after["_image"]["data"])))
    assert image.getpixel((800, 20))[1] > 200


def test_real_covered_element_does_not_receive_guessed_click(browser):
    adapter, sid, tid, _ = browser
    state = adapter._tab(sid, tid)
    state.tab.run_js(
        "document.body.insertAdjacentHTML('beforeend','<div style=\"position:fixed;inset:0;z-index:999;background:rgba(0,0,0,.1)\"></div>')"
    )
    obs, target = node(adapter, sid, tid, "Personal Information")
    with pytest.raises(BrowserError) as exc:
        adapter.act(sid, tid, obs["revision"], {"type": "click", "node_id": target["node_id"]})
    assert exc.value.code == "NODE_NOT_ACTIONABLE"
    assert node(adapter, sid, tid, "Personal Information")[1]["expanded"] == "true"
