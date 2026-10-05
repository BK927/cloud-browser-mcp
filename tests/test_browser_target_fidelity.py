import json

import pytest
from test_browser import browser as browser

from cloud_browser.models import BrowserError

pytestmark = pytest.mark.browser


def nodes(adapter, sid, tid, **options):
    result = adapter.observe(sid, tid, mode="interactive", max_chars=100000, **options)
    return result, [
        json.loads(line) for line in result["observation"]["interactive_snapshot"].splitlines()
    ]


def test_scoped_queries_preserve_unchanged_backend_but_not_replacements(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("document.body.innerHTML='<button id=a>Alpha</button><button id=b>Beta</button>'")
    seen, rows = nodes(adapter, sid, tid)
    beta = next(row for row in rows if row["name"] == "Beta")
    nodes(adapter, sid, tid, query={"selector": "#a"})
    prepared = adapter.prepare(
        sid, tid, seen["revision"], {"type": "click", "node_id": beta["node_id"]}
    )
    assert prepared["target"] == "Beta"
    tab.run_js("document.querySelector('#b').outerHTML=document.querySelector('#b').outerHTML")
    with pytest.raises(BrowserError) as error:
        adapter.prepare(sid, tid, seen["revision"], {"type": "click", "node_id": beta["node_id"]})
    assert error.value.code == "STALE_NODE"


def test_retained_target_rechecks_value_form_and_protected_meaning(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js("document.body.innerHTML='<button id=a>Alpha</button><input id=b aria-label=Draft>'")
    seen, rows = nodes(adapter, sid, tid)
    draft = next(row for row in rows if row["name"] == "Draft")
    nodes(adapter, sid, tid, query={"selector": "#a"})
    tab.run_js("document.querySelector('#b').value='changed' ")
    with pytest.raises(BrowserError) as error:
        adapter.prepare(
            sid, tid, seen["revision"], {"type": "fill", "node_id": draft["node_id"], "text": "x"}
        )
    assert error.value.code == "STALE_NODE"
    tab.run_js("document.querySelector('#b').type='password'")
    with pytest.raises(BrowserError) as error:
        adapter.prepare(
            sid, tid, seen["revision"], {"type": "fill", "node_id": draft["node_id"], "text": "x"}
        )
    assert error.value.code in ("AUTH_REQUIRED", "SENSITIVE_TARGET")


def test_cancelled_checkbox_and_truncated_fill_do_not_claim_goal_success(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.innerHTML='<input type=checkbox aria-label=Checkbox onclick=\"event.preventDefault()\">'"
    )
    seen, rows = nodes(adapter, sid, tid)
    with pytest.raises(BrowserError) as error:
        adapter.act(
            sid,
            tid,
            seen["revision"],
            {"type": "check", "checked": True, "node_id": rows[0]["node_id"]},
        )
    assert error.value.code == "ACTION_GOAL_NOT_MET"
    assert error.value.details["action_result"] == {
        "performed": True,
        "target_state_verified": False,
    }
    assert not tab.run_js("return document.querySelector('input').checked")
    tab.run_js("document.body.innerHTML='<input maxlength=2 aria-label=Draft>'")
    seen, rows = nodes(adapter, sid, tid)
    with pytest.raises(BrowserError) as error:
        adapter.act(
            sid,
            tid,
            seen["revision"],
            {"type": "fill", "text": "abcdef", "node_id": rows[0]["node_id"]},
        )
    assert error.value.code == "ACTION_GOAL_NOT_MET"


def test_typing_drives_keyboard_listeners_and_select_fires_both_events(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.innerHTML='<input aria-label=Draft>';window.keys=[];"
        "document.querySelector('input').addEventListener('keyup',e=>window.keys.push(e.key));"
    )
    seen, rows = nodes(adapter, sid, tid)
    result = adapter.act(
        sid,
        tid,
        seen["revision"],
        {"type": "type", "text": "aB1!한", "node_id": rows[0]["node_id"]},
    )
    assert tab.run_js("return document.querySelector('input').value") == "aB1!한"
    assert tab.run_js("return window.keys") == ["a", "B", "1", "!"]
    assert result["action_result"]["typing_semantics"] == "ascii-key-events-unicode-text-insertion"
    tab.run_js(
        "document.body.innerHTML='<select aria-label=Selection><option value=a>A</option><option value=b>B</option></select>';"
        "window.events=[];for(const type of ['input','change'])document.querySelector('select').addEventListener(type,e=>window.events.push([e.type,e.isTrusted]));"
    )
    seen, rows = nodes(adapter, sid, tid)
    result = adapter.act(
        sid, tid, seen["revision"], {"type": "select", "value": "b", "node_id": rows[0]["node_id"]}
    )
    assert tab.run_js("return window.events") == [["input", False], ["change", False]]
    assert result["action_result"]["target_state_verified"] is True
    assert result["action_result"]["selection_events"] == "synthetic-input-change"


def test_goal_read_does_not_compare_a_control_that_became_sensitive(browser):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    tab.run_js(
        "document.body.innerHTML='<input aria-label=Draft oninput=\"this.type=String.fromCharCode(112,97,115,115,119,111,114,100)\">'"
    )
    seen, rows = nodes(adapter, sid, tid)
    with pytest.raises(BrowserError) as error:
        adapter.act(
            sid,
            tid,
            seen["revision"],
            {"type": "fill", "text": "test text", "node_id": rows[0]["node_id"]},
        )
    assert error.value.code == "RESULT_UNCERTAIN"
    assert error.value.details["action_result"]["performed"] is True
    assert "test text" not in str(error.value.details)


def test_registry_budget_omits_evicted_ids_and_inflight_retained_target_stays_valid(browser):
    adapter, sid, tid, _ = browser
    state = adapter._tab(sid, tid)
    _, registry = adapter._registry_for(state)
    registry.max_bytes = 65536
    state.tab.run_js(
        "document.body.innerHTML=Array.from({length:200},(_,i)=>"
        "'<button style=\"position:fixed;top:20px\">Button '+i+'</button>').join('')"
    )
    seen, rows = nodes(adapter, sid, tid)
    assert rows and seen["observation"]["interactive_truncated"]
    assert registry.bytes <= 65536
    assert all(row["node_id"] in registry.records for row in rows)
    retained = rows[-1]
    adapter.prepare(sid, tid, seen["revision"], {"type": "click", "node_id": retained["node_id"]})
    assert registry.bytes <= 65536
    evicted = next(nid for nid in reversed(registry.evicted) if nid not in registry.records)
    with pytest.raises(BrowserError) as error:
        adapter.prepare(sid, tid, seen["revision"], {"type": "click", "node_id": evicted})
    assert error.value.code == "STALE_NODE"
    assert error.value.details["reason"] == "registry_evicted"
