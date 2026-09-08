import io
import json
import time

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import UploadFile
from test_service import opened

from cloud_browser.authentication import AuthRule
from cloud_browser.config import Settings
from cloud_browser.console import control_app
from cloud_browser.models import Action, BrowserError
from cloud_browser.oauth import Auth
from cloud_browser.page_tools import PageTools, scrub_result, validate_arguments
from cloud_browser.uploads import Uploads

SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


def test_page_tool_schema_never_fetches_refs_and_validates():
    validate_arguments(SCHEMA, {"query": "Godot"})
    for args in ({}, {"query": 2}, {"query": "x", "extra": True}):
        with pytest.raises(BrowserError) as exc:
            validate_arguments(SCHEMA, args)
        assert exc.value.code == "INVALID_INPUT"
    with pytest.raises(BrowserError) as exc:
        validate_arguments({"$ref": "https://example.com/schema"}, {})
    assert exc.value.code == "UNSUPPORTED_OPERATION"


def test_page_tool_result_redaction():
    result = scrub_result(
        {
            "password": "private-value",
            "nested": [{"cookies": "a=b"}],
            "url": "https://example.com/?code=private-value",
        }
    )
    assert "private-value" not in json.dumps(result)
    assert result["nested"][0]["cookies"] == "[REDACTED]"
    assert "private-value" not in scrub_result('{"password":"private-value"}')


class NativeProbe:
    def __init__(self, status="Completed", supported=True):
        self._driver = self
        self.callbacks = {}
        self.calls = []
        self.status, self.supported = status, supported

    def set_callback(self, name, callback):
        self.callbacks[name] = callback

    def run_cdp(self, method, **kwargs):
        self.calls.append(method)
        if method == "WebMCP.enable":
            if not self.supported:
                raise RuntimeError("Unknown method")
            self.callbacks["WebMCP.toolsAdded"](
                tools=[
                    {
                        "frameId": "root",
                        "name": "search",
                        "description": "Find",
                        "inputSchema": SCHEMA,
                    }
                ]
            )
        if method == "WebMCP.invokeTool":
            self.callbacks["WebMCP.toolResponded"](
                invocationId="inv1",
                status=self.status,
                output={"text": "result", "password": "secret-value"},
            )
            return {"invocationId": "inv1"}
        return {}


def test_native_page_tools_inventory_invocation_and_disable():
    tab = NativeProbe()
    bridge = PageTools(tab)
    bridge.enable()
    generation, tools = bridge.snapshot("root")
    assert generation == 1 and tools[0]["name"] == "search"
    assert bridge.snapshot("other")[1] == []
    assert bridge.invoke("root", "search", {"query": "Godot"})["password"] == "[REDACTED]"
    bridge.removed([{"frameId": "root", "name": "search"}])
    assert not bridge.snapshot("root")[1]
    bridge.disable()
    assert not bridge.enabled and not any(tab.callbacks.values())


@pytest.mark.parametrize("status", ["Error", "Canceled"])
def test_page_tool_failure_is_never_retried(status):
    tab = NativeProbe(status)
    bridge = PageTools(tab)
    bridge.enable()
    with pytest.raises(BrowserError) as exc:
        bridge.invoke("root", "search", {})
    assert exc.value.code == "RESULT_UNCERTAIN"
    assert tab.calls.count("WebMCP.invokeTool") == 1


def test_missing_native_interface_is_explicit():
    bridge = PageTools(NativeProbe(supported=False))
    with pytest.raises(BrowserError) as exc:
        bridge.enable()
    assert exc.value.code == "UNSUPPORTED_OPERATION"
    assert not bridge.enabled


def test_page_tool_timeout_dispatches_only_once():
    class SilentProbe(NativeProbe):
        def run_cdp(self, method, **kwargs):
            if method == "WebMCP.invokeTool":
                self.calls.append(method)
                return {"invocationId": "silent"}
            return super().run_cdp(method, **kwargs)

    tab = SilentProbe()
    bridge = PageTools(tab)
    bridge.enable()
    with pytest.raises(BrowserError) as exc:
        bridge.invoke("root", "search", {}, timeout=0.01)
    assert exc.value.code == "RESULT_UNCERTAIN"
    assert tab.calls.count("WebMCP.invokeTool") == 1
    assert "WebMCP.cancelInvocation" in tab.calls
    assert not bridge.armed


async def test_upload_budget_removes_partial_file(cfg):
    cfg.max_upload_mb = 1
    files = Uploads(cfg)
    with pytest.raises(BrowserError) as exc:
        await files.stage(UploadFile(io.BytesIO(b"x" * 1048577), filename="large.bin"))
    assert exc.value.code == "UPLOAD_TOO_LARGE"
    assert not files.items and not list(files.root.iterdir())


async def test_auth_report_endpoint_requires_csrf(service):
    sid, tid = await opened(service)
    await service.call(
        "auth_request", session_id=sid, tab_id=tid, site_origin="https://example.com"
    )
    hid = service.leases[sid]["handoff_id"]
    service.store.put("control", "cookie", {"csrf": "csrf"}, 120)
    app = control_app(service.cfg, Auth(service.cfg, service.store), service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=service.cfg.control_origin,
        headers={"Origin": service.cfg.control_origin},
        cookies={"cb_control": "cookie"},
    ) as client:
        bad = await client.post(
            f"/handoff/{hid}/auth-result", data={"csrf": "bad", "outcome": "failed"}
        )
        assert bad.status_code == 409
        assert service.leases[sid]["authenticated"] is None
        good = await client.post(
            f"/handoff/{hid}/auth-result", data={"csrf": "csrf", "outcome": "unsupported"}
        )
        assert good.status_code == 303
    control = (await service.call("status", session_id=sid))["sessions"][0]["control"]
    assert control["automation_paused"] and control["authentication_outcome"] == "unsupported"


async def test_page_tools_use_private_approval_and_dedup(service):
    sid, tid = await opened(service)
    args = dict(
        session_id=sid, tab_id=tid, revision=1, tool_name="search", arguments={"query": "Godot"}
    )
    result = await service.call("call_page_tool", **args)
    assert result["status"] == "confirmation_required"
    token = result["confirmation"]["confirmation_token"]
    assert (await service.call("call_page_tool", **args, confirmation_token=token))[
        "status"
    ] == "confirmation_required"
    assert service.worker.executions == 0
    await service.approve(next(iter(service.pending)), True)
    changed = await service.call(
        "call_page_tool", **(args | {"arguments": {"query": "changed"}}), confirmation_token=token
    )
    assert changed["error"]["code"] == "CONFIRMATION_STALE"
    assert (await service.call("call_page_tool", **args, confirmation_token=token))[
        "status"
    ] == "ok"
    assert (await service.call("call_page_tool", **args))["error"]["code"] == "CONFIRMATION_USED"
    assert service.worker.executions == 1


async def test_page_tools_credentials_and_handoff_blocked(service):
    sid, tid = await opened(service)
    result = await service.call(
        "call_page_tool",
        session_id=sid,
        tab_id=tid,
        revision=1,
        tool_name="login",
        arguments={"credentials": {"password": "private-value"}},
    )
    assert result["error"]["code"] == "SENSITIVE_INPUT"
    await service.call(
        "auth_request", session_id=sid, tab_id=tid, site_origin="https://example.com"
    )
    for method, kwargs in (
        ("list_page_tools", {}),
        ("call_page_tool", {"revision": 1, "tool_name": "search", "arguments": {}}),
    ):
        result = await service.call(method, session_id=sid, tab_id=tid, **kwargs)
        assert result["error"]["code"] == "AUTH_IN_PROGRESS"
    assert service.worker.executions == 0


def test_auth_rules_validate_exact_origin(cfg):
    with pytest.raises(ValidationError):
        Settings(**(cfg.model_dump() | {"auth_rules": {"https://example.com/login": {}}}))


async def test_unsupported_auth_and_private_failure_report(service):
    sid, tid = await opened(service)
    service.cfg.auth_rules = {"https://example.com": AuthRule(supported_methods=["passkey"])}
    result = await service.call(
        "auth_request", session_id=sid, tab_id=tid, site_origin="https://example.com"
    )
    assert result["error"]["code"] == "AUTH_METHOD_UNSUPPORTED"
    assert not service.leases and not any(method == "focus" for method, _ in service.worker.calls)
    service.cfg.auth_rules = {}
    result = await service.call(
        "auth_request", session_id=sid, tab_id=tid, site_origin="https://example.com"
    )
    hid = result["auth"]["handoff_id"]
    failed = await service.report_auth_result(hid, "failed")
    assert failed["authenticated"] is False and failed["automation_paused"]
    assert failed["result"]["error"]["code"] == "AUTH_FAILED"
    unsupported = await service.report_auth_result(hid, "unsupported")
    assert unsupported["result"]["error"]["code"] == "AUTH_METHOD_UNSUPPORTED"
    with pytest.raises(BrowserError):
        await service.report_auth_result(hid, "success")


async def test_upload_handles_path_safety_integrity_and_expiry(cfg):
    files = Uploads(cfg)
    item = await files.stage(UploadFile(io.BytesIO(b"test content"), filename="../../outside.txt"))
    assert item["filename"] == "outside.txt" and "path" not in item
    resolved = files.resolve([item["upload_id"]])[0]
    assert resolved["path"].startswith(str(cfg.data_dir))
    with pytest.raises(BrowserError):
        files.resolve([item["upload_id"], item["upload_id"]])
    stored = files.items[item["upload_id"]]
    stored["sha256"] = "changed"
    with pytest.raises(BrowserError) as exc:
        files.resolve([item["upload_id"]])
    assert exc.value.code == "UPLOAD_CHANGED"
    stored["expires"] = time.time() - 1
    assert files.list() == []
    assert not files.root.joinpath(item["upload_id"]).exists()


def test_upload_mcp_does_not_accept_paths():
    with pytest.raises(ValidationError):
        TypeAdapter(Action).validate_python(
            {"type": "upload", "node_id": "n", "upload_ids": ["u"], "path": "/etc/passwd"}
        )


async def test_private_upload_csrf_limit_and_approval(service):
    auth = Auth(service.cfg, service.store)
    service.store.put("control", "test-cookie", {"csrf": "test-csrf"}, 120)
    service.cfg.max_upload_mb = 1
    app = control_app(service.cfg, auth, service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=service.cfg.control_origin,
        headers={"Origin": service.cfg.control_origin},
    ) as client:
        assert (await client.post("/uploads", files={"file": ("x.txt", b"x")})).status_code == 303
        client.cookies.set("cb_control", "test-cookie")
        denied = await client.post(
            "/uploads", data={"csrf": "bad"}, files={"file": ("x.txt", b"x")}
        )
        assert denied.status_code == 403 and not service.uploads.list()
        large = await client.post(
            "/uploads", data={"csrf": "test-csrf"}, files={"file": ("large.bin", b"x" * 1200000)}
        )
        assert large.status_code == 413 and not service.uploads.list()
        assert (
            await client.post(
                "/uploads", data={"csrf": "test-csrf"}, files={"file": ("x.txt", b"content")}
            )
        ).status_code == 303
        upload_id = service.uploads.list()[0]["upload_id"]
        sid, tid = await opened(service)
        args = dict(
            session_id=sid,
            tab_id=tid,
            expected_revision=1,
            action={"type": "upload", "node_id": "node_1", "upload_ids": [upload_id]},
        )
        proposal = await service.call("act", **args)
        token = proposal["confirmation"]["confirmation_token"]
        assert "path" not in json.dumps(proposal) and service.worker.executions == 0
        await service.approve(next(iter(service.pending)), True)
        await service.discard_upload(upload_id)
        assert (await service.call("act", **args, confirmation_token=token))["error"][
            "code"
        ] == "CONFIRMATION_STALE"
        assert service.worker.executions == 0
