import pytest

from cloud_browser.navigation import outcome


async def test_close_updates_cached_selection_without_list_tabs(service, monkeypatch):
    sid = "test_session"
    service.tab_cache[sid] = {
        "tabs": [
            {"tab_id": "closed", "selected": True},
            {"tab_id": "remaining", "selected": False},
        ],
        "selected_tab_id": "closed",
    }

    async def close_result(method, **args):
        return {"session_id": sid, "tab_id": "closed", "selected_tab_id": "remaining"}

    monkeypatch.setattr(service.worker, "call", close_result)
    await service._rpc("close", session_id=sid, scope="tab", tab_id="closed")
    cached = service.tab_cache[sid]
    assert cached["selected_tab_id"] == "remaining"
    assert cached["tabs"] == [{"tab_id": "remaining", "selected": True}]


@pytest.mark.parametrize(
    "changes,operation,kind",
    [
        ({}, None, "none"),
        ({"document": "new"}, "reload", "reload"),
        ({"document": "new"}, None, "full_document"),
        ({"url": "https://example.com/#active"}, None, "same_document"),
        ({"sequence": 2, "same_document_kind": "history_api"}, None, "same_document"),
    ],
)
def test_navigation_evidence_is_not_url_equality(changes, operation, kind):
    before = {"url": "https://example.com/", "document": "old", "sequence": 1}
    result = outcome(before, before | changes, operation=operation)
    assert result["navigation_kind"] == kind
    assert result["navigation_occurred"] == (kind != "none")
    assert result["url_changed"] == ("url" in changes)
    assert result["document_changed"] == ("document" in changes)
    assert result["same_document_kind"] == changes.get("same_document_kind")
