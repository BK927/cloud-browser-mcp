"""Rendered privacy edges that do not have ordinary control bounding boxes."""

import base64
import io
import json
import time

import pytest
from PIL import Image
from test_browser import browser as browser
from test_browser_observation_repair import install, snapshot
from test_browser_target_fidelity import nodes

from cloud_browser.authentication import AuthRule
from cloud_browser.models import BrowserError

pytestmark = pytest.mark.browser


@pytest.mark.parametrize(
    "field",
    [
        '<label for="field">Verification code</label><input id="field" value="PRIVATE-CODE">',
        '<label for="field">인증 코드</label><input id="field" value="PRIVATE-CODE">',
        '<span id="caption">OTP</span><input id="field" aria-labelledby="caption" value="PRIVATE-CODE">',
        '<label>Verification <strong>code</strong><input id="field" value="PRIVATE-CODE"></label>',
    ],
    ids=["linked-label", "korean-label", "aria-labelledby", "wrapping-label"],
)
def test_label_only_sensitive_fields_withhold_form_without_losing_public_body(browser, field):
    adapter, sid, tid = install(
        browser,
        "<article><p>Public article body</p><button>Public button</button></article>"
        "<form><p>PRIVATE-FORM-TEXT</p>"
        + field
        + '<input name="draft" value="PRIVATE-DRAFT"><button>Private submit</button></form>',
    )
    raw = snapshot(browser)
    assert not raw["protected"] and not raw["privacy_incomplete"]
    assert raw["has_sensitive_regions"] and raw["protected_regions"]
    assert "Public article body" in raw["semantic_text"]
    assert "PRIVATE-" not in json.dumps(raw)
    assert raw["_forms"] == [] and raw["_form_states"] == []
    assert [entry["name"] for entry in raw["nodes"]] == ["Public button"]
    observed = adapter.observe(sid, tid, mode="auto")
    assert "Public article body" in observed["observation"]["semantic_snapshot"]
    assert "PRIVATE-" not in json.dumps(observed)
    assert "Private submit" not in json.dumps(observed)


def test_open_shadow_connected_label_is_checked_before_private_values(browser):
    adapter, sid, tid = install(
        browser,
        '<p>Public article body</p><div id="host"></div>',
        "document.querySelector('#host').attachShadow({mode:'open'}).innerHTML="
        + json.dumps(
            "<p>Public shadow body</p><button>Shadow public button</button>"
            '<form><label for="field">Verification code</label>'
            '<input id="field" value="PRIVATE-CODE"><p>PRIVATE-FORM-TEXT</p></form>'
        )
        + ";",
    )
    raw = snapshot(browser)
    assert raw["has_sensitive_regions"] and raw["protected_regions"]
    assert "Public shadow body" in raw["semantic_text"]
    assert "PRIVATE-" not in json.dumps(raw)
    assert [entry["name"] for entry in raw["nodes"]] == ["Shadow public button"]
    observed = adapter.observe(sid, tid, mode="semantic")
    assert "Public shadow body" in observed["observation"]["semantic_snapshot"]
    assert "PRIVATE-" not in json.dumps(observed)


@pytest.mark.parametrize(
    "field",
    [
        '<label for="field">'
        + "<span>Ordinary label fragment</span>" * 40
        + 'Verification code</label><input id="field" value="PRIVATE-CODE">',
        '<label for="field">'
        + "A" * 1100
        + ' Verification code</label><input id="field" value="PRIVATE-CODE">',
        "".join(f'<span id="label{number}">Label</span>' for number in range(8))
        + '<span id="label8">OTP</span><input id="field" aria-labelledby="'
        + " ".join(f"label{number}" for number in range(9))
        + '" value="PRIVATE-CODE">',
    ],
    ids=["label-node-budget", "label-character-budget", "aria-reference-budget"],
)
def test_sensitive_label_inspection_budget_withholds_field_or_form(browser, field):
    adapter, sid, tid = install(
        browser,
        "<p>Public article body</p><button>Public button</button><form>"
        + field
        + '<input name="draft" value="PRIVATE-DRAFT"><button>Private submit</button></form>',
    )
    raw = snapshot(browser)
    assert not raw["protected"] and not raw["privacy_incomplete"]
    assert raw["has_sensitive_regions"]
    assert "Public article body" in raw["semantic_text"]
    assert "PRIVATE-" not in json.dumps(raw)
    assert [entry["name"] for entry in raw["nodes"]] == ["Public button"]
    observed = adapter.observe(sid, tid, mode="semantic")
    assert "Public article body" in observed["observation"]["semantic_snapshot"]
    assert "PRIVATE-" not in json.dumps(observed)


@pytest.mark.parametrize(
    "protected_html",
    [
        '<form id="private" style="display:contents">PRIVATE-DIRECT-TEXT'
        '<br><input type="password" value="PRIVATE-PASSWORD"></form>',
        '<div id="private" contenteditable="true" aria-label="API key" '
        'style="display:contents">PRIVATE-DIRECT-TEXT</div>',
    ],
    ids=["contents-form", "contents-editable"],
)
def test_boxless_protected_direct_text_is_masked_by_actual_range_geometry(browser, protected_html):
    adapter, sid, tid = install(
        browser,
        '<p>Public article body</p><section style="position:absolute;left:350px;top:300px;'
        'font:24px sans-serif">' + protected_html + "</section>",
    )
    tab = adapter._tab(sid, tid).tab
    private_rects = tab.run_js(
        "const range=document.createRange();"
        "range.selectNode(document.querySelector('#private').firstChild);"
        "return [...range.getClientRects()].filter(r=>r.width>0&&r.height>0)"
        ".map(r=>({x:r.x,y:r.y,width:r.width,height:r.height}));"
    )
    assert private_rects  # A zero-box wrapper still paints text in the viewport.
    raw = snapshot(browser)
    assert "Public article body" in raw["semantic_text"] and "PRIVATE-" not in json.dumps(raw)
    for rect in private_rects:
        assert any(
            region["x"] <= rect["x"]
            and region["y"] <= rect["y"]
            and region["x"] + region["width"] >= rect["x"] + rect["width"]
            and region["y"] + region["height"] >= rect["y"] + rect["height"]
            for region in raw["protected_regions"]
        )
    try:
        captured = adapter.observe(sid, tid, mode="visual")
    except BrowserError as error:
        assert error.code == "SENSITIVE_SCREEN"
        assert adapter._tab(sid, tid).screenshot is None
        return
    assert captured["observation"]["screenshot"]["masked_regions"]
    image = Image.open(io.BytesIO(base64.b64decode(captured["_image"]["data"]))).convert("RGB")
    for rect in private_rects:
        center = (int(rect["x"] + rect["width"] / 2), int(rect["y"] + rect["height"] / 2))
        assert image.getpixel(center) == (0, 0, 0)


@pytest.mark.parametrize("pseudo", ["before", "after"])
@pytest.mark.parametrize("owner", ["form", "editable"])
def test_protected_pseudo_content_outside_box_rejects_capture(browser, pseudo, owner):
    if owner == "form":
        private = (
            '<form><input type="password" value="PRIVATE-PASSWORD">'
            '<span id="decorated" data-private="PRIVATE-PAINTED-TEXT">Label</span></form>'
        )
    else:
        private = (
            '<div contenteditable="true" aria-label="API key">'
            '<span id="decorated" data-private="PRIVATE-PAINTED-TEXT">PRIVATE-EDITABLE</span>'
            "</div>"
        )
    adapter, sid, tid = install(
        browser,
        "<p>Public article body</p>" + private,
        "const style=document.createElement('style');style.textContent="
        + json.dumps(
            f"#decorated::{pseudo}{{content:attr(data-private);position:fixed;"
            "left:500px;top:400px;font:24px sans-serif;}"
        )
        + ";document.head.append(style);",
    )
    raw = snapshot(browser)
    assert "Public article body" in raw["semantic_text"] and "PRIVATE-" not in json.dumps(raw)
    assert raw["privacy_mask_unsafe"] or any(
        not region["mask_safe"] for region in raw["protected_regions"]
    )
    with pytest.raises(BrowserError) as error:
        adapter.observe(sid, tid, mode="visual")
    assert error.value.code == "SENSITIVE_SCREEN"
    assert adapter._tab(sid, tid).screenshot is None


def test_cancelled_ascii_keydown_does_not_insert_characters(browser):
    adapter, sid, tid = install(
        browser,
        '<input aria-label="Draft">',
        "window.events=[];const input=document.querySelector('input');"
        "input.addEventListener('keydown',event=>{event.preventDefault();"
        "window.events.push([event.type,event.key,event.isTrusted]);});"
        "input.addEventListener('keyup',event=>"
        "window.events.push([event.type,event.key,event.isTrusted]));"
        "input.addEventListener('input',event=>window.events.push([event.type]));",
    )
    seen, entries = nodes(adapter, sid, tid)
    result = adapter.act(
        sid,
        tid,
        seen["revision"],
        {"type": "type", "text": "aB", "node_id": entries[0]["node_id"]},
    )
    tab = adapter._tab(sid, tid).tab
    assert tab.run_js("return document.querySelector('input').value") == ""
    assert tab.run_js("return window.events") == [
        ["keydown", "a", True],
        ["keyup", "a", True],
        ["keydown", "B", True],
        ["keyup", "B", True],
    ]
    assert result["action_result"]["typing_semantics"] == (
        "ascii-key-events-unicode-text-insertion"
    )


def assert_private_frame_pixels_masked(observed, center=(520, 340)):
    assert observed["observation"]["screenshot"]["masked_regions"]
    image = Image.open(io.BytesIO(base64.b64decode(observed["_image"]["data"]))).convert("RGB")
    assert image.getpixel(center) == (0, 0, 0)


def test_frame_owned_by_protected_form_never_collects_child_body_or_nodes(browser):
    adapter, sid, tid = install(
        browser,
        "<p>Public article body</p><button>Public button</button><form>"
        '<input type="password" value="PRIVATE-PASSWORD">'
        '<iframe id="child" style="position:fixed;left:450px;top:300px;'
        'width:180px;height:100px;border:0" '
        'srcdoc="<p>PRIVATE-OTP</p><button>Private recovery</button>"></iframe></form>',
    )
    adapter.cfg.iframe_screenshot_policy = "inspect"
    tab = adapter._tab(sid, tid).tab
    deadline = time.monotonic() + 3
    while not tab.run_js(
        "return document.querySelector('#child').contentDocument?.body?.textContent"
        ".includes('PRIVATE-OTP') || false"
    ):
        assert time.monotonic() < deadline, "Controlled srcdoc did not finish loading"
        time.sleep(0.02)
    observed = adapter.observe(sid, tid, mode="auto", max_chars=100000)
    assert "Public article body" in observed["observation"]["semantic_snapshot"]
    assert "PRIVATE-" not in json.dumps(observed) and "Private recovery" not in json.dumps(observed)
    frames = observed["observation"]["frames"]
    assert len(frames) == 1 and frames[0]["reason"] == "PROTECTED_PARENT"
    assert not frames[0]["readable"] and not frames[0]["actionable"]
    state = adapter._tab(sid, tid)
    assert all("PRIVATE-" not in json.dumps(child.data) for child in state.frame_states.values())
    assert_private_frame_pixels_masked(adapter.observe(sid, tid, mode="visual"))


def test_frame_moved_into_protected_form_rejects_old_node_and_scoped_reads(browser):
    adapter, sid, tid = install(
        browser,
        '<p>Public article body</p><form id="private"><input type="password"></form>'
        '<iframe id="child" style="position:fixed;left:450px;top:300px;'
        'width:180px;height:100px;border:0"></iframe>',
        "document.querySelector('#child').contentDocument.body.innerHTML="
        "'<p>Ordinary frame body</p><button>Frame action</button>';",
    )
    adapter.cfg.iframe_screenshot_policy = "inspect"
    seen, entries = nodes(adapter, sid, tid)
    target = next(entry for entry in entries if entry["name"] == "Frame action")
    frame_id = target["frame_id"]
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "const frame=document.querySelector('#child');"
        "document.querySelector('#private').appendChild(frame);"
        "frame.contentDocument.body.innerHTML="
        "'<p>PRIVATE-OTP</p><button>Private recovery</button>';"
    )
    after = adapter.observe(sid, tid, mode="auto", max_chars=100000)
    assert "Public article body" in after["observation"]["semantic_snapshot"]
    assert "PRIVATE-" not in json.dumps(after) and "Private recovery" not in json.dumps(after)
    assert any(frame["reason"] == "PROTECTED_PARENT" for frame in after["observation"]["frames"])
    assert_private_frame_pixels_masked(adapter.observe(sid, tid, mode="visual"))
    with pytest.raises(BrowserError) as action_error:
        adapter.prepare(sid, tid, seen["revision"], {"type": "click", "node_id": target["node_id"]})
    assert action_error.value.code in ("STALE_NODE", "SENSITIVE_TARGET", "AUTH_REQUIRED")
    with pytest.raises(BrowserError) as query_error:
        adapter.observe(sid, tid, mode="semantic", query={"frame_id": frame_id})
    assert query_error.value.code in (
        "FRAME_STALE",
        "PROTECTED_PARENT",
        "SENSITIVE_FRAME",
        "FRAME_UNAVAILABLE",
    )


def test_main_world_geometry_overrides_do_not_unmask_sensitive_frame(browser):
    adapter, sid, tid = install(
        browser,
        '<p>Public article body</p><iframe id="child" '
        'style="position:fixed;left:450px;top:300px;width:180px;height:100px;border:0"></iframe>',
        "document.querySelector('#child').contentDocument.body.innerHTML="
        "'<body style=background:red><input type=password value=PRIVATE-PASSWORD></body>';"
        "Element.prototype.getBoundingClientRect=function(){"
        "return {x:0,y:0,left:0,top:0,right:1,bottom:1,width:1,height:1};};"
        "window.getComputedStyle=function(){return {transform:'none',filter:'none',"
        "perspective:'none',backdropFilter:'none',webkitBoxReflect:'none',"
        "mixBlendMode:'normal',visibility:'visible',display:'block',zoom:'1'};};",
    )
    adapter.cfg.iframe_screenshot_policy = "inspect"
    observed = adapter.observe(sid, tid, mode="visual")
    assert "PRIVATE-" not in json.dumps(observed)
    assert any(frame["reason"] == "SENSITIVE_FRAME" for frame in observed["observation"]["frames"])
    assert_private_frame_pixels_masked(observed)


@pytest.mark.parametrize("pseudo", ["before", "after"])
@pytest.mark.parametrize("owner", ["form", "editable"])
def test_boxless_empty_protected_descendant_pseudo_content_rejects_capture(browser, pseudo, owner):
    decoration = (
        '<span id="decorated" style="display:contents" data-private="PRIVATE-PAINTED-TEXT"></span>'
    )
    private = (
        '<form style="display:contents"><input type="password" hidden>' + decoration + "</form>"
        if owner == "form"
        else '<div contenteditable="true" aria-label="API key" style="display:contents">'
        + decoration
        + "</div>"
    )
    adapter, sid, tid = install(
        browser,
        "<p>Public article body</p>" + private,
        "const style=document.createElement('style');style.textContent="
        + json.dumps(
            f"#decorated::{pseudo}{{content:attr(data-private);position:fixed;"
            "left:500px;top:400px;font:24px sans-serif;}"
        )
        + ";document.head.append(style);",
    )
    raw = snapshot(browser)
    assert raw["privacy_mask_unsafe"]
    assert "Public article body" in raw["semantic_text"] and "PRIVATE-" not in json.dumps(raw)
    with pytest.raises(BrowserError) as error:
        adapter.observe(sid, tid, mode="visual")
    assert error.value.code == "SENSITIVE_SCREEN"
    assert adapter._tab(sid, tid).screenshot is None


@pytest.mark.parametrize(
    "selector,match_count,empty_reason,semantic_source,text",
    [
        ("#missing", 0, "missing", None, ""),
        ("#hidden", 1, "hidden", None, ""),
        ("#visible", 1, None, "div", "Visible child body"),
    ],
)
def test_frame_scoped_query_metadata_describes_child_not_parent(
    browser, selector, match_count, empty_reason, semantic_source, text
):
    adapter, sid, tid = install(
        browser,
        '<div id="missing">Parent match</div><div id="hidden">Parent visible</div>'
        '<div id="visible" hidden>Parent hidden</div>'
        '<iframe id="child" style="width:400px;height:200px"></iframe>',
        "document.querySelector('#child').contentDocument.body.innerHTML="
        "'<div id=hidden hidden>Hidden child body</div><div id=visible>Visible child body</div>';",
    )
    seen = adapter.observe(sid, tid, mode="semantic")
    frame_id = seen["observation"]["frames"][0]["frame_id"]
    queried = adapter.observe(
        sid, tid, mode="semantic", query={"frame_id": frame_id, "selector": selector}
    )["observation"]
    assert queried["query_match_count"] == match_count
    assert queried["query_empty_reason"] == empty_reason
    assert queried["semantic_source"] == semantic_source
    assert queried["semantic_snapshot"] == text


@pytest.mark.parametrize("operation", ["list", "prepare"])
def test_protected_page_tool_guard_runs_before_native_bridge_enable(
    browser, monkeypatch, operation
):
    adapter, sid, tid = install(
        browser,
        '<p>Public article body</p><form><input type="password" value="PRIVATE-PASSWORD"></form>',
    )
    seen = adapter.observe(sid, tid, mode="semantic")

    def unavailable_bridge(tab):
        pytest.fail(
            "Protected page must be rejected before constructing/enabling the native bridge"
        )

    monkeypatch.setattr("cloud_browser.drission.PageTools", unavailable_bridge)
    with pytest.raises(BrowserError) as error:
        if operation == "list":
            adapter.list_page_tools(sid, tid)
        else:
            adapter.prepare(
                sid,
                tid,
                seen["revision"],
                {"type": "page_tool", "tool_name": "test_read", "arguments": {}},
            )
    assert error.value.code == "SENSITIVE_TARGET"
    assert "PRIVATE-" not in str(error.value.details)


def test_page_tool_creating_sensitive_form_withholds_dispatched_result(browser):
    adapter, sid, tid = install(browser, "<p>Public article body</p>")
    state = adapter._tab(sid, tid)

    class StubPageTools:
        """Tests the adapter guard without requiring Chromium's experimental domain."""

        enabled = False
        overflow = False
        invocations = 0
        disabled = 0

        def enable(self):
            self.enabled = True

        def snapshot(self, frame_id):
            return 1, [
                {
                    "name": "test_read",
                    "description": "Controlled privacy regression",
                    "inputSchema": {"type": "object", "additionalProperties": False},
                }
            ]

        def invoke(self, frame_id, name, arguments):
            self.invocations += 1
            state.tab.run_js(
                "document.body.insertAdjacentHTML('beforeend',"
                "'<form><input type=password value=PRIVATE-PASSWORD></form>')"
            )
            return {"text": "PRIVATE-TOOL-RESULT"}

        def disable(self):
            self.enabled = False
            self.disabled += 1

    bridge = StubPageTools()
    state.page_tools = bridge
    listed = adapter.list_page_tools(sid, tid)
    with pytest.raises(BrowserError) as error:
        adapter.act(
            sid,
            tid,
            listed["revision"],
            {"type": "page_tool", "tool_name": "test_read", "arguments": {}},
        )
    assert error.value.code == "RESULT_UNCERTAIN"
    assert error.value.details["action_result"]["performed"] is True
    assert "PRIVATE-" not in str(error.value.details)
    assert bridge.invocations == 1 and bridge.disabled >= 1 and not bridge.enabled
    assert state.advertised_tools is None


def test_private_control_return_invalidates_unchanged_backend_node_ids(browser):
    adapter, sid, tid = install(browser, '<button id="public">Public button</button>')
    before, entries = nodes(adapter, sid, tid)
    original = entries[0]
    state = adapter._tab(sid, tid)
    backend = state.nodes[original["node_id"]][0]
    adapter.focus(sid, tid)
    adapter.resume(sid, tid)
    after, current = nodes(adapter, sid, tid)
    assert after["revision"] >= before["revision"]
    assert current[0]["node_id"] != original["node_id"]
    assert state.nodes[current[0]["node_id"]][0] == backend
    with pytest.raises(BrowserError) as error:
        adapter.prepare(
            sid, tid, after["revision"], {"type": "click", "node_id": original["node_id"]}
        )
    assert error.value.code == "STALE_NODE"


@pytest.mark.parametrize(
    "hidden",
    ["display:none", "visibility:hidden", None],
    ids=["display-none-protected-form", "visibility-hidden-protected-form", "visible-password"],
)
def test_auth_rule_success_still_checks_visible_protected_controls(browser, hidden):
    adapter, sid, tid = install(browser, '<p id="signed-in">Public signed-in indicator</p>')
    base = browser[3]
    adapter.cfg.auth_rules = {base: AuthRule(success_selector="#signed-in")}
    adapter.focus(sid, tid)
    state = adapter._tab(sid, tid)
    state.tab.run_js(
        "document.body.insertAdjacentHTML('beforeend',arguments[0])",
        f'<form style="{hidden}"><input type="password" value="PRIVATE-PASSWORD"></form>'
        if hidden
        else '<form><input type="password" value="PRIVATE-PASSWORD"></form>',
    )
    if hidden:
        resumed = adapter.resume(sid, tid, auth_origin=base)
        assert resumed["authentication"]["authenticated"] is True
        assert resumed["authentication"]["verification"] == "operator_rule"
        assert not adapter._session(sid)["paused"]
        assert "PRIVATE-" not in json.dumps(resumed)
        assert "PRIVATE-" not in json.dumps(adapter.observe(sid, tid, mode="semantic"))
    else:
        with pytest.raises(BrowserError) as error:
            adapter.resume(sid, tid, auth_origin=base)
        assert error.value.code == "AUTH_REQUIRED"
        assert adapter._session(sid)["paused"]
        assert "PRIVATE-" not in str(error.value.details)
