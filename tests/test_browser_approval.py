import asyncio
import time

import pytest
from test_browser import browser as browser
from test_browser import node

from cloud_browser.service import BrowserService
from cloud_browser.store import Store

pytestmark = pytest.mark.browser


@pytest.fixture
def balanced(browser):
    adapter, sid, tid, base = browser
    adapter.cfg.approval_policy = "balanced"
    adapter.navigate(sid, tid, "goto", base + "/approval.html")

    class AdapterWorker:
        async def call(self, method, **args):
            return await asyncio.to_thread(getattr(adapter, method), **args)

    store = Store(adapter.cfg.data_dir / "approval-test.sqlite3")
    service = BrowserService(adapter.cfg, store, worker=AdapterWorker())
    service.sessions[sid] = {"expires": time.time() + 600, "uncertain": False}
    yield service, adapter, sid, tid, base
    store.close()


def args_for(adapter, sid, tid, name, **action):
    obs, target = node(adapter, sid, tid, name)
    return {
        "session_id": sid,
        "tab_id": tid,
        "expected_revision": obs["revision"],
        "action": {"type": "click", "node_id": target["node_id"], **action},
    }


async def test_real_view_automatic_unknown_approval_and_duplicate_guard(balanced):
    service, adapter, sid, tid, _ = balanced
    click = args_for(adapter, sid, tid, "Personal Information")
    result = await service.call("act", **click)
    assert result["status"] == "ok" and result["action_policy"]["reason"] == "view_control"
    assert node(adapter, sid, tid, "Personal Information")[1]["expanded"] == "false"
    assert not service.pending
    assert (await service.call("act", **click))["error"]["code"] == "ACTION_ALREADY_DISPATCHED"
    unknown = args_for(adapter, sid, tid, "Generic action")
    proposal = await service.call("act", **unknown)
    assert proposal["status"] == "confirmation_required"
    token = proposal["confirmation"]["confirmation_token"]
    assert proposal["confirmation"]["action_policy"]["reason"] == "unclassified_activation"
    assert (await service.call("act", **unknown, confirmation_token=token))[
        "status"
    ] == "confirmation_required"
    assert adapter._tab(sid, tid).tab.run_js("return window.writes") == 0
    await service.approve(
        next(iter(service.pending)), True
    )  # Test-only private approval equivalent.
    executed = await service.call("act", **unknown, confirmation_token=token)
    assert executed["status"] == "ok"
    assert adapter._tab(sid, tid).tab.run_js("return window.writes") == 1
    assert (await service.call("act", **unknown))["error"]["code"] == "ACTION_ALREADY_DISPATCHED"


async def test_real_search_fill_select_check_and_enter_without_approval(balanced):
    service, adapter, sid, tid, _ = balanced
    for name, action, field, expected in (
        ("Search", {"type": "fill", "text": "Godot"}, "value", "Godot"),
        ("Order", {"type": "select", "value": "recent"}, "value", "recent"),
        ("Images only", {"type": "check", "checked": True}, "checked", True),
    ):
        result = await service.call("act", **args_for(adapter, sid, tid, name, **action))
        assert result["status"] == "ok" and not result["action_policy"]["approval_required"]
        assert node(adapter, sid, tid, name)[1][field] == expected
    result = await service.call(
        "act", **args_for(adapter, sid, tid, "Search", type="keypress", keys=["ENTER"])
    )
    assert result["status"] == "ok" and result["action_policy"]["reason"] == "get_search_submit"
    assert "q=Godot" in adapter._tab(sid, tid).tab.url
    assert not service.pending


async def test_real_link_automatic_general_edits_and_effects_still_gated(balanced):
    service, adapter, sid, tid, _ = balanced
    for name, action in (
        ("Draft message", {"type": "fill", "text": "Do not send"}),
        ("Display name", {"type": "fill", "text": "Do not save"}),
        ("Delete account", {"type": "click"}),
        ("Save profile", {"type": "click"}),
    ):
        result = await service.call("act", **args_for(adapter, sid, tid, name, **action))
        assert result["status"] == ("ok" if action["type"] == "fill" else "confirmation_required")
    assert adapter._tab(sid, tid).tab.run_js("return window.writes") == 0
    assert adapter._tab(sid, tid).tab.run_js("return document.querySelector('#draft').value") == "Do not send"
    result = await service.call("act", **args_for(adapter, sid, tid, "Documentation"))
    assert result["status"] == "ok" and result["action_policy"]["reason"] == "http_navigation"
    assert adapter._tab(sid, tid).tab.url.endswith("/browser.html")


async def test_real_dom_change_between_prepare_and_dispatch_still_blocks(balanced, monkeypatch):
    service, adapter, sid, tid, _ = balanced
    action = args_for(adapter, sid, tid, "Personal Information")
    prepare = adapter.prepare
    called = False

    def change_after_prepare(*args, **kwargs):
        nonlocal called
        result = prepare(*args, **kwargs)
        if not called:
            called = True
            adapter._tab(sid, tid).tab.run_js(
                "document.querySelector('#view').textContent='Delete account'"
            )
        return result

    monkeypatch.setattr(adapter, "prepare", change_after_prepare)
    result = await service.call("act", **action)
    assert result["error"]["code"] == "STALE_NODE"
    assert node(adapter, sid, tid, "Delete account")[1]["expanded"] == "true"


@pytest.mark.parametrize(
    "mutation",
    [
        "form.method='post'",
        "button.setAttribute('formmethod','post')",
        "button.setAttribute('formaction','https://other.example/search')",
        "button.textContent='Delete account'",
        "form.append(Object.assign(document.createElement('input'),{type:'hidden',name:'operation',value:'delete'}))",
        "form.append(Object.assign(document.createElement('button'),{textContent:'Another submit'}))",
    ],
    ids=[
        "post-form",
        "method-override",
        "action-override",
        "effect-submitter",
        "operation-field",
        "multiple-submitters",
    ],
)
async def test_real_implicit_search_submission_cannot_skip_approval(balanced, mutation):
    service, adapter, sid, tid, _ = balanced
    adapter._tab(sid, tid).tab.run_js(
        "const form=document.querySelector('form[role=search]');"
        "const button=form.querySelector('button');" + mutation
    )
    before = adapter._tab(sid, tid).tab.url
    result = await service.call(
        "act", **args_for(adapter, sid, tid, "Search", type="keypress", keys=["ENTER"])
    )
    assert result["status"] == "confirmation_required"
    assert adapter._tab(sid, tid).tab.url == before
