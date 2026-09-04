"""The only module that imports DrissionPage (separately licensed).

Element handles are obtained once while observing; actions never rerun selectors.
All callers must serialize access through the worker.
"""

import base64
import hashlib
import json
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .models import BrowserError
from .security import SENSITIVE, TOKEN, redact, safe_url, validate_url

SNAPSHOT = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")


@dataclass
class TabState:
    tab: object
    revision: int = 0
    fingerprint: str = ""
    nodes: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)
    screenshot: dict | None = None
    cursors: dict = field(default_factory=dict)
    document_method: str | None = None
    options: dict = field(
        default_factory=lambda: {
            "viewport_width": 1024,
            "viewport_height": 768,
            "screenshot_quality": 75,
            "max_chars": 30000,
            "wait_ms": 500,
        }
    )


class DrissionAdapter:
    def __init__(self, settings: Settings):
        from DrissionPage import Chromium, ChromiumOptions
        from DrissionPage._elements.chromium_element import ChromiumElement

        self.Chromium, self.Options, self.Element = Chromium, ChromiumOptions, ChromiumElement
        self.cfg = settings
        self.sessions = {}

    def _session(self, sid):
        if sid not in self.sessions:
            raise BrowserError("SESSION_EXPIRED", "Browser session is no longer running")
        return self.sessions[sid]

    def _sync(self, sid):
        session = self._session(sid)
        try:
            live = session["browser"].get_tabs()
        except Exception as exc:
            raise BrowserError("SESSION_EXPIRED", "Browser process disconnected") from exc
        ids = {tab.tab_id for tab in live}
        session["tabs"] = {
            key: value for key, value in session["tabs"].items() if value.tab.tab_id in ids
        }
        known = {state.tab.tab_id for state in session["tabs"].values()}
        added = []
        for tab in live:
            if tab.tab_id not in known:
                key = "tab_" + secrets.token_urlsafe(12)
                state = TabState(tab)
                self._viewport(state)

                self._watch_document(state)
                session["tabs"][key] = state
                added.append(key)
        if session["selected"] not in session["tabs"]:
            session["selected"] = next(iter(session["tabs"]), None)
        return added

    def _tab(self, sid, tid):
        self._sync(sid)
        state = self._session(sid)["tabs"].get(tid)
        if state is None:
            raise BrowserError("TAB_NOT_FOUND", "Tab was closed or does not exist")
        return state

    def _viewport(self, state):
        state.tab.run_cdp(
            "Emulation.setDeviceMetricsOverride",
            width=state.options["viewport_width"],
            height=state.options["viewport_height"],
            deviceScaleFactor=1,
            mobile=False,
        )

    def _watch_document(self, state):
        def request_seen(request, type=None, frameId=None, **kwargs):
            if type == "Document" and frameId == getattr(state.tab, "_frame_id", None):
                state.document_method = request.get("method")

        state.tab._driver.set_callback("Network.requestWillBeSent", request_seen)
        state.tab.run_cdp("Network.enable")

    def _capture_state(self, state):
        tab = state.tab
        tab.run_cdp("Runtime.releaseObjectGroup", objectGroup="cb-observation")
        frame = tab.run_cdp("Page.getFrameTree")["frameTree"]["frame"]["id"]
        world = tab.run_cdp(
            "Page.createIsolatedWorld", frameId=frame, worldName="cloud-browser-observer"
        )["executionContextId"]
        result = tab.run_cdp(
            "Runtime.evaluate",
            expression=SNAPSHOT,
            contextId=world,
            objectGroup="cb-observation",
            returnByValue=False,
        )
        if "exceptionDetails" in result or "objectId" not in result.get("result", {}):
            raise BrowserError("OBSERVATION_FAILED", "Page cannot be observed yet")
        oid = result["result"]["objectId"]
        data = tab.run_cdp(
            "Runtime.callFunctionOn",
            objectId=oid,
            functionDeclaration="function(){return this.data}",
            returnByValue=True,
        )["result"]["value"]
        elements = tab.run_cdp(
            "Runtime.callFunctionOn",
            objectId=oid,
            functionDeclaration="function(){return this.elements}",
            objectGroup="cb-observation",
        )["result"]["objectId"]
        props = tab.run_cdp("Runtime.getProperties", objectId=elements, ownProperties=True)[
            "result"
        ]
        backends = []
        for prop in props:
            if prop["name"].isdigit():
                backends.append(
                    tab.run_cdp("DOM.describeNode", objectId=prop["value"]["objectId"])["node"][
                        "backendNodeId"
                    ]
                )
        # Actual backend IDs distinguish replacements with identical visible text.
        fingerprint = hashlib.sha256(
            json.dumps([data, backends], sort_keys=True).encode()
        ).hexdigest()
        if fingerprint != state.fingerprint:
            state.revision += 1
            state.fingerprint = fingerprint
            state.screenshot = None
            state.cursors.clear()
            state.nodes = {
                "node_" + secrets.token_urlsafe(10): (bid, meta)
                for bid, meta in zip(backends, data["nodes"], strict=True)
            }
        state.data = data
        return data

    def _result(self, sid, tid, **extra):
        state = self._tab(sid, tid)
        return dict(
            session_id=sid,
            tab_id=tid,
            revision=state.revision,
            page={"url": safe_url(state.tab.url), "title": redact(state.tab.title or "") or None},
            **extra,
        )

    def open(self, session_id, url=None, new_tab=True):
        if url:
            validate_url(url)
        sid = session_id
        created = sid not in self.sessions or new_tab
        if sid not in self.sessions:
            profile = self.cfg.data_dir / "profiles" / sid
            profile.mkdir(parents=True, exist_ok=True)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            options = self.Options(read_file=False)
            options.set_browser_path(self.cfg.chromium_path).set_local_port(
                port
            ).set_user_data_path(str(profile))
            options.set_timeouts(base=5, page_load=20, script=5).set_retry(times=0)
            options.headless(self.cfg.headless)
            options.set_argument("--window-size=1024,768")
            options.set_argument("--disable-quic")
            options.set_argument("--no-first-run")
            options.set_argument("--no-default-browser-check")
            options.set_argument("--dns-prefetch-disable")
            options.set_argument("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
            options.set_argument("--disable-features=Translate,MediaRouter")
            if self.cfg.browser_proxy:
                options.set_proxy(self.cfg.browser_proxy)
                options.set_argument("--proxy-bypass-list=<-loopback>")
            browser = self.Chromium(options)
            self.sessions[sid] = {"browser": browser, "tabs": {}, "selected": None}
            self._sync(sid)
        elif new_tab:
            self.sessions[sid]["browser"].new_tab()
            added = self._sync(sid)
            if added:
                self.sessions[sid]["selected"] = added[-1]
        session = self._session(sid)
        tid = session["selected"]
        if tid is None:
            session["browser"].new_tab()
            self._sync(sid)
            tid = session["selected"]
        if url:
            return self.navigate(sid, tid, "goto", url)
        if created:
            self._tab(sid, tid).tab.get("about:blank", retry=0)
        self._capture_state(self._tab(sid, tid))
        return self._result(sid, tid)

    def list_tabs(self, session_id):
        self._sync(session_id)
        session = self._session(session_id)
        return {
            "session_id": session_id,
            "selected_tab_id": session["selected"],
            "tabs": [
                {
                    "tab_id": tid,
                    "url": safe_url(s.tab.url),
                    "title": redact(s.tab.title or "") or None,
                    "selected": tid == session["selected"],
                }
                for tid, s in reversed(list(session["tabs"].items()))
            ],
        }

    def navigate(self, session_id, tab_id, operation, url=None):
        state = self._tab(session_id, tab_id)
        old_url = state.tab.url
        if operation == "goto":
            if not url:
                raise BrowserError("INVALID_URL", "goto requires a URL")
            validate_url(url)
            completed = state.tab.get(url, retry=0, timeout=20)
            if not completed:
                raise BrowserError(
                    "NAVIGATION_TIMEOUT", "Navigation did not complete; observe before deciding"
                )
        elif operation in ("back", "forward"):
            history = state.tab.run_cdp("Page.getNavigationHistory")
            index = history["currentIndex"] + (-1 if operation == "back" else 1)
            if index < 0 or index >= len(history["entries"]):
                return self._result(session_id, tab_id, status="no_change")
            validate_url(history["entries"][index]["url"])
            state.tab.run_cdp(
                "Page.navigateToHistoryEntry", entryId=history["entries"][index]["id"]
            )
        elif operation == "reload":
            if state.document_method != "GET":
                raise BrowserError(
                    "CONFIRMATION_REQUIRED",
                    "Unknown/POST document reload requires manual control",
                    "confirmation_required",
                )
            state.tab.refresh(ignore_cache=False)
        else:
            raise BrowserError("UNSUPPORTED_OPERATION", "Unknown navigation operation")
        time.sleep(state.options["wait_ms"] / 1000)
        state.fingerprint = ""
        self._capture_state(state)
        self._session(session_id)["selected"] = tab_id
        return self._result(
            session_id,
            tab_id,
            navigation={
                "operation": operation,
                "redirected": operation == "goto" and state.tab.url != url,
                "navigation_occurred": state.tab.url != old_url,
            },
        )

    def observe(
        self, session_id, tab_id, mode="auto", full_page=False, max_chars=None, cursor=None
    ):
        state = self._tab(session_id, tab_id)
        data = self._capture_state(state)
        if data["protected"]:
            raise BrowserError(
                "AUTH_REQUIRED",
                "Protected input screen: use browser_auth_request",
                "user_action_required",
            )
        if data.get("challenge"):
            kind = data["challenge"]
            raise BrowserError(
                "CAPTCHA_REQUIRED" if kind == "captcha" else "BOT_BLOCKED",
                "Page shows explicit human-verification or automation-blocking text; no bypass attempted",
                "user_action_required" if kind == "captcha" else "blocked",
            )
        if cursor:
            saved = state.cursors.get(cursor)
            if not saved or saved["revision"] != state.revision:
                raise BrowserError("CURSOR_STALE", "Observation cursor is no longer valid")
            snapshot, offset, budget = saved["snapshot"], saved["offset"], saved["budget"]
        else:
            budget = max_chars or state.options["max_chars"]
            semantic = redact(data["text"])
            try:
                if data["has_iframe"]:
                    raise ValueError("Embedded accessibility tree is not safely exposed")
                ax = state.tab.run_cdp("Accessibility.getFullAXTree")["nodes"]
                lines = []
                for node in ax:
                    role = node.get("role", {}).get("value", "")
                    name = node.get("name", {}).get("value", "")
                    if not node.get("ignored") and name and not SENSITIVE.search(role):
                        lines.append(f"- {role}: {redact(str(name))}")
                semantic = "\n".join(lines) + "\n\n" + semantic
            except Exception:
                semantic = "[Accessibility tree unavailable; rendered DOM follows]\n" + semantic
            interactive = []
            for nid, (_, meta) in state.nodes.items():
                clean = {
                    k: (redact(v) if isinstance(v, str) else v)
                    for k, v in meta.items()
                    if k != "href"
                }
                if meta["href"]:
                    clean["href"] = safe_url(meta["href"])
                interactive.append({"node_id": nid, **clean})
            snapshot = {
                "semantic_snapshot": semantic if mode in ("auto", "semantic") else "",
                "interactive_snapshot": "\n".join(
                    json.dumps(n, ensure_ascii=False) for n in interactive
                )
                if mode in ("auto", "interactive")
                else "",
            }
            offset = 0
        combined = snapshot["semantic_snapshot"] + "\n" + snapshot["interactive_snapshot"]
        end = offset + budget
        # Pagination slices each field while preserving one common character budget.
        split = len(snapshot["semantic_snapshot"])
        obs = {
            "semantic_snapshot": combined[offset : min(end, split)] if offset < split else "",
            "interactive_snapshot": combined[max(offset, split + 1) : end]
            if end > split + 1
            else "",
            "truncated": end < len(combined),
            "next_cursor": None,
            "screenshot": None,
            "viewport": data["viewport"],
            "interactive_truncated": data["interactive_truncated"],
        }
        if obs["truncated"]:
            next_cursor = "cursor_" + secrets.token_urlsafe(16)
            state.cursors[next_cursor] = {
                "revision": state.revision,
                "snapshot": snapshot,
                "offset": end,
                "budget": budget,
            }
            # Bound server memory even if a client retains many cursors.
            if len(state.cursors) > 64:
                state.cursors.pop(next(iter(state.cursors)))
            obs["next_cursor"] = next_cursor
        extra = {}
        if mode == "visual" or (mode == "auto" and data["has_canvas"]):
            if data["has_iframe"] or TOKEN.search(data["text"]):
                raise BrowserError(
                    "SENSITIVE_SCREEN",
                    "Cannot safely capture embedded or sensitive content",
                    "blocked",
                )
            height = data["height"] if full_page else data["viewport"]["height"]
            width = data["viewport"]["width"]
            if width * height > self.cfg.max_capture_pixels:
                raise BrowserError(
                    "RESOURCE_PRESSURE",
                    "Capture exceeds operator pixel budget; use viewport capture",
                )
            capture = (
                {"clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1}}
                if full_page
                else {}
            )
            image = state.tab.run_cdp(
                "Page.captureScreenshot",
                format="jpeg",
                quality=state.options["screenshot_quality"],
                captureBeyondViewport=full_page,
                **capture,
            )["data"]
            previous = state.fingerprint
            self._capture_state(state)
            if state.fingerprint != previous:
                raise BrowserError("SCREEN_CHANGED", "Page changed during capture; observe again")
            screenshot_id = "screen_" + secrets.token_urlsafe(16)
            state.screenshot = {
                "id": screenshot_id,
                "revision": state.revision,
                "full_page": full_page,
                "digest": hashlib.sha256(base64.b64decode(image)).hexdigest(),
            }
            obs["screenshot"] = {
                "screenshot_id": screenshot_id,
                "width": width,
                "height": height,
                "coordinate_units": "CSS pixels",
                "full_page": full_page,
            }
            extra["_image"] = {"data": image, "mimeType": "image/jpeg"}
        notices = []
        if data["has_iframe"]:
            notices.append("Iframe contents are not exposed in this version; use manual control")
        return self._result(session_id, tab_id, observation=obs, notices=notices, **extra)

    def prepare(self, session_id, tab_id, expected_revision, action):
        state = self._tab(session_id, tab_id)
        data = self._capture_state(state)
        if data["protected"]:
            raise BrowserError(
                "AUTH_REQUIRED", "Use private authentication for this page", "user_action_required"
            )
        if state.revision != expected_revision:
            raise BrowserError("STALE_NODE", "Page changed; observe again", revision=state.revision)
        nid = action.get("node_id")
        meta = None
        if nid:
            target = state.nodes.get(nid)
            if target is None:
                raise BrowserError("NODE_NOT_FOUND", "Node was not issued for this observation")
            bid, meta = target
            element = self.Element(state.tab, backend_id=bid)
            if not element.run_js("return this.isConnected"):
                raise BrowserError("STALE_NODE", "Observed element was detached")
            if meta["type"] == "file":
                raise BrowserError("UNSUPPORTED_OPERATION", "File uploads require manual control")
            if meta["disabled"]:
                raise BrowserError("NODE_NOT_ACTIONABLE", "Element is disabled")
            if action["type"] == "fill" and not (
                meta["editable"]
                or meta["tag"] == "textarea"
                or (
                    meta["tag"] == "input"
                    and meta["type"] in ("text", "search", "email", "url", "tel", "number")
                )
            ):
                raise BrowserError("NODE_NOT_ACTIONABLE", "Target is not an editable text control")
            if action["type"] == "select" and meta["tag"] != "select":
                raise BrowserError("NODE_NOT_ACTIONABLE", "Target is not a select control")
            if action["type"] == "check" and meta["type"] not in ("checkbox", "radio"):
                raise BrowserError("NODE_NOT_ACTIONABLE", "Target is not a checkbox or radio")
        if "screenshot_id" in action:
            shot = state.screenshot
            if (
                not shot
                or shot["id"] != action["screenshot_id"]
                or shot["revision"] != state.revision
                or shot["full_page"]
            ):
                raise BrowserError("STALE_SCREENSHOT", "A current viewport screenshot is required")
            if (
                action["x"] >= data["viewport"]["width"]
                or action["y"] >= data["viewport"]["height"]
            ):
                raise BrowserError(
                    "INVALID_COORDINATES", "Coordinates must lie inside the observed viewport"
                )
            image = state.tab.run_cdp(
                "Page.captureScreenshot",
                format="jpeg",
                quality=state.options["screenshot_quality"],
                captureBeyondViewport=False,
            )["data"]
            if hashlib.sha256(base64.b64decode(image)).hexdigest() != shot["digest"]:
                state.screenshot = None
                raise BrowserError("STALE_SCREENSHOT", "Pixels changed since the screenshot")
            if action["type"] in ("click_at", "double_click_at"):
                for _, (_, candidate) in state.nodes.items():
                    rect = candidate["rect"]
                    if (
                        rect["x"] <= action["x"] < rect["x"] + rect["width"]
                        and rect["y"] <= action["y"] < rect["y"] + rect["height"]
                    ):
                        raise BrowserError(
                            "DOM_TARGET_AVAILABLE",
                            "Use the observed node_id for this interactive element instead of coordinates",
                        )
        return self._result(
            session_id,
            tab_id,
            target=redact(meta["name"]) if meta else "viewport",
            requires_confirmation=action["type"] not in ("scroll", "scroll_at", "move_to"),
        )

    def act(self, session_id, tab_id, expected_revision, action):
        self.prepare(session_id, tab_id, expected_revision, action)
        state = self._tab(session_id, tab_id)
        before_url, before_fp = state.tab.url, state.fingerprint
        old_tabs = set(self._session(session_id)["tabs"])
        typ = action["type"]
        target = state.nodes.get(action.get("node_id"))
        element = self.Element(state.tab, backend_id=target[0]) if target else None
        performed = True

        def native_click(click_count=1):
            rect = element.run_js(
                "const r=this.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}"
            )
            x, y = rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2
            if not element.run_js(
                "const hit=document.elementFromPoint(arguments[0],arguments[1]);return this.isConnected && (hit===this || this.contains(hit))",
                x,
                y,
            ):
                raise BrowserError(
                    "NODE_NOT_ACTIONABLE", "Observed element is covered or outside the viewport"
                )
            for count in range(1, click_count + 1):
                state.tab.run_cdp(
                    "Input.dispatchMouseEvent",
                    type="mousePressed",
                    x=x,
                    y=y,
                    button="left",
                    clickCount=count,
                )
                state.tab.run_cdp(
                    "Input.dispatchMouseEvent",
                    type="mouseReleased",
                    x=x,
                    y=y,
                    button="left",
                    clickCount=count,
                )

        def focus_exact():
            state.tab.run_cdp("DOM.focus", backendNodeId=target[0])
            if not element.run_js(
                "return document.activeElement===this || this.contains(document.activeElement)"
            ):
                raise BrowserError("NODE_NOT_ACTIONABLE", "Observed element cannot receive focus")

        def key_event(key, code, virtual, text=None, modifiers=0):
            parameters = {
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": virtual,
                "modifiers": modifiers,
            }
            state.tab.run_cdp(
                "Input.dispatchKeyEvent",
                type="keyDown",
                **parameters,
                **({"text": text} if text else {}),
            )
            state.tab.run_cdp("Input.dispatchKeyEvent", type="keyUp", **parameters)

        try:
            if typ in ("click", "double_click"):
                native_click(2 if typ == "double_click" else 1)
            elif typ == "fill":
                focus_exact()
                key_event("a", "KeyA", 65, modifiers=2)
                if action["text"]:
                    state.tab.run_cdp("Input.insertText", text=action["text"])
                else:
                    key_event("Backspace", "Backspace", 8)
            elif typ == "select":
                if not element.select.by_value(action["value"], timeout=1):
                    raise BrowserError("NODE_NOT_ACTIONABLE", "Option not found")
            elif typ == "check":
                if target[1]["checked"] != action["checked"]:
                    native_click()
                else:
                    performed = False
            elif typ == "keypress":
                focus_exact()
                key, virtual, text = {
                    "ENTER": ("Enter", 13, "\r"),
                    "TAB": ("Tab", 9, None),
                    "ESCAPE": ("Escape", 27, None),
                    "SPACE": (" ", 32, " "),
                    "ARROWUP": ("ArrowUp", 38, None),
                    "ARROWDOWN": ("ArrowDown", 40, None),
                    "ARROWLEFT": ("ArrowLeft", 37, None),
                    "ARROWRIGHT": ("ArrowRight", 39, None),
                    "BACKSPACE": ("Backspace", 8, None),
                    "DELETE": ("Delete", 46, None),
                    "HOME": ("Home", 36, None),
                    "END": ("End", 35, None),
                }[action["keys"][0]]
                key_event(key, "Space" if key == " " else key, virtual, text)
            elif typ == "scroll" and element:
                element.run_js(
                    "this.scrollBy(arguments[0],arguments[1])", action["delta_x"], action["delta_y"]
                )
            elif typ == "scroll":
                state.tab.run_js(
                    "window.scrollBy(arguments[0],arguments[1])",
                    action["delta_x"],
                    action["delta_y"],
                )
            elif typ in ("click_at", "double_click_at", "move_to", "scroll_at"):
                x, y = action["x"], action["y"]
                if typ == "move_to":
                    state.tab.run_cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
                elif typ == "scroll_at":
                    state.tab.run_cdp(
                        "Input.dispatchMouseEvent",
                        type="mouseWheel",
                        x=x,
                        y=y,
                        deltaX=action["delta_x"],
                        deltaY=action["delta_y"],
                    )
                else:
                    for count in range(1, 3 if typ == "double_click_at" else 2):
                        state.tab.run_cdp(
                            "Input.dispatchMouseEvent",
                            type="mousePressed",
                            x=x,
                            y=y,
                            button="left",
                            clickCount=count,
                        )
                        state.tab.run_cdp(
                            "Input.dispatchMouseEvent",
                            type="mouseReleased",
                            x=x,
                            y=y,
                            button="left",
                            clickCount=count,
                        )
            else:
                raise BrowserError("UNSUPPORTED_OPERATION", "Action is not implemented")
            time.sleep(state.options["wait_ms"] / 1000)
            self._sync(session_id)
            self._capture_state(state)
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(
                "RESULT_UNCERTAIN", "Action may have been dispatched; do not repeat automatically"
            ) from exc
        changed = before_fp != state.fingerprint
        added = list(set(self._session(session_id)["tabs"]) - old_tabs)
        return self._result(
            session_id,
            tab_id,
            status="ok" if changed or added else "no_change",
            action_result={
                "performed": performed,
                "page_changed": changed,
                "navigation_occurred": before_url != state.tab.url,
                "new_tab_ids": added,
            },
        )

    def configure(self, session_id, tab_id, options):
        state = self._tab(session_id, tab_id)
        state.options.update(options)
        self._viewport(state)
        state.fingerprint = ""
        self._capture_state(state)
        return self._result(session_id, tab_id, configuration=state.options)

    def focus(self, session_id, tab_id):
        state = self._tab(session_id, tab_id)
        self._session(session_id)["browser"].activate_tab(state.tab.tab_id)
        self._session(session_id)["selected"] = tab_id
        for current in self._session(session_id)["tabs"].values():
            current.tab._driver.set_callback("Network.requestWillBeSent", None)
            current.tab.run_cdp("Network.disable")
            current.document_method = None
        return {"session_id": session_id, "tab_id": tab_id}

    def resume(self, session_id, tab_id):
        for state in self._session(session_id)["tabs"].values():
            self._watch_document(state)
            state.fingerprint = ""
            state.screenshot = None
            state.cursors.clear()
        state = self._tab(session_id, tab_id)
        self._capture_state(state)
        return self._result(session_id, tab_id)

    def close(self, session_id, scope, tab_id=None):
        session = self._session(session_id)
        if scope == "session":
            session["browser"].quit()
            del self.sessions[session_id]
            return {"session_id": session_id}
        self._tab(session_id, tab_id).tab.close()
        self._sync(session_id)
        return {"session_id": session_id, "tab_id": tab_id, "selected_tab_id": session["selected"]}

    def shutdown(self):
        for session in self.sessions.values():
            try:
                session["browser"].quit()
            except Exception:
                pass
        self.sessions.clear()
