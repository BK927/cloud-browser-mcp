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
