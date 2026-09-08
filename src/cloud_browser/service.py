import asyncio
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from .approval import PASSIVE_ACTIONS
from .authentication import MANUAL_METHODS
from .config import Settings
from .models import BrowserError, response
from .ownership import check_ownership, durable_owner, new_ownership
from .resources import memory_state
from .security import SENSITIVE, TOKEN, origin, redact, validate_url
from .store import Store
from .uploads import Uploads
from .worker import Worker


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


class BrowserService:
    def __init__(self, settings: Settings, store: Store, worker=None):
        self.cfg, self.store = settings, store
        self.worker = worker or Worker(settings)
        self.lock = asyncio.Lock()
        self.sessions = {}
        self.pending = {}
        self.leases = {}
        self.control_disconnectors = set()
        self.uploads = Uploads(settings)
        self.owners = {}
        self.tab_cache = {}
        self.tasks = set()
        self.operations = {}
        self.upload_owners = {}

    async def stage_upload(self, source):
        async with self.lock:
            self._admit()
            try:
                result = await self.uploads.stage(source)
                # Private staging explicitly belongs to the current exclusive job.
                self.upload_owners[result["upload_id"]] = next(iter(self.sessions), None)
                return result
            except (OSError, KeyError) as exc:
                raise BrowserError(
                    "UPLOAD_FAILED", "Private file staging failed; check storage configuration"
                ) from exc

    async def discard_upload(self, upload_id):
        async with self.lock:
            self.uploads.discard(upload_id)

    def resources(self, admission=0):
        return memory_state(self.cfg.memory_reserve_mb, admission)

    def _admit(self, admission=0):
        state = self.resources(admission)
        if not state["can_admit"]:
            raise BrowserError(
                "RESOURCE_PRESSURE",
                "Insufficient memory headroom; reuse or close a tab, or reduce capture size",
                resources=state,
            )

    def _session(self, sid):
        state = self.sessions.get(sid)
        if state is None:
            saved = self.store.get("session", sid or "")
            code = (
                "SESSION_EXPIRED" if saved and saved["state"] != "closed" else "SESSION_NOT_FOUND"
            )
            if saved and saved.get("reason") == "last_tab_closed":
                code = "SESSION_CLOSED"
            raise BrowserError(
                code,
                "Session is not active; state has not been silently recreated",
                termination_reason=(saved or {}).get("reason"),
            )
        if state["expires"] <= time.time():
            raise BrowserError("SESSION_EXPIRED", "Session expired; explicitly open a new session")
        return state

    def _remember_session(self, sid, state, reason):
        self.store.put(
            "session",
            sid,
            {"state": state, "reason": reason, "owner": durable_owner(self.owners.get(sid))},
        )

    def _lease(self, sid):
        lease = self.leases.get(sid)
        # An expired lease stays locked until human completion/cancellation. Auto-unlock
        # could reveal credentials left onscreen. Websocket access still expires.
        return lease if lease and lease["state"] == "active" else None

    def _check_control(self, sid, observation=False):
        lease = self._lease(sid)
        if lease:
            raise BrowserError(
                "AUTH_IN_PROGRESS" if lease["kind"] == "auth" else "USER_CONTROL_ACTIVE",
                "User controls this browser session; poll browser_status",
                "user_action_required",
            )

    def _check_uncertain(self, sid):
        if self._session(sid)["uncertain"]:
            raise BrowserError(
                "RESULT_UNCERTAIN",
                "Resolve the previous action through manual control before further actions or navigation",
            )

    async def _rpc(self, method, **args):
        try:
            result = await self.worker.call(method, **args)
            sid = args.get("session_id")
            if sid and "tabs" in result:
                self.tab_cache[sid] = {
                    k: result[k] for k in ("tabs", "selected_tab_id") if k in result
                }
            elif sid and result.get("tab_id") and result.get("page"):
                cached = self.tab_cache.setdefault(sid, {"tabs": []})
                tabs = cached["tabs"]
                tid = result["tab_id"]
                row = {"tab_id": tid, **result["page"]}
                cached["tabs"] = [x for x in tabs if x["tab_id"] != tid] + [row]
            if method == "close" and sid in self.tab_cache:
                cached = self.tab_cache[sid]
                cached["tabs"] = [x for x in cached["tabs"] if x["tab_id"] != args.get("tab_id")]
            if sid in self.tab_cache:
                self.tab_cache[sid]["tabs_observed_at"] = iso(time.time())
            return result
        except asyncio.CancelledError:
            # A cancelled caller must never leave a pending worker reply for a
            # later command. Invalidate rather than resume uncertain browser state.
            for sid in self.sessions:
                self._remember_session(sid, "expired", "worker_cancelled")
            self.sessions.clear()
            self.leases.clear()
            self.owners.clear()
            self.tab_cache.clear()
            await asyncio.shield(self.worker.shutdown())
            raise
        except BrowserError as exc:
            if exc.code in ("SESSION_EXPIRED", "WORKER_TIMEOUT"):
                for sid in list(self.sessions):
                    self._remember_session(sid, "expired", exc.code.lower())
                self.sessions.clear()
                self.leases.clear()
                self.owners.clear()
                self.tab_cache.clear()
                await self.worker.shutdown()
            elif exc.code == "RESULT_UNCERTAIN":
                sid = args.get("session_id")
                if sid in self.sessions:
                    self.sessions[sid]["uncertain"] = True
            raise

    async def call(self, method, *, _principal=None, lease_id=None, operation_id=None, **args):
        """Public calls supply an authenticated principal. None is private in-process use.

        Keep the entire serialized command alive when an HTTP waiter disappears;
        both worker reply correlation and execution-result recording must finish.
        """
        task = asyncio.create_task(
            self._call_owned(method, _principal, lease_id, operation_id, args)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        # Retrieve exceptions even if the HTTP client has gone away.
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return await asyncio.shield(task)

    async def _call_owned(self, method, principal, lease_id, operation_id, args):
        sid = args.get("session_id")
        key = None
        try:
            if principal is not None:
                if method == "open" and not sid:
                    if any(
                        s["expires"] > time.time() or self._lease(key)
                        for key, s in self.sessions.items()
                    ):
                        raise BrowserError(
                            "BROWSER_BUSY",
                            "Another work lease owns the browser; retry later",
                            retry_after_seconds=15,
                        )
                elif method == "status" and not sid and not lease_id:
                    return response(
                        resources=self.resources(),
                        busy=bool(self.sessions),
                        sessions=[],
                        approvals=[],
                        staged_uploads=[],
                        capabilities=self._capabilities(),
                    )
                else:
                    if not sid and lease_id:
                        sid = next(
                            (
                                key
                                for key, value in self.owners.items()
                                if value["lease_id"] == lease_id
                            ),
                            None,
                        )
                        args["session_id"] = sid
                    owner = self.owners.get(sid) or (
                        self.store.get("session", sid or "") or {}
                    ).get("owner")
                    check_ownership(owner, principal, lease_id)
            if operation_id is not None and (
                not isinstance(operation_id, str) or not 8 <= len(operation_id) <= 128
            ):
                raise BrowserError("INVALID_INPUT", "operation_id must be 8..128 characters")
            # Status never waits behind navigation, capture or user completion.
            if method == "status":
                result = (
                    await self._status(**args)
                    if sid in self.sessions or not operation_id
                    else {"resources": self.resources(), "session_id": sid}
                )
                if operation_id:
                    record = self.operations.get((principal, lease_id, operation_id))
                    result["operation"] = self._operation_output(record)
                return response(**result)
            if len(self.tasks) > 32:
                raise BrowserError(
                    "BROWSER_BUSY",
                    "Command queue is full; poll status before retrying",
                    retry_after_seconds=2,
                )
            key = (principal, lease_id, operation_id) if operation_id else None
            digest = hashlib.sha256(json.dumps([method, args], sort_keys=True).encode()).hexdigest()
            if key and key in self.operations:
                record = self.operations[key]
                if record["digest"] != digest:
                    raise BrowserError(
                        "OPERATION_CONFLICT", "operation_id is bound to different arguments"
                    )
                if record["state"] == "completed":
                    return record["result"] | {"replayed": True}
                return response("no_change", operation=self._operation_output(record))
            if key:
                # Bound result memory; never evict running commands.
                for old_key in list(self.operations):
                    if len(self.operations) < 128:
                        break
                    if self.operations[old_key]["state"] == "completed":
                        del self.operations[old_key]
                if len(self.operations) >= 128:
                    raise BrowserError("BROWSER_BUSY", "Execution result budget is full")
                self.operations[key] = {"digest": digest, "state": "running"}
            result = await self._serialized_call(method, principal, lease_id, **args)
            if key:
                self.operations[key].update(
                    state="completed", result={k: v for k, v in result.items() if k != "_image"}
                )
            return result
        except BrowserError as exc:
            result = self._error_response(exc)
            if key and key in self.operations and self.operations[key]["state"] == "running":
                self.operations[key].update(state="completed", result=result)
            return result

    @staticmethod
    def _operation_output(record):
        if not record:
            return {"state": "not_found"}
        return {k: v for k, v in record.items() if k != "digest"}

    @staticmethod
    def _error_response(exc, sid=None, tid=None):
        recovery = {
            "STALE_NODE": "browser_observe",
            "STALE_REVISION": "browser_observe",
            "STALE_SCREENSHOT": "browser_observe",
            "CURSOR_STALE": "browser_observe",
            "DOM_TARGET_AVAILABLE": "browser_observe",
            "TAB_NOT_FOUND": "browser_list_tabs",
            "SESSION_EXPIRED": "browser_open",
            "SESSION_CLOSED": "browser_open",
            "SESSION_NOT_FOUND": "browser_open",
            "LEASE_REQUIRED": "browser_open",
            "AUTH_REQUIRED": "browser_auth_request",
            "CAPTCHA_REQUIRED": "browser_handoff",
        }
        return response(
            exc.status,
            session_id=sid,
            tab_id=tid,
            error={
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.code
                in (
                    "STALE_NODE",
                    "STALE_REVISION",
                    "STALE_SCREENSHOT",
                    "CURSOR_STALE",
                    "BROWSER_BUSY",
                ),
                "suggested_tool": recovery.get(exc.code, "browser_status"),
            },
            **exc.details,
        )

    @asynccontextmanager
    async def _command_lock(self):
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=46)
        except TimeoutError as exc:
            raise BrowserError(
                "BROWSER_BUSY",
                "Queued command was not dispatched within its wait budget; poll status",
            ) from exc
        try:
            yield
        finally:
            self.lock.release()

    async def _serialized_call(self, method, principal, lease_id, **args):
        async with self._command_lock():
            sid, tid = args.get("session_id"), args.get("tab_id")
            try:
                # Recheck after waiting: another opener may have acquired the lease.
                # Reap timed-out sessions without exposing/observing their pages.
                for old_sid, state in list(self.sessions.items()):
                    if state["expires"] <= time.time() and not self._lease(old_sid):
                        try:
                            await self.worker.call("close", session_id=old_sid, scope="session")
                        except BrowserError:
                            pass
                        self.sessions.pop(old_sid, None)
                        self.leases.pop(old_sid, None)
                        self._remember_session(old_sid, "expired", "lease_expired")
                        self.owners.pop(old_sid, None)
                if principal is not None and method == "open" and not sid and self.sessions:
                    raise BrowserError(
                        "BROWSER_BUSY", "Another work lease owns the browser; retry later"
                    )
                result = await getattr(self, "_" + method)(**args)
                if method == "open" and principal is not None:
                    created_sid = result["session_id"]
                    if not sid:
                        self.owners[created_sid] = new_ownership(principal)
                        self._remember_session(created_sid, "active", "opened")
                    result["lease_id"] = self.owners[created_sid]["lease_id"]
                return response(**result)
            except BrowserError as exc:
                return self._error_response(exc, sid, tid)

    async def _open(self, session_id=None, url=None, new_tab=True):
        if url:
            await asyncio.to_thread(
                validate_url,
                url,
                dns_proxy=self.cfg.browser_proxy if self.cfg.network_isolated else None,
            )
        if session_id:
            self._session(session_id)
            self._check_control(session_id)
            if url:
                self._check_uncertain(session_id)
            if new_tab:
                self._admit(self.cfg.memory_per_tab_mb)
            elif not (await self._rpc("list_tabs", session_id=session_id))["tabs"]:
                self._admit(self.cfg.memory_per_tab_mb)
        else:
            if len(self.sessions) >= self.cfg.max_sessions:
                raise BrowserError(
                    "BROWSER_BUSY",
                    "Another work lease owns the browser; retry later",
                )
            self._admit(self.cfg.memory_per_tab_mb)
            session_id = "ses_" + secrets.token_urlsafe(18)
            self.store.put("session", session_id, {"state": "active"})
            self.sessions[session_id] = {
                "expires": time.time() + self.cfg.session_ttl,
                "uncertain": False,
            }
        try:
            result = await self._rpc("open", session_id=session_id, url=url, new_tab=new_tab)
        except BrowserError:
            # Preserve a possibly created session so status can diagnose it.
            raise
        result["expires_at"] = iso(self.sessions[session_id]["expires"])
        return result

    async def _list_tabs(self, session_id):
        self._session(session_id)
        self._check_control(session_id, observation=True)
        return await self._rpc("list_tabs", session_id=session_id)

    async def _navigate(self, session_id, tab_id, operation, url=None):
        self._session(session_id)
        self._check_control(session_id)
        self._check_uncertain(session_id)
        self._admit()
        return await self._rpc(
            "navigate", session_id=session_id, tab_id=tab_id, operation=operation, url=url
        )

    async def _observe(self, session_id, tab_id, **options):
        self._session(session_id)
        self._check_control(session_id, observation=True)
        resources = self.resources()
        constrained = not resources["can_admit"]
        if constrained and options.get("mode") == "visual":
            raise BrowserError(
                "RESOURCE_PRESSURE",
                "Image capture is unavailable; request bounded semantic or interactive observation",
                resources=resources,
            )
        if constrained:
            options.update(max_chars=min(options.get("max_chars") or 4000, 4000), lightweight=True)
            if options.get("mode", "auto") == "auto":
                options["mode"] = "interactive"
        result = await self._rpc("observe", session_id=session_id, tab_id=tab_id, **options)
        if constrained:
            result.setdefault("notices", []).append(
                "RESOURCE_PRESSURE: bounded fresh observation; capture and broad scanning omitted"
            )
            result["observation"]["resource_limited"] = True
        return result

    async def _configure(self, session_id, tab_id, options):
        self._session(session_id)
        self._check_control(session_id)
        return await self._rpc("configure", session_id=session_id, tab_id=tab_id, options=options)

    async def _list_page_tools(self, session_id, tab_id):
        self._session(session_id)
        self._check_control(session_id, observation=True)
        self._admit()
        return await self._rpc("list_page_tools", session_id=session_id, tab_id=tab_id)

    async def _call_page_tool(
        self, session_id, tab_id, revision, tool_name, arguments, confirmation_token=None
    ):
        def sensitive(value):
            if isinstance(value, dict):
                return any(
                    SENSITIVE.search(k)
                    or k.lower() in ("cookie", "cookies", "token")
                    or sensitive(v)
                    for k, v in value.items()
                )
            return isinstance(value, list) and any(sensitive(v) for v in value)

        if sensitive(arguments):
            raise BrowserError(
                "SENSITIVE_INPUT", "Credentials must not be supplied to page tools", "blocked"
            )
        return await self._act(
            session_id,
            tab_id,
            revision,
            {"type": "page_tool", "tool_name": tool_name, "arguments": arguments},
            confirmation_token,
        )

    async def _act(self, session_id, tab_id, expected_revision, action, confirmation_token=None):
        self._session(session_id)
        self._check_control(session_id)
        self._check_uncertain(session_id)
        engine_action = action
        if action["type"] == "upload":
            if session_id in self.owners and any(
                self.upload_owners.get(uid) != session_id for uid in action["upload_ids"]
            ):
                raise BrowserError(
                    "UPLOAD_NOT_FOUND", "Prepared file does not belong to this work lease"
                )
            try:
                engine_action = action | {"_uploads": self.uploads.resolve(action["upload_ids"])}
            except BrowserError as exc:
                if confirmation_token:
                    raise BrowserError(
                        "CONFIRMATION_STALE", "Approved upload is missing, changed or expired"
                    ) from exc
                raise
        if TOKEN.search(json.dumps(action)):
            raise BrowserError(
                "SENSITIVE_INPUT",
                "Credentials and secret tokens must not be sent through MCP",
                "blocked",
            )
        binding = hashlib.sha256(
            json.dumps([session_id, tab_id, expected_revision, action], sort_keys=True).encode()
        ).hexdigest()
        approval_binding = hashlib.sha256(
            json.dumps([session_id, tab_id, action], sort_keys=True).encode()
        ).hexdigest()
        if self.store.get("execution", binding):
            raise BrowserError(
                "CONFIRMATION_USED" if confirmation_token else "ACTION_ALREADY_DISPATCHED",
                "This exact action and revision were already dispatched; do not request it again",
            )
        if confirmation_token:
            record = self.store.get("approval", confirmation_token)
            if not record or record["binding"] != approval_binding:
                raise BrowserError(
                    "CONFIRMATION_STALE", "Approval expired or does not match this exact action"
                )
            if record["state"] == "consumed":
                raise BrowserError(
                    "CONFIRMATION_USED",
                    "Approval was already consumed; the action will not run twice",
                )
            if record["state"] == "denied":
                raise BrowserError(
                    "CONFIRMATION_DENIED", "User denied this action; do not retry it", "blocked"
                )
            if record["state"] != "approved":
                raise BrowserError(
                    "CONFIRMATION_REQUIRED",
                    "Approval must be granted by the user in the private console",
                    "confirmation_required",
                )
        try:
            prepared = await self._rpc(
                "prepare",
                session_id=session_id,
                tab_id=tab_id,
                expected_revision=expected_revision,
                action=engine_action,
            )
        except BrowserError as exc:
            if confirmation_token and exc.code in (
                "STALE_NODE",
                "STALE_REVISION",
                "STALE_SCREENSHOT",
                "NODE_NOT_FOUND",
                "PAGE_TOOL_STALE",
                "PAGE_TOOL_NOT_FOUND",
                "UPLOAD_CHANGED",
                "UPLOAD_NOT_FOUND",
            ):
                raise BrowserError(
                    "CONFIRMATION_STALE",
                    "Approved page or target changed; observe and request new approval",
                ) from exc
            raise
        if confirmation_token:
            record = self.store.get("approval", confirmation_token)
            if record.get("target_binding") != prepared.get("target_binding"):
                raise BrowserError(
                    "CONFIRMATION_STALE", "Approved target or submitted data changed"
                )
        if prepared["requires_confirmation"] and not confirmation_token:
            # Remove expired proposals before admission; a client cannot grow memory
            # unboundedly by requesting confirmations without using them.
            self.pending = {
                key: item for key, item in self.pending.items() if item["expires"] > time.time()
            }
            for item in self.pending.values():
                record = self.store.get("approval", item["token"])
                if (
                    record
                    and record["binding"] == approval_binding
                    and record.get("target_binding") == prepared.get("target_binding")
                ):
                    if record["state"] == "denied":
                        raise BrowserError(
                            "CONFIRMATION_DENIED",
                            "User denied this action; do not retry it",
                            "blocked",
                        )
                    return {
                        **{k: prepared[k] for k in ("session_id", "tab_id", "revision", "page")},
                        "status": "confirmation_required",
                        "confirmation": item["confirmation"],
                    }
            if len(self.pending) >= 32:
                raise BrowserError(
                    "RESOURCE_PRESSURE", "Too many pending approvals; finish or wait for expiration"
                )
            token = "confirm_" + secrets.token_urlsafe(24)
            review_id = secrets.token_urlsafe(18)
            expires = time.time() + self.cfg.approval_ttl
            confirmation = {
                "confirmation_token": token,
                "summary": f"{action['type']}: {prepared['target']}",
                "current_page": prepared["page"]["url"],
                "destination": prepared.get("destination"),
                "destination_kind": prepared.get("destination_kind", "unknown"),
                "destination_verified": False,
                "data_sent": prepared.get("data_sent")
                or [key for key in ("text", "value", "checked", "keys") if key in action],
                "data_sent_truncated": prepared.get("data_sent_truncated", False),
                "data_sent_verified": False,
                "files": prepared.get("files", []),
                "expires_at": iso(expires),
                "control_url": self.cfg.control_origin + "/",
            }
            if "action_policy" in prepared:
                confirmation["action_policy"] = prepared["action_policy"]
            self.store.put(
                "approval",
                token,
                {
                    "binding": approval_binding,
                    "target_binding": prepared.get("target_binding"),
                    "state": "pending",
                },
                self.cfg.approval_ttl,
            )
            self.pending[review_id] = {
                "session_id": session_id,
                "tab_id": tab_id,
                "token": token,
                "expires": expires,
                "action": action,
                "confirmation": confirmation,
            }
            return {
                **{k: prepared[k] for k in ("session_id", "tab_id", "revision", "page")},
                "status": "confirmation_required",
                "confirmation": confirmation,
            }
        # Ordinary edits need no new-tab reserve. Capture/navigation have their
        # own admission budgets; always preserve cleanup and small observations.
        if confirmation_token:
            # Consume before dispatch, including when dispatch returns an uncertain result.
            with self.store.transaction():
                record = self.store.get("approval", confirmation_token)
                if not record or record["state"] != "approved":
                    raise BrowserError("CONFIRMATION_USED", "Approval cannot be reused")
                record["state"] = "consumed"
                self.store.put("approval", confirmation_token, record, 86400)
                self.store.put(
                    "execution", binding, {"dispatched": True}, max(86400, self.cfg.session_ttl)
                )
            self.pending = {
                key: item
                for key, item in self.pending.items()
                if item["token"] != confirmation_token
            }
        elif action["type"] not in PASSIVE_ACTIONS:
            # Balanced actions still get one dispatch per exact binding, including
            # a no-change result or a lost response. Approval-free is not retry-safe.
            self.store.put(
                "execution", binding, {"dispatched": True}, max(86400, self.cfg.session_ttl)
            )
        result = await self._rpc(
            "act",
            session_id=session_id,
            tab_id=tab_id,
            expected_revision=expected_revision,
            action=engine_action,
        )
        if "action_policy" in prepared:
            result["action_policy"] = prepared["action_policy"]
        return result

    async def approve(self, review_id, approved: bool):
        async with self.lock:
            item = self.pending.get(review_id)
            if not item or item["expires"] <= time.time():
                raise BrowserError("CONFIRMATION_STALE", "Approval request expired")
            token = item["token"]
            record = self.store.get("approval", token)
            if not record or record["state"] != "pending":
                raise BrowserError("CONFIRMATION_STALE", "Approval is no longer pending")
            record["state"] = "approved" if approved else "denied"
            self.store.put("approval", token, record, max(1, item["expires"] - time.time()))
            # Keep the bounded record until expiry so browser_status reports denials.

    async def _start_handoff(self, session_id, tab_id, kind, reason, site_origin=None):
        self._session(session_id)
        self._check_control(session_id)
        if not self.cfg.manual_control_enabled:
            raise BrowserError(
                "HANDOFF_UNAVAILABLE", "Operator has not enabled the private remote-control console"
            )
        if self.cfg.max_sessions != 1:
            raise BrowserError(
                "HANDOFF_UNAVAILABLE",
                "Shared-display manual control requires a single browser session",
            )
        tabs = await self._rpc("list_tabs", session_id=session_id)
        target = next((t for t in tabs["tabs"] if t["tab_id"] == tab_id), None)
        if target is None:
            raise BrowserError("TAB_NOT_FOUND", "Requested tab is closed")
        if site_origin and origin(target["url"]) != site_origin:
            raise BrowserError(
                "AUTH_ORIGIN_MISMATCH", "Authentication origin does not match the current tab"
            )
        rule = self.cfg.auth_rules.get(site_origin) if kind == "auth" else None
        if rule and not MANUAL_METHODS.intersection(rule.supported_methods):
            raise BrowserError(
                "AUTH_METHOD_UNSUPPORTED",
                "Operator configuration identifies only passkey/security-key authentication; forwarding is unsupported",
                "user_action_required",
            )
        await self._rpc("focus", session_id=session_id, tab_id=tab_id)
        lease = {
            "handoff_id": "handoff_" + secrets.token_urlsafe(18),
            "session_id": session_id,
            "tab_id": tab_id,
            "kind": kind,
            "reason": redact(reason),
            "state": "active",
            "expires": time.time() + self.cfg.handoff_ttl,
            "authenticated": None,
            "verification": "not_checked",
            "site_origin": site_origin,
        }
        if kind == "auth":
            lease.update(
                supported_methods=["password", "email_code", "sso"],
                unsupported_methods=["passkey", "security_key"],
                methods_scope="manual_console_capabilities",
                site_methods_verified=False,
            )
            if rule:
                lease.update(
                    supported_methods=[m for m in rule.supported_methods if m in MANUAL_METHODS],
                    methods_scope="operator_configuration",
                    site_methods_verified=False,
                    site_methods_configured=True,
                )
        self.leases[session_id] = lease
        return {
            "status": "user_action_required",
            "session_id": session_id,
            "tab_id": tab_id,
            "auth" if kind == "auth" else "handoff": self._lease_output(lease),
        }

    def _lease_output(self, lease):
        return {k: v for k, v in lease.items() if k != "expires"} | {
            "expires_at": iso(lease["expires"]),
            "control_url": self.cfg.control_origin + "/",
            "automation_paused": lease["state"] == "active",
            "control_access_expired": lease["state"] == "active"
            and lease["expires"] <= time.time(),
        }

    async def _auth_request(self, session_id, tab_id, site_origin):
        return await self._start_handoff(
            session_id,
            tab_id,
            "auth",
            "Authenticate directly; do not send credentials to ChatGPT",
            site_origin,
        )

    async def _handoff(self, session_id, tab_id, reason):
        return await self._start_handoff(session_id, tab_id, "manual", reason)

    async def complete_handoff(self, handoff_id):
        async with self.lock:
            lease = next(
                (item for item in self.leases.values() if item["handoff_id"] == handoff_id), None
            )
            if not lease or lease["state"] != "active":
                raise BrowserError("HANDOFF_NOT_FOUND", "Manual control is not active")
            self._session(lease["session_id"])
            # Gate new desktop connections before disconnecting existing ones. Do not
            # restore automation until both disconnection and fresh observation succeed.
            lease["state"] = "returning"
            try:
                disconnected = await asyncio.wait_for(
                    asyncio.gather(
                        *(close() for close in list(self.control_disconnectors)),
                        return_exceptions=True,
                    ),
                    timeout=5,
                )
                if any(isinstance(result, BaseException) for result in disconnected):
                    raise BrowserError(
                        "CONTROL_DISCONNECT_FAILED", "Private control could not be disconnected"
                    )
                result = await self._rpc(
                    "resume",
                    session_id=lease["session_id"],
                    tab_id=lease["tab_id"],
                    auth_origin=lease["site_origin"] if lease["kind"] == "auth" else None,
                )
                lease["result"] = result
                lease["state"] = "completed"
                lease["verification"] = (
                    "unverified" if lease["kind"] == "auth" else "not_applicable"
                )
                self.sessions[lease["session_id"]]["uncertain"] = False
                if lease["kind"] == "auth":
                    lease.update(result.get("authentication", {}))
            except TimeoutError:
                lease["result"] = {
                    "error": {
                        "code": "CONTROL_DISCONNECT_FAILED",
                        "message": "Private control disconnect timed out",
                    }
                }
            except BrowserError as exc:
                lease["result"] = {"error": {"code": exc.code, "message": exc.message}}
                if lease["kind"] == "auth" and exc.code == "AUTH_FAILED":
                    lease.update(
                        authenticated=False,
                        verification="operator_rule",
                        authentication_outcome="failed",
                    )
            finally:
                if lease["state"] == "returning":
                    lease["state"] = "active" if lease["session_id"] in self.sessions else "failed"
            return self._lease_output(lease)

    async def report_auth_result(self, handoff_id, outcome):
        """Private-console report; the MCP cannot mark its own authentication complete."""
        async with self.lock:
            lease = next((x for x in self.leases.values() if x["handoff_id"] == handoff_id), None)
            if not lease or lease["state"] != "active" or lease["kind"] != "auth":
                raise BrowserError("HANDOFF_NOT_FOUND", "Authentication control is not active")
            if outcome not in ("failed", "unsupported"):
                raise BrowserError("INVALID_INPUT", "Choose failed or unsupported")
            code = "AUTH_FAILED" if outcome == "failed" else "AUTH_METHOD_UNSUPPORTED"
            lease.update(
                authenticated=False if outcome == "failed" else None,
                verification="user_reported",
                authentication_outcome=outcome,
                result={
                    "status": "user_action_required",
                    "error": {
                        "code": code,
                        "message": "User reported authentication failure"
                        if outcome == "failed"
                        else "User reported passkey/security-key-only authentication",
                    },
                },
            )
            return self._lease_output(lease)

    async def renew_handoff(self, handoff_id):
        """Private-console action only; never restores automation or claims login success."""
        async with self.lock:
            lease = next((x for x in self.leases.values() if x["handoff_id"] == handoff_id), None)
            if not lease or lease["state"] != "active":
                raise BrowserError("HANDOFF_NOT_FOUND", "Manual control is not active")
            session = self._session(lease["session_id"])
            lease["expires"] = min(time.time() + self.cfg.handoff_ttl, session["expires"])
            return self._lease_output(lease)

    async def cancel_handoff(self, handoff_id):
        """Cancel by closing the session, not by exposing an unfinished login screen."""
        async with self.lock:
            lease = next((x for x in self.leases.values() if x["handoff_id"] == handoff_id), None)
            if not lease or lease["state"] != "active":
                raise BrowserError("HANDOFF_NOT_FOUND", "Manual control is not active")
            sid = lease["session_id"]
            lease["state"] = "cancelled"
            await asyncio.gather(
                *(close() for close in list(self.control_disconnectors)), return_exceptions=True
            )
            try:
                await self._rpc("close", session_id=sid, scope="session")
            except BrowserError:
                # If closing failed, retain the paused session for human recovery.
                # Fatal worker failures already invalidate sessions inside _rpc.
                if sid in self.sessions:
                    lease["state"] = "active"
                raise
            self.sessions.pop(sid, None)
            self.leases.pop(sid, None)
            self._remember_session(sid, "closed", "manual_cancel")
            self.owners.pop(sid, None)
            self.pending = {key: x for key, x in self.pending.items() if x["session_id"] != sid}
            return {"state": "cancelled", "session_id": sid, "session_closed": True}

    def _capabilities(self):
        return {
            "protocol_contract": "0.4-draft",
            "work_leases": "exclusive-principal-bound",
            "operation_results": "operation_id-and-browser_status",
            "image_content": True,
            "observation_format": "rendered-main-v1",
            "accessibility": "chromium-ax-with-dom-fallback",
            "select_options": True,
            "scroll_containers": True,
            "history_policy": "observed-get-only",
            "duplicate_action_policy": "exact-session-tab-revision-action",
            "pagination": "revision-bound-complete-nodes",
            "approval_policy": "strict-per-action"
            if self.cfg.approval_policy == "strict"
            else "balanced-v2",
            "iframe_screenshot_policy": self.cfg.iframe_screenshot_policy,
            "file_upload_automation": True,
            "file_upload_scope": "private-staged-files-only",
            "page_tools": "native-runtime-dependent" if self.cfg.webmcp_enabled else "disabled",
            "passkey_forwarding": False,
            "manual_control": self.cfg.manual_control_enabled,
            "webmcp": self.cfg.webmcp_enabled,
            "webmcp_testing": self.cfg.webmcp_testing,
            "webmcp_runtime_check": "browser_list_page_tools",
            "iframe_semantic_reading": "bounded-cdp-same-and-cross-origin",
            "iframe_automation": True,
            "frame_observation": "frame-ids-with-explicit-partial-results",
            "authentication_verification": bool(self.cfg.auth_rules),
            "authentication_verification_scope": "operator_rules",
        }

    async def _status(self, session_id=None):
        self.pending = {
            key: item for key, item in self.pending.items() if item["expires"] > time.time()
        }
        approvals = []
        for item in self.pending.values():
            record = self.store.get("approval", item["token"])
            if record and (not session_id or item["session_id"] == session_id):
                approvals.append(
                    {
                        "session_id": item["session_id"],
                        "tab_id": item["tab_id"],
                        "state": record["state"],
                        "summary": item["confirmation"]["summary"],
                        "expires_at": iso(item["expires"]),
                    }
                )
        result = {
            "session_id": session_id,
            "resources": self.resources(),
            "sessions": [],
            "approvals": approvals,
            "staged_uploads": [
                x
                for x in self.uploads.list()
                if not session_id
                or session_id not in self.owners
                or self.upload_owners.get(x["upload_id"]) == session_id
            ],
            "control_url": self.cfg.control_origin + "/",
            "capabilities": self._capabilities(),
        }
        ids = [session_id] if session_id else list(self.sessions)
        for sid in ids:
            state = self._session(sid)
            lease = self.leases.get(sid)
            item = {
                "session_id": sid,
                "expires_at": iso(state["expires"]),
                "result_uncertain": state["uncertain"],
                "control": self._lease_output(lease) if lease else None,
            }
            if not self._lease(sid):
                item.update(self.tab_cache.get(sid, {"tabs": None}))
                item["tabs_cached"] = True
            else:
                item["tabs"] = None  # No URL/title collection during authentication.
            result["sessions"].append(item)
        return result

    async def _close(self, session_id, scope, tab_id=None):
        self._session(session_id)
        self._check_control(session_id)
        result = await self._rpc("close", session_id=session_id, scope=scope, tab_id=tab_id)
        if scope == "session" or result.get("session_closed"):
            self.sessions.pop(session_id, None)
            self.leases.pop(session_id, None)
            self._remember_session(
                session_id, "closed", result.get("termination_reason", "explicit_close")
            )
            self.owners.pop(session_id, None)
            self.tab_cache.pop(session_id, None)
            self.pending = {
                key: x for key, x in self.pending.items() if x["session_id"] != session_id
            }
        return result

    async def reclaim_session(self, session_id):
        """Authenticated private administrator action, never an MCP capability."""
        async with self._command_lock():
            if session_id not in self.sessions:
                raise BrowserError("SESSION_NOT_FOUND", "No active session to reclaim")
            lease = self.leases.get(session_id)
            if lease:
                lease["state"] = "returning"
            await asyncio.wait_for(
                asyncio.gather(*(close() for close in list(self.control_disconnectors))), 5
            )
            try:
                await self._rpc("close", session_id=session_id, scope="session")
            except BrowserError:
                if lease and session_id in self.sessions:
                    lease["state"] = "active"
                raise
            self._remember_session(session_id, "closed", "administrator_reclaimed")
            self.sessions.pop(session_id, None)
            self.owners.pop(session_id, None)
            self.leases.pop(session_id, None)
            self.tab_cache.pop(session_id, None)
            self.pending = {k: v for k, v in self.pending.items() if v["session_id"] != session_id}
            return {"session_closed": True, "termination_reason": "administrator_reclaimed"}

    async def shutdown(self):
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await self.worker.shutdown()
        self.uploads.close()
        for sid in self.sessions:
            self._remember_session(sid, "expired", "server_shutdown")
        self.sessions.clear()
