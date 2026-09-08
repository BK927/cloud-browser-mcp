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

from .approval import decide
from .config import Settings
from .image_privacy import mask_frames
from .models import BrowserError
from .observation import compact_node, paginate
from .page_tools import PageTools, scrub_result, validate_arguments
from .security import TOKEN, origin, redact, redact_tree, safe_url, validate_url
from .uploads import verify_file

SNAPSHOT = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")


@dataclass
class TabState:
    tab: object
    revision: int = 0
    fingerprint: str = ""
    dom_fingerprint: str = ""
    nodes: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)
    screenshot: dict | None = None
    cursors: dict = field(default_factory=dict)
    document_method: str | None = None
    document_loader: str | None = None
    history_methods: dict = field(default_factory=dict)
    page_tools: object | None = None
    advertised_tools: dict | None = None
    document_key: str = ""
    revision_documents: dict = field(default_factory=dict)
    ax_cache: dict = field(default_factory=dict)
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
                # DrissionPage enables synthetic focus on every tab by default.
                # Preserve real selection/visibility for the shared human browser.
                tab.run_cdp("Emulation.setFocusEmulationEnabled", enabled=False)
                self._viewport(state)

                if not session.get("paused"):
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
                state.document_loader = kwargs.get("loaderId")

        state.tab._driver.set_callback("Network.requestWillBeSent", request_seen)
        state.tab.run_cdp("Network.enable")

    def _capture_state(self, state, *, mode="auto", lightweight=False):
        tab = state.tab
        tab.run_cdp("Runtime.releaseObjectGroup", objectGroup="cb-observation")
        document = tab.run_cdp("Page.getFrameTree")["frameTree"]["frame"]
        frame = document["id"]
        document_key = frame + ":" + document.get("loaderId", "")
        if document_key != state.document_key:
            state.nodes.clear()
            state.screenshot = None
            state.document_key = document_key
        history = tab.run_cdp("Page.getNavigationHistory")
        if history["entries"]:
            entry = history["entries"][history["currentIndex"]]["id"]
            if document.get("loaderId") == state.document_loader and state.document_method:
                state.history_methods[entry] = state.document_method
            else:
                state.document_method = state.history_methods.get(entry)
            live_entries = {item["id"] for item in history["entries"]}
            state.history_methods = {
                key: method for key, method in state.history_methods.items() if key in live_entries
            }
        world = tab.run_cdp(
            "Page.createIsolatedWorld", frameId=frame, worldName="cloud-browser-observer"
        )["executionContextId"]
        result = tab.run_cdp(
            "Runtime.evaluate",
            expression="globalThis.__cbOptions="
            + json.dumps({"mode": mode, "lightweight": lightweight})
            + ";"
            + SNAPSHOT,
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
        data["form_digest"] = hashlib.sha256(
            json.dumps(data.pop("_form_states", []), sort_keys=True).encode()
        ).hexdigest()
        form_digests = [
            hashlib.sha256(json.dumps(form, sort_keys=True).encode()).hexdigest()
            for form in data.pop("_forms", [])
        ]
        for meta in data["nodes"]:
            index = meta.pop("_form_index", None)
            meta["_form_digest"] = form_digests[index] if index is not None else None
            meta["_value_digest"] = hashlib.sha256(
                json.dumps(meta.pop("_own_value", None)).encode()
            ).hexdigest()
        if state.page_tools and state.page_tools.enabled:
            data["page_tools_generation"] = state.page_tools.snapshot(frame)[0]
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
        # Query only the observed elements, not unrelated frame trees or AX values.
        # Reuse computed names on unchanged DOM to bound repeated observation cost.
        dom_fingerprint = hashlib.sha256(
            json.dumps([data, backends], sort_keys=True).encode()
        ).hexdigest()
        if dom_fingerprint == state.dom_fingerprint:
            data["nodes"] = state.data["nodes"]
            data["accessibility_source"] = state.data["accessibility_source"]
        else:
            data["accessibility_source"] = "chromium-ax"
            if data["protected"]:
                data["accessibility_source"] = "withheld"
            else:
                # Reuse names by actual backend and DOM signature. Never request an
                # unbounded full AX tree on a large page just to read 60 controls.
                ax_budget = 0 if lightweight else 60
                new_cache = {}
                for bid, meta in zip(backends, data["nodes"], strict=True):
                    try:
                        cache_key = (bid, self._node_signature(meta))
                        current = state.ax_cache.get(cache_key)
                        if current is None and ax_budget:
                            ax_budget -= 1
                            ax = tab.run_cdp(
                                "Accessibility.getPartialAXTree",
                                backendNodeId=bid,
                                fetchRelatives=False,
                            )
                            current = next(
                                (
                                    n
                                    for n in ax["nodes"]
                                    if n.get("backendDOMNodeId") == bid and not n.get("ignored")
                                ),
                                {},
                            )
                        new_cache[cache_key] = current or {}
                        if current:
                            meta["_dom_name"] = meta["name"]
                            meta["name"] = " ".join(
                                str(current.get("name", {}).get("value", meta["name"])).split()
                            )[:500]
                            meta["role"] = current.get("role", {}).get("value", meta["role"])
                            for prop in current.get("properties", []):
                                key, value = prop["name"], prop["value"].get("value")
                                if key in ("expanded", "pressed", "selected"):
                                    meta[key] = (
                                        str(value).lower() if isinstance(value, bool) else value
                                    )
                    except Exception:
                        data["accessibility_source"] = "dom-fallback"
                state.ax_cache = new_cache
            state.dom_fingerprint = dom_fingerprint
        # Actual backend IDs distinguish replacements with identical visible text.
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    {
                        k: data.get(k)
                        for k in (
                            "url",
                            "title",
                            "form_digest",
                            "protected",
                            "mutations",
                            "viewport",
                            "scroll",
                            "height",
                            "page_tools_generation",
                        )
                    },
                    backends,
                    [
                        {
                            k: meta.get(k)
                            for k in (
                                "focused",
                                "rect",
                                "scroll",
                                "checked",
                                "expanded",
                                "pressed",
                                "selected",
                                "value",
                            )
                        }
                        for meta in data["nodes"]
                    ],
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if fingerprint != state.fingerprint:
            state.revision += 1
            state.fingerprint = fingerprint
            state.cursors.clear()
        existing = {
            (bid, self._node_signature(meta)): nid for nid, (bid, meta) in state.nodes.items()
        }
        state.nodes = {
            existing.get((bid, self._node_signature(meta)), "node_" + secrets.token_urlsafe(10)): (
                bid,
                meta,
            )
            for bid, meta in zip(backends, data["nodes"], strict=True)
        }
        state.revision_documents[state.revision] = document_key
        while len(state.revision_documents) > 256:
            del state.revision_documents[next(iter(state.revision_documents))]
        state.data = data
        return data

    @staticmethod
    def _node_signature(meta):
        # Layout and focus changes do not turn the same element into another one.
        # Native state, accessible meaning and the owning form's digest do.
        value = {
            k: v for k, v in meta.items() if k not in ("rect", "focused", "scroll", "_dom_name")
        }
        if "_dom_name" in meta:
            value["name"] = meta["_dom_name"]
        return json.dumps(value, sort_keys=True)

    @staticmethod
    def _guard_page(data):
        if data["protected"]:
            raise BrowserError(
                "AUTH_REQUIRED",
                "Protected input screen: use private authentication",
                "user_action_required",
            )
        if data.get("challenge"):
            captcha = data["challenge"] == "captcha"
            raise BrowserError(
                "CAPTCHA_REQUIRED" if captcha else "BOT_BLOCKED",
                "Page shows explicit human-verification or automation-blocking text; no bypass attempted",
                "user_action_required" if captcha else "blocked",
            )

    def _result(self, sid, tid, **extra):
        state = self._tab(sid, tid)
        return dict(
            session_id=sid,
            tab_id=tid,
            revision=state.revision,
            page={"url": safe_url(state.tab.url), "title": redact(state.tab.title or "") or None},
            **extra,
        )

    def _validate_url(self, url):
        validate_url(url, dns_proxy=self.cfg.browser_proxy if self.cfg.network_isolated else None)

    def open(self, session_id, url=None, new_tab=True):
        if url:
            self._validate_url(url)
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
            if self.cfg.webmcp_testing:
                options.set_argument("--enable-features=WebMCP")
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
        if not new_tab:
            self.list_tabs(sid)  # Reuse the actual selected tab after human selection.
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
        if not session.get("paused"):
            focused, visible = [], []
            for tid, state in session["tabs"].items():
                try:
                    flags = state.tab.run_cdp(
                        "Runtime.evaluate",
                        expression="({focused:document.hasFocus(),visible:document.visibilityState==='visible'})",
                        returnByValue=True,
                        _timeout=1,
                    )["result"]["value"]
                    if flags["focused"]:
                        focused.append(tid)
                    if flags["visible"]:
                        visible.append(tid)
                except Exception:
                    continue  # A closing/loading tab must not select another tab as a side effect.
            selected = focused if len(focused) == 1 else visible
            if len(selected) == 1:
                session["selected"] = selected[0]
                state = session["tabs"].pop(selected[0])
                session["tabs"][selected[0]] = state
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
        expected_entry = None
        expected_loader = None
        previous_loader = None
        if operation == "goto":
            if not url:
                raise BrowserError("INVALID_URL", "goto requires a URL")
            self._validate_url(url)
        elif operation in ("back", "forward"):
            history = state.tab.run_cdp("Page.getNavigationHistory")
            index = history["currentIndex"] + (-1 if operation == "back" else 1)
            if index < 0 or index >= len(history["entries"]):
                return self._result(
                    session_id,
                    tab_id,
                    status="no_change",
                    navigation={
                        "operation": operation,
                        "redirected": False,
                        "navigation_occurred": False,
                    },
                )
            self._validate_url(history["entries"][index]["url"])
            expected_entry = history["entries"][index]["id"]
            if state.history_methods.get(expected_entry) != "GET":
                raise BrowserError(
                    "UNSUPPORTED_OPERATION",
                    "Unknown/POST history entry requires browser_handoff; no navigation performed",
                    "user_action_required",
                )
        elif operation == "reload":
            if state.document_method != "GET":
                raise BrowserError(
                    "UNSUPPORTED_OPERATION",
                    "Unknown/POST document reload requires browser_handoff; no automatic reload performed",
                    "user_action_required",
                )
            previous_loader = state.tab.run_cdp("Page.getFrameTree")["frameTree"]["frame"][
                "loaderId"
            ]
        else:
            raise BrowserError("UNSUPPORTED_OPERATION", "Unknown navigation operation")

        # Invalidate observations before dispatch, including navigation that times out.
        self._stop_page_tools(state)
        state.fingerprint = ""
        state.nodes.clear()
        state.screenshot = None
        state.cursors.clear()
        deadline = time.monotonic() + self.cfg.navigation_timeout
        try:
            if operation == "goto":
                result = state.tab.run_cdp(
                    "Page.navigate", url=url, _timeout=self.cfg.navigation_timeout
                )
                if result.get("errorText") or result.get("isDownload"):
                    raise BrowserError(
                        "NAVIGATION_FAILED",
                        "Document navigation failed or started an unsupported download",
                    )
                expected_loader = result.get("loaderId")
            elif expected_entry is not None:
                state.tab.run_cdp(
                    "Page.navigateToHistoryEntry",
                    entryId=expected_entry,
                    _timeout=self.cfg.navigation_timeout,
                )
            else:
                state.tab.run_cdp(
                    "Page.reload", ignoreCache=False, _timeout=self.cfg.navigation_timeout
                )
            self._wait_navigation(state, deadline, expected_loader, previous_loader, expected_entry)
        except BrowserError:
            raise
        except Exception as exc:
            code = "NAVIGATION_TIMEOUT" if time.monotonic() >= deadline else "NAVIGATION_FAILED"
            raise BrowserError(
                code, "Navigation could not be completed; observe before deciding"
            ) from exc
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

    @staticmethod
    def _wait_navigation(
        state, deadline, expected_loader=None, previous_loader=None, expected_entry=None
    ):
        """Wait for the requested document/history entry, never the still-visible old one."""
        while time.monotonic() < deadline:
            try:
                timeout = max(0.05, deadline - time.monotonic())
                frame = state.tab.run_cdp("Page.getFrameTree", _timeout=timeout)["frameTree"][
                    "frame"
                ]
                if frame.get("unreachableUrl") or frame["url"].startswith("chrome-error:"):
                    raise BrowserError(
                        "NAVIGATION_FAILED", "Chromium could not load the requested document"
                    )
                matches = (not expected_loader or frame["loaderId"] == expected_loader) and (
                    not previous_loader or frame["loaderId"] != previous_loader
                )
                if expected_entry is not None:
                    history = state.tab.run_cdp("Page.getNavigationHistory", _timeout=timeout)
                    matches = (
                        matches
                        and history["entries"][history["currentIndex"]]["id"] == expected_entry
                    )
                ready = state.tab.run_cdp(
                    "Runtime.evaluate",
                    expression="document.readyState",
                    returnByValue=True,
                    _timeout=timeout,
                )
                if matches and ready.get("result", {}).get("value") == "complete":
                    return
            except BrowserError:
                raise
            except Exception:
                # Execution contexts can disappear while the new document commits.
                pass
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        raise BrowserError(
            "NAVIGATION_TIMEOUT",
            "Navigation did not finish; observe the current page before deciding",
        )

    def observe(
        self,
        session_id,
        tab_id,
        mode="auto",
        full_page=False,
        max_chars=None,
        cursor=None,
        lightweight=False,
    ):
        state = self._tab(session_id, tab_id)
        data = self._capture_state(state, mode=mode, lightweight=lightweight)
        self._guard_page(data)
        if cursor:
            saved = state.cursors.get(cursor)
            if not saved or saved["revision"] != state.revision:
                raise BrowserError("CURSOR_STALE", "Observation cursor is no longer valid")
            snapshot, offsets, budget = saved["snapshot"], saved["offsets"], saved["budget"]
            mode = saved["mode"]
        else:
            budget = max_chars or state.options["max_chars"]
            interactive = []
            if mode in ("auto", "interactive"):
                for nid, (_, meta) in state.nodes.items():
                    clean = {
                        k: redact_tree(v)
                        for k, v in meta.items()
                        if not k.startswith("_")
                        and k
                        not in (
                            "href",
                            "form_action",
                            "form_method",
                            "submits_form",
                            "form_fields",
                            "form_fields_truncated",
                        )
                    }
                    if meta["href"]:
                        clean["href"] = safe_url(meta["href"])
                    interactive.append(compact_node({"node_id": nid, **clean}))
            snapshot = {
                "semantic": redact(data["semantic_text"]) if mode in ("auto", "semantic") else "",
                "nodes": interactive,
            }
            offsets = (0, 0)
        obs, next_offsets = paginate(snapshot, offsets, budget)
        obs.update(
            screenshot=None,
            viewport=data["viewport"],
            interactive_truncated=data["interactive_truncated"],
            semantic_source=data["semantic_source"],
            semantic_source_truncated=data["semantic_source_truncated"],
            accessibility_source=data["accessibility_source"],
            scroll_scan_truncated=data["scroll_scan_truncated"],
            readable_frames=data["readable_frames"],
            frame_reading_truncated=data["frame_reading_truncated"],
        )
        if obs["truncated"]:
            next_cursor = "cursor_" + secrets.token_urlsafe(16)
            state.cursors[next_cursor] = {
                "revision": state.revision,
                "snapshot": snapshot,
                "offsets": next_offsets,
                "budget": budget,
                "mode": mode,
            }
            # Bound server memory even if a client retains many cursors.
            if len(state.cursors) > 64:
                state.cursors.pop(next(iter(state.cursors)))
            obs["next_cursor"] = next_cursor
        extra = {}
        if not cursor and (mode == "visual" or (mode == "auto" and data["has_canvas"])):
            if TOKEN.search(data["text"]) or (
                data["has_iframe"] and (self.cfg.iframe_screenshot_policy == "block" or full_page)
            ):
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
            raw_digest = hashlib.sha256(base64.b64decode(image)).hexdigest()
            masked_regions = []
            mime_type = "image/jpeg"
            if data["has_iframe"]:
                image, masked_regions = mask_frames(image, data["iframe_regions"], data["viewport"])
                mime_type = "image/png"
            screenshot_id = "screen_" + secrets.token_urlsafe(16)
            state.screenshot = {
                "id": screenshot_id,
                "revision": state.revision,
                "full_page": full_page,
                "digest": raw_digest,
                "masked_regions": masked_regions,
            }
            obs["screenshot"] = {
                "screenshot_id": screenshot_id,
                "width": width,
                "height": height,
                "coordinate_units": "CSS pixels",
                "full_page": full_page,
                "masked_regions": masked_regions,
            }
            extra["_image"] = {"data": image, "mimeType": mime_type}
        notices = []
        if data["accessibility_source"] == "dom-fallback":
            notices.append(
                "Chromium accessibility information unavailable; rendered DOM fallback used"
            )
        if data["has_iframe"]:
            notices.append(
                "Only accessible same-origin iframe text is included; iframe actions require manual control"
            )
        return self._result(session_id, tab_id, observation=obs, notices=notices, **extra)

    def prepare(self, session_id, tab_id, expected_revision, action):
        state = self._tab(session_id, tab_id)
        data = self._capture_state(state)
        self._guard_page(data)
        if not data["form_state_complete"]:
            raise BrowserError(
                "UNSUPPORTED_OPERATION",
                "Form state exceeds safe observation budget; use manual control",
                "user_action_required",
            )
        if state.revision_documents.get(expected_revision) != state.document_key:
            raise BrowserError(
                "STALE_REVISION",
                "The observed document is no longer current; observe again",
                revision=state.revision,
            )
        if action["type"] == "page_tool":
            advertised = state.advertised_tools
            if not advertised or advertised["revision"] != state.revision or not state.page_tools:
                raise BrowserError("PAGE_TOOL_STALE", "List page tools again before calling one")
            generation, tools = state.page_tools.snapshot(advertised["frame_id"])
            if generation != advertised["generation"]:
                raise BrowserError("PAGE_TOOL_STALE", "The page changed its tool registration")
            tool = next((t for t in tools if t["name"] == action["tool_name"]), None)
            if not tool:
                raise BrowserError("PAGE_TOOL_NOT_FOUND", "This page has not advertised that tool")
            validate_arguments(tool["inputSchema"], action["arguments"])
            return self._result(
                session_id,
                tab_id,
                target=redact(tool["name"]),
                destination=origin(state.tab.url),
                destination_kind="page_tool",
                data_sent=list(action["arguments"]),
                requires_confirmation=True,
                action_policy=decide(self.cfg.approval_policy, action),
            )
        nid = action.get("node_id")
        meta = None
        if nid:
            target = state.nodes.get(nid)
            if target is None:
                raise BrowserError(
                    "STALE_NODE", "Observed target changed or was replaced; observe again"
                )
            bid, meta = target
            element = self.Element(state.tab, backend_id=bid)
            if not element.run_js("return this.isConnected"):
                raise BrowserError("STALE_NODE", "Observed element was detached")
            if meta["type"] == "file" and action["type"] != "upload":
                raise BrowserError("UNSUPPORTED_OPERATION", "File uploads require manual control")
            if action["type"] == "upload":
                if meta["type"] != "file" or not action.get("_uploads"):
                    raise BrowserError(
                        "NODE_NOT_ACTIONABLE",
                        "Upload requires a file input and privately staged files",
                    )
                if len(action["_uploads"]) > 1 and not meta.get("multiple"):
                    raise BrowserError(
                        "NODE_NOT_ACTIONABLE", "This input does not accept multiple files"
                    )
                for item in action["_uploads"]:
                    verify_file(item)
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
            if action["type"] == "fill" and meta.get("readonly"):
                raise BrowserError("NODE_NOT_ACTIONABLE", "Target is read-only")
            if action["type"] == "select":
                if meta.get("multiple"):
                    raise BrowserError(
                        "UNSUPPORTED_OPERATION", "Multi-select controls require manual control"
                    )
                choices = [o for o in meta["options"] if o["value"] == action["value"]]
                if not choices or any(o["disabled"] for o in choices):
                    raise BrowserError(
                        "NODE_NOT_ACTIONABLE", "Option is unavailable or was not observed"
                    )
                if len(choices) != 1:
                    raise BrowserError(
                        "NODE_AMBIGUOUS", "Option value matches more than one choice"
                    )
            if action["type"] == "check" and meta["type"] not in ("checkbox", "radio"):
                raise BrowserError("NODE_NOT_ACTIONABLE", "Target is not a checkbox or radio")
            if action["type"] == "check" and meta["type"] == "radio" and not action["checked"]:
                raise BrowserError(
                    "NODE_NOT_ACTIONABLE",
                    "A radio cannot be unchecked directly; select another option",
                )
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
            for region in shot.get("masked_regions", []):
                if (
                    region["x"] <= action["x"] < region["x"] + region["width"]
                    and region["y"] <= action["y"] < region["y"] + region["height"]
                ):
                    raise BrowserError(
                        "SENSITIVE_SCREEN",
                        "Cannot act on masked embedded content; use handoff",
                        "blocked",
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
        destination = None
        destination_kind = "unknown"
        if meta and action["type"] in ("click", "double_click") and meta.get("href"):
            destination = safe_url(meta["href"])
            destination_kind = "declared_link"
        elif (
            meta
            and meta.get("form_action")
            and (
                (meta.get("submits_form") and action["type"] in ("click", "double_click"))
                or (action["type"] == "keypress" and action.get("keys") == ["ENTER"])
            )
        ):
            destination = safe_url(meta["form_action"])
            destination_kind = "declared_form"
        policy = decide(self.cfg.approval_policy, action, meta, state.tab.url)
        return self._result(
            session_id,
            tab_id,
            target=redact(meta["name"]) if meta else "viewport",
            destination=destination,
            destination_kind=destination_kind,
            data_sent=redact_tree(meta.get("form_fields", []))
            if meta and destination_kind == "declared_form"
            else None,
            data_sent_truncated=bool(meta and meta.get("form_fields_truncated")),
            files=[
                {k: v for k, v in item.items() if k != "path"}
                for item in action.get("_uploads", [])
            ],
            requires_confirmation=policy["approval_required"],
            action_policy=policy,
            target_binding=hashlib.sha256(
                json.dumps(
                    [
                        state.document_key,
                        state.tab.url,
                        nid,
                        self._node_signature(meta) if meta else data["viewport"],
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
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
        dispatched = False
        tool_result = None

        def native_click(click_count=1):
            nonlocal dispatched
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
                dispatched = True
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
            nonlocal dispatched
            dispatched = True
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
            if typ == "page_tool":
                dispatched = True
                tool_result = state.page_tools.invoke(
                    state.advertised_tools["frame_id"], action["tool_name"], action["arguments"]
                )
            elif typ in ("click", "double_click"):
                native_click(2 if typ == "double_click" else 1)
            elif typ == "upload":
                dispatched = True
                state.tab.run_cdp(
                    "DOM.setFileInputFiles",
                    backendNodeId=target[0],
                    files=[item["path"] for item in action["_uploads"]],
                )
            elif typ == "fill":
                focus_exact()
                key_event("a", "KeyA", 65, modifiers=2)
                if action["text"]:
                    state.tab.run_cdp("Input.insertText", text=action["text"])
                else:
                    key_event("Backspace", "Backspace", 8)
            elif typ == "select":
                dispatched = True
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
                dispatched = True
                element.run_js(
                    "this.scrollBy(arguments[0],arguments[1])", action["delta_x"], action["delta_y"]
                )
            elif typ == "scroll":
                dispatched = True
                state.tab.run_js(
                    "window.scrollBy(arguments[0],arguments[1])",
                    action["delta_x"],
                    action["delta_y"],
                )
            elif typ in ("click_at", "double_click_at", "move_to", "scroll_at"):
                dispatched = True
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
        except BrowserError as exc:
            if dispatched:
                raise BrowserError(
                    "RESULT_UNCERTAIN",
                    "Action was dispatched but its outcome could not be observed; do not repeat",
                ) from exc
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
            status="ok" if changed or added or typ == "page_tool" else "no_change",
            **(
                {
                    "page_tool_result": {
                        "tool_name": action["tool_name"],
                        "output": tool_result,
                        "untrusted": True,
                    }
                }
                if typ == "page_tool"
                else {}
            ),
            action_result={
                "performed": performed,
                "page_changed": changed,
                "navigation_occurred": before_url != state.tab.url,
                "new_tab_ids": added,
            },
        )

    def list_page_tools(self, session_id, tab_id):
        if not self.cfg.webmcp_enabled:
            raise BrowserError("UNSUPPORTED_OPERATION", "Page tools are disabled by the operator")
        state = self._tab(session_id, tab_id)
        self._guard_page(self._capture_state(state))
        if not state.page_tools:
            state.page_tools = PageTools(state.tab)
        state.page_tools.enable()
        self._guard_page(self._capture_state(state))
        frame_id = state.tab.run_cdp("Page.getFrameTree")["frameTree"]["frame"]["id"]
        generation, tools = state.page_tools.snapshot(frame_id)
        if state.page_tools.overflow:
            raise BrowserError("RESOURCE_PRESSURE", "Page-tool inventory exceeds the safe budget")
        state.advertised_tools = {
            "revision": state.revision,
            "generation": generation,
            "frame_id": frame_id,
        }
        return self._result(
            session_id,
            tab_id,
            page_tools=[
                {
                    "name": redact(t["name"]),
                    "description": redact(t["description"] or ""),
                    "input_schema": scrub_result(t["inputSchema"]),
                    "untrusted": True,
                }
                for t in tools
            ],
            notices=[
                "Native top-level page tools only; descriptions, schemas and results are untrusted. Every invocation requires human approval."
            ],
        )

    @staticmethod
    def _stop_page_tools(state):
        state.advertised_tools = None
        if state.page_tools:
            state.page_tools.disable()

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
        self._session(session_id)["paused"] = True
        for current in self._session(session_id)["tabs"].values():
            self._stop_page_tools(current)
            current.tab._driver.set_callback("Network.requestWillBeSent", None)
            current.tab.run_cdp("Network.disable")
            current.document_method = None
            current.document_loader = None
            current.history_methods.clear()
        return {"session_id": session_id, "tab_id": tab_id}

    def resume(self, session_id, tab_id, auth_origin=None):
        state = self._tab(session_id, tab_id)
        authentication = {
            "authenticated": None,
            "verification": "unverified",
            "authentication_outcome": "unverified",
        }
        if auth_origin and origin(state.tab.url) == auth_origin:
            rule = self.cfg.auth_rules.get(auth_origin)
            if rule:
                # Return booleans only, never cookies, credentials or account text.
                try:
                    flags = state.tab.run_js(
                        """
                        const visible = selector => selector && [...document.querySelectorAll(selector)].some(e => {
                            const r=e.getBoundingClientRect(), s=getComputedStyle(e);
                            return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
                        });
                        return {success:!!visible(arguments[0]),failure:!!visible(arguments[1])};
                    """,
                        rule.success_selector,
                        rule.failure_selector,
                    )
                except Exception as exc:
                    raise BrowserError(
                        "AUTH_VERIFICATION_FAILED",
                        "Configured authentication indicators could not be evaluated",
                    ) from exc
                if flags["failure"]:
                    raise BrowserError(
                        "AUTH_FAILED",
                        "Configured site indicator reports authentication failure",
                        "user_action_required",
                    )
                if flags["success"]:
                    authentication = {
                        "authenticated": True,
                        "verification": "operator_rule",
                        "authentication_outcome": "verified",
                    }
        # Do not reactivate collection while a sensitive/challenge screen remains.
        self._guard_page(self._capture_state(state))
        for state in self._session(session_id)["tabs"].values():
            self._watch_document(state)
            state.fingerprint = ""
            state.screenshot = None
            state.cursors.clear()
        state = self._tab(session_id, tab_id)
        self._capture_state(state)
        self._session(session_id)["paused"] = False
        return self._result(
            session_id, tab_id, **({"authentication": authentication} if auth_origin else {})
        )

    def close(self, session_id, scope, tab_id=None):
        session = self._session(session_id)
        if scope == "session":
            for state in session["tabs"].values():
                self._stop_page_tools(state)
            session["browser"].quit()
            del self.sessions[session_id]
            return {"session_id": session_id}
        state = self._tab(session_id, tab_id)
        self._stop_page_tools(state)
        if len(session["tabs"]) == 1:
            session["browser"].quit()
            del self.sessions[session_id]
            return {
                "session_id": session_id,
                "tab_id": tab_id,
                "selected_tab_id": None,
                "session_closed": True,
                "termination_reason": "last_tab_closed",
            }
        state.tab.close()
        self._sync(session_id)
        return {"session_id": session_id, "tab_id": tab_id, "selected_tab_id": session["selected"]}

    def shutdown(self):
        for session in self.sessions.values():
            try:
                session["browser"].quit()
            except Exception:
                pass
        self.sessions.clear()
