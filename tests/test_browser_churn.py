import pytest
from test_browser import browser as browser
from test_browser import node

from cloud_browser.models import BrowserError

pytestmark = pytest.mark.browser


def test_unrelated_churn_keeps_target_but_replacement_is_stale(browser):
    adapter, sid, tid, _ = browser
    observed, target = node(adapter, sid, tid, "Personal Information")
    tab = adapter._tab(sid, tid).tab
    tab.run_js("document.body.insertAdjacentHTML('beforeend','<div id=ad>new ad</div>')")
    result = adapter.act(
        sid, tid, observed["revision"], {"type": "click", "node_id": target["node_id"]}
    )
    assert result["action_result"]["performed"]
    observed, target = node(adapter, sid, tid, "Personal Information")
    tab.run_js(
        "document.querySelector('#accordion').outerHTML=document.querySelector('#accordion').outerHTML"
    )
    with pytest.raises(BrowserError) as error:
        adapter.act(sid, tid, observed["revision"], {"type": "click", "node_id": target["node_id"]})
    assert error.value.code == "STALE_NODE"


def test_page_scroll_accepts_churn_but_not_document_navigation(browser):
    adapter, sid, tid, base = browser
    observed = adapter.observe(sid, tid)
    adapter._tab(sid, tid).tab.run_js(
        "document.body.insertAdjacentHTML('beforeend','<p>recommendation</p>')"
    )
    result = adapter.act(
        sid, tid, observed["revision"], {"type": "scroll", "delta_x": 0, "delta_y": 200}
    )
    assert result["action_result"]["performed"]
    adapter.navigate(sid, tid, "goto", base + "/approval.html")
    with pytest.raises(BrowserError) as error:
        adapter.act(
            sid, tid, observed["revision"], {"type": "scroll", "delta_x": 0, "delta_y": 200}
        )
    assert error.value.code == "STALE_REVISION"


def test_last_tab_close_is_explicit(browser):
    adapter, sid, tid, _ = browser
    result = adapter.close(sid, "tab", tid)
    assert result["session_closed"] and result["termination_reason"] == "last_tab_closed"


def test_input_tail_and_hidden_own_form_are_not_retargeted(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.innerHTML='<input aria-label=Draft>';document.querySelector('input').value='x'.repeat(3000)+'a'"
    )
    observed, target = node(adapter, sid, tid, "Draft")
    tab.run_js("document.querySelector('input').value='x'.repeat(3000)+'b'")
    with pytest.raises(BrowserError) as error:
        adapter.prepare(
            sid,
            tid,
            observed["revision"],
            {"type": "fill", "node_id": target["node_id"], "text": "changed"},
        )
    assert error.value.code == "STALE_NODE"
