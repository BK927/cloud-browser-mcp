"""Operator-selected approval policy, not a proof that page JavaScript is harmless.

Balanced mode permits a small set of observed navigation/view/search operations.
Ordinary non-sensitive edits and bounded local UI patterns are automatic in balanced-v3. Execution buttons
with unclassified effects stay approval-gated. This is not a JS effect proof.
"""

import re
import unicodedata
from urllib.parse import unquote, urlsplit

PASSIVE_ACTIONS = frozenset({"scroll", "scroll_at", "move_to"})
_EFFECT_EN = (
    r"\b(?:delete|remove|erase|destroy|purchase|buy|pay|payment|checkout|submit|send|publish|"
    r"save|update|upload|grant|revoke|subscribe|unsubscribe|logout|reset|transfer|confirm|"
    r"accept|agree|consent)\b|\b(?:log|sign)[\s_/-]*out\b"
)
_EFFECT_CJK = (
    "삭제|구매|결제|주문|전송|발송|게시|등록|저장|변경|업로드|허용|권한|동의|구독|해지|탈퇴|로그아웃|초기화|확정|승인|"
    "削除|購入|注文|送信|投稿|保存|更新|許可|権限|同意|登録|解約|退会"
)
_EFFECT = re.compile(_EFFECT_EN + "|" + _EFFECT_CJK, re.IGNORECASE)
_LINK_EFFECT_EN = re.compile(_EFFECT_EN, re.IGNORECASE)
_FOCUS_KEYS = frozenset({"TAB", "ESCAPE"})
_EDIT_KEYS = frozenset(
    {"ARROWUP", "ARROWDOWN", "ARROWLEFT", "ARROWRIGHT", "HOME", "END", "BACKSPACE", "DELETE"}
)
_PRIVILEGED_EN = r"password|otp|auth.?code|permission|access control|enable access|administrator|credit.?card|api.?key"
_PRIVILEGED_CJK = "권한|보안|비밀번호|인증|관리자|カード|権限"
_PRIVILEGED = re.compile(_PRIVILEGED_EN + "|" + _PRIVILEGED_CJK, re.I)
_LINK_PRIVILEGED_EN = re.compile(_PRIVILEGED_EN, re.I)
_LINK_ENDINGS = ("하기", "하다", "합니다", "하세요", "해요", "완료")
_LINK_EFFECT_WORDS = frozenset(_EFFECT_CJK.split("|"))
_LINK_PRIVILEGED_WORDS = frozenset(_PRIVILEGED_CJK.split("|"))
_LINK_EFFECT_TOKENS = frozenset(
    word + ending for word in _LINK_EFFECT_WORDS for ending in _LINK_ENDINGS
)
_LINK_PRIVILEGED_TOKENS = frozenset(
    word + ending for word in _LINK_PRIVILEGED_WORDS for ending in _LINK_ENDINGS
)
_LINK_PATH_WORDS = frozenset((_EFFECT_CJK + "|" + _PRIVILEGED_CJK).split("|"))
_LOCAL_TOOLS = frozenset(
    {
        "selection",
        "select",
        "rectangle",
        "diamond",
        "ellipse",
        "arrow",
        "line",
        "pencil",
        "free draw",
        "freedraw",
        "draw",
        "hand",
        "pan",
        "lasso",
        "text",
        "선택",
        "사각형",
        "직사각형",
        "마름모",
        "타원",
        "화살표",
        "선",
        "연필",
        "자유 그리기",
        "손",
        "텍스트",
        "選択",
        "長方形",
        "楕円",
        "矢印",
        "手のひら",
        "テキスト",
    }
)
_LOCAL_HELP = frozenset(
    {"help", "keyboard shortcuts", "도움말", "키보드 단축키", "ヘルプ", "キーボードショートカット"}
)
_SENSITIVE_UI = re.compile(
    r"account|privacy|sharing|billing|security|permission|계정|개인정보|공유|결제|보안|권한|アカウント|プライバシー|共有",
    re.I,
)


def _decoded(value):
    text = unicodedata.normalize("NFKC", str(value or ""))[:8192]
    for _ in range(3):
        text = unquote(text)
    return text


def _effect(value):
    return bool(_EFFECT.search(_decoded(value).replace("_", " ")))


def _link_name(value, *, privileged=False):
    text = _decoded(value)
    english = _LINK_PRIVILEGED_EN if privileged else _LINK_EFFECT_EN
    if english.search(str(value or "") if privileged else text.replace("_", " ")):
        return True
    tokens = "".join(
        " " if c.isspace() or unicodedata.category(c).startswith("P") else c for c in text
    ).split()
    denied = _LINK_PRIVILEGED_TOKENS if privileged else _LINK_EFFECT_TOKENS
    bare_words = _LINK_PRIVILEGED_WORDS if privileged else _LINK_EFFECT_WORDS
    return any(
        token in denied or (token in bare_words and (len(tokens) <= 2 or index == len(tokens) - 1))
        for index, token in enumerate(tokens)
    )


def _link_href(value):
    text = _decoded(value)
    if _LINK_EFFECT_EN.search(text.replace("_", " ")):
        return True
    # Decode after URL parsing so escaped '?' in a path cannot become a query.
    path = _decoded(urlsplit(value).path)
    return any(segment in _LINK_PATH_WORDS for segment in re.split(r"[/_.-]", path))


def _http(value):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    if "\\" in value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or not url.hostname or url.username is not None:
            return None
        if url.port == 0:
            return None
        return url
    except ValueError:
        return None


def _origin(url):
    return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)


def decide(policy, action, meta=None, page_url=""):
    """Return bounded reason codes only; never return page strings or input values."""

    def result(required, reason):
        return {"mode": policy, "approval_required": required, "reason": reason}

    typ = action.get("type")
    if typ in PASSIVE_ACTIONS:
        return result(False, "passive_pointer_or_scroll")
    if policy != "balanced":
        return result(True, "strict_per_action")
    if typ in ("upload", "page_tool"):
        return result(True, "upload_or_page_tool")
    if typ in ("click_at", "double_click_at") or not meta:
        return result(True, "unidentified_target")
    keys = action.get("keys")
    key = keys[0] if typ == "keypress" and isinstance(keys, list) and len(keys) == 1 else None
    modifiers = set(action.get("modifiers", []))
    if key in _FOCUS_KEYS and not modifiers - {"SHIFT"}:
        return result(False, "focus_or_escape")
    plain_link = bool(
        typ in ("click", "double_click")
        and meta.get("tag") == "a"
        and meta.get("role") != "button"
        and _http(meta.get("href"))
        and not meta.get("download")
        and not meta.get("link_ping")
        and not meta.get("submits_form")
        and not meta.get("form_action")
    )
    name_effect = _link_name(meta.get("name")) if plain_link else _effect(meta.get("name"))
    if name_effect or meta.get("download") or meta.get("link_ping"):
        return result(True, "effect_or_transfer_indicator")
    if (
        _link_name(meta.get("name"), privileged=True)
        if plain_link
        else _PRIVILEGED.search(str(meta.get("name", "")))
    ):
        return result(True, "sensitive_or_permission_control")
    editable = not meta.get("readonly") and (
        meta.get("editable")
        or meta.get("tag") == "textarea"
        or (
            meta.get("tag") == "input"
            and meta.get("type") in ("text", "search", "email", "url", "tel", "number")
        )
    )
    if typ in ("fill", "type") and editable:
        return result(False, "ordinary_text_edit")
    if typ in ("select", "select_multiple") and meta.get("tag") == "select":
        return result(False, "ordinary_selection")
    if typ == "check" and meta.get("type") in ("checkbox", "radio"):
        return result(False, "ordinary_check")
    if (
        typ == "keypress"
        and editable
        and (
            (key in _EDIT_KEYS and not modifiers - {"SHIFT", "CONTROL"})
            or (key in ("A", "Z", "Y") and modifiers == {"CONTROL"})
        )
    ):
        return result(False, "ordinary_edit_key")
    # Enter/activation needs stronger evidence than ordinary editing.
    page, destination = _http(page_url), _http(meta.get("form_action"))
    search_form = bool(
        meta.get("search_form")
        and meta.get("form_method") == "GET"
        and page
        and destination
        and _origin(page) == _origin(destination)
        and not _effect(meta.get("form_action"))
        and not _effect(meta.get("search_submitter_name"))
    )
    search_input = bool(
        meta.get("tag") == "input"
        and (meta.get("type") == "search" or meta.get("role") == "searchbox" or search_form)
        and meta.get("type") in ("search", "text")
        and (not meta.get("form_action") or search_form)
        and not meta.get("readonly")
    )
    if typ == "fill" and search_input:
        return result(False, "search_input")
    if typ in ("select", "check") and search_form:
        return result(False, "search_filter")
    if typ == "keypress" and key in _EDIT_KEYS and search_input:
        return result(False, "search_edit_key")
    if typ == "keypress" and key == "ENTER" and search_form and search_input:
        if modifiers:
            return result(True, "modified_submission")
        return result(False, "get_search_submit")
    if (
        typ == "keypress"
        and key == "ENTER"
        and meta.get("search_context")
        and editable
        and not meta.get("form_action")
        and not modifiers
    ):
        return result(False, "structured_search_submit")
    activate = typ in ("click", "double_click") or (typ == "keypress" and key in ("ENTER", "SPACE"))
    if modifiers:
        return result(True, "modified_activation")
    if not activate:
        return result(True, "unclassified_edit_or_key")
    if meta.get("submits_form"):
        return result(not search_form, "get_search_submit" if search_form else "form_submission")
    if meta.get("href"):
        if (
            meta.get("tag") != "a"
            or not _http(meta["href"])
            or (_link_href(meta["href"]) if plain_link else _effect(meta["href"]))
        ):
            return result(True, "unclassified_or_effectful_link")
        return result(False, "http_navigation")
    if meta.get("view_control") in ("popup",) or meta.get("local_ui"):
        context = str(meta.get("ui_context", ""))
        if _PRIVILEGED.search(context) or _SENSITIVE_UI.search(context) or _effect(context):
            return result(True, "sensitive_ui_context")
    if meta.get("view_control") in ("disclosure", "tab", "popup"):
        return result(False, "view_control")
    name = unicodedata.normalize("NFKC", str(meta.get("name", ""))).strip().casefold()
    if not meta.get("form_action") and not meta.get("href"):
        if meta.get("local_ui") == "editor_tool" and name in _LOCAL_TOOLS:
            return result(False, "local_ui_tool_selection")
        if meta.get("local_ui") == "editor_help" and name in _LOCAL_HELP:
            return result(False, "local_ui_help")
    if search_form and meta.get("type") in ("checkbox", "radio"):
        return result(False, "search_filter")
    return result(True, "unclassified_activation")
