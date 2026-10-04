import json
import socket
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cloud_browser import operation_diagnostics as diagnostics
from cloud_browser import security
from cloud_browser.drission import DrissionAdapter
from cloud_browser.models import BrowserError, response


@pytest.mark.parametrize(
    "codes,category,retryable,tool",
    [
        (
            "STALE_NODE STALE_REVISION STALE_SCREENSHOT CURSOR_STALE FRAME_STALE SCREEN_CHANGED DOM_TARGET_AVAILABLE",
            "stale_state",
            True,
            "browser_observe",
        ),
        ("BROWSER_BUSY RESOURCE_PRESSURE CAPTURE_TIMEOUT", "capacity", True, "browser_status"),
        ("WORKER_TIMEOUT CLEANUP_REQUIRED", "capacity", False, "browser_status"),
        ("CONFIRMATION_STALE", "approval", True, "browser_observe"),
        ("CONFIRMATION_USED ACTION_ALREADY_DISPATCHED", "approval", False, "browser_observe"),
        ("CONFIRMATION_REQUIRED CONFIRMATION_DENIED", "approval", False, "browser_status"),
        (
            "NAVIGATION_TIMEOUT NAVIGATION_FAILED NAVIGATION_CANCELLED NAVIGATION_IN_PROGRESS",
            "navigation",
            False,
            "browser_observe",
        ),
        (
            "NODE_NOT_ACTIONABLE NODE_AMBIGUOUS NODE_NOT_FOUND TAB_NOT_FOUND FRAME_UNAVAILABLE ACTION_GOAL_NOT_MET",
            "target",
            False,
            "browser_observe",
        ),
        (
            "INVALID_INPUT INVALID_URL INVALID_SELECTOR INVALID_COORDINATES LEASE_INVALID OPERATION_CONFLICT UNSUPPORTED_OPERATION",
            "input",
            False,
            "browser_status",
        ),
        ("READ_NOT_FOUND", "input", False, "browser_read"),
        ("LEASE_REQUIRED", "input", False, "browser_open"),
        ("PRIVACY_INSPECTION_INCOMPLETE", "page_limit", False, None),
        ("SENSITIVE_SCREEN", "privacy_guard", False, "browser_observe"),
        (
            "SENSITIVE_INPUT SENSITIVE_TARGET SENSITIVE_CONTENT POLICY_BLOCKED",
            "privacy_guard",
            False,
            "browser_status",
        ),
        ("CAPTCHA_REQUIRED BOT_BLOCKED", "site_challenge", False, None),
        ("AUTH_REQUIRED", "site_challenge", False, "browser_auth_request"),
        (
            "USER_CONTROL_ACTIVE AUTH_IN_PROGRESS HANDOFF_UNAVAILABLE",
            "human_control",
            False,
            "browser_status",
        ),
        ("SESSION_EXPIRED SESSION_NOT_FOUND SESSION_CLOSED", "session", False, "browser_open"),
        ("RESULT_UNCERTAIN", "uncertain", False, "browser_close"),
        ("BROWSER_ERROR UNLISTED_ERROR", "browser", False, "browser_status"),
    ],
)
def test_error_policy(service, codes, category, retryable, tool):
    for code in codes.split():
        error = service._error_response(BrowserError(code, "page payload"))["error"]
        assert error["category"] == category
        assert error["retryable"] is retryable
        assert error["suggested_tool"] == tool
        assert error["next_step"].isascii()
        assert "page payload" not in error["next_step"]


@pytest.mark.parametrize(
    "enabled,registered", [(False, False), (False, True), (True, False), (True, True)]
)
@pytest.mark.parametrize(
    "code", ["RESULT_UNCERTAIN", "CAPTCHA_REQUIRED", "BOT_BLOCKED", "PRIVACY_INSPECTION_INCOMPLETE"]
)
def test_handoff_suggestion_requires_enabled_registered_tool(service, enabled, registered, code):
    service.cfg.manual_control_enabled = enabled
    service.handoff_registered = registered
    error = service._error_response(BrowserError(code, "test"))["error"]
    fallback = "browser_close" if code == "RESULT_UNCERTAIN" else None
    assert error["suggested_tool"] == ("browser_handoff" if enabled and registered else fallback)
    assert error["retryable"] is False


def test_page_limit_is_error_but_still_refuses(service):
    with pytest.raises(BrowserError) as raised:
        DrissionAdapter._guard_page({"privacy_incomplete": True})
    assert raised.value.status == "error"
    result = service._error_response(raised.value)
    assert result["status"] == "error"
    assert (
        result["error"]["message"]
        == "Page exceeds the bounded privacy scan (element/input budget); size limit, not a detected secret"
    )
    assert "observation" not in result
    # Normalize legacy/worker blocked errors too, without allowing the operation.
    assert (
        service._error_response(BrowserError("PRIVACY_INSPECTION_INCOMPLETE", "old", "blocked"))[
            "status"
        ]
        == "error"
    )


@pytest.mark.parametrize(
    "url,reason",
    [
        ("file:///secret", "scheme"),
        ("https://user:secret@example.com", "credentials"),
        ("https://example.com:9000", "port"),
        ("https://example.com:bad", "port"),
        ("http://127.0.0.1", "private_address"),
        ("", "missing"),
        ("https:///path", "missing"),
    ],
)
def test_invalid_url_syntax_reasons(url, reason):
    with pytest.raises(BrowserError) as raised:
        security.validate_url(url)
    assert raised.value.code == "INVALID_URL"
    assert raised.value.details == {"reason": reason}


@pytest.mark.parametrize(
    "addresses,reason",
    [(None, "unresolved"), ([], "unresolved"), (["8.8.8.8", "10.0.0.1"], "private_address")],
)
def test_invalid_url_dns_reasons(monkeypatch, addresses, reason):
    def resolve(*args, **kwargs):
        if addresses is None:
            raise socket.gaierror("private host diagnostic")
        return [(0, 0, 0, "", (ip, 443)) for ip in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(BrowserError) as raised:
        security.validate_url("https://example.com")
    assert raised.value.details == {"reason": reason}


def test_proxy_rejection_reason(monkeypatch):
    connection = Mock()
    connection.getresponse.return_value = SimpleNamespace(
        status=403, getheader=lambda name: security.DNS_POLICY_VERSION
    )
    monkeypatch.setattr(security.http.client, "HTTPConnection", lambda *a, **kw: connection)
    with pytest.raises(BrowserError) as raised:
        security.validate_url("https://example.com", dns_proxy="http://egress:3128")
    assert raised.value.code == "INVALID_URL"
    assert raised.value.details == {"reason": "egress_rejected"}
    connection.close.assert_called_once()


async def test_missing_navigation_url_reason_at_service_and_adapter(service):
    opened = await service.call("open")
    result = await service.call(
        "navigate", session_id=opened["session_id"], tab_id=opened["tab_id"], operation="goto"
    )
    assert result["error"]["code"] == "INVALID_URL"
    assert result["reason"] == "missing"
    adapter = DrissionAdapter.__new__(DrissionAdapter)
    adapter.cfg = service.cfg
    adapter._tab = lambda *args: SimpleNamespace(navigation_job=None, options={})
    with pytest.raises(BrowserError) as raised:
        adapter.navigation_begin("ses", "tab", "goto")
    assert raised.value.details == {"reason": "missing"}


@pytest.mark.parametrize(
    "changes,policy,reason",
    [
        ({"text": "sk-proj-" + "Ab9c" * 10}, "inspect", "secret_text"),
        ({"has_iframe": True}, "block", "iframe_policy"),
        ({"privacy_incomplete": True}, "inspect", "privacy_incomplete"),
        ({"privacy_mask_unsafe": True}, "inspect", "mask_unsafe"),
        (
            {
                "protected_regions": [{"x": 0, "y": 0, "width": 1, "height": 1, "mask_safe": True}]
                * 101
            },
            "inspect",
            "too_many_regions",
        ),
        (
            {"protected_regions": [{"x": 0, "y": 0, "width": 1, "height": 1, "mask_safe": False}]},
            "inspect",
            "mask_unsafe",
        ),
        (
            {
                "protected_regions": [
                    {"x": 200, "y": 200, "width": 1, "height": 1, "mask_safe": False}
                ]
            },
            "inspect",
            "mask_unbounded",
        ),
    ],
)
def test_sensitive_capture_reasons(service, changes, policy, reason):
    adapter = DrissionAdapter.__new__(DrissionAdapter)
    adapter.cfg = service.cfg
    adapter.cfg.iframe_screenshot_policy = policy
    data = {
        "text": "public",
        "has_iframe": False,
        "height": 100,
        "viewport": {"width": 100, "height": 100},
        "iframe_regions": [],
        "restricted_frame_regions": [],
        **changes,
    }
    with pytest.raises(BrowserError) as raised:
        adapter._capture_image_once(None, data, False, False, time.monotonic() + 15)
    assert raised.value.code == "SENSITIVE_SCREEN"
    assert raised.value.status == "blocked"
    assert raised.value.details == {"reason": reason}


def test_all_error_logs_share_capacity_budget_and_exclude_payload(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(diagnostics, "_logger", logger)
    diagnostics._events.clear()
    try:
        for index in range(130):
            diagnostics.log_capacity(
                {
                    "request_id": "req_fixture",
                    "error": {
                        "code": "INVALID_URL" if index % 2 else "BROWSER_BUSY",
                        "message": "secret",
                        "category": "secret",
                    },
                    "busy_reason": "secret",
                    "reason": "secret",
                    "url": "https://secret",
                    "selector": "secret",
                }
            )
        assert logger.info.call_count == 120
        assert "secret" not in str(logger.info.call_args_list)
        assert json.loads(logger.info.call_args_list[0].args[0]) == {
            "event": "browser_capacity",
            "request_id": "req_fixture",
            "code": "BROWSER_BUSY",
            "reason": None,
        }
        assert json.loads(logger.info.call_args_list[1].args[0]) == {
            "event": "browser_error",
            "request_id": "req_fixture",
            "code": "INVALID_URL",
            "category": "input",
        }
    finally:
        diagnostics._events.clear()


@pytest.mark.parametrize("request_id", ["", "req_", "other", "req_secret!", "req_" + "x" * 65])
def test_error_logging_requires_bounded_request_id(monkeypatch, request_id):
    logger = Mock()
    monkeypatch.setattr(diagnostics, "_logger", logger)
    diagnostics.log_capacity({"request_id": request_id, "error": {"code": "STALE_NODE"}})
    logger.info.assert_not_called()


async def test_recent_errors_are_principal_scoped_bounded_and_clear_on_shutdown(service):
    results = [
        await service.call("observe", _principal="alice", session_id="secret", tab_id="secret")
        for _ in range(25)
    ]
    await service.call("observe", _principal="bob", session_id="secret")
    status = await service.call("status", _principal="alice")
    assert len(status["recent_errors"]) == 20
    assert [item["request_id"] for item in status["recent_errors"]] == [
        item["request_id"] for item in results[-20:]
    ]
    for item in status["recent_errors"]:
        assert set(item) == {"tool", "code", "category", "request_id", "at"}
        assert item["tool"] == "browser_observe"
        assert item["code"] == "LEASE_REQUIRED"
        assert item["category"] == "input"
        assert datetime.fromisoformat(item["at"]).tzinfo is not None
        assert "secret" not in json.dumps(item)
    assert len((await service.call("status", _principal="bob"))["recent_errors"]) == 1
    opened = await service.call("open", _principal="alice")
    for args in (
        {"session_id": opened["session_id"], "lease_id": opened["lease_id"]},
        {"lease_id": opened["lease_id"]},
    ):
        assert (await service.call("status", _principal="alice", **args))[
            "recent_errors"
        ] == status["recent_errors"]
    await service.shutdown()
    assert not service.recent_errors and not service.diagnosed_errors


def test_recent_error_principal_memory_is_bounded(service):
    result = service._error_response(BrowserError("BROWSER_ERROR", "secret"))
    for index in range(129):
        service._remember_error(str(index), "browser_observe", result)
    assert len(service.recent_errors) == 128
    assert "0" not in service.recent_errors


def test_embedded_errors_log_with_outer_request_id_and_skip_page_tool_json(service, monkeypatch):
    logger = Mock()
    monkeypatch.setattr(diagnostics, "_logger", logger)
    diagnostics._events.clear()
    try:
        result = response(
            observation={
                "screenshot_omitted": {
                    "code": "SENSITIVE_SCREEN",
                    "message": "secret",
                    "reason": "secret_text",
                }
            },
            page_tool_result={"output": {"error": {"code": "SECRET"}}},
        )
        service._diagnose_result(result, "alice", "browser_observe")
        service._diagnose_result(result, "alice", "browser_observe")
        assert logger.info.call_count == 1
        assert json.loads(logger.info.call_args.args[0]) == {
            "event": "browser_error",
            "request_id": result["request_id"],
            "code": "SENSITIVE_SCREEN",
            "category": "privacy_guard",
        }
        assert service.recent_errors["alice"][0]["request_id"] == result["request_id"]
    finally:
        diagnostics._events.clear()


async def test_global_status_lists_only_principals_approval_summaries(service):
    service.cfg.max_sessions = 3
    proposals = []
    for principal in ("alice", "alice", "bob"):
        opened = await service.call("open", _principal=principal)
        args = {k: opened[k] for k in ("session_id", "tab_id", "lease_id")}
        proposal = await service.call(
            "act",
            _principal=principal,
            **args,
            expected_revision=1,
            action={"type": "fill", "node_id": "node_1", "text": "private-value"},
        )
        assert proposal["status"] == "confirmation_required"
        proposals.append((opened, proposal))
    await service.approve(next(iter(service.pending)), True)
    status = await service.call("status", _principal="alice")
    assert {item["session_id"] for item in status["approvals"]} == {
        opened["session_id"] for opened, _ in proposals[:2]
    }
    assert {item["state"] for item in status["approvals"]} == {"pending", "approved"}
    for item in status["approvals"]:
        assert set(item) == {
            "session_id",
            "tab_id",
            "state",
            "summary",
            "expires_at",
            "approval_state",
        }
        assert item["approval_state"] == item["state"]
    wire = json.dumps(status)
    assert "private-value" not in wire
    assert all(
        proposal["confirmation"]["confirmation_token"] not in wire for _, proposal in proposals
    )
    owned = proposals[0][0]
    per_session = await service.call(
        "status", _principal="alice", session_id=owned["session_id"], lease_id=owned["lease_id"]
    )
    assert per_session["approvals"] == [status["approvals"][0]]
