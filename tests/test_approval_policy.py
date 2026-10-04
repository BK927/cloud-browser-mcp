import pytest
from conftest import FakeWorker, fake_worker_resources
from pydantic import ValidationError

from cloud_browser.approval import decide
from cloud_browser.config import Settings
from cloud_browser.models import Configuration, NodeAction
from cloud_browser.service import BrowserService
from cloud_browser.store import Store

PAGE = "https://example.com/page"
LINK = {"tag": "a", "href": "https://example.com/docs", "name": "Documentation"}
VIEW = {"tag": "button", "type": "button", "view_control": "disclosure", "name": "Details"}
SEARCH = {
    "tag": "input",
    "type": "search",
    "name": "Search",
    "search_form": True,
    "form_method": "GET",
    "form_action": "https://example.com/search",
}


@pytest.mark.parametrize(
    "name",
    [
        "자유게시판",
        "게시글 목록",
        "등록일순",
        "주문 많은 순",
        "보안 취약점 분석 기사",
        "구독자 많은 채널",
        "削除記事",
        "注文履歴",
        "Undeleted articles",
    ],
)
@pytest.mark.parametrize("action", [{"type": "click"}, {"type": "double_click"}])
def test_balanced_plain_link_names_use_cjk_tokens(name, action):
    assert decide("balanced", action, LINK | {"name": name}, PAGE) == {
        "mode": "balanced",
        "approval_required": False,
        "reason": "http_navigation",
    }
    assert decide("strict", action, LINK | {"name": name}, PAGE)["approval_required"]


@pytest.mark.parametrize(
    "name",
    [
        "삭제",
        "로그아웃",
        "구독하기",
        "결제 완료",
        "회원 탈퇴",
        "게시하기",
        "Delete",
        "Sign out",
        "(삭제)",
        "削除",
        "権限",
        "권한",
        "등록 완료",
    ],
)
def test_balanced_plain_link_deny_tokens_still_require_approval(name):
    assert decide("balanced", {"type": "click"}, LINK | {"name": name}, PAGE)["approval_required"]


@pytest.mark.parametrize("ending", ["하기", "하다", "합니다", "하세요", "해요", "완료"])
@pytest.mark.parametrize("word", ["삭제", "인증"])
def test_balanced_plain_link_deny_endings(ending, word):
    assert decide("balanced", {"type": "click"}, LINK | {"name": word + ending}, PAGE)[
        "approval_required"
    ]
    assert decide(
        "balanced", {"type": "click"}, LINK | {"name": "공개 " + word + ending + " 후기"}, PAGE
    )["approval_required"]


@pytest.mark.parametrize(
    "name,approval_required",
    [
        ("합격 인증 후기", False),
        ("인증하기", True),
        ("보안", True),
        ("계정 보안", True),
        ("구독자 많은 채널", False),
        ("결제 완료", True),
        ("주문 많은 삭제", True),
        ("최근 인증 후기", False),
        ("인증 후기", True),
        ("공개 결제완료 후기", True),
        ("削除、関連、記事", False),
        ("最新、記事、削除", True),
    ],
)
def test_balanced_plain_link_name_position_and_token_count(name, approval_required):
    for action in ({"type": "click"}, {"type": "double_click"}):
        assert (
            decide("balanced", action, LINK | {"name": name}, PAGE)["approval_required"]
            is approval_required
        )
        assert decide("strict", action, LINK | {"name": name}, PAGE)["approval_required"]


@pytest.mark.parametrize(
    "path",
    [
        "/logout",
        "/delete?id=1",
        "/구매/123",
        "/%EA%B5%AC%EB%A7%A4/123",
        "/order-구매_123",
        "/구매.html",
        "/보안/123",
    ],
)
def test_balanced_plain_link_href_deny_segments(path):
    assert decide(
        "balanced", {"type": "click"}, LINK | {"href": "https://example.com" + path}, PAGE
    )["approval_required"]


@pytest.mark.parametrize(
    "href",
    [
        "https://search.naver.com/search.naver?query=%EA%B5%AC%EB%A7%A4",
        "https://example.com/구매내역/123",
        "https://example.com/?query=삭제",
        "https://example.com/등록일순",
        "https://example.com/#구매",
    ],
)
def test_balanced_plain_link_href_cjk_queries_and_substrings_are_navigation(href):
    assert not decide("balanced", {"type": "click"}, LINK | {"href": href}, PAGE)[
        "approval_required"
    ]


@pytest.mark.parametrize(
    "meta,action",
    [
        (LINK | {"tag": "button", "name": "자유게시판 등록"}, {"type": "click"}),
        (LINK | {"name": "자유게시판", "role": "button"}, {"type": "click"}),
        (LINK | {"name": "자유게시판", "form_action": PAGE}, {"type": "click"}),
        (LINK | {"name": "자유게시판", "submits_form": True}, {"type": "click"}),
        (LINK | {"name": "자유게시판", "download": True}, {"type": "click"}),
        (LINK | {"name": "자유게시판", "link_ping": True}, {"type": "click"}),
        (LINK | {"name": "자유게시판"}, {"type": "keypress", "keys": ["ENTER"]}),
        (LINK | {"tag": "input", "type": "text", "name": "자유게시판"}, {"type": "fill"}),
        (LINK | {"name": "자유게시판", "href": "javascript:void(0)"}, {"type": "click"}),
    ],
)
def test_cjk_link_exception_does_not_change_other_paths(meta, action):
    assert decide("balanced", action, meta, PAGE)["approval_required"]


@pytest.mark.parametrize("typ", ["scroll", "scroll_at", "move_to"])
@pytest.mark.parametrize("mode", ["strict", "balanced"])
def test_passive_actions_preserved(typ, mode):
    assert not decide(mode, {"type": typ}, page_url=PAGE)["approval_required"]


@pytest.mark.parametrize(
    "action,meta",
    [
        ({"type": "click"}, LINK),
        ({"type": "double_click"}, LINK),
        ({"type": "click"}, VIEW),
        ({"type": "click"}, VIEW | {"tag": "summary"}),
        ({"type": "click"}, VIEW | {"role": "tab", "view_control": "tab"}),
        ({"type": "keypress", "keys": ["ENTER"]}, VIEW),
        ({"type": "keypress", "keys": ["SPACE"]}, VIEW),
        ({"type": "keypress", "keys": ["TAB"]}, {"name": "Delete"}),
        ({"type": "keypress", "keys": ["ESCAPE"]}, {"name": "Delete"}),
        ({"type": "fill", "text": "Godot"}, SEARCH),
        ({"type": "fill", "text": "Godot"}, SEARCH | {"form_action": None, "search_form": False}),
        ({"type": "select", "value": "recent"}, SEARCH | {"tag": "select", "type": "select-one"}),
        ({"type": "check", "checked": True}, SEARCH | {"type": "checkbox"}),
        ({"type": "keypress", "keys": ["DELETE"]}, SEARCH),
        ({"type": "keypress", "keys": ["ARROWLEFT"]}, SEARCH),
        ({"type": "keypress", "keys": ["ENTER"]}, SEARCH),
        ({"type": "click"}, SEARCH | {"tag": "button", "type": "submit", "submits_form": True}),
    ],
    ids=[
        "link",
        "double-link",
        "disclosure",
        "summary",
        "tab",
        "view-enter",
        "view-space",
        "focus-tab",
        "escape",
        "search-fill",
        "standalone-search",
        "search-select",
        "search-check",
        "search-delete",
        "search-arrow",
        "search-enter",
        "search-submit",
    ],
)
def test_balanced_allows_recognized_actions_and_strict_does_not(action, meta):
    assert not decide("balanced", action, meta, PAGE)["approval_required"]
    assert decide("strict", action, meta, PAGE)["approval_required"]


@pytest.mark.parametrize(
    "action,meta",
    [
        ({"type": "click"}, {"tag": "button", "name": "Continue"}),
        ({"type": "click"}, {"tag": "button", "name": "Search", "expanded": "true"}),
        ({"type": "click"}, VIEW | {"name": "Delete account"}),
        ({"type": "click"}, VIEW | {"name": "결제"}),
        ({"type": "click"}, VIEW | {"name": "購入"}),
        ({"type": "click"}, LINK | {"download": True}),
        ({"type": "click"}, LINK | {"link_ping": True}),
        ({"type": "click"}, LINK | {"href": "javascript:void(0)"}),
        ({"type": "click"}, LINK | {"href": "https://example.com/%2564elete"}),
        ({"type": "click"}, LINK | {"href": "https://example.com/?action=delete"}),
        ({"type": "click"}, LINK | {"href": "https://user:secret@example.com/"}),
        ({"type": "click"}, LINK | {"href": "https://example.com\\evil.test"}),
        ({"type": "click"}, LINK | {"href": "https://example.com:bad/"}),
        ({"type": "click"}, LINK | {"href": "https://example.com/log_out"}),
        ({"type": "click"}, LINK | {"tag": "button"}),
        ({"type": "click_at"}, VIEW),
        ({"type": "double_click_at"}, VIEW),
        ({"type": "upload"}, SEARCH),
        ({"type": "page_tool"}, SEARCH),
        ({"type": "check"}, {"tag": "input", "type": "checkbox", "name": "Enable access"}),
        ({"type": "select"}, {"tag": "select", "name": "Permission"}),
        (
            {"type": "keypress", "keys": ["ENTER"]},
            SEARCH | {"search_submitter_name": "Delete account"},
        ),
        ({"type": "click"}, VIEW | {"submits_form": True}),
        ({"type": "keypress", "keys": ["ENTER"]}, {"tag": "button", "name": "Continue"}),
        ({"type": "keypress", "keys": ["DELETE"]}, VIEW),
        ({"type": "keypress", "keys": []}, VIEW),
        ({"type": "keypress", "keys": ["TAB", "ENTER"]}, VIEW),
        ({"type": "future_action"}, VIEW),
        ({"type": "click"}, None),
    ],
    ids=[
        "unknown",
        "expanded-alone",
        "delete",
        "korean",
        "japanese",
        "download",
        "ping",
        "javascript",
        "encoded-effect",
        "query-effect",
        "userinfo",
        "backslash",
        "bad-port",
        "logout",
        "non-anchor-href",
        "coordinate",
        "coordinate-double",
        "upload",
        "page-tool",
        "general-check",
        "general-select",
        "implicit-effect-submitter",
        "submit-wins",
        "unknown-enter",
        "view-delete",
        "empty-keys",
        "multi-keys",
        "future",
        "missing-meta",
    ],
)
def test_balanced_keeps_approval_for_unclassified_or_effectful_actions(action, meta):
    assert decide("balanced", action, meta, PAGE)["approval_required"]


@pytest.mark.parametrize(
    "action,meta",
    [
        ({"type": "fill"}, {"tag": "textarea", "name": "Message", "editable": True}),
        ({"type": "fill"}, {"tag": "input", "type": "text", "name": "Display name"}),
        ({"type": "check"}, {"tag": "input", "type": "checkbox", "name": "Images only"}),
        ({"type": "select"}, {"tag": "select", "name": "Category"}),
        ({"type": "fill"}, SEARCH | {"form_method": "POST"}),
        ({"type": "fill"}, SEARCH | {"form_action": "https://other.example/search"}),
        ({"type": "fill"}, SEARCH | {"form_action": "https://example.com:444/search"}),
        ({"type": "fill"}, SEARCH | {"form_action": "https://example.com/submit"}),
    ],
)
def test_balanced_v2_edits_are_not_implicit_form_submission(action, meta):
    assert not decide("balanced", action, meta, PAGE)["approval_required"]
    assert decide("strict", action, meta, PAGE)["approval_required"]


def test_operator_only_opt_in_and_reason_has_no_page_data(monkeypatch):
    monkeypatch.setenv("APPROVAL_TEST_APPROVAL_POLICY", "balanced")
    cfg = Settings(_env_file=None, _env_prefix="APPROVAL_TEST_", development=True)
    assert cfg.approval_policy == "balanced"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, development=True, approval_policy="off")
    with pytest.raises(ValidationError):
        Configuration(approval_policy="balanced")
    with pytest.raises(ValidationError):
        NodeAction(type="click", node_id="node_1", approved=True)
    decision = decide("balanced", {"type": "fill", "text": "not-for-output"}, SEARCH, PAGE)
    assert set(decision) == {"mode", "reason", "approval_required"}
    assert "not-for-output" not in str(decision)


class AutomaticWorker(FakeWorker):
    async def call(self, method, **args):
        result = await super().call(method, **args)
        if method == "prepare":
            result["requires_confirmation"] = False
            result["action_policy"] = decide("balanced", args["action"], VIEW, PAGE)
        if method == "act":
            # Simulate a dispatched event without observable revision change.
            self.sessions[args["session_id"]][args["tab_id"]]["revision"] -= 1
            result["revision"] -= 1
        return result


@pytest.mark.parametrize("uncertain", [False, True])
async def test_automatic_dispatch_is_not_retried_without_visible_change(cfg, uncertain):
    cfg.approval_policy = "balanced"
    worker = AutomaticWorker()
    store = Store(cfg.data_dir / "balanced-test.sqlite3")
    service = BrowserService(cfg, store, worker=worker)
    service.resources = fake_worker_resources
    try:
        opened = await service.call("open")
        args = {k: opened[k] for k in ("session_id", "tab_id")}
        worker.uncertain = uncertain
        action = args | {"expected_revision": 1, "action": {"type": "click", "node_id": "node_1"}}
        first = await service.call("act", **action)
        assert first["status"] == ("error" if uncertain else "ok")
        second = await service.call("act", **action)
        assert second["error"]["code"] == (
            "RESULT_UNCERTAIN" if uncertain else "ACTION_ALREADY_DISPATCHED"
        )
        assert worker.executions == 1
        assert not service.pending
        status = await service.call("status")
        assert status["capabilities"]["approval_policy"] == "balanced-v3"
    finally:
        await service.shutdown()
        store.close()


@pytest.mark.parametrize(
    "meta,allowed",
    [
        ({"name": "Rectangle", "local_ui": "editor_tool"}, True),
        ({"name": "Help", "local_ui": "editor_help"}, True),
        ({"name": "Help"}, False),
        ({"name": "Rectangle", "pressed": "false"}, False),
        ({"name": "Image", "local_ui": "editor_tool"}, False),
        ({"name": "Eraser", "local_ui": "editor_tool"}, False),
        ({"name": "Grant access", "local_ui": "editor_tool"}, False),
        ({"name": "Help", "local_ui": "editor_help", "ui_context": "Account permissions"}, False),
        ({"name": "Rectangle", "local_ui": "editor_tool", "submits_form": True}, False),
        ({"name": "New Todo Input", "tag": "input", "type": "text"}, False),
    ],
)
def test_local_ui_is_not_a_general_activation_bypass(meta, allowed):
    action = (
        {"type": "click"} if meta.get("tag") != "input" else {"type": "keypress", "keys": ["ENTER"]}
    )
    target = {"tag": "button", "type": "button"} | meta
    assert decide("balanced", action, target, PAGE)["approval_required"] is not allowed
    assert decide("strict", action, target, PAGE)["approval_required"]
    assert decide("balanced", action | {"modifiers": ["CONTROL"]}, target, PAGE)[
        "approval_required"
    ]
