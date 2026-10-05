"""Opt-in, single-page WPE/Cog preview using the local W3C WebDriver.

It never calls /window/new: Cog 0.18 can return the existing handle for that
request, and closing that handle destroys the only browser window.
"""

import base64
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import psutil

from .models import BrowserError
from .observation import compact_node, paginate
from .runtime import DisplayRuntime
from .security import TOKEN, origin, redact, safe_url, validate_url


class _Driver:
    """Small W3C client; error details from arbitrary pages never reach MCP."""

    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        self.http = build_opener(ProxyHandler({}))
        self.session_id: str | None = None

    def request(self, method: str, path: str, body: dict | None = None):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            self.base + path,
            data=payload,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.http.open(request, timeout=25) as response:
                value = json.load(response).get("value")
        except HTTPError as exc:
            try:
                code = json.load(exc).get("value", {}).get("error", "webdriver error")
            except (ValueError, OSError):
                code = "webdriver error"
            if code == "invalid session id":
                raise BrowserError("SESSION_EXPIRED", "WPE browser session ended") from exc
            raise BrowserError(
                "BROWSER_ERROR",
                "WPE WebDriver rejected the command",
                driver_error=code if code in _DRIVER_ERRORS else "other",
            ) from exc
        except (OSError, URLError, ValueError) as exc:
            raise BrowserError("BROWSER_ERROR", "WPE WebDriver did not respond") from exc
        if isinstance(value, dict) and "error" in value:
            raise BrowserError("BROWSER_ERROR", "WPE WebDriver rejected the command")
        return value

    def call(self, method: str, path: str, body: dict | None = None):
        if not self.session_id:
            raise BrowserError("SESSION_EXPIRED", "WPE browser session ended")
        return self.request(method, f"/session/{self.session_id}{path}", body)

    def start(self, binary: str):
        result = self.request(
            "POST",
            "/session",
            {
                "capabilities": {
                    "alwaysMatch": {
                        "pageLoadStrategy": "normal",
                        "timeouts": {"implicit": 0, "pageLoad": 20000, "script": 5000},
                        "wpe:browserOptions": {
                            "binary": binary,
                            "args": ["--automation", "--platform=wl"],
                        },
                    }
                }
            },
        )
        self.session_id = result["sessionId"]

    def close(self):
        if self.session_id:
            try:
                self.request("DELETE", f"/session/{self.session_id}")
            finally:
                self.session_id = None


class _Runtime:
    def __init__(self, settings):
        self.cfg = settings
        self.temp = tempfile.TemporaryDirectory(prefix="cb-wpe-")
        self.root = Path(self.temp.name)
        self.processes: list[subprocess.Popen] = []
        self.driver: _Driver | None = None
        self.display: DisplayRuntime | None = None

    def start(self):
        if os.name != "posix" or os.geteuid() == 0:
            raise BrowserError("ENGINE_UNAVAILABLE", "WPE preview requires unprivileged Linux")
        for binary in (
            self.cfg.wpe_driver_path,
            self.cfg.wpe_cog_path,
            self.cfg.wpe_weston_path,
        ):
            if not Path(binary).is_file():
                raise BrowserError("ENGINE_UNAVAILABLE", "WPE, Cog or Weston is not installed")
        if getattr(self.cfg, "managed_display", False):
            self.display = DisplayRuntime(self.cfg)
            self.display.start()
        env = os.environ.copy()
        for key in ("DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY"):
            env.pop(key, None)
        if self.display is None:
            env.pop("DISPLAY", None)
            env.pop("XAUTHORITY", None)
        for key in ("XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
            directory = self.root / key.lower()
            directory.mkdir(mode=0o700)
            env[key] = str(directory)
        if any(
            "DISABLE_SANDBOX" in key and value not in ("", "0") for key, value in env.items()
        ):
            raise BrowserError("POLICY_BLOCKED", "WPE sandbox cannot be disabled", "blocked")
        name = "cb-wpe-" + secrets.token_hex(6)
        env["WAYLAND_DISPLAY"] = name
        env["COG_PLATFORM_WL_VIEW_WIDTH"] = "1024"
        env["COG_PLATFORM_WL_VIEW_HEIGHT"] = "768"
        weston = [
            self.cfg.wpe_weston_path,
            "--backend=x11" if self.display else "--backend=headless",
            "--renderer=pixman",
            "--width=1024",
            "--height=768",
            "--scale=1",
            "--shell=kiosk",
            f"--socket={name}",
            "--no-config",
        ]
        self._spawn(weston, env)
        socket_path = Path(env["XDG_RUNTIME_DIR"]) / name
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.processes[0].poll() is not None:
                break
            if socket_path.exists():
                break
            time.sleep(0.05)
        else:
            raise BrowserError("ENGINE_UNAVAILABLE", "Private WPE display did not start")
        if not socket_path.exists():
            raise BrowserError("ENGINE_UNAVAILABLE", "Private WPE display exited")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self._spawn(
            [self.cfg.wpe_driver_path, "--host=127.0.0.1", f"--port={port}"], env
        )
        driver = _Driver(port)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.processes[-1].poll() is not None:
                break
            try:
                driver.request("GET", "/status")
                break
            except BrowserError:
                time.sleep(0.1)
        else:
            raise BrowserError("ENGINE_UNAVAILABLE", "WPE WebDriver did not start")
        if self.processes[-1].poll() is not None:
            raise BrowserError("ENGINE_UNAVAILABLE", "WPE WebDriver exited")
        # A failed session-create might still have launched Cog; never retry it.
        driver.start(self.cfg.wpe_cog_path)
        self.driver = driver

    def _spawn(self, command, env):
        if any("--no-sandbox" in arg or "--disable-web-security" in arg for arg in command):
            raise BrowserError("POLICY_BLOCKED", "Unsafe browser launch option", "blocked")
        self.processes.append(
            subprocess.Popen(
                command,
                env=env,
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        )

    def close(self):
        if self.driver:
            try:
                self.driver.close()
            except BrowserError:
                pass
        for process in reversed(self.processes):
            try:
                family = psutil.Process(process.pid)
                children = family.children(recursive=True)
            except psutil.Error:
                children = []
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=2)
        self.processes.clear()
        if self.display:
            self.display.close()
            self.display = None
        self.temp.cleanup()

    def start_control(self):
        if not self.display:
            raise BrowserError("HANDOFF_UNAVAILABLE", "WPE private display is not configured")
        self.display.start_control()

    def stop_control(self):
        if self.display:
            self.display.stop_control()


_ELEMENT = "element-6066-11e4-a52e-4f735466cecf"
_DRIVER_ERRORS = {
    "element click intercepted",
    "element not interactable",
    "stale element reference",
    "no such element",
    "invalid argument",
    "timeout",
    "unknown error",
}
_READ_PAGE = """const sensitive=/password|passwd|one.?time|otp|auth.?code|credit.?card|card.?number|cc-number|cc-csc|secret|api.?key|access.?token|refresh.?token/i;
const inputs=[...document.querySelectorAll('input,textarea,[contenteditable]')];
const protectedPage=inputs.some(e=>sensitive.test([e.type,e.name,e.id,e.autocomplete,e.getAttribute('aria-label')].join(' ')));
const filled=inputs.some(e=>!['checkbox','radio','button','submit','hidden'].includes(e.type)&&!!(e.value||e.textContent||'').trim());
const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&r.right>0&&r.bottom>0&&r.left<innerWidth&&r.top<innerHeight&&s.display!=='none'&&s.visibility!=='hidden'};
const candidates=[...document.querySelectorAll('a[href],button,input:not([type=hidden]),select,textarea,[role=button]')].filter(visible);
const controls=candidates.slice(0,100).map(e=>{const r=e.getBoundingClientRect(),form=e.form;return {
  element:e,tag:e.localName||'',name:(e.getAttribute('aria-label')||e.innerText||e.getAttribute('title')||e.getAttribute('placeholder')||'').trim().slice(0,300),
  role:e.getAttribute('role')||'',type:e.type||'',disabled:!!e.disabled,readonly:!!e.readOnly,
  editable:e.matches('input:not([type=button]),textarea,[contenteditable=true]'),
  rect:{x:r.x,y:r.y,width:r.width,height:r.height},href:e.href||'',target:e.target||'',
  formAction:form?.action||'',formMethod:(form?.method||'').toUpperCase(),submitsForm:!!form&&e.type==='submit'
}});
return {title:document.title||'',text:protectedPage?'':(document.body?.innerText||'').slice(0,100000),protectedPage,filled,
  hasIframe:!!document.querySelector('iframe,frame'),hasVisualMedia:!!document.querySelector('canvas,video,svg'),
  controls,controlsTruncated:candidates.length>100,
  viewport:{width:innerWidth,height:innerHeight},scroll:{x:scrollX,y:scrollY}};"""


class WPEAdapter:
    """One WPE window per work, with conservative observation and click approval."""

    def __init__(self, settings):
        self.cfg = settings
        self.sessions: dict[str, dict] = {}

    def __getattr__(self, name):
        raise BrowserError(
            "UNSUPPORTED_OPERATION", f"WPE single-tab preview does not support {name}"
        )

    def _session(self, sid, tid=None):
        state = self.sessions.get(sid)
        if not state:
            raise BrowserError("SESSION_EXPIRED", "WPE browser session is no longer running")
        if tid is not None and tid != state["tab_id"]:
            raise BrowserError("TAB_NOT_FOUND", "This WPE work has only one page")
        return state

    def _call(self, state, method, path, body=None):
        return state["runtime"].driver.call(method, path, body)

    def _result(self, sid, state, **extra):
        url = self._call(state, "GET", "/url")
        title = self._call(state, "GET", "/title")
        return {
            "session_id": sid,
            "tab_id": state["tab_id"],
            "selected_tab_id": state["tab_id"],
            "revision": state["revision"],
            "page": {"url": safe_url(url), "title": redact(title or "") or None},
            **extra,
        }

    def open(self, session_id, url=None, new_tab=True):
        if session_id in self.sessions:
            state = self.sessions[session_id]
            if new_tab:
                raise BrowserError(
                    "UNSUPPORTED_OPERATION",
                    "WPE supports one page per work; use new_tab=false or a new work",
                )
            return self.navigate(session_id, state["tab_id"], "goto", url) if url else self._result(session_id, state)
        if self.sessions:
            raise BrowserError("BROWSER_BUSY", "WPE preview permits one work at a time")
        runtime = _Runtime(self.cfg)
        try:
            runtime.start()
            state = {
                "runtime": runtime,
                "tab_id": "tab_" + secrets.token_urlsafe(12),
                "revision": 0,
                "digest": "",
                "cursors": {},
                "nodes": {},
                "screenshot": None,
                "paused": False,
                "max_chars": 30000,
            }
            self.sessions[session_id] = state
            if url:
                return self.navigate(session_id, state["tab_id"], "goto", url)
            return self._result(session_id, state)
        except BaseException:
            self.sessions.pop(session_id, None)
            runtime.close()
            raise

    def list_tabs(self, session_id):
        state = self._session(session_id)
        page = self._result(session_id, state)["page"]
        return {
            "session_id": session_id,
            "selected_tab_id": state["tab_id"],
            "tabs": [{"tab_id": state["tab_id"], **page, "selected": True}],
        }

    def navigate(self, session_id, tab_id, operation, url=None):
        state = self._session(session_id, tab_id)
        if operation != "goto" or not url:
            raise BrowserError(
                "UNSUPPORTED_OPERATION",
                "WPE preview supports explicit goto only; history/reload may repeat requests",
            )
        validate_url(url, dns_proxy=self.cfg.browser_proxy if self.cfg.network_isolated else None)
        before = self._call(state, "GET", "/url")
        # Never retry after dispatch: a timeout may have changed the page.
        self._call(state, "POST", "/url", {"url": url})
        state["revision"] += 1
        state["digest"] = ""
        state["cursors"].clear()
        state["nodes"].clear()
        state["screenshot"] = None
        result = self._result(session_id, state)
        final_url = self._call(state, "GET", "/url")
        result["navigation"] = {
            "operation": "goto",
            "redirected": final_url != url,
            "navigation_occurred": True,
            "url_changed": final_url != before,
            "document_changed": True,
            "navigation_kind": "full_document",
        }
        return result

    def observe(
        self,
        session_id,
        tab_id,
        mode="auto",
        full_page=False,
        max_chars=None,
        cursor=None,
        lightweight=False,
        query=None,
        **_options,
    ):
        state = self._session(session_id, tab_id)
        if full_page or query:
            raise BrowserError(
                "UNSUPPORTED_OPERATION",
                "WPE preview cannot inspect frames or capture a full-page image",
            )
        if mode not in ("auto", "semantic", "interactive", "visual"):
            raise BrowserError("INVALID_INPUT", "Unknown observation mode")
        data, semantic = self._snapshot(state)
        if cursor:
            saved = state["cursors"].get(cursor)
            if not saved or saved["revision"] != state["revision"]:
                raise BrowserError("CURSOR_STALE", "Observation cursor is no longer valid")
            snapshot, offsets, budget = saved["snapshot"], saved["offsets"], saved["budget"]
        else:
            nodes = [
                compact_node({"node_id": node_id, **item["meta"]})
                for node_id, item in state["nodes"].items()
            ]
            snapshot, offsets = {
                "semantic": semantic if mode in ("auto", "semantic") else "",
                "nodes": nodes if mode in ("auto", "interactive") else [],
            }, (0, 0)
            budget = max_chars or state["max_chars"]
        observation, next_offsets = paginate(snapshot, offsets, budget)
        observation.update(
            screenshot=None,
            viewport=data.get("viewport") or {"width": 1024, "height": 768},
            semantic_source="rendered-dom",
            accessibility_source="unavailable",
            interactive_truncated=bool(data.get("controlsTruncated")),
            query_scan_truncated=bool(data.get("controlsTruncated")),
            semantic_source_truncated=len(semantic) >= 100000,
            scroll_scan_truncated=False,
            readable_frames=0,
            frame_reading_truncated=bool(data.get("hasIframe")),
            frames=[],
            file_chooser=None,
        )
        if observation["truncated"]:
            next_cursor = "cursor_" + secrets.token_urlsafe(12)
            state["cursors"][next_cursor] = {
                "revision": state["revision"],
                "snapshot": snapshot,
                "offsets": next_offsets,
                "budget": budget,
            }
            if len(state["cursors"]) > 64:
                state["cursors"].pop(next(iter(state["cursors"])))
            observation["next_cursor"] = next_cursor
        extra = {}
        if mode == "visual":
            if data.get("hasIframe") or data.get("hasVisualMedia") or data.get("filled"):
                raise BrowserError(
                    "SENSITIVE_SCREEN",
                    "Screenshot needs an unfilled page without frames or visual media",
                    "blocked",
                )
            image = self._call(state, "GET", "/screenshot")
            if not isinstance(image, str) or len(image) > 8 * 1024 * 1024:
                raise BrowserError("SCREEN_UNAVAILABLE", "WPE screenshot exceeded safety limit")
            try:
                raw_image = base64.b64decode(image, validate=True)
            except ValueError as exc:
                raise BrowserError("SCREEN_UNAVAILABLE", "WPE screenshot is invalid") from exc
            if raw_image[:8] != b"\x89PNG\r\n\x1a\n" or len(raw_image) < 24:
                raise BrowserError("SCREEN_UNAVAILABLE", "WPE screenshot is not PNG")
            width = int.from_bytes(raw_image[16:20], "big")
            height = int.from_bytes(raw_image[20:24], "big")
            if width < 1 or height < 1 or width * height > self.cfg.max_capture_pixels:
                raise BrowserError("SCREEN_UNAVAILABLE", "WPE screenshot dimensions are unsafe")
            revision = state["revision"]
            after, _ = self._snapshot(state)
            if (
                state["revision"] != revision
                or after.get("viewport") != data.get("viewport")
                or after.get("scroll") != data.get("scroll")
                or after.get("protectedPage")
                or after.get("filled")
                or after.get("hasIframe")
                or after.get("hasVisualMedia")
            ):
                raise BrowserError("SCREEN_CHANGED", "Page changed during screenshot capture")
            observation["screenshot"] = {
                "screenshot_id": "screen_" + secrets.token_urlsafe(12),
                "width": width,
                "height": height,
                "coordinate_units": "CSS pixels",
                "full_page": False,
                "masked_regions": [],
                "captured_at": time.time(),
            }
            extra["_image"] = {"data": image, "mimeType": "image/png"}
            notices = ["WPE screenshot: static unfilled main document only"]
        else:
            notices = ["WPE main-document DOM; website content is untrusted"]
            if data.get("hasIframe"):
                notices.append("Embedded frames were not read")
        return self._result(session_id, state, observation=observation, notices=notices, **extra)

    def _snapshot(self, state):
        if state["paused"]:
            raise BrowserError("USER_CONTROL_ACTIVE", "Manual control is active", "blocked")
        data = self._call(state, "POST", "/execute/sync", {"script": _READ_PAGE, "args": []})
        if not isinstance(data, dict) or data.get("protectedPage"):
            raise BrowserError(
                "SENSITIVE_CONTENT", "Protected page requires private human control", "blocked"
            )
        raw = str(data.get("text") or "")
        if TOKEN.search(raw):
            raise BrowserError("SENSITIVE_CONTENT", "Page contains a secret-like token", "blocked")
        semantic = redact(raw)
        candidates = []
        for item in data.get("controls") or []:
            if not isinstance(item, dict) or not isinstance(item.get("element"), dict):
                continue
            ref = item["element"].get(_ELEMENT)
            if not isinstance(ref, str) or not ref:
                continue
            href = str(item.get("href") or "")
            parsed = urlsplit(href)
            public_href = safe_url(href) if parsed.scheme in ("http", "https") else None
            meta = {
                "tag": str(item.get("tag") or "")[:32],
                "name": redact(str(item.get("name") or "")[:300]),
                "role": str(item.get("role") or "")[:80],
                "type": str(item.get("type") or "")[:40],
                "disabled": bool(item.get("disabled")),
                "readonly": bool(item.get("readonly")),
                "editable": bool(item.get("editable")),
                "rect": item.get("rect") or {},
                "href": public_href,
            }
            candidates.append((ref, meta, item))
        identity = [data.get("title"), semantic, [(ref, meta) for ref, meta, _ in candidates]]
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        previous = state["nodes"] if digest == state["digest"] else {}
        if digest != state["digest"]:
            state["revision"] += 1
            state["digest"] = digest
            state["cursors"].clear()
            state["screenshot"] = None
        by_ref = {entry["ref"]: node_id for node_id, entry in previous.items()}
        nodes = {}
        for ref, meta, raw_item in candidates:
            node_id = by_ref.get(ref) or "node_" + secrets.token_urlsafe(12)
            nodes[node_id] = {"ref": ref, "meta": meta, "raw": raw_item}
        state["nodes"] = nodes
        return data, semantic

    def prepare(self, session_id, tab_id, expected_revision, action):
        state = self._session(session_id, tab_id)
        if action.get("type") != "click" or not action.get("node_id"):
            raise BrowserError("UNSUPPORTED_OPERATION", "WPE preview supports observed-node click only")
        if expected_revision != state["revision"]:
            raise BrowserError("STALE_REVISION", "Observe the current page before acting")
        self._snapshot(state)
        if expected_revision != state["revision"]:
            raise BrowserError("STALE_REVISION", "Page changed after observation")
        item = state["nodes"].get(action["node_id"])
        if not item:
            raise BrowserError("STALE_NODE", "Observed control is no longer available")
        meta, raw_item = item["meta"], item["raw"]
        if meta["disabled"]:
            raise BrowserError("NODE_NOT_ACTIONABLE", "Control is disabled")
        if raw_item.get("submitsForm"):
            raise BrowserError("UNSUPPORTED_OPERATION", "Form submission needs a verified form snapshot")
        if raw_item.get("target") not in (None, "", "_self"):
            raise BrowserError("UNSUPPORTED_OPERATION", "Links opening another window are unavailable")
        href = str(raw_item.get("href") or "")
        if href and urlsplit(href).scheme not in ("http", "https"):
            raise BrowserError("UNSUPPORTED_OPERATION", "Non-HTTP link activation is unavailable")
        if href:
            # This is only a pre-dispatch guard for declared destinations.
            # JavaScript handlers and redirects still require network confinement.
            validate_url(
                href,
                dns_proxy=self.cfg.browser_proxy if self.cfg.network_isolated else None,
            )
        target_binding = hashlib.sha256(
            json.dumps(
                [self._call(state, "GET", "/url"), state["revision"], item["ref"], meta],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return self._result(
            session_id,
            state,
            target=meta["name"] or meta["tag"],
            destination=meta["href"],
            destination_kind="declared_link" if meta["href"] else "unknown",
            data_sent=[],
            data_sent_truncated=False,
            files=[],
            requires_confirmation=True,
            action_policy={
                "mode": "strict",
                "approval_required": True,
                "reason": "wpe_unclassified_click",
            },
            resolved_node_id=action["node_id"],
            target_binding=target_binding,
        )

    def act(self, session_id, tab_id, expected_revision, action):
        prepared = self.prepare(session_id, tab_id, expected_revision, action)
        state = self._session(session_id, tab_id)
        before_url = self._call(state, "GET", "/url")
        before_digest = state["digest"]
        ref = state["nodes"][action["node_id"]]["ref"]
        try:
            self._call(state, "POST", f"/element/{ref}/click", {})
        except BrowserError as exc:
            raise BrowserError(
                "RESULT_UNCERTAIN",
                "Click may have been dispatched; do not repeat automatically",
                **({"driver_error": exc.details["driver_error"]} if "driver_error" in exc.details else {}),
            ) from exc
        state["revision"] += 1
        state["digest"] = ""
        state["nodes"].clear()
        state["cursors"].clear()
        state["screenshot"] = None
        try:
            self._snapshot(state)
        except BrowserError as exc:
            if exc.code != "SENSITIVE_CONTENT":
                raise BrowserError(
                    "RESULT_UNCERTAIN", "Click was dispatched; inspect before another action"
                ) from exc
            return {
                "status": "user_action_required",
                "session_id": session_id,
                "tab_id": tab_id,
                "revision": state["revision"],
                "page": None,
                "action_result": {"performed": True, "page_changed": True},
                "notices": ["Protected page opened; request private authentication control"],
            }
        try:
            after_url = self._call(state, "GET", "/url")
            handles = self._call(state, "GET", "/window/handles")
        except BrowserError as exc:
            raise BrowserError(
                "RESULT_UNCERTAIN", "Click was dispatched; inspect before another action"
            ) from exc
        if len(handles) != 1:
            raise BrowserError("RESULT_UNCERTAIN", "Click opened another window; inspect privately")
        changed = state["digest"] != before_digest or after_url != before_url
        return self._result(
            session_id,
            state,
            status="ok" if changed else "no_change",
            action_result={
                "performed": True,
                "page_changed": changed,
                "url_changed": after_url != before_url,
                "document_changed": after_url != before_url,
                "navigation_occurred": after_url != before_url,
                "new_tab_ids": [],
            },
            action_policy=prepared["action_policy"],
        )

    def configure(self, session_id, tab_id, options):
        state = self._session(session_id, tab_id)
        if set(options) - {"max_chars"}:
            raise BrowserError("UNSUPPORTED_OPERATION", "WPE preview can only change max_chars")
        if "max_chars" in options:
            state["max_chars"] = options["max_chars"]
        return self._result(
            session_id,
            state,
            configuration={
                "viewport_width": 1024,
                "viewport_height": 768,
                "screenshot_quality": 75,
                "max_chars": state["max_chars"],
                "wait_ms": 0,
            },
        )

    def list_page_tools(self, session_id, tab_id):
        state = self._session(session_id, tab_id)
        return self._result(session_id, state, tools=[])

    def focus(self, session_id, tab_id):
        state = self._session(session_id, tab_id)
        state["paused"] = True
        state["nodes"].clear()
        state["cursors"].clear()
        state["screenshot"] = None
        state["runtime"].start_control()
        return {"session_id": session_id, "tab_id": tab_id}

    def resume(self, session_id, tab_id, auth_origin=None):
        state = self._session(session_id, tab_id)
        if not state["paused"]:
            raise BrowserError("HANDOFF_UNAVAILABLE", "No WPE private control is active")
        data = self._call(state, "POST", "/execute/sync", {"script": _READ_PAGE, "args": []})
        if not isinstance(data, dict) or data.get("protectedPage"):
            raise BrowserError(
                "SENSITIVE_CONTENT", "Protected screen remains open; finish login privately",
                "user_action_required",
            )
        authentication = {
            "authenticated": None,
            "verification": "unverified",
            "authentication_outcome": "unverified",
        }
        if auth_origin:
            current = self._call(state, "GET", "/url")
            if origin(current) == auth_origin:
                rule = self.cfg.auth_rules.get(auth_origin)
                if rule:
                    flags = self._call(
                        state,
                        "POST",
                        "/execute/sync",
                        {
                            "script": "const visible=s=>s&&[...document.querySelectorAll(s)].some(e=>{const r=e.getBoundingClientRect(),c=getComputedStyle(e);return r.width>0&&r.height>0&&c.display!=='none'&&c.visibility!=='hidden'});return {success:!!visible(arguments[0]),failure:!!visible(arguments[1])}",
                            "args": [rule.success_selector, rule.failure_selector],
                        },
                    )
                    if flags.get("failure"):
                        raise BrowserError(
                            "AUTH_FAILED", "Configured site indicator reports login failure",
                            "user_action_required",
                        )
                    if flags.get("success"):
                        authentication = {
                            "authenticated": True,
                            "verification": "operator_rule",
                            "authentication_outcome": "verified",
                        }
        # Keep the private bridge open until a fresh safe snapshot succeeds.
        # On any failure, restore the pause before another MCP call can read.
        state["paused"] = False
        state["revision"] += 1
        state["digest"] = ""
        try:
            self._snapshot(state)
        except BaseException:
            state["paused"] = True
            state["nodes"].clear()
            state["cursors"].clear()
            raise
        try:
            state["runtime"].stop_control()
        except BaseException:
            state["paused"] = True
            state["nodes"].clear()
            state["cursors"].clear()
            raise
        return self._result(
            session_id,
            state,
            **({"authentication": authentication} if auth_origin else {}),
        )

    def close(self, session_id, scope, tab_id=None):
        state = self._session(session_id, tab_id)
        if scope not in ("tab", "session"):
            raise BrowserError("INVALID_INPUT", "Close scope must be tab or session")
        state["runtime"].close()
        del self.sessions[session_id]
        return {
            "session_id": session_id,
            "tab_id": state["tab_id"] if scope == "tab" else None,
            "selected_tab_id": None,
            "session_closed": True,
            "termination_reason": "last_tab_closed" if scope == "tab" else "session_closed",
        }

    def shutdown(self):
        for session_id in list(self.sessions):
            self.close(session_id, "session")
