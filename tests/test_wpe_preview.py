import asyncio
import json
import socket

import httpx2
import pytest
import uvicorn
from argon2 import PasswordHasher
from conftest import FakeWorker
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from cloud_browser.config import Settings
from cloud_browser.models import BrowserError, response
from cloud_browser.output_models import (
    ActOutput,
    Capabilities,
    ConfigureOutput,
    NavigateOutput,
    ObserveOutput,
    OpenOutput,
    TabsOutput,
)
from cloud_browser.security import validate_url as real_validate_url
from cloud_browser.server import create_apps
from cloud_browser.service import BrowserService
from cloud_browser.store import Store
from cloud_browser.wpe import WPEAdapter


def settings(**changes):
    values = {
        "development": True,
        "max_sessions": 1,
        "webmcp_enabled": False,
        "browser_proxy": "",
        "engine": "wpe",
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


class FakeDriver:
    def __init__(self):
        self.url = "about:blank"
        self.text = "hello " * 200
        self.protected = False
        self.filled = False
        self.has_iframe = False
        self.has_visual_media = False
        self.fail_after_click = False
        self.protect_after_resume_probe = False
        self.control_href = ""
        self.control_target = ""
        self.control_submits_form = False
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if path == "/url" and method == "POST":
            self.url = body["url"]
            return None
        if path == "/url":
            return self.url
        if path == "/title":
            return "Fixture"
        if path == "/execute/sync":
            if "arguments[0]" in body["script"]:
                return {"success": False, "failure": False}
            if self.protect_after_resume_probe:
                self.protect_after_resume_probe = False
                self.protected = True
                return {"protectedPage": False}
            return {
                "title": "Fixture",
                "text": self.text,
                "protectedPage": self.protected,
                "filled": self.filled,
                "hasIframe": self.has_iframe,
                "hasVisualMedia": self.has_visual_media,
                "controlsTruncated": False,
                "controls": [
                    {
                        "element": {"element-6066-11e4-a52e-4f735466cecf": "wpe-button"},
                        "tag": "button",
                        "name": "More",
                        "role": "",
                        "type": "button",
                        "disabled": False,
                        "readonly": False,
                        "editable": False,
                        "rect": {"x": 10, "y": 10, "width": 80, "height": 40},
                        "href": self.control_href,
                        "target": self.control_target,
                        "submitsForm": self.control_submits_form,
                    }
                ],
                "viewport": {"width": 1024, "height": 768},
                "scroll": {"x": 0, "y": 0},
            }
        if path == "/screenshot":
            return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII="
        if path == "/element/wpe-button/click":
            self.text = "Clicked page"
            if self.fail_after_click:
                self.protected = False
                self.fail_after_click = False
                raise BrowserError("BROWSER_ERROR", "WebDriver lost response after click")
            return None
        if path == "/window/handles":
            return ["only-window"]
        raise AssertionError(path)


class FakeRuntime:
    instances = []

    def __init__(self, _settings):
        self.driver = FakeDriver()
        self.closed = False
        self.control_started = False
        self.instances.append(self)

    def start(self):
        pass

    def close(self):
        self.closed = True

    def start_control(self):
        self.control_started = True

    def stop_control(self):
        self.control_started = False


@pytest.fixture
def adapter(monkeypatch):
    FakeRuntime.instances.clear()
    monkeypatch.setattr("cloud_browser.wpe._Runtime", FakeRuntime)
    monkeypatch.setattr("cloud_browser.wpe.validate_url", lambda *args, **kwargs: None)
    return WPEAdapter(settings())


def test_wpe_is_opt_in_and_not_production():
    assert Settings(_env_file=None, development=True).engine == "chromium"
    with pytest.raises(ValidationError, match="not enabled for production"):
        Settings(_env_file=None, engine="wpe")
    with pytest.raises(ValidationError, match="one session"):
        settings(max_sessions=2)
    with pytest.raises(ValidationError, match="loopback-only"):
        settings(bind_host="0.0.0.0")
    with pytest.raises(ValidationError, match="headed managed display"):
        settings(manual_control_enabled=True)
    with pytest.raises(ValidationError, match="Argon2id"):
        settings(managed_display=True, manual_control_enabled=True)


def test_one_tab_reuse_never_creates_or_closes_a_window(adapter):
    opened = adapter.open("ses_one")
    OpenOutput.model_validate(response(**opened))
    tab_id = opened["tab_id"]
    tabs = adapter.list_tabs("ses_one")
    TabsOutput.model_validate(response(**tabs))
    assert tabs["tabs"][0]["tab_id"] == tab_id
    with pytest.raises(BrowserError, match="one page per work"):
        adapter.open("ses_one", new_tab=True)
    assert not FakeRuntime.instances[0].closed
    reused = adapter.open("ses_one", "https://example.com/", new_tab=False)
    NavigateOutput.model_validate(response(**reused))
    assert reused["tab_id"] == tab_id
    assert len(FakeRuntime.instances) == 1
    assert all("/window/new" not in path for _, path in FakeRuntime.instances[0].driver.calls)
    with pytest.raises(BrowserError, match="one work"):
        adapter.open("ses_two")
    assert adapter.close("ses_one", "tab", tab_id)["session_closed"]
    assert FakeRuntime.instances[0].closed


def test_semantic_observation_pages_and_sensitive_guard(adapter):
    tab_id = adapter.open("ses_one")["tab_id"]
    first = adapter.observe("ses_one", tab_id, max_chars=256)
    ObserveOutput.model_validate(response(**first))
    assert first["observation"]["truncated"]
    cursor = first["observation"]["next_cursor"]
    second = adapter.observe("ses_one", tab_id, cursor=cursor)
    assert second["observation"]["semantic_snapshot"]
    visual = adapter.observe("ses_one", tab_id, mode="visual")
    ObserveOutput.model_validate(response(**visual))
    assert visual["_image"]["mimeType"] == "image/png"
    FakeRuntime.instances[0].driver.filled = True
    with pytest.raises(BrowserError) as screen:
        adapter.observe("ses_one", tab_id, mode="visual")
    assert screen.value.code == "SENSITIVE_SCREEN"
    FakeRuntime.instances[0].driver.filled = False
    FakeRuntime.instances[0].driver.has_iframe = True
    with pytest.raises(BrowserError) as frame:
        adapter.observe("ses_one", tab_id, mode="visual")
    assert frame.value.code == "SENSITIVE_SCREEN"
    FakeRuntime.instances[0].driver.has_iframe = False
    FakeRuntime.instances[0].driver.has_visual_media = True
    with pytest.raises(BrowserError) as media:
        adapter.observe("ses_one", tab_id, mode="visual")
    assert media.value.code == "SENSITIVE_SCREEN"
    FakeRuntime.instances[0].driver.has_visual_media = False
    FakeRuntime.instances[0].driver.protected = True
    with pytest.raises(BrowserError) as caught:
        adapter.observe("ses_one", tab_id)
    assert caught.value.code == "SENSITIVE_CONTENT"
    adapter.shutdown()


def test_wpe_status_and_configuration_shapes(adapter):
    tab_id = adapter.open("ses_one")["tab_id"]
    configured = adapter.configure("ses_one", tab_id, {"max_chars": 4000})
    ConfigureOutput.model_validate(response(**configured))
    Capabilities.model_validate(BrowserService._capabilities(SimpleService(adapter.cfg)))
    adapter.shutdown()


class SimpleService:
    def __init__(self, cfg):
        self.cfg = cfg


def test_observed_click_requires_confirmation_and_stale_revisions_fail(adapter):
    tab_id = adapter.open("ses_one")["tab_id"]
    observed = adapter.observe("ses_one", tab_id)
    node_id = json.loads(observed["observation"]["interactive_snapshot"])["node_id"]
    revision = observed["revision"]
    prepared = adapter.prepare("ses_one", tab_id, revision, {"type": "click", "node_id": node_id})
    assert prepared["requires_confirmation"] is True
    clicked = adapter.act("ses_one", tab_id, revision, {"type": "click", "node_id": node_id})
    ActOutput.model_validate(response(**clicked))
    assert clicked["action_result"]["performed"]
    assert clicked["action_result"]["new_tab_ids"] == []
    assert "Clicked page" in adapter.observe("ses_one", tab_id)["observation"]["semantic_snapshot"]
    with pytest.raises(BrowserError) as caught:
        adapter.prepare("ses_one", tab_id, revision, {"type": "click", "node_id": node_id})
    assert caught.value.code == "STALE_REVISION"
    with pytest.raises(BrowserError) as caught:
        adapter.prepare("ses_one", tab_id, clicked["revision"], {"type": "fill", "node_id": node_id})
    assert caught.value.code == "UNSUPPORTED_OPERATION"
    adapter.shutdown()


def test_click_lost_response_is_not_retryable(adapter):
    tab_id = adapter.open("ses_one")["tab_id"]
    observed = adapter.observe("ses_one", tab_id)
    node_id = json.loads(observed["observation"]["interactive_snapshot"])["node_id"]
    FakeRuntime.instances[0].driver.fail_after_click = True
    with pytest.raises(BrowserError) as caught:
        adapter.act("ses_one", tab_id, observed["revision"], {"type": "click", "node_id": node_id})
    assert caught.value.code == "RESULT_UNCERTAIN"
    adapter.shutdown()


def test_declared_click_destinations_and_form_submit_are_guarded(adapter, monkeypatch):
    tab_id = adapter.open("ses_one")["tab_id"]
    driver = FakeRuntime.instances[0].driver

    def prepare_current():
        observed = adapter.observe("ses_one", tab_id)
        node_id = json.loads(observed["observation"]["interactive_snapshot"])["node_id"]
        return adapter.prepare(
            "ses_one", tab_id, observed["revision"], {"type": "click", "node_id": node_id}
        )

    driver.control_submits_form = True
    with pytest.raises(BrowserError) as form:
        prepare_current()
    assert form.value.code == "UNSUPPORTED_OPERATION"
    driver.control_submits_form = False
    driver.control_target = "_blank"
    with pytest.raises(BrowserError) as window:
        prepare_current()
    assert window.value.code == "UNSUPPORTED_OPERATION"
    driver.control_target = ""
    driver.control_href = "file:///etc/passwd"
    with pytest.raises(BrowserError) as scheme:
        prepare_current()
    assert scheme.value.code == "UNSUPPORTED_OPERATION"
    driver.control_href = "http://127.0.0.1/private"

    monkeypatch.setattr("cloud_browser.wpe.validate_url", real_validate_url)
    with pytest.raises(BrowserError) as private:
        prepare_current()
    assert private.value.code == "INVALID_URL"
    assert ("POST", "/element/wpe-button/click") not in driver.calls
    adapter.shutdown()


@pytest.mark.asyncio
async def test_wpe_service_handoff_requires_private_return(tmp_path):
    cfg = settings(
        data_dir=tmp_path,
        managed_display=True,
        manual_control_enabled=True,
        admin_password_hash=PasswordHasher().hash("separate preview administrator password"),
    )
    store = Store(tmp_path / "wpe-state.sqlite3")
    service = BrowserService(cfg, store, FakeWorker())
    try:
        opened = await service.call("open", new_tab=False)
        sid, tid = opened["session_id"], opened["tab_id"]
        handoff = await service.call(
            "auth_request", session_id=sid, tab_id=tid, site_origin="https://example.com"
        )
        assert handoff["status"] == "user_action_required"
        blocked = await service.call("observe", session_id=sid, tab_id=tid)
        assert blocked["error"]["code"] in ("AUTH_IN_PROGRESS", "USER_CONTROL_ACTIVE")
        finished = await service.complete_handoff(handoff["auth"]["handoff_id"])
        assert finished["state"] == "completed"
        assert finished["authenticated"] is None
        assert (await service.call("observe", session_id=sid, tab_id=tid))["status"] == "ok"
    finally:
        await service.shutdown()
        store.close()


def test_private_handoff_pauses_observation(adapter):
    tab_id = adapter.open("ses_one")["tab_id"]
    adapter.focus("ses_one", tab_id)
    assert FakeRuntime.instances[0].control_started
    with pytest.raises(BrowserError) as caught:
        adapter.observe("ses_one", tab_id)
    assert caught.value.code == "USER_CONTROL_ACTIVE"
    adapter.resume("ses_one", tab_id)
    assert not FakeRuntime.instances[0].control_started
    adapter.observe("ses_one", tab_id)
    adapter.shutdown()


def test_handoff_keeps_private_bridge_on_late_protected_page(adapter):
    tab_id = adapter.open("ses_one")["tab_id"]
    adapter.focus("ses_one", tab_id)
    FakeRuntime.instances[0].driver.protect_after_resume_probe = True
    with pytest.raises(BrowserError) as caught:
        adapter.resume("ses_one", tab_id)
    assert caught.value.code == "SENSITIVE_CONTENT"
    assert adapter.sessions["ses_one"]["paused"]
    assert FakeRuntime.instances[0].control_started
    with pytest.raises(BrowserError) as blocked:
        adapter.observe("ses_one", tab_id)
    assert blocked.value.code == "USER_CONTROL_ACTIVE"
    adapter.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_wpe_mcp_registry_exposes_only_supported_tools(tmp_path, manual):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    cfg = settings(
        data_dir=tmp_path,
        public_origin=f"http://127.0.0.1:{port}",
        public_port=port,
        managed_display=manual,
        manual_control_enabled=manual,
        admin_password_hash=PasswordHasher().hash("preview test password") if manual else "",
    )
    public, _, _, auth = create_apps(cfg, worker=FakeWorker())
    auth.store.put("grant", "wpe-test-grant", {"active": True})
    token = auth.issue("wpe-test-grant")["access_token"]
    server = uvicorn.Server(
        uvicorn.Config(public, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
            async with streamable_http_client(cfg.resource, http_client=http) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    tools = (await client.list_tools()).tools
                    names = {tool.name for tool in tools}
                    expected = {
                        "browser_open",
                        "browser_list_tabs",
                        "browser_navigate",
                        "browser_observe",
                        "browser_act",
                        "browser_close",
                        "browser_status",
                        "browser_configure",
                    }
                    if manual:
                        expected |= {"browser_auth_request", "browser_handoff"}
                    assert names == expected
                    opened = next(tool for tool in tools if tool.name == "browser_open")
                    assert opened.input_schema["properties"]["new_tab"]["default"] is False
                    assert "no second tab" in opened.description
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
