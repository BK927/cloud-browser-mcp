"""Public registration/SDK validation without a browser, network or runtime store."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from jsonschema import Draft202012Validator, ValidationError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from pydantic import ValidationError as ModelValidationError

from cloud_browser import server
from cloud_browser.config import Settings
from cloud_browser.models import BrowserError, response
from cloud_browser.ownership import request_principal
from cloud_browser.service import BrowserService

IDS = {"session_id": "ses_test", "tab_id": "tab_test", "lease_id": "lease_test"}
PAGE = {"url": "https://example.com/", "title": None}
ARTIFACT = {
    "artifact_id": "artifact_test",
    "name": "page.txt",
    "mime_type": "text/plain",
    "kind": "export",
    "state": "completed",
    "size": 4,
    "created_at": 1.0,
    "expires_at": 1801.0,
    "sha256": "abc",
    "source_truncated": False,
}
CONTROL = {
    "handoff_id": "handoff_test",
    "session_id": "ses_test",
    "tab_id": "tab_test",
    "kind": "auth",
    "reason": "Authenticate",
    "state": "active",
    "authenticated": None,
    "verification": "not_checked",
    "site_origin": "https://example.com",
    "expires_at": "2026-09-09T12:00:00Z",
    "control_url": "https://control.example/",
    "automation_paused": True,
    "control_access_expired": False,
}
OBSERVATION = {
    "semantic_snapshot": "Example",
    "interactive_snapshot": '{"node_id":"node_1"}',
    "truncated": True,
    "next_cursor": "cursor_test",
    "viewport": {"width": 1024, "height": 768},
    "frames": [
        {
            "frame_id": None,
            "parent_frame_id": None,
            "readable": False,
            "actionable": False,
            "reason": "FRAME_BUDGET",
        }
    ],
    "screenshot": {
        "screenshot_id": "screen_test",
        "width": 1024,
        "height": 768,
        "coordinate_units": "CSS pixels",
        "full_page": False,
        "masked_regions": [{"x": 0, "y": 0, "width": 20, "height": 20}],
        "captured_at": 1.0,
    },
}
CONFIRMATION = {
    "confirmation_token": "confirm_test",
    "summary": "click: Submit",
    "current_page": PAGE["url"],
    "destination": None,
    "destination_kind": "unknown",
    "destination_verified": False,
    "data_sent": ["text", {"name": "choice", "value": ["a"]}],
    "data_sent_truncated": False,
    "data_sent_verified": False,
    "files": [],
    "expires_at": "2026-09-09T12:00:00Z",
    "control_url": "https://control.example/",
}

# Real service/engine wire shapes, including meaningful nulls and provider extensions.
CASES = [
    ("open", {}, {"lease_id": "lease_test", "expires_at": "2026-09-09T12:00:00Z"}),
    (
        "list_tabs",
        {k: IDS[k] for k in ("session_id", "lease_id")},
        {"selected_tab_id": "tab_test", "tabs": [{"tab_id": "tab_test", **PAGE, "selected": True}]},
    ),
    (
        "navigate",
        IDS | {"operation": "back"},
        {"navigation": {"operation": "back", "redirected": False, "navigation_occurred": False}},
    ),
    ("observe", IDS, {"observation": OBSERVATION}),
    (
        "act",
        IDS | {"expected_revision": 1, "action": {"type": "scroll"}},
        {
            "action_result": {"performed": True, "page_changed": True, "new_tab_ids": ["tab_new"]},
            "completion": {
                "matched": False,
                "error": {"code": "TIMEOUT", "message": "Wait failed"},
            },
            "follow_up": {"error": {"code": "FRAME_UNAVAILABLE", "message": "Frame closed"}},
        },
    ),
    (
        "auth_request",
        IDS | {"site_origin": "https://example.com"},
        {"status": "user_action_required", "auth": CONTROL},
    ),
    (
        "handoff",
        IDS | {"reason": "Help"},
        {"status": "user_action_required", "handoff": CONTROL | {"kind": "manual"}},
    ),
    (
        "close",
        IDS | {"scope": "tab"},
        {"selected_tab_id": None, "session_closed": True, "termination_reason": "last_tab_closed"},
    ),
    ("status", {}, {"busy": False, "sessions": [], "approvals": [], "staged_uploads": []}),
    (
        "configure",
        IDS | {"configuration": {"max_chars": 1000}},
        {
            "configuration": {
                "viewport_width": 1024,
                "viewport_height": 768,
                "screenshot_quality": 80,
                "max_chars": 1000,
                "wait_ms": 500,
            }
        },
    ),
    (
        "list_page_tools",
        IDS,
        {
            "page_tools": [
                {
                    "name": "lookup",
                    "description": "Page tool",
                    "input_schema": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "x-provider": [1, None],
                    },
                    "schema_sha256": "abc",
                    "operator_read_approved": False,
                    "untrusted": True,
                }
            ]
        },
    ),
    (
        "call_page_tool",
        IDS | {"revision": 1, "tool_name": "lookup", "arguments": {}},
        {
            "page_tool_result": {
                "tool_name": "lookup",
                "output": [None, {"custom": [1, True]}],
                "untrusted": True,
            }
        },
    ),
    (
        "wait",
        IDS | {"condition": {"type": "dialog"}},
        {
            "status": "no_change",
            "wait": {"matched": False, "timed_out": True, "condition": "dialog", "partial": False},
        },
    ),
    (
        "dialog",
        IDS,
        {
            "dialog": {
                "dialog_id": "dialog_test",
                "type": "confirm",
                "message": "OK?",
                "sensitive": False,
                "url": PAGE["url"],
            },
            "page_cached": True,
        },
    ),
    (
        "logs",
        IDS,
        {
            "logs": {
                "records": [
                    {
                        "sequence": 1,
                        "timestamp": 1.0,
                        "kind": "console",
                        "level": "log",
                        "url": None,
                    }
                ],
                "next_sequence": 1,
                "truncated": False,
                "lost_before": None,
                "payload_policy": "withheld",
            }
        },
    ),
    (
        "clipboard",
        {"session_id": "ses_test", "lease_id": "lease_test", "operation": "write"},
        {"clipboard": {"text": None, "length": 4, "scope": "work-local-text-only"}},
    ),
    ("artifacts", {"session_id": "ses_test", "lease_id": "lease_test"}, {"artifacts": [ARTIFACT]}),
]


@pytest.fixture
def registered(monkeypatch, tmp_path):
    mcp = MCPServer("schema-test")
    service = SimpleNamespace(call=AsyncMock(), _error_response=BrowserService._error_response)
    monkeypatch.setattr(server, "MCPServer", lambda *a, **kw: mcp)
    monkeypatch.setattr(server, "Store", Mock())
    monkeypatch.setattr(server, "Auth", Mock())
    monkeypatch.setattr(server, "BrowserService", lambda *a, **kw: service)
    monkeypatch.setattr(server, "control_app", Mock())
    server.create_apps(Settings(development=True, data_dir=tmp_path))
    assert list(tmp_path.iterdir()) == []  # No real store/profile/runtime created.
    return mcp, service


async def invoke(registered, name, arguments, payload):
    mcp, service = registered
    service.call.return_value = copy.deepcopy(payload)
    token = request_principal.set("schema-test")
    try:
        result = await mcp.call_tool("browser_" + name, arguments)
    finally:
        request_principal.reset(token)
    schema = next(t.output_schema for t in await mcp.list_tools() if t.name == "browser_" + name)
    Draft202012Validator(schema).validate(result.structured_content)
    expected = {k: v for k, v in payload.items() if k != "_image"}
    assert result.structured_content == expected
    assert json.loads(result.content[0].text) == expected
    assert result.is_error == (payload["status"] in ("error", "blocked"))
    return result


async def test_every_registered_tool_has_meaningful_output_schema(registered):
    tools = await registered[0].list_tools()
    assert {t.name for t in tools} == {"browser_" + name for name, _, _ in CASES}
    for tool in tools:
        schema = tool.output_schema
        Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object"
        assert {"status", "request_id", "error", "session_id", "revision"} <= set(
            schema["required"]
        )
        assert len(schema["properties"]) > 11  # Includes method-specific result fields.
        assert schema["properties"]["revision"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    size = len(
        json.dumps(
            [t.model_dump(by_alias=True, exclude_none=True) for t in tools],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    print(f"\n17 tools/list descriptors: {size} bytes including output schemas")
    assert size < 100_000
    page_schema = tools[0].output_schema["$defs"]["Page"]
    assert "title" in page_schema["properties"]  # Keep the real page-title field.
    assert "title" not in page_schema["properties"]["title"]


@pytest.mark.parametrize("name,arguments,extra", CASES, ids=[x[0] for x in CASES])
async def test_success_result_preserves_wire_shape(registered, name, arguments, extra):
    payload = response(session_id="ses_test", tab_id="tab_test", revision=1, page=PAGE, **extra)
    payload["provider_extension"] = {"future": [None, 42]}
    await invoke(registered, name, arguments, payload)


@pytest.mark.parametrize("name,arguments,_", CASES, ids=[x[0] for x in CASES])
@pytest.mark.parametrize("status", ["error", "blocked", "user_action_required"])
async def test_error_envelopes_match_advertised_schema(registered, name, arguments, _, status):
    payload = BrowserService._error_response(
        BrowserError(
            "AUTH_REQUIRED",
            "Use the private console",
            status,
            retry_after_seconds=15,
            provider_detail={"pending": True},
        )
    )
    await invoke(registered, name, arguments, payload)


@pytest.mark.parametrize("name", ["act", "call_page_tool", "dialog", "clipboard"])
async def test_confirmation_and_replay_shapes(registered, name):
    arguments = next(args for method, args, _ in CASES if method == name)
    await invoke(
        registered,
        name,
        arguments,
        response("confirmation_required", confirmation=CONFIRMATION, replayed=True),
    )
    await invoke(registered, name, arguments, response("no_change", operation={"state": "running"}))


@pytest.mark.parametrize("name", ["observe", "artifacts"])
async def test_images_remain_multimodal_without_entering_schema(registered, name):
    arguments = next(args for method, args, _ in CASES if method == name)
    shot = {"data": "aGVsbG8=", "mimeType": "image/png"}
    payload = response(
        **({"observation": OBSERVATION} if name == "observe" else {"artifact": ARTIFACT})
    )
    result = await invoke(registered, name, arguments, payload | {"_image": shot})
    assert len(result.content) == 2
    assert result.content[1].type == "image"
    assert result.content[1].data == shot["data"]
    assert result.content[1].mime_type == shot["mimeType"]
    assert "_image" not in result.structured_content


async def test_validation_rejects_wrong_nested_types(registered):
    schema = next(
        t.output_schema for t in await registered[0].list_tools() if t.name == "browser_observe"
    )
    payload = response(observation=copy.deepcopy(OBSERVATION))
    payload["observation"]["screenshot"]["screenshot_id"] = 12
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(payload)


@pytest.mark.parametrize("field,value", [("revision", "1"), ("truncated", "false")])
async def test_sdk_rejects_coercible_values_without_rewriting_results(registered, field, value):
    payload = response(revision=1, observation=copy.deepcopy(OBSERVATION))
    if field == "revision":
        payload[field] = value
    else:
        payload["observation"][field] = value
    with pytest.raises(UnexpectedToolError) as caught:
        await invoke(registered, "observe", IDS, payload)
    assert isinstance(caught.value.__cause__, ModelValidationError)


async def test_missing_auth_and_local_input_error_keep_structured_error(registered):
    mcp, service = registered
    for name, arguments in (("browser_open", {}), ("browser_observe", IDS | {"max_chars": 1})):
        result = await mcp.call_tool(name, arguments)
        schema = next(t.output_schema for t in await mcp.list_tools() if t.name == name)
        Draft202012Validator(schema).validate(result.structured_content)
        assert result.is_error
        assert result.structured_content["error"]["code"] in ("AUTH_REQUIRED", "INVALID_INPUT")
    service.call.assert_not_awaited()
