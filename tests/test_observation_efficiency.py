"""Observation optimizations must preserve fresh policy and privacy evidence."""

import base64
import io
import json
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
from test_browser import browser as browser

from cloud_browser.drission import TOKEN_JS_PATTERN, DrissionAdapter, TabState
from cloud_browser.models import BrowserError
from cloud_browser.security import TOKEN


def test_token_expression_matches_python_for_all_unicode_whitespace():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is used only for the optional JavaScript regex parity test")
    whitespace = [chr(i) for i in range(0x110000) if chr(i).isspace()]
    credential = "fixture_credential_1234567890"
    samples = [
        *["Bearer" + char + credential for char in whitespace],
        *["Bearer" + char + "credential" for char in whitespace],
        *["Bearer " + char for char in whitespace],
        "Bearer\ufeffcredential",
        "Bearer \ufeff",
        "bearer credential",
        "Bearer",
        "eyJabcdefghij.abc.def",
        "eyJabcdefghi.abc.def",
        "eyJabcdefghij.abc.",
        *[
            prefix + "a" * count
            for prefix in ("sk-", "ghp_", "github_pat_")
            for count in (14, 15, 30)
        ],
        *[
            prefix + "a" * count + "1"
            for prefix in ("sk-", "ghp_", "github_pat_")
            for count in (18, 19, 30)
        ],
        "word-sk-" + credential,
        "Bearer\ufeff" + credential,
    ]
    completed = subprocess.run(
        [
            node,
            "-e",
            "const fs=require('fs'); const [pattern,samples]=JSON.parse(fs.readFileSync(0,'utf8'));"
            "console.log(JSON.stringify(samples.map(s=>new RegExp(pattern,'u').test(s))));",
        ],
        input=json.dumps([TOKEN_JS_PATTERN, samples]),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    assert json.loads(completed.stdout) == [bool(TOKEN.search(s)) for s in samples]
    assert all(bool(re.fullmatch(r"\s", char)) for char in whitespace)


@pytest.mark.parametrize(
    "data,code",
    [
        ({"protected": True, "challenge": None}, "AUTH_REQUIRED"),
        ({"protected": False, "challenge": "captcha"}, "CAPTCHA_REQUIRED"),
        ({"protected": False, "challenge": "bot"}, "BOT_BLOCKED"),
    ],
)
def test_url_probe_checks_page_guards_without_mutating_targets(data, code):
    calls = []

    def cdp(method, **kwargs):
        calls.append((method, kwargs))
        return {
            "Page.getFrameTree": {"frameTree": {"frame": {"id": "root"}}},
            "Page.createIsolatedWorld": {"executionContextId": 7},
            "Runtime.evaluate": {"result": {"value": {"url": "https://example.com", **data}}},
        }[method]

    state = TabState(SimpleNamespace(run_cdp=cdp), revision=9, nodes={"node_before": (1, {})})
    adapter = object.__new__(DrissionAdapter)
    with pytest.raises(BrowserError) as error:
        adapter._probe_url(state)
    assert error.value.code == code
    assert state.revision == 9 and "node_before" in state.nodes
    assert [method for method, _ in calls] == [
        "Page.getFrameTree",
        "Page.createIsolatedWorld",
        "Runtime.evaluate",
    ]
    assert calls[-1][1]["returnByValue"] is True
    assert calls[-1][1]["contextId"] == 7


def test_url_probe_fails_closed_on_dialog_or_evaluation_failure():
    adapter = object.__new__(DrissionAdapter)
    state = TabState(SimpleNamespace(run_cdp=Mock()))
    state.events.dialog = {"type": "alert"}
    with pytest.raises(BrowserError) as error:
        adapter._probe_url(state)
    assert error.value.code == "DIALOG_OPEN"
    state.tab.run_cdp.assert_not_called()
    state.events.dialog = None
    state.tab.run_cdp.side_effect = [
        {"frameTree": {"frame": {"id": "root"}}},
        {"executionContextId": 7},
        {"exceptionDetails": {}},
    ]
    with pytest.raises(BrowserError) as error:
        adapter._probe_url(state)
    assert error.value.code == "OBSERVATION_FAILED"


@pytest.mark.parametrize(
    "timeout,urls,matched",
    [
        (1000, ["https://example.com/a", "https://example.com/a", "https://example.com/b"], True),
        (0, ["https://example.com/a"], False),
    ],
)
def test_url_wait_observes_once_at_completion(monkeypatch, timeout, urls, matched):
    adapter = object.__new__(DrissionAdapter)
    state = TabState(SimpleNamespace(url=urls[-1]))
    adapter._tab = Mock(return_value=state)
    adapter._probe_url = Mock(side_effect=urls)
    elapsed = [0.0]
    monkeypatch.setattr("cloud_browser.drission.time.monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        "cloud_browser.drission.time.sleep",
        lambda delay: elapsed.__setitem__(0, elapsed[0] + delay),
    )

    def observe(*args, **kwargs):
        state.revision = 42
        state.data = {"url": state.tab.url, "title": "Fresh page"}

    adapter.observe = Mock(side_effect=observe)
    result = adapter.wait(
        "session", "tab", {"type": "url", "value": "https://example.com/b"}, timeout
    )
    assert adapter._probe_url.call_count == len(urls)
    assert adapter._tab.call_count == 1
    assert adapter.observe.call_count == 1
    assert result["wait"] == {
        "matched": matched,
        "timed_out": not matched,
        "condition": "url",
        "partial": False,
    }
    assert result["revision"] == 42 and result["page"]["title"] == "Fresh page"


def test_url_wait_rechecks_match_after_terminal_observation(monkeypatch):
    adapter = object.__new__(DrissionAdapter)
    state = TabState(SimpleNamespace(url="https://example.com/a"))
    adapter._tab = Mock(return_value=state)
    adapter._probe_url = Mock(return_value="https://example.com/b")
    monkeypatch.setattr("cloud_browser.drission.time.monotonic", lambda: 0)

    def observe(*args, **kwargs):
        if adapter.observe.call_count == 2:
            state.tab.url = "https://example.com/b"
        state.data = {"url": state.tab.url, "title": "Fresh"}

    adapter.observe = Mock(side_effect=observe)
    assert adapter.wait("session", "tab", {"type": "url", "value": "https://example.com/b"}, 1000)[
        "wait"
    ]["matched"]
    assert adapter.observe.call_count == 2


def test_url_wait_preserves_browser_errors_without_resync():
    adapter = object.__new__(DrissionAdapter)
    adapter._tab = Mock(return_value=TabState(SimpleNamespace()))
    failure = BrowserError("AUTH_REQUIRED", "Protected page")
    adapter._probe_url = Mock(side_effect=failure)
    adapter.observe = Mock()
    with pytest.raises(BrowserError) as error:
        adapter.wait("session", "tab", {"type": "url", "value": "https://example.com"}, 0)
    assert error.value is failure
    assert adapter._tab.call_count == adapter._probe_url.call_count == 1
    adapter.observe.assert_not_called()


@pytest.mark.parametrize("code", ["TAB_NOT_FOUND", "SESSION_EXPIRED"])
def test_url_wait_classifies_closed_target_after_cdp_error(code):
    adapter = object.__new__(DrissionAdapter)
    failure = BrowserError(code, "Target closed")
    adapter._tab = Mock(side_effect=[TabState(SimpleNamespace()), failure])
    adapter._probe_url = Mock(side_effect=RuntimeError("CDP disconnected"))
    adapter.observe = Mock()
    with pytest.raises(BrowserError) as error:
        adapter.wait("session", "tab", {"type": "url", "value": "https://example.com"}, 0)
    assert error.value is failure
    assert adapter._tab.call_count == 2
    assert adapter._probe_url.call_count == 1
    adapter.observe.assert_not_called()


def test_url_wait_preserves_unexpected_failure_for_live_target():
    adapter = object.__new__(DrissionAdapter)
    adapter._tab = Mock(return_value=TabState(SimpleNamespace()))
    failure = RuntimeError("CDP command failed")
    adapter._probe_url = Mock(side_effect=failure)
    adapter.observe = Mock()
    with pytest.raises(RuntimeError) as error:
        adapter.wait("session", "tab", {"type": "url", "value": "https://example.com"}, 0)
    assert error.value is failure
    assert adapter._tab.call_count == 2
    assert adapter._probe_url.call_count == 1
    adapter.observe.assert_not_called()


@pytest.mark.browser
@pytest.mark.parametrize("mode", ["semantic", "auto"])
@pytest.mark.parametrize("reader", [False, True])
def test_semantic_and_reader_observations_keep_full_text_and_links(browser, mode, reader):
    adapter, sid, tid, base = browser
    state = adapter._tab(sid, tid)
    body = "Public article sentence. " * 500 + "Complete article tail."
    state.tab.run_js(
        "document.body.innerHTML='<main><h1>Reader article</h1><p></p>' +"
        "'<a href=\"/article?blogId=fixture\">Article link</a></main>' +"
        "'<form><input type=password><p>PRIVATE-FORM-TEXT</p></form>';"
        "document.querySelector('p').textContent=" + json.dumps(body)
    )
    result = adapter.observe(
        sid,
        tid,
        mode=mode,
        max_chars=30000,
        reader_options={"collect_links": True, "max_text_chars": 60000} if reader else None,
    )
    observation = result["observation"]
    assert body in state.data["text"] and body in state.data["semantic_text"]
    assert body in observation["semantic_snapshot"]
    assert not observation["truncated"] and not observation["semantic_source_truncated"]
    assert observation["protected_regions_omitted"]
    assert "PRIVATE-FORM-TEXT" not in json.dumps(result)
    if reader:
        links = [{"text": "Article link", "url": base + "/article?blogId=fixture"}]
        assert state.data["reader_links"] == observation["links"] == links
        assert not observation["links_truncated"]


@pytest.mark.browser
@pytest.mark.parametrize("mode", ["interactive", "visual"])
def test_reader_capture_retains_text_even_in_transport_optimized_modes(browser, mode):
    adapter, sid, tid, base = browser
    state = adapter._tab(sid, tid)
    state.tab.run_js(
        "document.body.innerHTML='<main>Full reader text <a href=\"/article\">Link</a></main>'"
    )
    data = adapter._capture_state(state, mode=mode, reader_options={"collect_links": True})
    assert "Full reader text" in data["text"] and "Full reader text" in data["semantic_text"]
    assert data["reader_links"] == [{"text": "Link", "url": base + "/article"}]


@pytest.mark.browser
def test_text_omission_preserves_complete_privacy_and_challenge_checks(browser):
    adapter, sid, tid, _ = browser
    state = adapter._tab(sid, tid)
    state.tab.run_js(
        "document.body.innerHTML='<p></p>'; document.querySelector('p').textContent='x'.repeat(260000)+' sk-'+ 'a'.repeat(19)+'1'"
    )
    adapter.observe(sid, tid, mode="interactive")
    assert state.data["text"] == "" and state.data["sensitive_text"]
    with pytest.raises(BrowserError) as error:
        adapter.observe(sid, tid, mode="visual")
    assert error.value.code == "SENSITIVE_SCREEN"
    adapter.observe(sid, tid, mode="semantic")
    # The paragraph's opening newline uses one budget character before trim().
    # Main caps formatted text, so this fixture retains exactly 249999 x's.
    assert state.data["text"] == "x" * 249999
    assert state.data["text_scan_truncated"] and state.data["sensitive_text"]
    assert state.data["semantic_source_truncated"]
    state.tab.run_js(
        "document.querySelector('p').textContent='x'.repeat(260000)+' Verify that you are human'"
    )
    # Quoted challenge text is readable on main; a live challenge control is
    # required as well, and must be detected beyond the output text cap.
    adapter.observe(sid, tid, mode="interactive")
    assert adapter._probe_url(state) == state.tab.url
    state.tab.run_js(
        "document.body.insertAdjacentHTML('beforeend','<button name=captcha style=\"position:fixed;top:0;left:0\">Verify</button>')"
    )
    for call in (
        lambda: adapter.observe(sid, tid, mode="interactive"),
        lambda: adapter._probe_url(state),
    ):
        with pytest.raises(BrowserError) as error:
            call()
        assert error.value.code == "CAPTCHA_REQUIRED"
    state.tab.run_js(
        "document.body.innerHTML='<p>Public article</p><form><p>Private text</p><input type=password></form>'"
    )
    assert adapter._probe_url(state) == state.tab.url
    seen = adapter.observe(sid, tid, mode="semantic")
    assert seen["observation"]["protected_regions_omitted"]
    assert "Public article" in seen["observation"]["semantic_snapshot"]
    assert "Private text" not in json.dumps(seen)


@pytest.mark.browser
def test_shared_form_metadata_is_refreshed_after_live_property_changes(browser):
    adapter, sid, tid, _ = browser
    state = adapter._tab(sid, tid)
    state.tab.run_js("""
      document.body.innerHTML='<form role=search><input type=search name=q aria-label=Search>'+
        '<input type=checkbox name=details aria-label=Details><button>Search</button></form>';
    """)
    adapter.observe(sid, tid, mode="interactive")
    before = [meta for _, meta in state.nodes.values()]
    assert len(before) == 3 and all(meta["search_form"] for meta in before)
    assert all(meta["form_fields"] == ["Search"] for meta in before)
    assert all(meta["search_submitter_name"] == "Search" for meta in before)
    state.tab.run_js("""
      document.querySelector('[name=details]').checked=true;
      document.querySelector('button').formAction='/different';
      document.querySelector('[name=q]').style.width='301px';
    """)
    adapter.observe(sid, tid, mode="interactive")
    after = [meta for _, meta in state.nodes.values()]
    assert all(not meta["search_form"] for meta in after)
    assert all(meta["form_fields"] == ["Search", "Details"] for meta in after)
    assert after[0]["rect"]["width"] != before[0]["rect"]["width"]
    assert after[0]["_form_digest"] != before[0]["_form_digest"]


@pytest.mark.browser
def test_token_only_frame_is_withheld_and_pixels_are_masked(browser):
    adapter, sid, tid, _ = browser
    state = adapter._tab(sid, tid)
    state.tab.run_js("""
      document.body.innerHTML='<iframe style="position:fixed;left:20px;top:20px;width:260px;height:120px;border:0"></iframe>';
      const child=document.querySelector('iframe').contentDocument;
      child.body.style.background='red';
      child.body.innerHTML='<p>Bearer frame_fixture_secret1234</p><button>Private frame control</button>';
    """)
    interactive = adapter.observe(sid, tid, mode="interactive")
    frames = interactive["observation"]["frames"]
    assert len(frames) == 1 and frames[0]["reason"] == "SENSITIVE_FRAME"
    assert not frames[0]["readable"] and not frames[0]["actionable"]
    assert not state.frame_nodes
    # Main discards unreadable frame snapshots and their registered node records;
    # verify withholding through the inventory entry and retained mask geometry.
    assert not state.frame_states
    assert all(
        record.owner == state.registry_token for record in state.node_registry.records.values()
    )
    assert interactive["observation"]["readable_frames"] == 0
    assert interactive["observation"]["frame_reading_truncated"]
    regions = state.data["restricted_frame_regions"]
    assert len(regions) == 1
    assert {key: regions[0][key] for key in ("x", "y", "width", "height", "mask_safe")} == {
        "x": 20,
        "y": 20,
        "width": 260,
        "height": 120,
        "mask_safe": True,
    }
    assert "Private frame control" not in json.dumps(interactive)
    assert "frame_fixture_secret" not in json.dumps(interactive)

    visual = adapter.observe(sid, tid, mode="visual")
    visual_frames = visual["observation"]["frames"]
    assert len(visual_frames) == 1 and visual_frames[0]["reason"] == "SENSITIVE_FRAME"
    assert not visual_frames[0]["readable"] and not visual_frames[0]["actionable"]
    assert not state.frame_states and not state.frame_nodes
    assert visual["observation"]["screenshot"]["masked_regions"] == [
        {"x": 4, "y": 4, "width": 292, "height": 152}
    ]
    assert visual["_image"]["mimeType"] == "image/png"
    image = Image.open(io.BytesIO(base64.b64decode(visual["_image"]["data"])))
    assert image.getpixel((80, 80)) == (0, 0, 0)
    assert image.crop((20, 20, 280, 140)).getextrema() == ((0, 0),) * 3
    assert "Private frame control" not in json.dumps(visual)
    assert "frame_fixture_secret" not in json.dumps(visual)


@pytest.mark.browser
def test_token_after_pixel_capture_prevents_image_result(browser, monkeypatch):
    adapter, sid, tid, _ = browser
    state = adapter._tab(sid, tid)
    state.tab.run_js("""
      document.body.innerHTML='<p id=fixture style="position:fixed;left:20px;top:20px;width:500px;height:20px">Clear</p>';
    """)
    assert state.screenshot is None
    original_cdp = state.tab.run_cdp
    captured = []

    def capture_then_insert_token(method, **kwargs):
        result = original_cdp(method, **kwargs)
        if method == "Page.captureScreenshot":
            captured.append(bool(result.get("data")))
            state.tab.run_js(
                "document.querySelector('#fixture').textContent='Bearer post_capture_fixture_secret1234'"
            )
        return result

    monkeypatch.setattr(state.tab, "run_cdp", capture_then_insert_token)
    with pytest.raises(BrowserError) as error:
        adapter.observe(sid, tid, mode="visual")
    assert error.value.code == "SCREEN_CHANGED"
    assert captured == [True]
    assert state.data["text"] == "" and state.data["sensitive_text"]
    assert state.screenshot is None


@pytest.mark.browser
def test_url_wait_reports_actual_tab_close_without_losing_survivor(browser, monkeypatch):
    adapter, sid, tid, base = browser
    survivor = adapter.open(sid, base + "/browser.html")["tab_id"]
    original_probe = adapter._probe_url
    probes = []

    def close_before_second_probe(state):
        probes.append(state)
        if len(probes) == 2:
            state.tab.close()
        return original_probe(state)

    monkeypatch.setattr(adapter, "_probe_url", close_before_second_probe)
    with pytest.raises(BrowserError) as error:
        adapter.wait(sid, tid, {"type": "url", "value": base + "/never-arrives"}, 5000)
    assert error.value.code == "TAB_NOT_FOUND"
    assert len(probes) == 2
    assert [tab["tab_id"] for tab in adapter.list_tabs(sid)["tabs"]] == [survivor]
    assert adapter.observe(sid, survivor, mode="interactive")["observation"]
