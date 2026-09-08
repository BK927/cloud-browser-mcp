"""Operator-selected approval policy, not a proof that page JavaScript is harmless.

Balanced mode permits a small set of observed navigation/view/search operations.
Ordinary non-sensitive edits are automatic in balanced-v2. Execution buttons
with unclassified effects stay approval-gated. This is not a JS effect proof.
"""

import re
import unicodedata
from urllib.parse import unquote, urlsplit

PASSIVE_ACTIONS = frozenset({"scroll", "scroll_at", "move_to"})
_EFFECT = re.compile(
    r"\b(?:delete|remove|erase|destroy|purchase|buy|pay|payment|checkout|submit|send|publish|"
    r"save|update|upload|grant|revoke|subscribe|unsubscribe|logout|reset|transfer|confirm|"
    r"accept|agree|consent)\b|\b(?:log|sign)[\s_/-]*out\b|"
    r"삭제|구매|결제|주문|전송|발송|게시|등록|저장|변경|업로드|허용|권한|동의|구독|해지|탈퇴|로그아웃|초기화|확정|승인|"
    r"削除|購入|注文|送信|投稿|保存|更新|許可|権限|同意|登録|解約|退会",
    re.IGNORECASE,
)
_FOCUS_KEYS = frozenset({"TAB", "ESCAPE"})
_EDIT_KEYS = frozenset(
    {"ARROWUP", "ARROWDOWN", "ARROWLEFT", "ARROWRIGHT", "HOME", "END", "BACKSPACE", "DELETE"}
)
_PRIVILEGED = re.compile(
    r"password|otp|auth.?code|permission|access control|enable access|administrator|credit.?card|api.?key|권한|보안|비밀번호|인증|관리자|カード|権限",
    re.I,
)


def _effect(value):
    text = unicodedata.normalize("NFKC", str(value or ""))[:8192]
    for _ in range(3):
        text = unquote(text)
    return bool(_EFFECT.search(text.replace("_", " ")))


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
    if _effect(meta.get("name")) or meta.get("download") or meta.get("link_ping"):
        return result(True, "effect_or_transfer_indicator")
    if _PRIVILEGED.search(str(meta.get("name", ""))):
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
        if meta.get("tag") != "a" or not _http(meta["href"]) or _effect(meta["href"]):
            return result(True, "unclassified_or_effectful_link")
        return result(False, "http_navigation")
    if meta.get("view_control") in ("disclosure", "tab"):
        return result(False, "view_control")
    if search_form and meta.get("type") in ("checkbox", "radio"):
        return result(False, "search_filter")
    return result(True, "unclassified_activation")
