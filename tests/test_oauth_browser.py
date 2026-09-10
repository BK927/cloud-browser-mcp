"""Real browser form/redirect checks; no user profile, credentials or remote sites."""

import asyncio
import base64
import hashlib
import http.server
import json
import os
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import uvicorn
from conftest import FakeWorker

from cloud_browser.security import public_document_csp
from cloud_browser.server import create_apps

pytestmark = pytest.mark.browser


@pytest.fixture
def oauth_browser(cfg, tmp_path, request):
    executable = os.getenv("CB_TEST_CHROMIUM")
    if not executable:
        pytest.skip("Set CB_TEST_CHROMIUM for real OAuth browser checks")
    from DrissionPage import Chromium, ChromiumOptions

    arrivals = []
    arrived = threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path != "/favicon.ico":
                arrivals.append((self.command, self.path, dict(self.headers)))
                arrived.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<title>Local callback received</title>")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.do_GET()

        def log_message(self, *args):
            pass  # Do not print even test-only authorization codes.

    receiver = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Callback)
    receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    receiver_thread.start()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    private_listener = socket.socket()
    private_listener.bind(("127.0.0.1", 0))
    cfg.public_origin = f"http://127.0.0.1:{port}"
    cfg.control_origin = f"http://127.0.0.1:{private_listener.getsockname()[1]}"
    callback = f"http://127.0.0.1:{receiver.server_port}/callback"
    # Test-only loopback HTTP origin pair. Production Settings still requires
    # exact HTTPS callbacks; this tests browser CSP, not TLS or deployment isolation.
    cfg.oauth_redirect_uris = [callback]
    public, control, service, auth = create_apps(cfg, worker=FakeWorker())
    mode = getattr(request, "param", "current")
    diagnostics = {"post_statuses": [], "form_action_errors": [], "private_posts": []}

    async def seed_control():
        opened = await service.call("open")
        assert opened["status"] == "ok"
        target = {key: opened[key] for key in ("session_id", "tab_id")}
        proposal = await service.call(
            "act",
            **target,
            expected_revision=opened["revision"],
            action={"type": "click", "node_id": "node_1"},
        )
        assert proposal["status"] == "confirmation_required"
        handoff = await service.call("handoff", **target, reason="Local form regression")
        assert handoff["status"] == "user_action_required"
        return target, proposal["confirmation"]["confirmation_token"]

    target, approval_token = asyncio.run(seed_control())

    async def observed_public(scope, receive, send):
        if scope.get("method") == "POST" and scope.get("path") == "/authorize":
            headers = dict(scope["headers"])
            request_origin = headers.get(b"origin", b"").decode()
            diagnostics["origin"] = (
                "matching"
                if request_origin == cfg.public_origin
                else "null"
                if request_origin == "null"
                else "other-or-missing"
            )
            diagnostics["oauth_cookie_present"] = b"cb_oauth=" in headers.get(b"cookie", b"")

        async def record(message):
            if (
                scope.get("method") == "GET"
                and scope.get("path") == "/authorize"
                and message["type"] == "http.response.start"
            ):
                overrides = {
                    "legacy-referrer": {b"referrer-policy": b"no-referrer"},
                    "legacy-csp": {
                        b"content-security-policy": public_document_csp().encode("ascii")
                    },
                }.get(mode, {})
                message["headers"] = [
                    (k, overrides.get(k.lower(), v)) for k, v in message["headers"]
                ]
            if (
                scope.get("method") == "POST"
                and scope.get("path") == "/authorize"
                and message["type"] == "http.response.start"
            ):
                diagnostics["post_statuses"].append(message["status"])
            await send(message)

        await public(scope, receive, record)

    async def observed_control(scope, receive, send):
        if scope.get("method") == "GET" and scope.get("path") in ("/login", "/favicon.ico"):
            diagnostics.setdefault("private_gets", []).append(scope["path"])

        async def record(message):
            if message["type"] == "http.response.start":
                if mode == "legacy-console" and scope.get("method") == "GET":
                    message["headers"] = [
                        (k, b"no-referrer" if k.lower() == b"referrer-policy" else v)
                        for k, v in message["headers"]
                    ]
                if scope.get("method") == "POST":
                    headers = dict(scope["headers"])
                    diagnostics["private_login_cookie_present"] = b"cb_login=" in headers.get(
                        b"cookie", b""
                    )
                    diagnostics["private_posts"].append(
                        (
                            scope["path"].split("/")[1],
                            message["status"],
                            headers.get(b"origin") == cfg.control_origin.encode(),
                        )
                    )
            if scope.get("method") == "POST" and message["type"] == "http.response.body":
                for label in (b"Invalid login form", b"Login denied", b"Invalid origin"):
                    if label in message.get("body", b""):
                        diagnostics["private_error"] = label.decode()
            await send(message)

        await control(scope, receive, record)

    server = uvicorn.Server(uvicorn.Config(observed_public, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    private_server = uvicorn.Server(
        uvicorn.Config(observed_control, log_level="error", access_log=False)
    )
    private_thread = threading.Thread(
        target=private_server.run, kwargs={"sockets": [private_listener]}, daemon=True
    )
    private_thread.start()
    browser = None
    try:
        deadline = time.monotonic() + 10
        while (
            not (server.started and private_server.started)
            and thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert server.started and private_server.started
        options = ChromiumOptions(read_file=False).set_browser_path(executable)
        options.set_tmp_path(str(tmp_path / "oauth-test-profile")).headless().auto_port()
        browser = Chromium(options)
        tab = browser.latest_tab
        tab.set.timeouts(base=3, page_load=5, script=3)

        def logged(entry):
            if "form-action" in entry.get("text", ""):
                diagnostics["form_action_errors"].append(entry.get("level"))

        tab.driver.set_callback("Log.entryAdded", logged)
        tab.run_cdp("Log.enable")
        verifier = "v" * 64
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        params = {
            "client_id": cfg.oauth_client_id,
            "redirect_uri": callback,
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "resource": cfg.resource,
            "scope": "browser",
            "state": "local-browser-test",
        }
        yield SimpleNamespace(
            tab=tab,
            cfg=cfg,
            params=params,
            verifier=verifier,
            arrived=arrived,
            arrivals=arrivals,
            diagnostics=diagnostics,
            mode=mode,
            service=service,
            auth=auth,
            target=target,
            approval_token=approval_token,
        )
    finally:
        if browser:
            browser.quit()
        private_server.should_exit = True
        private_thread.join(timeout=15)
        private_listener.close()
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        receiver.shutdown()
        receiver.server_close()
        receiver_thread.join(timeout=3)
        assert not thread.is_alive() and not private_thread.is_alive()


@pytest.mark.parametrize(
    "oauth_browser", ["current", "legacy-referrer", "legacy-csp"], indirect=True
)
def test_real_oauth_form_reaches_cross_origin_callback(oauth_browser):
    case = oauth_browser
    tab, cfg, params, diagnostics = case.tab, case.cfg, case.params, case.diagnostics
    tab.get(cfg.public_origin + "/authorize?" + urlencode(params))
    tab.ele("css:input[name=password]").input("test administrator password")
    tab.ele("css:button").click()
    completed = case.arrived.wait(3)
    if case.mode == "legacy-referrer":
        assert not completed and diagnostics["post_statuses"] == [403]
        assert diagnostics["origin"] == "null"
        return
    if case.mode == "legacy-csp":
        assert not completed and diagnostics["post_statuses"] == [303]
        assert diagnostics["origin"] == "matching"
        assert diagnostics["form_action_errors"] == ["error"]
        return
    assert diagnostics["post_statuses"] == [303], diagnostics
    assert completed, f"Separate OAuth callback not reached; policy diagnostics: {diagnostics}"
    assert diagnostics["origin"] == "matching" and not diagnostics["form_action_errors"]
    method, path, headers = case.arrivals[0]
    assert method == "GET" and "Content-Length" not in headers
    assert "Referer" not in headers
    result = parse_qs(urlsplit(path).query)
    assert set(result) == {"code", "state", "iss"}
    assert result["state"] == [params["state"]]
    assert result["iss"] == [cfg.public_origin]
    with httpx.Client(base_url=cfg.public_origin, trust_env=False) as client:
        form = {
            "grant_type": "authorization_code",
            "client_id": cfg.oauth_client_id,
            "resource": cfg.resource,
            "redirect_uri": params["redirect_uri"],
            "code_verifier": case.verifier,
            "code": result["code"][0],
        }
        token = client.post("/token", data=form)
        assert token.status_code == 200
        assert client.post("/token", data=form).status_code == 400


@pytest.mark.parametrize("destination", ["other-origin", "other-path"])
def test_real_oauth_csp_blocks_unregistered_form_target(oauth_browser, destination):
    case = oauth_browser
    tab = case.tab
    url = case.cfg.public_origin + "/authorize?" + urlencode(case.params)
    tab.get(url)
    # Inspect the current execution context, not DrissionPage's previous cached
    # document handle. Only read-only readiness is polled; mutation/submission run once.
    deadline = time.monotonic() + 5
    while True:
        ready = tab.run_cdp(
            "Runtime.evaluate",
            expression="location.href==="
            + json.dumps(url)
            + "&&document.readyState==='complete'&&!!document.querySelector('form input[name=password]')",
            returnByValue=True,
        )
        if ready.get("result", {}).get("value") is True:
            break
        assert time.monotonic() < deadline, "OAuth fixture document did not become ready"
        time.sleep(0.02)
    target = case.params["redirect_uri"]
    target = (
        target.replace("127.0.0.1", "localhost")
        if destination == "other-origin"
        else target + "/unregistered"
    )
    changed = tab.run_cdp(
        "Runtime.evaluate",
        expression="document.querySelector('form').action=" + json.dumps(target),
        returnByValue=True,
    )
    assert changed.get("result", {}).get("value") == target
    tab.ele("css:input[name=password]").input("test administrator password")
    tab.ele("css:button").click()
    assert not case.arrived.wait(1)
    assert case.diagnostics["form_action_errors"] == ["error"]
    assert not case.diagnostics["post_statuses"] and not case.arrivals


@pytest.mark.parametrize("oauth_browser", ["current", "legacy-console"], indirect=True)
def test_real_console_login_approval_and_handback_forms(oauth_browser):
    case = oauth_browser
    tab, cfg = case.tab, case.cfg

    def submit(selector):
        count = len(case.diagnostics["private_posts"])
        tab.ele(selector).click()
        deadline = time.monotonic() + 5
        while len(case.diagnostics["private_posts"]) == count and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(case.diagnostics["private_posts"]) == count + 1

    tab.get(cfg.control_origin + "/login")
    tab.ele("css:input[name=password]").input("test administrator password")
    submit("css:button")
    if case.mode == "legacy-console":
        assert case.diagnostics["private_posts"] == [("login", 403, False)], case.diagnostics
        return
    approve = "css:form[action^='/approval/'] button[value=approve]"
    tab.ele(approve)
    assert case.diagnostics["private_posts"] == [("login", 303, True)], str(case.diagnostics)
    assert case.diagnostics["private_gets"].count("/login") == 1
    # Alter only the test DOM to prove a valid Origin does not bypass CSRF.
    tab.run_js(
        "document.querySelector('form[action^=\"/approval/\"] input[name=csrf]').value='wrong';"
    )
    submit(approve)
    assert case.diagnostics["private_posts"][-1] == ("approval", 409, True)
    assert case.auth.store.get("approval", case.approval_token)["state"] == "pending"
    tab.get(cfg.control_origin + "/")
    submit(approve)
    assert case.auth.store.get("approval", case.approval_token)["state"] == "approved"
    assert case.diagnostics["private_posts"][-1] == ("approval", 303, True)
    assert case.service.worker.executions == 0  # Approval is not execution.
    submit("css:form[action$='/complete'] button")
    assert tab.ele("css:h1").text == "Control returned"
    lease = case.service.leases[case.target["session_id"]]
    assert lease["state"] == "completed"
    assert lease["result"]["tab_id"] == case.target["tab_id"]
    assert lease["result"]["revision"] == 2
    assert case.diagnostics["private_posts"][-1] == ("handoff", 200, True)
