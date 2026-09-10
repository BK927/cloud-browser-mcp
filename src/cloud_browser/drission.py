"""The only module that imports DrissionPage (separately licensed).

Element handles are obtained once while observing; actions never rerun selectors.
All callers must serialize access through the worker.
"""

import base64
import hashlib
import html
import json
import mimetypes
import os
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

from .approval import PASSIVE_ACTIONS, decide
from .artifacts import Artifacts
from .config import Settings
from .events import Events
from .image_privacy import mask_frames
from .input_driver import NativeInput
from .models import BrowserError
from .navigation import outcome as navigation_outcome
from .observation import compact_node, paginate
from .page_tools import PageTools, schema_fingerprint, scrub_result, validate_arguments
from .runtime import DisplayRuntime
from .security import SENSITIVE, TOKEN, origin, redact, redact_tree, safe_url, validate_url
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
    same_document_sequence: int = 0
    same_document_kind: str | None = None
    revision_documents: dict = field(default_factory=dict)
    ax_cache: dict = field(default_factory=dict)
    frame_id: str | None = None
    parent_frame_id: str | None = None
    frame_states: dict = field(default_factory=dict)
    frame_nodes: dict = field(default_factory=dict)
    frames_fingerprint: str = ""
    frame_backend_ids: list = field(default_factory=list)
    query: dict = field(default_factory=dict)
    events: object = field(default_factory=Events)
    pending_input: object | None = None
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
        from DrissionPage._pages.chromium_frame import ChromiumFrame

        self.Chromium, self.Options, self.Element = Chromium, ChromiumOptions, ChromiumElement
        self.Frame = ChromiumFrame
        self.cfg = settings
        self.sessions = {}
        self.runtimes = {}

    def _runtime(self, sid):
        return self.runtimes[sid]

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

        def within_document(frameId=None, navigationType=None, **kwargs):
            if frameId == getattr(state.tab, "_frame_id", None):
                state.same_document_sequence += 1
                state.same_document_kind = {"fragment": "hash", "historyApi": "history_api"}.get(
                    navigationType, "other"
                )

        state.tab._driver.set_callback("Page.navigatedWithinDocument", within_document)
        state.tab.run_cdp("Network.enable")
        self._watch_events(state)

    @staticmethod
    def _watch_events(state):
        events, tab = state.events, state.tab
        events.enabled = True

        def opened(**kwargs):
            tab._on_alert_open(**kwargs)
            events.opened(**kwargs)

        def closed(**kwargs):
            tab._on_alert_close(**kwargs)
            events.closed(**kwargs)

        tab._driver.set_callback("Page.javascriptDialogOpening", opened, immediate=True)
        tab._driver.set_callback("Page.javascriptDialogClosed", closed)
        tab._driver.set_callback("Runtime.consoleAPICalled", events.console)
        tab._driver.set_callback("Runtime.exceptionThrown", events.exception)
        tab._driver.set_callback("Page.fileChooserOpened", events.file_chooser)
        tab.run_cdp("Page.setInterceptFileChooserDialog", enabled=True)

    @staticmethod
    def _pause_events(state):
        state.events.pause()
        for event in (
            "Runtime.consoleAPICalled",
            "Runtime.exceptionThrown",
            "Page.fileChooserOpened",
        ):
            state.tab._driver.set_callback(event, None)
        state.tab.run_cdp("Page.setInterceptFileChooserDialog", enabled=False)

    def _capture_state(self, state, *, mode="auto", lightweight=False):
        tab = state.tab
        if state.events.dialog:
            raise BrowserError("DIALOG_OPEN", "A JavaScript dialog is open; inspect browser_dialog")
        tab.run_cdp("Runtime.releaseObjectGroup", objectGroup="cb-observation")
        tree = tab.run_cdp("Page.getFrameTree")["frameTree"]
        document = tree["frame"]
        if state.frame_id:

            def find_frame(branch):
                if branch["frame"]["id"] == tab._frame_id:
                    return branch["frame"]
                return next(
                    (
                        found
                        for child in branch.get("childFrames", [])
                        if (found := find_frame(child))
                    ),
                    None,
                )

            document = find_frame(tree)
            if not document:
                raise BrowserError("FRAME_STALE", "Observed frame detached or moved")
        frame = document["id"]
        document_key = frame + ":" + document.get("loaderId", "")
        if document_key != state.document_key:
            state.nodes.clear()
            state.screenshot = None
            state.document_key = document_key
            state.frame_states.clear()
            state.frame_nodes.clear()
        history = (
            tab.run_cdp("Page.getNavigationHistory") if not state.frame_id else {"entries": []}
        )
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
            + json.dumps({"mode": mode, "lightweight": lightweight, "query": state.query})
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
        if data.get("query_error"):
            raise BrowserError("INVALID_SELECTOR", "Observation CSS selector is invalid")
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
            functionDeclaration="function(){return this.elements.concat(this.frame_elements || [])}",
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
        state.frame_backend_ids = backends[len(data["nodes"]) :]
        backends = backends[: len(data["nodes"])]
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
                ax_budget = (12 if state.query else 0) if lightweight else 60
                new_cache = {}
                for bid, meta in zip(backends, data["nodes"], strict=True):
                    try:
                        cache_key = (bid, self._node_signature(meta))
                        current = state.ax_cache.get(cache_key)
                        if not current and ax_budget:
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
                            meta["_dom_role"] = meta["role"]
                            meta["name"] = " ".join(
                                str(current.get("name", {}).get("value") or meta["name"]).split()
                            )[:500]
                            meta["role"] = current.get("role", {}).get("value", meta["role"])
                            for prop in current.get("properties", []):
                                key, value = prop["name"], prop["value"].get("value")
                                if key in ("expanded", "pressed", "selected"):
                                    meta[key] = (
                                        str(value).lower() if isinstance(value, bool) else value
                                    )
                        else:
                            data["accessibility_source"] = "dom-fallback"
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

    def _capture_page(self, state, *, mode="auto", lightweight=False):
        """Inspect bounded frame documents; collect no pixels or values from protected frames."""
        data = self._capture_state(state, mode=mode, lightweight=lightweight)
        state.frame_nodes = {}
        inventory, texts, masks, visited = [], [], [], set()
        root_regions = data["iframe_regions"]
        frame_budget = 4 if lightweight else 16

        def inspect(parent, parent_id, depth, ancestor_visible=True, root_region=None):
            restricted = False
            if not parent.data.get("has_iframe"):
                return False
            try:
                children = parent.frame_backend_ids
                remaining = max(0, frame_budget - len(visited))
                if parent.data.get("frame_count", len(children)) > remaining:
                    inventory.append(
                        {
                            "frame_id": None,
                            "parent_frame_id": parent_id,
                            "readable": False,
                            "actionable": False,
                            "reason": "FRAME_BUDGET",
                        }
                    )
                    masks.extend([root_region] if root_region else root_regions)
                    restricted = True
                frames = []
                for backend in children[:remaining]:
                    frame_element = self.Element(parent.tab, backend_id=backend)
                    frames.append(self.Frame(parent.tab, frame_element))
            except Exception:
                inventory.append(
                    {
                        "frame_id": None,
                        "parent_frame_id": parent_id,
                        "readable": False,
                        "actionable": False,
                        "reason": "FRAME_UNAVAILABLE",
                    }
                )
                masks.extend([root_region] if root_region else root_regions)
                return True
            for frame in frames:
                public_id = None
                region = root_region
                try:
                    local_region = frame.frame_ele.run_js("""
                            const r=this.getBoundingClientRect();let safe=true;
                            for(let p=this;p;p=p.parentElement){const s=getComputedStyle(p);
                              if(s.transform!=='none'||s.filter!=='none'||s.perspective!=='none'||
                                (s.backdropFilter&&s.backdropFilter!=='none')||
                                (s.webkitBoxReflect&&s.webkitBoxReflect!=='none')||s.mixBlendMode!=='normal')safe=false;}
                            const s=getComputedStyle(this);
                            return {x:r.x,y:r.y,width:r.width,height:r.height,mask_safe:safe,
                              visible:r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.visibility!=='collapse'&&s.display!=='none'};
                        """)
                    region = region or local_region
                    visible = ancestor_visible and local_region["visible"]
                    frame_key = (parent.document_key, frame._frame_id, frame._backend_id)
                    public_id = next(
                        (
                            key
                            for key, child in state.frame_states.items()
                            if getattr(child, "frame_key", None) == frame_key
                        ),
                        None,
                    )
                    if not public_id:
                        public_id = "frame_" + secrets.token_urlsafe(12)
                        child = TabState(frame, frame_id=public_id, parent_frame_id=parent_id)
                        child.frame_key = frame_key
                        state.frame_states[public_id] = child
                    child = state.frame_states[public_id]
                    visited.add(public_id)
                    entry = {
                        "frame_id": public_id,
                        "parent_frame_id": parent_id,
                        "origin": None,
                        "readable": False,
                        "actionable": False,
                        "reason": None,
                    }
                    inventory.append(entry)
                    if not visible:
                        entry["reason"] = "FRAME_NOT_VISIBLE"
                        continue
                    if len(visited) > frame_budget or depth > 4:
                        entry["reason"] = "FRAME_BUDGET"
                        restricted = True
                        masks.append(region)
                        continue
                    if state.query.get("_all_frames"):
                        child.query = dict(state.query)
                    self._capture_state(child, mode=mode, lightweight=lightweight)
                    entry["origin"] = (
                        origin(child.data["url"])
                        if child.data["url"].startswith(("http://", "https://"))
                        else None
                    )
                    sensitive = (
                        child.data["protected"]
                        or bool(TOKEN.search(child.data["text"]))
                        or bool(child.data.get("challenge"))
                    )
                    if sensitive:
                        entry["reason"] = "SENSITIVE_FRAME"
                        restricted = True
                        masks.append(region)
                        continue
                    entry.update(
                        readable=True,
                        actionable=visible,
                        rect={k: region[k] for k in ("x", "y", "width", "height")},
                    )
                    if child.data["semantic_text"]:
                        texts.append(f"[frame {public_id}]\n{child.data['semantic_text']}")
                    for nid, target in child.nodes.items():
                        if visible:
                            state.frame_nodes[nid] = (child, target)
                    if inspect(child, public_id, depth + 1, visible, region):
                        restricted = True
                except Exception:
                    if public_id and inventory and inventory[-1].get("frame_id") == public_id:
                        inventory[-1].update(
                            readable=False, actionable=False, reason="FRAME_UNAVAILABLE"
                        )
                    else:
                        inventory.append(
                            {
                                "frame_id": public_id,
                                "parent_frame_id": parent_id,
                                "readable": False,
                                "actionable": False,
                                "reason": "FRAME_UNAVAILABLE",
                            }
                        )
                    restricted = True
                    masks.extend([region] if region else root_regions)
            return restricted

        if data["has_iframe"] and not data["protected"]:
            # Inspect each top-level frame independently. Unknown plugin embeds and
            # frames beyond the budget remain masked. Never trust a page hint.
            inspect(state, None, 1)
            masks.extend(r for r in root_regions if r.get("tag") in ("object", "embed"))
        state.frame_states = {
            key: child for key, child in state.frame_states.items() if key in visited
        }
        for key in ("interactive_truncated", "query_scan_truncated"):
            data[key] = bool(data.get(key)) or any(
                child.data.get(key) for child in state.frame_states.values()
            )
        frame_fp = hashlib.sha256(
            json.dumps(
                [
                    (key, child.document_key, child.fingerprint)
                    for key, child in state.frame_states.items()
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if state.frames_fingerprint and frame_fp != state.frames_fingerprint:
            state.revision += 1
            state.cursors.clear()
        state.frames_fingerprint = frame_fp
        state.revision_documents[state.revision] = state.document_key
        data["frames"] = inventory
        data["restricted_frame_regions"] = masks
        data["readable_frames"] = sum(bool(f["readable"]) for f in inventory)
        data["frame_reading_truncated"] = any(not f["readable"] for f in inventory)
        data["file_chooser"] = None
        chooser = state.events.chooser
        if chooser and not data["protected"]:
            owner = (
                state
                if chooser["frame"] == getattr(state.tab, "_frame_id", None)
                else next(
                    (
                        child
                        for child in state.frame_states.values()
                        if child.tab._frame_id == chooser["frame"]
                    ),
                    None,
                )
            )
            if owner and not owner.data["protected"]:
                try:
                    element = self.Element(owner.tab, backend_id=chooser["backend"])
                    attributes = element.run_js(
                        "return {connected:this.isConnected,type:this.type,name:this.getAttribute('aria-label')||this.name||'File chooser',multiple:this.multiple,disabled:this.disabled}"
                    )
                    if (
                        attributes["connected"]
                        and attributes["type"] == "file"
                        and not SENSITIVE.search(attributes["name"])
                    ):
                        meta = {
                            "tag": "input",
                            "type": "file",
                            "name": attributes["name"],
                            "role": "button",
                            "href": None,
                            "disabled": attributes["disabled"],
                            "multiple": attributes["multiple"],
                            "rect": {"x": 0, "y": 0, "width": 0, "height": 0},
                            "_form_digest": owner.data["form_digest"],
                            "form_fields": [],
                            "form_action": None,
                            "form_method": None,
                            "submits_form": False,
                        }
                        target = (chooser["backend"], meta)
                        if owner is state:
                            state.nodes[chooser["node_id"]] = target
                        else:
                            state.frame_nodes[chooser["node_id"]] = (owner, target)
                        data["file_chooser"] = {
                            "node_id": chooser["node_id"],
                            "frame_id": owner.frame_id,
                            "multiple": attributes["multiple"],
                        }
                except Exception:
                    data["file_chooser"] = {"error": "FILE_CHOOSER_UNAVAILABLE"}
        if texts and mode in ("auto", "semantic"):
            data["semantic_text"] = (data["semantic_text"] + "\n" + "\n".join(texts))[:250000]
        return data

    @staticmethod
    def _node_target(state, node_id):
        if node_id in state.nodes:
            return state, state.nodes[node_id]
        if node_id in state.frame_nodes:
            return state.frame_nodes[node_id]
        return state, None

    def _frame_point(self, state, local_x, local_y):
        """Project an observed child point through exact frame owners, checking occlusion."""
        frame = state.tab
        while state.frame_id and hasattr(frame, "frame_ele"):
            owner = frame.frame_ele
            result = owner.run_js(
                """
                const r=this.getBoundingClientRect();let safe=true;
                for(let p=this;p;p=p.parentElement){const s=getComputedStyle(p);
                  if(s.transform!=='none'||s.perspective!=='none'||s.zoom!=='1')safe=false;}
                const x=r.x+this.clientLeft+arguments[0], y=r.y+this.clientTop+arguments[1];
                const hit=document.elementFromPoint(x,y);
                return {x,y,ok:this.isConnected && hit===this && safe};
            """,
                local_x,
                local_y,
            )
            if not result["ok"]:
                raise BrowserError(
                    "NODE_NOT_ACTIONABLE",
                    "Frame target is covered, transformed or outside the viewport",
                )
            local_x, local_y = result["x"], result["y"]
            frame = frame._target_page
        return local_x, local_y

    @staticmethod
    def _node_signature(meta):
        # Layout and focus changes do not turn the same element into another one.
        # Native state, accessible meaning and the owning form's digest do.
        value = {
            k: v
            for k, v in meta.items()
            if k not in ("rect", "focused", "scroll", "in_viewport", "_dom_name", "_dom_role")
        }
        if "_dom_name" in meta:
            value["name"] = meta["_dom_name"]
        if "_dom_role" in meta:
            value["role"] = meta["_dom_role"]
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
            if len(self.sessions) >= self.cfg.max_sessions:
                raise BrowserError("BROWSER_BUSY", "Browser work capacity is occupied")
            used = {runtime.cfg.display_number for runtime in self.runtimes.values()}
            number = next(
                n
                for n in range(
                    self.cfg.display_number, self.cfg.display_number + self.cfg.max_sessions
                )
                if n not in used
            )
            runtime = DisplayRuntime(self.cfg.model_copy(update={"display_number": number}))
            runtime.start()
            self.runtimes[sid] = runtime
            profile = self.cfg.data_dir / "profiles" / sid
            profile.mkdir(parents=True, exist_ok=True)
            if os.name == "posix" and not self.cfg.development:
                import grp

                os.chown(profile, -1, grp.getgrnam(self.cfg.browser_group).gr_gid)
                profile.chmod(0o2770)
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
            # Do not start Chrome's network-heavy new-tab application only to
            # replace it with about:blank immediately after CDP connects.
            options.set_argument("about:blank")
            options.set_argument("--dns-prefetch-disable")
            options.set_argument("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
            options.set_argument("--disable-features=Translate,MediaRouter")
            if self.cfg.webmcp_testing:
                options.set_argument("--enable-features=WebMCP")
            if self.cfg.browser_proxy:
                options.set_proxy(self.cfg.browser_proxy)
                options.set_argument("--proxy-bypass-list=<-loopback>")
            try:
                browser = self.Chromium(options)
            except Exception:
                self.runtimes.pop(sid).close()
                raise
            self.sessions[sid] = {"browser": browser, "tabs": {}, "selected": None}
            try:
                self._start_artifacts(sid)
                self._sync(sid)
            except BaseException:
                # Initialization failed before exposing this fresh session. Do
                # not leave a half-initialized browser sharing the display.
                try:
                    browser.quit()
                finally:
                    partial = self.sessions.pop(sid)
                    if partial.get("artifacts"):
                        partial["artifacts"].close()
                    self.runtimes.pop(sid).close()
                raise
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

    @staticmethod
    def _navigation_marker(state):
        return {
            "url": state.tab.url,
            "document": state.document_key,
            "sequence": state.same_document_sequence,
            "same_document_kind": state.same_document_kind,
        }

    def navigate(self, session_id, tab_id, operation, url=None):
        state = self._tab(session_id, tab_id)
        before = self._navigation_marker(state)
        frame = state.tab.run_cdp("Page.getFrameTree")["frameTree"]["frame"]
        before["document"] = frame["id"] + ":" + frame.get("loaderId", "")
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
                        **navigation_outcome(before, before),
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
                **navigation_outcome(before, self._navigation_marker(state), operation=operation),
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
        query=None,
        _wait_state=None,
    ):
        state = self._tab(session_id, tab_id)
        if not cursor:
            state.query = {}
            for child in state.frame_states.values():
                child.query = {}
        if query is not None:
            target_frame = query.get("frame_id")
            if target_frame and target_frame not in state.frame_states:
                self._capture_page(state, mode="interactive", lightweight=True)
            if target_frame and target_frame not in state.frame_states:
                raise BrowserError("FRAME_STALE", "Frame is not currently observable")
            (state.frame_states[target_frame] if target_frame else state).query = {
                k: v
                for k, v in query.items()
                if k in ("scope", "selector", "limit", "role", "name", "label") and v is not None
            }
            target_query = (state.frame_states[target_frame] if target_frame else state).query
            target_query["_all_frames"] = not target_frame
            if _wait_state:
                target_query["visibility"] = (
                    "all" if _wait_state in ("present", "absent") else "rendered"
                )
                target_query["enabled_only"] = _wait_state == "enabled"
        elif not cursor:
            state.query = {}
            for child in state.frame_states.values():
                child.query = {}
        data = self._capture_page(state, mode=mode, lightweight=lightweight)
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
                combined = list(state.nodes.items()) + [
                    (nid, (target[0], target[1] | {"frame_id": child.frame_id}))
                    for nid, (child, target) in state.frame_nodes.items()
                ]
                for nid, (_, meta) in combined:
                    if query:
                        if query.get("frame_id") and meta.get("frame_id") != query["frame_id"]:
                            continue
                        if (
                            query.get("role")
                            and query["role"].casefold() != str(meta.get("role") or "").casefold()
                        ):
                            continue
                        if any(
                            query.get(key)
                            and query[key].casefold() not in str(meta.get("name") or "").casefold()
                            for key in ("name", "label")
                        ):
                            continue
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
                    if meta.get("frame_id"):
                        clean["rect_coordinate_space"] = "frame viewport CSS pixels"
                    interactive.append(compact_node({"node_id": nid, **clean}))
            snapshot = {
                "semantic": redact(
                    (
                        state.frame_states[query["frame_id"]].data
                        if query and query.get("frame_id")
                        else data
                    )["semantic_text"]
                )
                if mode in ("auto", "semantic")
                else "",
                "nodes": interactive,
            }
            offsets = (0, 0)
        obs, next_offsets = paginate(snapshot, offsets, budget)
        obs.update(
            screenshot=None,
            viewport=data["viewport"],
            interactive_truncated=data["interactive_truncated"],
            query_scan_truncated=data.get("query_scan_truncated", False),
            semantic_source=data["semantic_source"],
            semantic_source_truncated=data["semantic_source_truncated"],
            accessibility_source=data["accessibility_source"],
            scroll_scan_truncated=data["scroll_scan_truncated"],
            readable_frames=data["readable_frames"],
            frame_reading_truncated=data["frame_reading_truncated"],
            frames=data["frames"],
            file_chooser=data.get("file_chooser"),
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
                data["has_iframe"] and self.cfg.iframe_screenshot_policy == "block"
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
            observed_targets = {
                nid: [bid, self._node_signature(meta), meta["rect"]]
                for nid, (bid, meta) in state.nodes.items()
            }
            image = state.tab.run_cdp(
                "Page.captureScreenshot",
                format="jpeg",
                quality=state.options["screenshot_quality"],
                captureBeyondViewport=full_page,
                **capture,
            )["data"]
            capture_geometry = [
                state.document_key,
                data["viewport"],
                data["scroll"],
                data["iframe_regions"],
            ]
            after = self._capture_page(state, mode="visual", lightweight=lightweight)
            self._guard_page(after)
            if capture_geometry != [
                state.document_key,
                after["viewport"],
                after["scroll"],
                after["iframe_regions"],
            ] or TOKEN.search(after["text"]):
                raise BrowserError(
                    "SCREEN_CHANGED",
                    "Capture geometry or privacy changed during capture; observe again",
                )
            raw_digest = hashlib.sha256(base64.b64decode(image)).hexdigest()
            masked_regions = []
            mime_type = "image/jpeg"
            regions = (
                data["iframe_regions"]
                if self.cfg.iframe_screenshot_policy == "mask"
                else data["restricted_frame_regions"] + after["restricted_frame_regions"]
            )
            if regions:
                if full_page:
                    regions = [
                        r | {"x": r["x"] + data["scroll"]["x"], "y": r["y"] + data["scroll"]["y"]}
                        for r in regions
                    ]
                image, masked_regions = mask_frames(
                    image, regions, {"width": width, "height": height}
                )
                mime_type = "image/png"
            screenshot_id = "screen_" + secrets.token_urlsafe(16)
            state.screenshot = {
                "id": screenshot_id,
                "revision": state.revision,
                "full_page": full_page,
                "digest": raw_digest,
                "masked_regions": masked_regions,
                "document_key": state.document_key,
                "geometry": [data["viewport"], data["scroll"]],
                "targets": observed_targets,
            }
            obs["screenshot"] = {
                "screenshot_id": screenshot_id,
                "width": width,
                "height": height,
                "coordinate_units": "CSS pixels",
                "full_page": full_page,
                "masked_regions": masked_regions,
                "captured_at": time.time(),
            }
            extra["_image"] = {"data": image, "mimeType": mime_type}
        notices = []
        if data["accessibility_source"] == "dom-fallback":
            notices.append(
                "Chromium accessibility information unavailable; rendered DOM fallback used"
            )
        if data["frame_reading_truncated"]:
            notices.append(
                "Partial frame observation: inspect frames[].reason; unavailable or sensitive regions are withheld"
            )
        return self._result(session_id, tab_id, observation=obs, notices=notices, **extra)

    def prepare(self, session_id, tab_id, expected_revision, action):
        state = self._tab(session_id, tab_id)
        if action["type"] == "dialog":
            dialog = state.events.dialog
            if not dialog or dialog["dialog_id"] != action["dialog_id"]:
                raise BrowserError("DIALOG_STALE", "The observed dialog is no longer current")
            if dialog["sensitive"]:
                raise BrowserError(
                    "SENSITIVE_INPUT",
                    "Protected dialog requires private authentication",
                    "user_action_required",
                )
            return self._cached_result(
                session_id,
                tab_id,
                state,
                target=dialog["message"],
                requires_confirmation=True,
                target_binding=dialog["_binding"] + dialog["dialog_id"],
                action_policy={
                    "mode": self.cfg.approval_policy,
                    "approval_required": True,
                    "reason": "dialog_response",
                },
            )
        data = self._capture_page(state)
        self._guard_page(data)
        if not data["form_state_complete"] and action["type"] not in PASSIVE_ACTIONS:
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
            digest = schema_fingerprint(tool["inputSchema"])
            preapproved = (
                self.cfg.webmcp_read_allowlist.get(origin(state.tab.url), {}).get(tool["name"])
                == digest
            )
            return self._result(
                session_id,
                tab_id,
                target=redact(tool["name"]),
                destination=origin(state.tab.url),
                destination_kind="page_tool",
                data_sent=list(action["arguments"]),
                requires_confirmation=not preapproved,
                target_binding=hashlib.sha256(
                    json.dumps(
                        [
                            state.document_key,
                            origin(state.tab.url),
                            tool["name"],
                            digest,
                            action["arguments"],
                        ],
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
                action_policy={
                    "mode": self.cfg.approval_policy,
                    "approval_required": not preapproved,
                    "reason": "operator_pinned_read_tool"
                    if preapproved
                    else "page_tool_requires_approval",
                },
            )
        nid = action.get("node_id")
        meta = None
        target_state = state
        if nid:
            target_state, target = self._node_target(state, nid)
            if target is None:
                raise BrowserError(
                    "STALE_NODE", "Observed target changed or was replaced; observe again"
                )
            bid, meta = target
            self._guard_page(target_state.data)
            if (
                not target_state.data["form_state_complete"]
                and action["type"] not in PASSIVE_ACTIONS
            ):
                raise BrowserError(
                    "UNSUPPORTED_OPERATION",
                    "Target frame form exceeds the verification budget; use manual control",
                    "user_action_required",
                )
            element = self.Element(target_state.tab, backend_id=bid)
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
            if action["type"] in ("fill", "type") and not (
                meta["editable"]
                or meta["tag"] == "textarea"
                or (
                    meta["tag"] == "input"
                    and meta["type"] in ("text", "search", "email", "url", "tel", "number")
                )
            ):
                raise BrowserError("NODE_NOT_ACTIONABLE", "Target is not an editable text control")
            if action["type"] in ("select", "select_multiple") and meta["tag"] != "select":
                raise BrowserError("NODE_NOT_ACTIONABLE", "Target is not a select control")
            if action["type"] in ("fill", "type") and meta.get("readonly"):
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
            if action["type"] == "select_multiple":
                if not meta.get("multiple"):
                    raise BrowserError("NODE_NOT_ACTIONABLE", "Target is not a multiple select")
                values = action["values"]
                if len(set(values)) != len(values):
                    raise BrowserError("INVALID_INPUT", "Duplicate selected values")
                for value in values:
                    choices = [
                        o for o in meta["options"] if o["value"] == value and not o["disabled"]
                    ]
                    if len(choices) != 1:
                        raise BrowserError(
                            "NODE_NOT_ACTIONABLE", "Option is ambiguous, disabled or unobserved"
                        )
            if (
                action["type"] == "keypress"
                and action["keys"][0] in ("C", "V", "X")
                and set(action.get("modifiers", [])) & {"CONTROL", "META"}
            ):
                raise BrowserError(
                    "POLICY_BLOCKED",
                    "Use the work-local browser_clipboard, not global clipboard shortcuts",
                    "blocked",
                )
        drag_binding = None
        if action["type"] == "drag":
            dest_state, dest = self._node_target(state, action["target_node_id"])
            if not dest:
                raise BrowserError("STALE_NODE", "Drag destination changed; observe it again")
            self._guard_page(dest_state.data)
            if dest[1]["disabled"] or dest[1]["type"] == "file":
                raise BrowserError("NODE_NOT_ACTIONABLE", "Drag destination is unavailable")
            drag_binding = [dest_state.document_key, dest[0], self._node_signature(dest[1])]
        if "screenshot_id" in action:
            shot = state.screenshot
            if (
                not shot
                or shot["id"] != action["screenshot_id"]
                or shot.get("document_key") != state.document_key
                or shot.get("geometry") != [data["viewport"], data["scroll"]]
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
            resolved = []
            if action["type"] in ("click_at", "double_click_at"):
                for candidate_id, (backend, candidate) in state.nodes.items():
                    rect = candidate["rect"]
                    if (
                        rect["x"] <= action["x"] < rect["x"] + rect["width"]
                        and rect["y"] <= action["y"] < rect["y"] + rect["height"]
                    ):
                        candidate_element = self.Element(state.tab, backend_id=backend)
                        if candidate_element.run_js(
                            "const hit=document.elementFromPoint(arguments[0],arguments[1]);return this.isConnected&&(hit===this||this.contains(hit))",
                            action["x"],
                            action["y"],
                        ):
                            resolved.append((candidate_id, backend, candidate))
            if len(resolved) > 1:
                raise BrowserError(
                    "NODE_AMBIGUOUS",
                    "Coordinate hits nested interactive targets; use an observed node_id",
                )
            if resolved:
                nid, backend, meta = resolved[0]
                if shot["targets"].get(nid) != [backend, self._node_signature(meta), meta["rect"]]:
                    raise BrowserError(
                        "STALE_SCREENSHOT", "Coordinate target changed since capture"
                    )
                if meta["disabled"] or meta["type"] == "file":
                    raise BrowserError(
                        "NODE_NOT_ACTIONABLE",
                        "Coordinate target is disabled or requires staged upload",
                    )
                # Classify the same actual element exactly as a node-based action.
                action = action | {
                    "type": "double_click" if action["type"] == "double_click_at" else "click"
                }
            else:
                # Canvas/unknown targets have no semantic identity to validate.
                # Keep the stricter pixel proof for this fallback only.
                image = state.tab.run_cdp(
                    "Page.captureScreenshot",
                    format="jpeg",
                    quality=state.options["screenshot_quality"],
                    captureBeyondViewport=False,
                )["data"]
                if hashlib.sha256(base64.b64decode(image)).hexdigest() != shot["digest"]:
                    state.screenshot = None
                    raise BrowserError(
                        "STALE_SCREENSHOT", "Unidentified target pixels changed since capture"
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
            resolved_node_id=nid,
            target_binding=hashlib.sha256(
                json.dumps(
                    [
                        state.document_key,
                        state.tab.url,
                        target_state.document_key,
                        nid,
                        self._node_signature(meta) if meta else data["viewport"],
                        drag_binding,
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
        )

    def act(self, session_id, tab_id, expected_revision, action):
        self.prepare(session_id, tab_id, expected_revision, action)
        state = self._tab(session_id, tab_id)
        before_navigation = self._navigation_marker(state)
        if action["type"] == "dialog":
            try:
                state.tab.handle_alert(
                    accept=action["operation"] == "accept", send=action.get("text"), timeout=0.1
                )
                time.sleep(0.05)
                if state.pending_input:
                    state.pending_input.release()
                    if state.pending_input.held_key or state.pending_input.held_button:
                        raise BrowserError("RESULT_UNCERTAIN", "Input release remains unconfirmed")
                    state.pending_input = None
                self._capture_page(state, mode="interactive")
            except Exception as exc:
                raise BrowserError(
                    "RESULT_UNCERTAIN", "Dialog response was dispatched; do not repeat it"
                ) from exc
            return self._result(
                session_id,
                tab_id,
                action_result={
                    "performed": True,
                    "page_changed": True,
                    **navigation_outcome(before_navigation, self._navigation_marker(state)),
                },
            )
        before_fp = state.fingerprint
        before_frames = state.frames_fingerprint
        old_tabs = set(self._session(session_id)["tabs"])
        typ = action["type"]
        target_state, target = self._node_target(state, action.get("node_id"))
        element = self.Element(target_state.tab, backend_id=target[0]) if target else None
        performed = True
        dispatched = False
        tool_result = None
        native = NativeInput(state.tab)

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
            x, y = self._frame_point(target_state, x, y)
            dispatched = True
            native.click(
                x,
                y,
                count=click_count,
                button={"right_click": "right", "middle_click": "middle"}.get(typ, "left"),
                modifiers=action.get("modifiers", []),
            )

        def focus_exact():
            nonlocal dispatched
            dispatched = True
            target_state.tab.run_cdp("DOM.focus", backendNodeId=target[0])
            if not element.run_js(
                "return document.activeElement===this || this.contains(document.activeElement)"
            ):
                raise BrowserError("NODE_NOT_ACTIONABLE", "Observed element cannot receive focus")

        try:
            if typ == "page_tool":
                dispatched = True
                tool_result = state.page_tools.invoke(
                    state.advertised_tools["frame_id"], action["tool_name"], action["arguments"]
                )
            elif typ in ("click", "double_click", "right_click", "middle_click"):
                native_click(2 if typ == "double_click" else 1)
            elif typ == "upload":
                dispatched = True
                target_state.tab.run_cdp(
                    "DOM.setFileInputFiles",
                    backendNodeId=target[0],
                    files=[item["path"] for item in action["_uploads"]],
                )
                state.events.chooser = None
            elif typ == "fill":
                focus_exact()
                native.key("A", ["CONTROL"])
                if action["text"]:
                    state.tab.run_cdp("Input.insertText", text=action["text"])
                else:
                    native.key("BACKSPACE")
            elif typ == "type":
                focus_exact()
                for char in action["text"]:
                    state.tab.run_cdp("Input.insertText", text=char)
                    if action.get("interval_ms"):
                        time.sleep(action["interval_ms"] / 1000)
            elif typ == "select_multiple":
                dispatched = True
                element.run_js(
                    "const values=new Set(JSON.parse(arguments[0]));for(const o of this.options)o.selected=values.has(o.value);this.dispatchEvent(new Event('input',{bubbles:true}));this.dispatchEvent(new Event('change',{bubbles:true}));",
                    json.dumps(action["values"]),
                )
            elif typ == "drag":
                destination_state, destination = self._node_target(state, action["target_node_id"])
                points = []
                for owner, target_node in (
                    (target_state, target),
                    (destination_state, destination),
                ):
                    handle = self.Element(owner.tab, backend_id=target_node[0])
                    point = handle.run_js(
                        "const r=this.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,h=document.elementFromPoint(x,y);return {x,y,ok:this.isConnected&&(h===this||this.contains(h))}"
                    )
                    if not point["ok"]:
                        raise BrowserError(
                            "NODE_NOT_ACTIONABLE",
                            "Drag endpoint is covered or outside the viewport",
                        )
                    points.append(self._frame_point(owner, point["x"], point["y"]))
                dispatched = True
                native.drag(
                    *points, steps=action.get("steps", 12), modifiers=action.get("modifiers", [])
                )
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
                native.key(action["keys"][0], action.get("modifiers", []))
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
                    native.click(x, y, count=2 if typ == "double_click_at" else 1)
            else:
                raise BrowserError("UNSUPPORTED_OPERATION", "Action is not implemented")
            time.sleep(state.options["wait_ms"] / 1000)
            if state.events.dialog:
                return self._cached_result(
                    session_id,
                    tab_id,
                    state,
                    action_result={
                        "performed": performed,
                        "page_changed": True,
                        "navigation_occurred": False,
                    },
                    dialog=self.dialog_info(session_id, tab_id)["dialog"],
                    notices=["Dialog opened; DOM collection waits for an explicit dialog response"],
                )
            self._sync(session_id)
            self._capture_page(state)
        except BrowserError as exc:
            if dispatched:
                raise BrowserError(
                    "RESULT_UNCERTAIN",
                    "Action was dispatched but its outcome could not be observed; do not repeat",
                ) from exc
            raise
        except Exception as exc:
            if dispatched and state.events.dialog:
                return self._cached_result(
                    session_id,
                    tab_id,
                    state,
                    action_result={
                        "performed": True,
                        "page_changed": True,
                        "navigation_occurred": False,
                    },
                    dialog=self.dialog_info(session_id, tab_id)["dialog"],
                    notices=[
                        "Dialog opened during input; any pending input release is completed after its response"
                    ],
                )
            raise BrowserError(
                "RESULT_UNCERTAIN", "Action may have been dispatched; do not repeat automatically"
            ) from exc
        finally:
            native.release()
            if native.held_key or native.held_button:
                state.pending_input = native
        changed = before_fp != state.fingerprint or before_frames != state.frames_fingerprint
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
                **navigation_outcome(before_navigation, self._navigation_marker(state)),
                "new_tab_ids": added,
            },
        )

    def _start_artifacts(self, session_id):
        session = self._session(session_id)
        root = self.cfg.data_dir / "artifacts" / session_id
        Artifacts.reap_orphans(root.parent, self.sessions, self.cfg.artifact_ttl)
        artifacts = Artifacts(
            root,
            max_bytes=self.cfg.max_artifact_mb * 1048576,
            file_bytes=self.cfg.max_artifact_file_mb * 1048576,
            ttl=self.cfg.artifact_ttl,
        )
        if os.name == "posix" and not self.cfg.development:
            import grp

            gid = grp.getgrnam(self.cfg.browser_group).gr_gid
            for directory in (root.parent, root):
                os.chown(directory, -1, gid)
                directory.chmod(0o2770)
        session["artifacts"] = artifacts
        session["downloads"] = {}
        browser = session["browser"]

        def cancel(guid):
            try:
                browser._run_cdp("Browser.cancelDownload", guid=guid)
            except Exception:
                pass

        def began(guid, suggestedFilename, **kwargs):
            if session.get("paused"):
                cancel(guid)
                return
            try:
                key = artifacts.reserve(
                    suggestedFilename,
                    mimetypes.guess_type(suggestedFilename)[0] or "application/octet-stream",
                    storage_name=guid,
                )
                session["downloads"][guid] = key
            except BrowserError:
                cancel(guid)

        def progress(guid, receivedBytes, state, **kwargs):
            key = session["downloads"].get(guid)
            if (
                not key
                or session.get("paused")
                or not artifacts.progress(
                    key,
                    max(receivedBytes, kwargs.get("totalBytes", 0))
                    if kwargs.get("totalBytes", 0) > artifacts.file_bytes
                    else receivedBytes,
                    state,
                )
            ):
                cancel(guid)

        browser._driver.set_callback("Browser.downloadWillBegin", began)
        browser._driver.set_callback("Browser.downloadProgress", progress)
        browser._run_cdp(
            "Browser.setDownloadBehavior",
            behavior="allowAndName",
            downloadPath=str(root.resolve()),
            eventsEnabled=True,
        )

    def artifacts(self, session_id, operation="list", artifact_id=None, tab_id=None, format="text"):
        session = self._session(session_id)
        files = session["artifacts"]
        if operation == "list":
            return {"session_id": session_id, "artifacts": files.list()}
        if operation == "get":
            return {"session_id": session_id, **files.get(artifact_id)}
        if operation in ("delete", "clear"):
            keys = (
                [artifact_id] if operation == "delete" else [i["artifact_id"] for i in files.list()]
            )
            for key in keys:
                for guid, item in list(session["downloads"].items()):
                    if item == key:
                        if files.items[key]["state"] == "in_progress":
                            session["browser"]._run_cdp("Browser.cancelDownload", guid=guid)
                        del session["downloads"][guid]
                files.delete(key)
            return {"session_id": session_id, "removed_artifact_ids": keys}
        if not tab_id:
            raise BrowserError("INVALID_INPUT", "Export requires a tab")
        if format == "image":
            seen = self.observe(session_id, tab_id, mode="visual")
            shot = seen["_image"]
            item = files.put(
                base64.b64decode(shot["data"]),
                "page.png" if shot["mimeType"] == "image/png" else "page.jpg",
                shot["mimeType"],
            )
        else:
            seen = self.observe(session_id, tab_id, mode="semantic", max_chars=100000)
            text = seen["observation"]["semantic_snapshot"]
            # Safe HTML is a new inert text document, never the site's executable markup.
            data = (
                (
                    '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'"><pre>'
                    + html.escape(text)
                    + "</pre>"
                )
                if format == "html"
                else text
            )
            item = files.put(
                data.encode(),
                "page.html" if format == "html" else "page.txt",
                "text/html" if format == "html" else "text/plain",
            )
            item["source_truncated"] = (
                seen["observation"]["truncated"] or seen["observation"]["semantic_source_truncated"]
            )
        return {"session_id": session_id, "tab_id": tab_id, "artifact": item}

    def private_download(self, session_id, artifact_id):
        return self._session(session_id)["artifacts"].private_download(artifact_id)

    @staticmethod
    def _cached_result(session_id, tab_id, state, **extra):
        return dict(
            session_id=session_id,
            tab_id=tab_id,
            revision=state.revision,
            page={
                "url": safe_url(state.data.get("url", "about:blank")),
                "title": redact(state.data.get("title", "")) or None,
            },
            page_cached=True,
            **extra,
        )

    def dialog_info(self, session_id, tab_id):
        state = self._tab(session_id, tab_id)
        dialog = state.events.dialog
        return self._cached_result(
            session_id,
            tab_id,
            state,
            dialog={k: v for k, v in dialog.items() if not k.startswith("_")} if dialog else None,
        )

    def logs(self, session_id, tab_id, after=0, limit=50):
        state = self._tab(session_id, tab_id)
        return self._cached_result(session_id, tab_id, state, logs=state.events.read(after, limit))

    def clipboard_read(self, session_id, tab_id, node_id, expected_revision):
        state = self._tab(session_id, tab_id)
        self.prepare(session_id, tab_id, expected_revision, {"type": "copy", "node_id": node_id})
        owner, target = self._node_target(state, node_id)
        element = self.Element(owner.tab, backend_id=target[0])
        text = element.run_js(
            "return String('value' in this && this.type!=='file'?this.value:this.innerText||'').slice(0,20000)"
        )
        return {"text": redact(text)}

    def wait(self, session_id, tab_id, condition, timeout_ms=5000):
        state = self._tab(session_id, tab_id)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            partial = False
            typ = condition["type"]
            if typ == "dialog":
                matched = bool(state.events.dialog)
            elif typ == "download":
                items = self._session(session_id)["artifacts"].list()
                matched = any(i["state"] == "completed" for i in items)
            else:
                seen = self.observe(
                    session_id,
                    tab_id,
                    mode="interactive",
                    max_chars=8000,
                    lightweight=True,
                    query=condition.get("query"),
                    _wait_state=condition.get("state", "present"),
                )
                if typ == "url":
                    matched = state.tab.url == condition["value"]
                else:
                    partial = any(
                        seen["observation"].get(key)
                        for key in ("truncated", "interactive_truncated", "frame_reading_truncated")
                    )
                    nodes = [
                        json.loads(line)
                        for line in seen["observation"]["interactive_snapshot"].splitlines()
                    ]
                    matched = (
                        any(not n.get("disabled") for n in nodes)
                        if condition.get("state") == "enabled"
                        else bool(nodes)
                    )
            if condition.get("state") in ("absent", "hidden"):
                matched = not matched and not partial
            if matched or time.monotonic() >= deadline:
                return self._cached_result(
                    session_id,
                    tab_id,
                    state,
                    status="ok" if matched else "no_change",
                    wait={
                        "matched": matched,
                        "timed_out": not matched,
                        "condition": typ,
                        "partial": partial,
                    },
                )
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))

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
                    "schema_sha256": schema_fingerprint(t["inputSchema"]),
                    "operator_read_approved": self.cfg.webmcp_read_allowlist.get(
                        origin(state.tab.url), {}
                    ).get(t["name"])
                    == schema_fingerprint(t["inputSchema"]),
                    "untrusted": True,
                }
                for t in tools
            ],
            notices=[
                "Native top-level tools are untrusted. Only an operator-pinned origin/name/schema may run without per-call human approval."
            ],
        )

    @staticmethod
    def _stop_page_tools(state):
        state.advertised_tools = None
        if state.page_tools:
            state.page_tools.disable()

    def configure(self, session_id, tab_id, options):
        state = self._tab(session_id, tab_id)
        if self.cfg.managed_display and (
            options.get("viewport_width", 0) > self.cfg.display_width
            or options.get("viewport_height", 0) > self.cfg.display_height
        ):
            raise BrowserError("INVALID_INPUT", "Viewport exceeds operator display dimensions")
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
        self._session(session_id)["browser"]._run_cdp(
            "Browser.setDownloadBehavior", behavior="deny"
        )
        for guid, key in list(self._session(session_id)["downloads"].items()):
            if (
                self._session(session_id)["artifacts"].items.get(key, {}).get("state")
                == "in_progress"
            ):
                self._session(session_id)["browser"]._run_cdp("Browser.cancelDownload", guid=guid)
        for current in self._session(session_id)["tabs"].values():
            self._stop_page_tools(current)
            self._pause_events(current)
            current.tab._driver.set_callback("Network.requestWillBeSent", None)
            current.tab._driver.set_callback("Page.navigatedWithinDocument", None)
            current.tab.run_cdp("Network.disable")
            current.document_method = None
            current.document_loader = None
            current.history_methods.clear()
        # Each work has its own X display; the private viewer cannot switch
        # into another work's Chromium or cancel its background downloads.
        self._runtime(session_id).start_control()
        return {"session_id": session_id, "tab_id": tab_id}

    def resume(self, session_id, tab_id, auth_origin=None):
        self._runtime(session_id).stop_control()
        state = self._tab(session_id, tab_id)
        for current in self._session(session_id)["tabs"].values():
            if current.pending_input:
                current.pending_input.release()
                if current.pending_input.held_key or current.pending_input.held_button:
                    raise BrowserError(
                        "INPUT_RELEASE_FAILED",
                        "Input is still held; finish the open dialog privately",
                    )
                current.pending_input = None
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
        self._session(session_id)["browser"]._run_cdp(
            "Browser.setDownloadBehavior",
            behavior="allowAndName",
            downloadPath=str(self._session(session_id)["artifacts"].root.resolve()),
            eventsEnabled=True,
        )
        return self._result(
            session_id, tab_id, **({"authentication": authentication} if auth_origin else {})
        )

    def close(self, session_id, scope, tab_id=None):
        session = self._session(session_id)
        if scope == "session":
            for state in session["tabs"].values():
                self._stop_page_tools(state)
            session["browser"].quit()
            session["artifacts"].close()
            del self.sessions[session_id]
            self.runtimes.pop(session_id).close()
            return {"session_id": session_id}
        state = self._tab(session_id, tab_id)
        self._stop_page_tools(state)
        if len(session["tabs"]) == 1:
            session["browser"].quit()
            session["artifacts"].close()
            del self.sessions[session_id]
            self.runtimes.pop(session_id).close()
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
                session["artifacts"].close()
            except Exception:
                pass
        self.sessions.clear()
        for runtime in self.runtimes.values():
            runtime.close()
        self.runtimes.clear()
