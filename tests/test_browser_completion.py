import json

import pytest
from test_browser import browser as browser
from test_browser import node

from cloud_browser.models import BrowserError

pytestmark = pytest.mark.browser


def test_native_accessibility_name_and_select_choices(browser):
    adapter, sid, tid, _ = browser
    adapter._tab(sid, tid).tab.run_js("""
        document.body.innerHTML = '<label for="search"><span>Accessible search</span></label>' +
          '<input id="search"><select aria-label="Sort order"><option value="old">Oldest</option>' +
          '<optgroup disabled label="Unavailable"><option value="blocked">Restricted</option></optgroup>' +
          '<option value="new">Newest</option></select>';
    """)
    obs, search = node(adapter, sid, tid, "Accessible search")
    assert search["role"] == "textbox"
    assert obs["observation"]["accessibility_source"] == "chromium-ax"
    obs, select = node(adapter, sid, tid, "Sort order")
    assert [(x["value"], x["label"]) for x in select["options"]] == [
        ("old", "Oldest"),
        ("blocked", "Restricted"),
        ("new", "Newest"),
    ]
    for value in ("blocked", "missing"):
        with pytest.raises(BrowserError) as exc:
            adapter.prepare(
                sid,
                tid,
                obs["revision"],
                {"type": "select", "node_id": select["node_id"], "value": value},
            )
        assert exc.value.code == "NODE_NOT_ACTIONABLE"
    adapter.act(
        sid, tid, obs["revision"], {"type": "select", "node_id": select["node_id"], "value": "new"}
    )
    assert node(adapter, sid, tid, "Sort order")[1]["value"] == "new"


def test_scroll_container_has_node_and_scroll_updates_revision(browser):
    adapter, sid, tid, _ = browser
    adapter._tab(sid, tid).tab.run_js("""
        document.body.innerHTML = '<div aria-label="Article pane" style="height:80px;width:250px;overflow:auto">' +
          '<div style="height:1000px">Scrollable article</div></div>';
    """)
    obs, pane = node(adapter, sid, tid, "Article pane")
    assert pane["scrollable"]
    result = adapter.act(
        sid,
        tid,
        obs["revision"],
        {
            "type": "scroll",
            "node_id": pane["node_id"],
            "delta_x": 0,
            "delta_y": 200,
        },
    )
    assert result["action_result"]["page_changed"]
    current, pane_after = node(adapter, sid, tid, "Article pane")
    assert pane_after["scroll"]["y"] == 200
    assert current["revision"] > obs["revision"]


@pytest.mark.parametrize(
    "text,code",
    [
        ("Verify that you are human", "CAPTCHA_REQUIRED"),
        ("Automation access is blocked", "BOT_BLOCKED"),
    ],
)
def test_challenge_blocks_action_even_without_observe(browser, text, code):
    adapter, sid, tid, _ = browser
    obs, button = node(adapter, sid, tid, "Personal Information")
    adapter._tab(sid, tid).tab.run_js(
        "document.body.insertAdjacentHTML('afterbegin', '<p>' + arguments[0] + '</p>')", text
    )
    with pytest.raises(BrowserError) as exc:
        adapter.prepare(sid, tid, obs["revision"], {"type": "click", "node_id": button["node_id"]})
    assert exc.value.code == code


def test_post_dispatch_observation_failure_is_uncertain(browser, monkeypatch):
    adapter, sid, tid, _ = browser
    obs, button = node(adapter, sid, tid, "Personal Information")
    original = adapter._capture_state
    calls = 0

    def capture(state, **options):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise BrowserError("OBSERVATION_FAILED", "Test post-click capture failure")
        return original(state, **options)

    monkeypatch.setattr(adapter, "_capture_state", capture)
    with pytest.raises(BrowserError) as exc:
        adapter.act(sid, tid, obs["revision"], {"type": "click", "node_id": button["node_id"]})
    assert exc.value.code == "RESULT_UNCERTAIN"


def test_secret_option_metadata_is_redacted_recursively(browser):
    adapter, sid, tid, _ = browser
    secret = "ghp_" + "a" * 24
    adapter._tab(sid, tid).tab.run_js(
        """
        document.body.innerHTML = '<select aria-label="Choice"><option value="' + arguments[0] + '">Item</option></select>';
    """,
        secret,
    )
    observed = adapter.observe(sid, tid, mode="interactive")
    assert secret not in json.dumps(observed)


def test_auth_resume_refuses_still_sensitive_screen(browser):
    adapter, sid, tid, _ = browser
    adapter.focus(sid, tid)
    adapter._tab(sid, tid).tab.run_js('document.body.innerHTML = "<input type=password>"')
    with pytest.raises(BrowserError) as exc:
        adapter.resume(sid, tid)
    assert exc.value.code == "AUTH_REQUIRED"


def test_real_back_forward_reload_and_empty_history(browser):
    adapter, sid, tid, base = browser
    first = adapter.observe(sid, tid, mode="interactive")
    second = adapter.navigate(sid, tid, "goto", base + "/browser.html?step=two")
    back = adapter.navigate(sid, tid, "back")
    assert adapter._tab(sid, tid).tab.url == base + "/browser.html"
    forward = adapter.navigate(sid, tid, "forward")
    assert adapter._tab(sid, tid).tab.url == base + "/browser.html?step=two"
    reloaded = adapter.navigate(sid, tid, "reload")
    assert (
        first["revision"]
        < second["revision"]
        < back["revision"]
        < forward["revision"]
        < reloaded["revision"]
    )
    no_history = adapter.navigate(sid, tid, "forward")
    assert no_history["status"] == "no_change"
    assert no_history["revision"] == reloaded["revision"]


def test_failed_navigation_invalidates_observed_coordinates(browser, monkeypatch):
    adapter, sid, tid, base = browser
    before = adapter.observe(sid, tid, mode="visual")
    state = adapter._tab(sid, tid)
    original = state.tab.run_cdp

    def fail_navigation(command, **kwargs):
        if command == "Page.navigate":
            return {"errorText": "net::ERR_CONNECTION_REFUSED"}
        return original(command, **kwargs)

    monkeypatch.setattr(state.tab, "run_cdp", fail_navigation)
    with pytest.raises(BrowserError) as exc:
        adapter.navigate(sid, tid, "goto", base + "/failure")
    assert exc.value.code == "NAVIGATION_FAILED"
    assert state.screenshot is None
    assert not state.nodes and not state.cursors
    assert adapter.observe(sid, tid, mode="interactive")["revision"] > before["revision"]


def test_unknown_reload_requests_manual_control_without_unusable_token(browser):
    adapter, sid, tid, _ = browser
    state = adapter._tab(sid, tid)
    state.document_method = None
    before = state.revision
    with pytest.raises(BrowserError) as exc:
        adapter.navigate(sid, tid, "reload")
    assert exc.value.status == "user_action_required"
    assert "browser_handoff" in exc.value.message
    assert state.revision == before


def test_tab_listing_tracks_human_selection_and_reuse(browser):
    adapter, sid, tid, _ = browser
    second = adapter.open(sid)
    assert second["tab_id"] != tid
    adapter._session(sid)["browser"].activate_tab(adapter._tab(sid, tid).tab.tab_id)
    tabs = adapter.list_tabs(sid)
    assert tabs["selected_tab_id"] == tid
    assert tabs["tabs"][0]["tab_id"] == tid
    assert adapter.open(sid, new_tab=False)["tab_id"] == tid


def test_history_with_unknown_request_method_is_not_replayed(browser):
    adapter, sid, tid, base = browser
    adapter.navigate(sid, tid, "goto", base + "/browser.html?next=1")
    state = adapter._tab(sid, tid)
    state.history_methods.clear()
    before = state.tab.url
    with pytest.raises(BrowserError) as exc:
        adapter.navigate(sid, tid, "back")
    assert exc.value.status == "user_action_required"
    assert state.tab.url == before
