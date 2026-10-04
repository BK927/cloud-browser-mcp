"""Controlled regressions for bounded, privacy-preserving rendered observation."""

import json

import pytest
from test_browser import browser as browser

from cloud_browser.drission import SNAPSHOT

pytestmark = pytest.mark.browser


def install(browser, html, script=""):
    adapter, sid, tid, _ = browser
    adapter._tab(sid, tid).tab.run_js("document.body.innerHTML=" + json.dumps(html) + ";" + script)
    return adapter, sid, tid


def snapshot(browser, **options):
    adapter, sid, tid, _ = browser
    tab = adapter._tab(sid, tid).tab
    frame = tab.run_cdp("Page.getFrameTree")["frameTree"]["frame"]["id"]
    world = tab.run_cdp(
        "Page.createIsolatedWorld", frameId=frame, worldName="cloud-browser-observer"
    )["executionContextId"]
    response = tab.run_cdp(
        "Runtime.evaluate",
        contextId=world,
        expression="globalThis.__cbOptions="
        + json.dumps({"mode": "auto", **options})
        + ";(()=>{const result="
        + SNAPSHOT
        + ";return result.data;})()",
        returnByValue=True,
    )
    assert "exceptionDetails" not in response, response
    return response["result"]["value"]


def test_public_article_remains_readable_beside_protected_form(browser):
    adapter, sid, tid = install(
        browser,
        "<article><h1>Public article</h1><p>Public body</p><button>Read more</button></article>"
        '<form><p>PRIVATE-FORM-LABEL</p><input name="username" value="PRIVATE-USERNAME">'
        '<input type="password" value="PRIVATE-PASSWORD"><button>Private submit</button></form>',
    )
    raw = snapshot(browser)
    assert not raw["protected"]
    assert raw["has_sensitive_regions"] and raw["protected_regions"]
    assert "Public body" in raw["semantic_text"]
    assert [n["name"] for n in raw["nodes"]] == ["Read more"]
    assert "PRIVATE-" not in json.dumps(raw)
    seen = adapter.observe(sid, tid, mode="semantic")
    assert "Public body" in seen["observation"]["semantic_snapshot"]


@pytest.mark.parametrize(
    "private_input",
    [
        '<input type="hidden" name="otp" value="PRIVATE-CODE">',
        '<input type="hidden" name="access_token" value="PRIVATE-CODE">',
        '<input style="display:none" autocomplete="one-time-code" value="PRIVATE-CODE">',
        '<div contenteditable="true" aria-label="API key">PRIVATE-CODE</div>',
    ],
)
def test_hidden_and_editable_secrets_exclude_entire_owning_form(browser, private_input):
    install(
        browser,
        '<p>Public body</p><button>Public button</button><form id="private">'
        + private_input
        + '<input name="normal" value="PRIVATE-NORMAL"><button>Private button</button></form>'
        '<input form="private" name="associated" value="PRIVATE-ASSOCIATED">'
        '<button aria-labelledby="private">Safe reference</button>',
    )
    raw = snapshot(browser)
    assert "PRIVATE-" not in json.dumps(raw)
    assert "Private button" not in raw["text"]
    assert {n["name"] for n in raw["nodes"]} == {"Public button", "Safe reference"}
    assert raw["_forms"] == []


def test_open_nested_shadow_and_slots_are_read_once_without_secrets(browser):
    install(
        browser,
        '<div id="host"><p slot="body">Assigned public text</p></div>',
        "const host=document.querySelector('#host');const shadow=host.attachShadow({mode:'open'});"
        "shadow.innerHTML='<h1>Shadow article</h1><slot name=body></slot><div id=nested></div>'"
        ";const nested=shadow.querySelector('#nested').attachShadow({mode:'open'});"
        "nested.innerHTML='<button>Shadow button</button><form><input type=password value=PRIVATE-PASSWORD>'"
        "+'<p>PRIVATE-FORM-TEXT</p><button>Private submit</button></form>';",
    )
    raw = snapshot(browser)
    assert raw["semantic_text"].count("Assigned public text") == 1
    assert "Shadow article" in raw["semantic_text"]
    assert [n["name"] for n in raw["nodes"]] == ["Shadow button"]
    assert "PRIVATE-" not in json.dumps(raw)


def test_selector_uses_rendered_description_not_first_hidden_duplicate(browser):
    install(
        browser,
        '<div id="description" hidden>Hidden duplicate</div>'
        '<div id="description">Visible description</div>',
    )
    raw = snapshot(browser, query={"selector": "#description", "limit": 1})
    assert raw["semantic_text"] == "Visible description"
    assert raw["semantic_source"] == "div"
    assert raw["query_match_count"] == 2
    assert raw["query_empty_reason"] is None


def test_interactive_selector_with_nodes_is_not_reported_as_empty(browser):
    adapter, sid, tid = install(browser, "<button id=public>Public control</button>")
    seen = adapter.observe(sid, tid, mode="interactive", query={"selector": "#public"})
    assert seen["observation"]["interactive_snapshot"]
    assert seen["observation"]["query_match_count"] == 1
    assert seen["observation"]["query_empty_reason"] is None


def test_scope_uses_rendered_match_not_first_hidden_duplicate(browser):
    install(
        browser,
        '<section class="description" hidden>Hidden scope</section>'
        '<section class="description"><p>Visible scope</p></section>',
    )
    raw = snapshot(browser, query={"scope": ".description"})
    assert raw["semantic_text"] == "Visible scope"
    assert raw["semantic_source"] == "section"
    assert raw["query_match_count"] == 2
    assert raw["query_empty_reason"] is None


@pytest.mark.parametrize(
    ("html", "selector", "reason"),
    [
        ("<p>Public</p>", "#missing", "missing"),
        ('<div id="description" hidden>Hidden</div>', "#description", "hidden"),
        ('<div id="description"></div>', "#description", "hidden"),
        ('<form id="private"><input type=password></form>', "#private", "protected"),
    ],
)
def test_empty_query_has_explicit_reason(browser, html, selector, reason):
    install(browser, html)
    raw = snapshot(browser, query={"selector": selector})
    assert raw["semantic_text"] == ""
    assert raw["query_empty_reason"] == reason


@pytest.mark.parametrize("style", ["display:contents", "height:0;overflow:visible"])
def test_rendered_children_of_empty_wrapper_can_be_queried(browser, style):
    install(browser, f'<div id="description" style="{style}"><p>Visible child</p></div>')
    raw = snapshot(browser, query={"selector": "#description"})
    assert raw["semantic_text"] == "Visible child"
    assert raw["query_empty_reason"] is None


def test_deep_dom_uses_iterative_bounded_walk(browser):
    install(
        browser,
        '<article id="root"></article>',
        "let root=document.querySelector('#root');for(let i=0;i<1500;i++){"
        "const next=document.createElement('div');root.appendChild(next);root=next;}"
        "root.innerHTML='<p>Deep public text</p>';",
    )
    raw = snapshot(browser)
    assert "Deep public text" in raw["semantic_text"]
    assert not raw["privacy_incomplete"]


def test_security_budget_exhaustion_is_explicit_and_fail_closed(browser):
    install(
        browser,
        "<p>Public body</p>",
        "document.body.insertAdjacentHTML('beforeend','<span></span>'.repeat(21000));",
    )
    raw = snapshot(browser, lightweight=True)
    assert raw["privacy_incomplete"] and raw["protected"]
    assert raw["nodes"] == [] and raw["_form_states"] == []
    assert not raw["text"] and not raw["semantic_text"]


def test_many_protected_regions_limit_images_without_discarding_public_text(browser):
    install(
        browser,
        "<p>Public body</p>",
        "document.body.insertAdjacentHTML('beforeend','<form><input type=password></form>'.repeat(101));",
    )
    raw = snapshot(browser)
    assert raw["privacy_mask_unsafe"]
    assert not raw["protected"] and not raw["privacy_incomplete"]
    assert "Public body" in raw["semantic_text"]


def test_challenge_phrases_in_article_are_not_access_blocking_evidence(browser):
    install(
        browser,
        "<article><h1>CAPTCHA research</h1><p>Verify you are human, automated traffic "
        "and complete the captcha are examples of challenge text.</p></article>",
    )
    assert snapshot(browser)["challenge"] is None


def test_visible_challenge_control_and_block_panel_remain_reported(browser):
    install(
        browser,
        '<form><p>Verify you are human</p><input id="captcha-answer"><button>Continue</button></form>',
    )
    assert snapshot(browser)["challenge"] == "captcha"
    install(browser, '<div role="alert">Automated traffic access is blocked</div>')
    assert snapshot(browser)["challenge"] == "bot"
