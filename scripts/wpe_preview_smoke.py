"""Unprivileged, local-fixture-only WPE single-tab smoke test on Linux."""

import json
import os
import pwd
import socket
import struct
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

from cloud_browser import wpe
from cloud_browser.models import BrowserError


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/login":
            body = (
                "<!doctype html><meta charset=utf-8><title>Login fixture</title>"
                "<main>Private login fixture</main><input id=pass type=password name=password>"
                "<button id=login type=button onclick=\"if(document.querySelector('#pass').value==='trial-only')document.body.innerHTML='<main>Signed in fixture</main>'\">Sign in</button>"
            )
        else:
            title = "First" if self.path == "/first" else "Second"
            body = (
                f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                f"<main>{title} page. Godot 한글 테스트.</main>"
                "<button id=more onclick=\"document.querySelector('main').textContent='Clicked page'\">More</button>"
            )
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header(
            "Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'"
        )
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


def recv_exact(remote, count):
    chunks = []
    while count:
        chunk = remote.recv(count)
        if not chunk:
            raise AssertionError("Private desktop closed during RFB handshake")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def rfb_connect(remote):
    assert recv_exact(remote, 12).startswith(b"RFB 003.008")
    remote.sendall(b"RFB 003.008\n")
    count = recv_exact(remote, 1)[0]
    assert count > 0
    methods = recv_exact(remote, count)
    assert 1 in methods  # x11vnc is loopback-only; private HTTP console authenticates.
    remote.sendall(b"\x01")
    assert recv_exact(remote, 4) == b"\x00\x00\x00\x00"
    remote.sendall(b"\x01")
    init = recv_exact(remote, 24)
    name_length = int.from_bytes(init[20:24], "big")
    assert name_length < 4096
    recv_exact(remote, name_length)


def websocket_bridge_reachable():
    with socket.create_connection(("127.0.0.1", 16080), timeout=2) as bridge:
        bridge.sendall(
            b"GET /websockify HTTP/1.1\r\n"
            b"Host: 127.0.0.1:16080\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        return bridge.recv(128).startswith(b"HTTP/1.1 101")


def rfb_click(remote, rect):
    x = int(rect["x"] + rect["width"] / 2)
    y = int(rect["y"] + rect["height"] / 2)
    remote.sendall(struct.pack(">BBHH", 5, 1, x, y))
    time.sleep(0.08)
    remote.sendall(struct.pack(">BBHH", 5, 0, x, y))
    time.sleep(0.1)


def rfb_type(remote, text):
    for char in text:
        key = ord(char)
        remote.sendall(struct.pack(">BBHI", 4, 1, 0, key))
        remote.sendall(struct.pack(">BBHI", 4, 0, 0, key))
        time.sleep(0.04)


def rfb_enter(remote):
    remote.sendall(struct.pack(">BBHI", 4, 1, 0, 0xFF0D))
    remote.sendall(struct.pack(">BBHI", 4, 0, 0, 0xFF0D))


def main():
    managed = os.environ.get("CB_WPE_SMOKE_MANAGED") == "1"
    private_root = tempfile.TemporaryDirectory(prefix="cb-wpe-managed-smoke-")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    original_validate = wpe.validate_url

    def fixture_only(url, *, dns_proxy=None):
        if dns_proxy is not None or url not in (
            base + "/first",
            base + "/second",
            base + "/login",
        ):
            raise BrowserError("INVALID_URL", "Smoke test permits only its local fixture")

    wpe.validate_url = fixture_only
    if managed:
        original_runtime = wpe._Runtime

        class LoggedRuntime(original_runtime):
            def _spawn(self, command, env):
                log_path = Path(private_root.name) / f"process-{len(self.processes)}.log"
                with log_path.open("wb") as log:
                    self.processes.append(
                        subprocess.Popen(
                            command,
                            env=env,
                            cwd=self.root,
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                            start_new_session=True,
                        )
                    )

        wpe._Runtime = LoggedRuntime
    adapter = wpe.WPEAdapter(
        SimpleNamespace(
            wpe_driver_path="/usr/bin/WPEWebDriver",
            wpe_cog_path="/usr/bin/cog",
            wpe_weston_path="/usr/bin/weston",
            browser_proxy="",
            network_isolated=False,
            max_capture_pixels=8_000_000,
            managed_display=managed,
            headless=False,
            display_number=197,
            display_width=1024,
            display_height=768,
            runtime_dir=Path(private_root.name),
            browser_group=pwd.getpwuid(os.geteuid()).pw_name,
            vnc_port=15900,
            vnc_bridge_port=16080,
            auth_rules={},
        )
    )
    try:
        opened = adapter.open("ses_preview", base + "/first")
        tab_id = opened["tab_id"]
        first = adapter.observe("ses_preview", tab_id)
        assert "First page" in first["observation"]["semantic_snapshot"]
        node_id = json.loads(first["observation"]["interactive_snapshot"])["node_id"]
        visual = adapter.observe("ses_preview", tab_id, mode="visual")
        assert visual["_image"]["mimeType"] == "image/png"
        assert visual["observation"]["screenshot"]["width"] >= 1
        prepared = adapter.prepare(
            "ses_preview", tab_id, first["revision"], {"type": "click", "node_id": node_id}
        )
        assert prepared["requires_confirmation"]
        clicked = adapter.act(
            "ses_preview", tab_id, first["revision"], {"type": "click", "node_id": node_id}
        )
        assert clicked["action_result"]["performed"]
        assert "Clicked page" in adapter.observe("ses_preview", tab_id)["observation"][
            "semantic_snapshot"
        ]
        refs = adapter._call(
            adapter.sessions["ses_preview"],
            "POST",
            "/execute/sync",
            {
                "script": "return [{element:document.querySelector('button'),name:'More'}]",
                "args": [],
            },
        )
        assert "element-6066-11e4-a52e-4f735466cecf" in refs[0]["element"]
        if managed:
            adapter.focus("ses_preview", tab_id)
            assert adapter.sessions["ses_preview"]["paused"]
            with socket.create_connection(("127.0.0.1", 15900), timeout=2) as remote:
                rfb_connect(remote)
            assert websocket_bridge_reachable()
            adapter.resume("ses_preview", tab_id)
            assert not adapter.sessions["ses_preview"]["paused"]
            for port in (15900, 16080):
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", port)) != 0
        try:
            adapter.open("ses_preview", new_tab=True)
        except BrowserError as exc:
            assert exc.code == "UNSUPPORTED_OPERATION"
        else:
            raise AssertionError("Second tab must be rejected")
        assert adapter.list_tabs("ses_preview")["tabs"][0]["tab_id"] == tab_id
        second = adapter.open("ses_preview", base + "/second", new_tab=False)
        assert second["tab_id"] == tab_id
        observed = adapter.observe("ses_preview", tab_id)
        assert "Second page" in observed["observation"]["semantic_snapshot"]
        adapter.navigate("ses_preview", tab_id, "goto", base + "/login")
        try:
            adapter.observe("ses_preview", tab_id, mode="visual")
        except BrowserError as exc:
            assert exc.code == "SENSITIVE_CONTENT"
        else:
            raise AssertionError("Protected login screen must not be captured")
        if managed:
            controls = adapter._call(
                adapter.sessions["ses_preview"],
                "POST",
                "/execute/sync",
                {
                    "script": "return ['pass','login'].map(id=>{const r=document.getElementById(id).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}})",
                    "args": [],
                },
            )
            adapter.focus("ses_preview", tab_id)
            try:
                adapter.resume("ses_preview", tab_id, auth_origin=base)
            except BrowserError as exc:
                assert exc.code == "SENSITIVE_CONTENT"
            else:
                raise AssertionError("Login handoff must remain paused on a password screen")
            assert adapter.sessions["ses_preview"]["paused"]
            with socket.create_connection(("127.0.0.1", 15900), timeout=2) as remote:
                rfb_connect(remote)
                rfb_click(remote, controls[0])
                rfb_type(remote, "trial-only")
                rfb_click(remote, controls[1])
                rfb_enter(remote)
            time.sleep(0.2)
            signed_in = adapter._call(
                adapter.sessions["ses_preview"],
                "POST",
                "/execute/sync",
                {
                    "script": "return document.body.innerText.includes('Signed in fixture')",
                    "args": [],
                },
            )
            assert signed_in, "Private RFB input did not finish the local login fixture"
            resumed = adapter.resume("ses_preview", tab_id, auth_origin=base)
            assert resumed["authentication"]["authenticated"] is None
            for port in (15900, 16080):
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", port)) != 0
            assert "Signed in fixture" in adapter.observe("ses_preview", tab_id)[
                "observation"
            ]["semantic_snapshot"]
        print(
            json.dumps(
                {
                    "ok": True,
                    "one_tab": True,
                    "semantic": True,
                    "click": True,
                    "screenshot": True,
                    "reuse": True,
                    "private_control_bridge": managed,
                    "protected_login_boundary": True,
                }
            )
        )
    except Exception:
        if managed:
            for path in sorted(Path(private_root.name).glob("process-*.log")):
                print(f"{path.name}: {path.read_text(errors='replace')[:8000]}")
        raise
    finally:
        adapter.shutdown()
        wpe.validate_url = original_validate
        if managed:
            wpe._Runtime = original_runtime
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        private_root.cleanup()


if __name__ == "__main__":
    main()
