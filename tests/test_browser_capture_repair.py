import base64
import io
import json

import pytest
from PIL import Image
from test_browser import browser as browser

from cloud_browser.models import BrowserError

pytestmark = pytest.mark.browser


def public_page(browser, height=350):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.style.margin='0';document.body.innerHTML="
        "'<main style=\"height:' + arguments[0] + 'px\"><h1>Public article</h1>' +"
        "'<p id=story>Fresh safe article content.</p><button>Read public section</button>' +"
        "'<video style=\"display:block;width:160px;height:90px;background:lime\"></video>' +"
        "'</main>';",
        height,
    )
    return adapter, sid, tid, tab


def assert_fresh_text(observed):
    assert "Fresh safe article content." in observed["observation"]["semantic_snapshot"]
    nodes = [
        json.loads(line)
        for line in observed["observation"]["interactive_snapshot"].splitlines()
        if line
    ]
    assert any(node["name"] == "Read public section" for node in nodes)


@pytest.mark.parametrize("mode", ["auto", "visual"])
@pytest.mark.parametrize("in_frame", [False, True])
def test_transient_protected_controls_during_capture_never_return_pixels(
    browser, monkeypatch, mode, in_frame
):
    adapter, sid, tid, tab = public_page(browser)
    if in_frame:
        tab.run_js(
            "document.body.insertAdjacentHTML('beforeend',"
            "'<iframe style=width:300px;height:200px></iframe>');"
            "document.querySelector('iframe').contentDocument.body.innerHTML='<p>Public child</p>';"
        )
    original = tab.run_cdp

    def capture_with_temporary_private_form(command, **parameters):
        if command != "Page.captureScreenshot":
            return original(command, **parameters)
        tab.run_js(
            "const doc=arguments[0]?document.querySelector('iframe').contentDocument:document;"
            "doc.body.insertAdjacentHTML('beforeend',"
            "'<form id=temporary-private><input autocomplete=one-time-code value=PRIVATE-CODE>'"
            "+'<p>PRIVATE-CODE</p></form>');",
            in_frame,
        )
        try:
            return original(command, **parameters)
        finally:
            tab.run_js(
                "const doc=arguments[0]?document.querySelector('iframe').contentDocument:document;"
                "doc.querySelector('#temporary-private').remove();",
                in_frame,
            )

    monkeypatch.setattr(tab, "run_cdp", capture_with_temporary_private_form)
    if mode == "visual":
        with pytest.raises(BrowserError) as error:
            adapter.observe(sid, tid, mode=mode)
        assert error.value.code == "SCREEN_CHANGED"
    else:
        observed = adapter.observe(sid, tid, mode=mode)
        assert_fresh_text(observed)
        assert observed["observation"]["screenshot_omitted"]["code"] == "SCREEN_CHANGED"
        assert "_image" not in observed and "PRIVATE-CODE" not in json.dumps(observed)
    assert adapter._tab(sid, tid).screenshot is None


def mutate_after_capture(monkeypatch, tab, script):
    original = tab.run_cdp
    mutations = []

    def capture_then_change(command, **parameters):
        result = original(command, **parameters)
        if command == "Page.captureScreenshot" and not mutations:
            tab.run_js(script)
            mutations.append(True)
        return result

    monkeypatch.setattr(tab, "run_cdp", capture_then_change)
    return mutations


@pytest.mark.parametrize("mode", ["auto", "visual"])
@pytest.mark.parametrize("existing_host", [False, True])
def test_transient_new_open_shadow_during_capture_never_returns_pixels(
    browser, monkeypatch, mode, existing_host
):
    adapter, sid, tid, tab = public_page(browser)
    if existing_host:
        tab.run_js(
            "document.body.insertAdjacentHTML('beforeend','<div id=shadow-capture-host></div>')"
        )
    original = tab.run_cdp

    def capture_with_shadow(command, **parameters):
        if command != "Page.captureScreenshot":
            return original(command, **parameters)
        tab.run_js(
            "if(!arguments[0])document.body.insertAdjacentHTML('beforeend',"
            "'<div id=shadow-capture-host></div>');"
            "document.querySelector('#shadow-capture-host').attachShadow({mode:'open'}).innerHTML="
            "'<form><input autocomplete=one-time-code value=PRIVATE-CODE><p>PRIVATE-CODE</p></form>';",
            existing_host,
        )
        try:
            return original(command, **parameters)
        finally:
            tab.run_js(
                "const host=document.querySelector('#shadow-capture-host');host.shadowRoot.innerHTML='';"
                "if(!arguments[0])host.remove();",
                existing_host,
            )

    monkeypatch.setattr(tab, "run_cdp", capture_with_shadow)
    if mode == "visual":
        with pytest.raises(BrowserError) as error:
            adapter.observe(sid, tid, mode=mode)
        assert error.value.code == "SCREEN_CHANGED"
    else:
        observed = adapter.observe(sid, tid, mode=mode)
        assert_fresh_text(observed)
        assert observed["observation"]["screenshot_omitted"]["code"] == "SCREEN_CHANGED"
        assert "_image" not in observed and "PRIVATE-CODE" not in json.dumps(observed)
    assert adapter._tab(sid, tid).screenshot is None


@pytest.mark.parametrize("lightweight", [False, True])
def test_auto_capture_denial_preserves_fresh_text_and_nodes(browser, monkeypatch, lightweight):
    adapter, sid, tid, tab = public_page(browser)
    old = adapter.observe(sid, tid, mode="semantic")
    tab.run_js(
        "document.querySelector('#story').textContent='Fresh safe article content. Updated.'"
    )

    def deny(width, height):
        raise BrowserError(
            "RESOURCE_PRESSURE", "Controlled capture denial", resources={"available_mb": 120}
        )

    monkeypatch.setattr(adapter, "_capture_admission", deny)
    observed = adapter.observe(sid, tid, mode="auto", lightweight=lightweight)
    assert_fresh_text(observed)
    assert "Updated." in observed["observation"]["semantic_snapshot"]
    assert observed["revision"] > old["revision"]
    assert observed["observation"]["screenshot"] is None and "_image" not in observed
    omitted = observed["observation"]["screenshot_omitted"]
    assert omitted["code"] == "RESOURCE_PRESSURE"
    assert omitted["resources"]["available_mb"] == 120
    assert adapter._tab(sid, tid).screenshot is None


def test_explicit_visual_propagates_capture_admission_denial(browser, monkeypatch):
    adapter, sid, tid, _ = public_page(browser)

    def deny(width, height):
        raise BrowserError(
            "RESOURCE_PRESSURE", "Controlled capture denial", resources={"available_mb": 120}
        )

    monkeypatch.setattr(adapter, "_capture_admission", deny)
    with pytest.raises(BrowserError) as error:
        adapter.observe(sid, tid, mode="visual")
    assert error.value.code == "RESOURCE_PRESSURE"
    assert error.value.details["resources"]["available_mb"] == 120
    assert adapter._tab(sid, tid).screenshot is None


@pytest.mark.parametrize("mode", ["auto", "visual"])
def test_full_page_pixel_limit_never_discards_optional_text(browser, mode):
    adapter, sid, tid, _ = public_page(browser, height=9000)
    if mode == "visual":
        with pytest.raises(BrowserError) as error:
            adapter.observe(sid, tid, mode=mode, full_page=True)
        assert error.value.code == "RESOURCE_PRESSURE"
    else:
        observed = adapter.observe(sid, tid, mode=mode, full_page=True)
        assert_fresh_text(observed)
        assert observed["observation"]["screenshot_omitted"]["code"] == "RESOURCE_PRESSURE"
        assert observed["observation"]["screenshot"] is None and "_image" not in observed
    assert adapter._tab(sid, tid).screenshot is None


@pytest.mark.parametrize("full_page,height", [(False, 1500), (True, 350), (True, 1500)])
def test_capture_admission_uses_actual_observed_dimensions(browser, monkeypatch, full_page, height):
    adapter, sid, tid, tab = public_page(browser, height=height)
    actual = tab.run_js(
        "return {width:innerWidth,height:arguments[0]?document.documentElement.scrollHeight:innerHeight}",
        full_page,
    )
    original = adapter._capture_admission
    admissions = []

    def record(width, capture_height):
        admissions.append((width, capture_height))
        return original(width, capture_height)

    monkeypatch.setattr(adapter, "_capture_admission", record)
    observed = adapter.observe(sid, tid, mode="visual", full_page=full_page)
    assert actual["width"] == 1024
    assert admissions == [(actual["width"], actual["height"])]
    assert actual["width"] * actual["height"] < adapter.cfg.max_capture_pixels
    shot = observed["observation"]["screenshot"]
    assert (shot["width"], shot["height"]) == admissions[0]
    image = Image.open(io.BytesIO(base64.b64decode(observed["_image"]["data"])))
    assert image.size == admissions[0]


@pytest.mark.parametrize(
    "style",
    [
        "display:none;width:180px;height:100px",
        "width:0;height:0;border:0",
        "position:fixed;left:100px;top:1700px;width:180px;height:100px;border:0",
    ],
    ids=["hidden", "zero-size", "offscreen"],
)
@pytest.mark.parametrize("policy", ["inspect", "block"])
def test_non_painted_frames_do_not_block_inspect_but_legacy_block_remains(browser, style, policy):
    adapter, sid, tid, tab = public_page(browser)
    adapter.cfg.iframe_screenshot_policy = policy
    tab.run_js(
        "const frame=document.createElement('iframe');frame.style.cssText=arguments[0];"
        "frame.srcdoc='<p>Ordinary embedded content</p>';document.body.append(frame);",
        style,
    )
    if policy == "block":
        with pytest.raises(BrowserError) as error:
            adapter.observe(sid, tid, mode="visual")
        assert error.value.code == "SENSITIVE_SCREEN"
        return
    observed = adapter.observe(sid, tid, mode="visual")
    assert observed["_image"]["data"]
    assert observed["observation"]["frames"]
    assert observed["observation"]["screenshot"]["masked_regions"] == []


@pytest.mark.parametrize("mode", ["auto", "visual"])
@pytest.mark.parametrize(
    "attributes",
    ["type=password", 'type=text autocomplete="one-time-code"'],
    ids=["password", "otp"],
)
def test_new_sensitive_field_during_capture_never_returns_pixels(
    browser, monkeypatch, mode, attributes
):
    adapter, sid, tid, tab = public_page(browser)
    mutation = (
        "document.body.insertAdjacentHTML('beforeend', "
        + json.dumps(
            f'<input {attributes} value="capture-private-test-value" '
            'style="position:fixed;left:350px;top:300px;width:180px;height:30px">'
        )
        + ");"
    )
    mutations = mutate_after_capture(monkeypatch, tab, mutation)
    if mode == "visual":
        with pytest.raises(BrowserError) as error:
            adapter.observe(sid, tid, mode=mode)
        assert error.value.code in ("SCREEN_CHANGED", "SENSITIVE_SCREEN")
    else:
        observed = adapter.observe(sid, tid, mode=mode)
        assert_fresh_text(observed)
        assert observed["observation"]["screenshot_omitted"]["code"] in (
            "SCREEN_CHANGED",
            "SENSITIVE_SCREEN",
        )
        assert observed["observation"]["screenshot"] is None and "_image" not in observed
        assert "capture-private-test-value" not in json.dumps(observed)
    assert mutations == [True]
    assert adapter._tab(sid, tid).screenshot is None


@pytest.mark.parametrize("mode", ["auto", "visual"])
def test_sensitive_frame_movement_during_capture_never_returns_pixels(browser, monkeypatch, mode):
    adapter, sid, tid, tab = public_page(browser)
    adapter.cfg.iframe_screenshot_policy = "inspect"
    tab.run_js(
        "const frame=document.createElement('iframe');frame.id='capture-frame';"
        "frame.style.cssText='position:fixed;left:600px;top:300px;width:180px;height:100px;border:0';"
        "document.body.append(frame);"
        "frame.contentDocument.body.innerHTML='<input type=password value=capture-private-test-value>';"
    )
    before = adapter.observe(sid, tid, mode="semantic")
    assert any(frame["reason"] == "SENSITIVE_FRAME" for frame in before["observation"]["frames"])
    mutations = mutate_after_capture(
        monkeypatch, tab, "document.querySelector('#capture-frame').style.left='650px'"
    )
    if mode == "visual":
        with pytest.raises(BrowserError) as error:
            adapter.observe(sid, tid, mode=mode)
        assert error.value.code in ("SCREEN_CHANGED", "SENSITIVE_SCREEN")
    else:
        observed = adapter.observe(sid, tid, mode=mode)
        assert_fresh_text(observed)
        assert observed["observation"]["screenshot_omitted"]["code"] in (
            "SCREEN_CHANGED",
            "SENSITIVE_SCREEN",
        )
        assert observed["observation"]["screenshot"] is None and "_image" not in observed
        assert "capture-private-test-value" not in json.dumps(observed)
    assert mutations == [True]
    assert adapter._tab(sid, tid).screenshot is None


@pytest.mark.parametrize("mode", ["auto", "visual"])
def test_offscreen_frame_motion_does_not_invalidate_viewport_capture(browser, monkeypatch, mode):
    adapter, sid, tid, tab = public_page(browser)
    adapter.cfg.iframe_screenshot_policy = "inspect"
    tab.run_js(
        "const frame=document.createElement('iframe');frame.id='capture-frame';"
        "frame.style.cssText='position:fixed;left:100px;top:1700px;width:180px;height:100px;border:0';"
        "document.body.append(frame);frame.contentDocument.body.innerHTML='<p>Ordinary frame text</p>';"
    )
    mutations = mutate_after_capture(
        monkeypatch, tab, "document.querySelector('#capture-frame').style.top='1900px'"
    )
    observed = adapter.observe(sid, tid, mode=mode)
    assert mutations == [True]
    assert observed["observation"]["screenshot"] and observed["_image"]["data"]
    assert observed["observation"]["screenshot"]["masked_regions"] == []
    assert "screenshot_omitted" not in observed["observation"]
    if mode == "auto":
        assert_fresh_text(observed)


@pytest.mark.parametrize("available,allowed", [(140, True), (139, False)])
def test_actual_capture_admission_keeps_existing_ram_floor(
    browser, monkeypatch, available, allowed
):
    adapter, sid, tid, _ = public_page(browser)
    monkeypatch.setattr(
        "cloud_browser.drission.memory_state",
        lambda reserve: {
            "available_mb": available,
            "host_available_mb": 1024,
            "can_admit": False,
            "memory_pressure": {"some": 0, "full": 0},
        },
    )
    floor, reserve = adapter.cfg.memory_floor_mb, adapter.cfg.memory_reserve_mb
    if allowed:
        observed = adapter.observe(sid, tid, mode="visual")
        assert observed["observation"]["screenshot"] and observed["_image"]["data"]
    else:
        with pytest.raises(BrowserError) as error:
            adapter.observe(sid, tid, mode="visual")
        assert error.value.code == "RESOURCE_PRESSURE"
        assert error.value.details["resources"]["required_headroom_mb"] == 140
        assert error.value.details["resources"]["admission_mb"] == 44
    assert (adapter.cfg.memory_floor_mb, adapter.cfg.memory_reserve_mb) == (floor, reserve)


def test_public_frame_position_change_recaptures_once_with_fresh_state(browser, monkeypatch):
    adapter, sid, tid, tab = public_page(browser)
    tab.run_js(
        "document.body.insertAdjacentHTML('beforeend','<iframe id=ordinary style=\"position:fixed;left:500px;top:300px;width:100px;height:100px\"></iframe>');document.querySelector('iframe').contentDocument.body.innerHTML='<p>Public frame</p>'"
    )
    original = tab.run_cdp
    captures = []

    def move_once(command, **args):
        if command == "Page.captureScreenshot":
            captures.append(True)
            if len(captures) == 1:
                tab.run_js("document.querySelector('#ordinary').style.left='600px'")
        return original(command, **args)

    monkeypatch.setattr(tab, "run_cdp", move_once)
    result = adapter.observe(sid, tid, mode="visual")
    assert len(captures) == 2
    assert result["observation"]["screenshot"]["capture_attempts"] == 2
    assert result["_image"]["data"]


def test_repeated_scroll_change_is_bounded_to_two_captures(browser, monkeypatch):
    adapter, sid, tid, tab = public_page(browser, height=3000)
    original = tab.run_cdp
    captures = []

    def shift(command, **args):
        if command == "Page.captureScreenshot":
            captures.append(True)
            tab.run_js("window.scrollBy(0,100)")
        return original(command, **args)

    monkeypatch.setattr(tab, "run_cdp", shift)
    with pytest.raises(BrowserError) as error:
        adapter.observe(sid, tid, mode="visual")
    assert error.value.code == "SCREEN_CHANGED"
    assert error.value.details["capture_attempts"] == 2
    assert error.value.details["capture_reasons"] == ["scroll"]
    assert len(captures) == 2


def test_frame_document_moves_away_and_back_are_not_retried(browser, monkeypatch):
    adapter, sid, tid, tab = public_page(browser)
    tab.run_js(
        "document.body.insertAdjacentHTML('beforeend','<iframe id=ordinary></iframe>');document.querySelector('iframe').contentDocument.body.innerHTML='<p>Public child</p>'"
    )
    original = tab.run_cdp
    captures = []

    def replace(command, **args):
        if command == "Page.captureScreenshot":
            captures.append(True)
            tab.run_js(
                "const f=document.querySelector('#ordinary');f.srcdoc='<input type=password value=PRIVATE-CAPTURE>';setTimeout(()=>{f.srcdoc='<p>Public child</p>'},20)"
            )
            import time

            time.sleep(0.15)
        return original(command, **args)

    monkeypatch.setattr(tab, "run_cdp", replace)
    with pytest.raises(BrowserError) as error:
        adapter.observe(sid, tid, mode="visual")
    assert error.value.code in ("SCREEN_CHANGED", "SENSITIVE_SCREEN")
    assert len(captures) == 1
    assert adapter._tab(sid, tid).screenshot is None


def test_transient_protection_in_retry_gap_prevents_second_capture(browser, monkeypatch):
    adapter, sid, tid, tab = public_page(browser, height=3000)
    original = tab.run_cdp
    import time

    sleep = time.sleep
    captures = []

    def move(command, **args):
        if command == "Page.captureScreenshot":
            captures.append(True)
            tab.run_js("window.scrollBy(0,100)")
        return original(command, **args)

    def appear_and_disappear(seconds):
        if seconds == 0.2:
            tab.run_js(
                "const root=document.createElement('div');document.body.append(root);root.attachShadow({mode:'open'}).innerHTML='<input type=password value=PRIVATE-RETRY>';setTimeout(()=>root.remove(),20)"
            )
        sleep(seconds)

    monkeypatch.setattr(tab, "run_cdp", move)
    monkeypatch.setattr("cloud_browser.drission.time.sleep", appear_and_disappear)
    with pytest.raises(BrowserError) as error:
        adapter.observe(sid, tid, mode="visual")
    assert error.value.code in ("SCREEN_CHANGED", "SENSITIVE_SCREEN")
    assert error.value.details["capture_attempts"] == 1
    assert len(captures) == 1 and adapter._tab(sid, tid).screenshot is None
