import json

import pytest
from test_browser import browser as browser
from test_browser import node

pytestmark = pytest.mark.browser


def test_same_url_reload_is_a_document_navigation(browser):
    adapter, sid, tid, _ = browser
    result = adapter.navigate(sid, tid, "reload")["navigation"]
    assert result["navigation_occurred"] and result["document_changed"]
    assert not result["url_changed"] and result["navigation_kind"] == "reload"


def test_open_navigate_and_close_report_selection_without_list_tabs(browser):
    adapter, sid, tid, base = browser
    second = adapter.open(sid)
    assert second["selected_tab_id"] == second["tab_id"]
    navigated = adapter.navigate(sid, tid, "goto", base + "/browser.html")
    assert navigated["selected_tab_id"] == tid
    closed = adapter.close(sid, "tab", tid)
    assert closed["selected_tab_id"] == second["tab_id"]


@pytest.mark.parametrize(
    "script,subtype",
    [
        ("location.hash='active'", "hash"),
        ("history.pushState({},'', '?search=example')", "history_api"),
        ("history.replaceState({},'', location.href)", "history_api"),
    ],
)
def test_same_document_actions_preserve_runtime_and_report_navigation(browser, script, subtype):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.innerHTML='<button>Local route</button>';window.survivor=42;"
        "document.querySelector('button').onclick=()=>{" + script + "}"
    )
    seen, button = node(adapter, sid, tid, "Local route")
    result = adapter.act(
        sid, tid, seen["revision"], {"type": "click", "node_id": button["node_id"]}
    )
    navigation = result["action_result"]
    assert navigation["navigation_occurred"] and not navigation["document_changed"]
    assert navigation["navigation_kind"] == "same_document"
    assert navigation["same_document_kind"] == subtype
    assert tab.run_js("return window.survivor") == 42
    assert json.dumps(navigation)  # No engine objects in the public evidence.


def test_targeted_heading_wait_does_not_require_an_interactive_node(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("document.body.innerHTML='<h1>Raspberry Pi</h1>'")
    result = adapter.wait(
        sid,
        tid,
        {
            "type": "element",
            "query": {"role": "heading", "name": "Raspberry Pi"},
            "state": "visible",
        },
        0,
    )
    assert result["wait"]["matched"] and not result["wait"]["partial"]


def test_target_query_filters_before_result_limit_and_reads_native_search_role(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.innerHTML=Array.from({length:150},(_,i)=>'<button style=\"position:fixed;top:200px\">Item '+i+'</button>').join('')+"
        "'<label for=q><span>Site search</span></label><input id=q type=search>'"
    )
    seen = adapter.observe(
        sid,
        tid,
        mode="interactive",
        lightweight=True,
        query={"role": " searchbox ", "name": " Site   search ", "limit": 1},
    )
    items = [json.loads(line) for line in seen["observation"]["interactive_snapshot"].splitlines()]
    assert len(items) == 1 and items[0]["role"] == "searchbox"
    assert not seen["observation"]["interactive_truncated"]
    assert adapter.wait(
        sid,
        tid,
        {"type": "element", "query": {"role": "button", "name": "Item 149", "limit": 1}},
        0,
    )["wait"]["matched"]
    wrong = adapter.observe(sid, tid, mode="interactive", query={"role": "box"})
    assert not wrong["observation"]["interactive_snapshot"]


def test_wait_distinguishes_presence_visibility_and_query_truncation(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.innerHTML='<h1 hidden id=target>Hidden title</h1><button disabled id=disabled>Waiting</button>'"
    )
    for state, expected in (
        ("present", True),
        ("absent", False),
        ("visible", False),
        ("hidden", True),
    ):
        result = adapter.wait(
            sid, tid, {"type": "element", "query": {"selector": "#target"}, "state": state}, 0
        )
        assert result["wait"]["matched"] is expected
    assert not adapter.wait(
        sid, tid, {"type": "element", "query": {"selector": "#disabled"}, "state": "enabled"}, 0
    )["wait"]["matched"]
    tab.run_js(
        "document.querySelector('#target').hidden=false;document.querySelector('#target').style.marginTop='2000px'"
    )
    assert adapter.wait(
        sid,
        tid,
        {
            "type": "element",
            "query": {"role": "heading", "name": "Hidden title"},
            "state": "visible",
        },
        0,
    )["wait"]["matched"]
    tab.run_js("document.body.innerHTML='<button>Filler</button>'.repeat(1100)")
    result = adapter.wait(
        sid,
        tid,
        {"type": "element", "query": {"role": "button", "name": "Missing"}, "state": "absent"},
        0,
    )
    assert result["wait"]["partial"] and not result["wait"]["matched"]


def test_targeted_heading_query_reaches_child_frames(browser):
    adapter, sid, tid, _ = browser
    adapter._tab(sid, tid).tab.run_js(
        "document.body.innerHTML='<iframe srcdoc=\"<h1>Nested heading</h1>\"></iframe>'"
    )
    result = adapter.wait(
        sid,
        tid,
        {
            "type": "element",
            "query": {"role": "heading", "name": "Nested heading"},
            "state": "visible",
        },
        1000,
    )
    assert result["wait"]["matched"]
