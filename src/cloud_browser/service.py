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
from .operation_diagnostics import log_capacity
from .ownership import check_ownership, durable_owner, new_ownership
from .resources import admission_state, memory_state
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
        self.clipboards = {}
        self.uploads = Uploads(settings)
        self.owners = {}
        self.tab_cache = {}
        self.tasks = set()
        self.operations = {}
        self.upload_owners = {}
        self.configurations = {}
        self.queued = {}
        self.running = None
        self.sweeper = None
        self.cleanup_required = False

    def start(self):
        if self.sweeper is None:
            self.sweeper = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self):
        while True:
            await asyncio.sleep(self.cfg.session_sweep_interval)
            if self.lock.locked() or self.queued or self._active_control():
                continue
            async with self.lock:
                try:
                    await self._reap_expired()
                except BrowserError:
                    self.cleanup_required = True

    def _active_control(self):
        return next((x for sid in self.leases if (x := self._lease(sid))), None)

    def _touch(self, sid):
        if sid in self.sessions:
            now = time.time()
            self.sessions[sid].update(expires=now + self.cfg.session_ttl, last_activity=now)

    def _forget(self, sid, state, reason):
        self._remember_session(sid, state, reason)
        self.sessions.pop(sid, None)
        self.leases.pop(sid, None)
        self.owners.pop(sid, None)
        self.tab_cache.pop(sid, None)
        self.configurations.pop(sid, None)
        self.pending = {k: v for k, v in self.pending.items() if v["session_id"] != sid}
        for upload, owner in list(self.upload_owners.items()):
            if owner == sid:
                self.uploads.discard(upload)
                self.upload_owners.pop(upload, None)

    async def _reap_expired(self):
        if self._active_control():
            return  # Never disturb the shared private desktop, even after expiry.
        for sid, state in list(self.sessions.items()):
            if (
                state["expires"] > time.time()
                or self.queued.get(sid)
                or (self.running and self.running[0] == sid)
            ):
                continue
            try:
                await self._rpc("close", session_id=sid, scope="session")
            except BrowserError as exc:
                if exc.code == "SESSION_EXPIRED" and sid not in self.sessions:
                    continue
                self.cleanup_required = True
                raise BrowserError(
                    "CLEANUP_REQUIRED",
                    "Expired work could not be closed; private administrator cleanup is required",
                    "blocked",
                ) from exc
            self._forget(sid, "expired", "idle_lease_expired")
        self.cleanup_required = False

    def _scheduler(self):
        control = self._active_control()
        expired = sum(s["expires"] <= time.time() for s in self.sessions.values())
        reason = (
            "user_control"
            if control
            else "cleanup_required"
            if self.cleanup_required
            else "cleanup_pending"
            if expired
            else "executing"
            if self.running
            else "session_capacity"
            if len(self.sessions) >= self.cfg.max_sessions
            else "available"
        )
        return {
            "state": reason,
            "active_sessions": len(self.sessions),
            "max_sessions": self.cfg.max_sessions,
            "expired_sessions": expired,
            "queued_commands": max(0, sum(self.queued.values()) - int(self.running is not None)),
            "running_commands": int(self.running is not None),
            "automation_paused": bool(control),
            "can_open_session": len(self.sessions) < self.cfg.max_sessions
            and not control
            and not self.cleanup_required,
            "owned_commands_can_queue": not control,
            "retry_after_seconds": 2 if reason in ("executing", "cleanup_pending") else 15,
        }

    async def stage_upload(self, source, session_id=None):
        async with self.lock:
            if session_id is None and len(self.sessions) == 1:
                session_id = next(iter(self.sessions))
            if session_id is None and self.sessions:
                raise BrowserError(
                    "SESSION_REQUIRED", "Choose the work that should receive this file"
                )
            if session_id:
                self._session(session_id)
            self._check_control(session_id)
            self._admit()
            try:
                result = await self.uploads.stage(source)
                self.upload_owners[result["upload_id"]] = session_id
                return result
            except (OSError, KeyError) as exc:
                raise BrowserError(
                    "UPLOAD_FAILED", "Private file staging failed; check storage configuration"
                ) from exc

    async def discard_upload(self, upload_id):
        async with self.lock:
            self.uploads.discard(upload_id)

    def resources(self, admission=0):
        return admission_state(
            memory_state(self.cfg.memory_reserve_mb, admission), self.cfg, cost_mb=admission
        )

    def _admit(self, admission=0, operation="general"):
        state = admission_state(
            self.resources(admission), self.cfg, cost_mb=admission, operation=operation
        )
        if not state["can_admit"]:
            raise BrowserError(
                "RESOURCE_PRESSURE",
                "Insufficient memory headroom; reuse or close a tab, or reduce capture size",
                resources=state,
                retry_after_seconds=5,
            )
        return state

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
        if state != "active":
            self.clipboards.pop(sid, None)
        self.store.put(
            "session",
            sid,
            {"state": state, "reason": reason, "owner": durable_owner(self.owners.get(sid))},
        )

    def _lease(self, sid):
        lease = self.leases.get(sid)
        # An expired lease stays locked until human completion/cancellation. Auto-unlock
        # could reveal credentials left onscreen. Websocket access still expires.
        return lease if lease and lease["state"] in ("active", "returning") else None

    def _check_control(self, sid, observation=False):
        lease = self._active_control()
        if lease:
            raise BrowserError(
                "AUTH_IN_PROGRESS"
                if lease["kind"] == "auth" and lease["session_id"] == sid
                else "USER_CONTROL_ACTIVE",
                "Private desktop is in use; all automation is paused. Poll browser_status",
                "user_action_required",
                busy_reason="user_control",
                retry_after_seconds=15,
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
            if sid in self.tab_cache and "selected_tab_id" in result:
                cached = self.tab_cache[sid]
                cached["selected_tab_id"] = result["selected_tab_id"]
                for row in cached["tabs"]:
                    row["selected"] = row["tab_id"] == result["selected_tab_id"]
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
        created_operation = False
        try:
            if principal is not None:
                if method == "open" and not sid:
                    pass  # Capacity is checked atomically at dispatch, not for the whole conversation.
                elif method == "status" and not sid and not lease_id:
                    scheduler = self._scheduler()
                    return response(
                        resources=self.resources(),
                        busy=scheduler["state"] != "available",
                        scheduler=scheduler,
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
            if len(self.tasks) > 32 or self.queued.get(sid, 0) >= self.cfg.max_queued_per_work:
                raise BrowserError(
                    "BROWSER_BUSY",
                    "Command queue is full; poll status before retrying",
                    busy_reason="queue_capacity",
                    retry_after_seconds=2,
                )
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
                created_operation = True
            self.queued[sid] = self.queued.get(sid, 0) + 1
            try:
                result = await self._serialized_call(method, principal, lease_id, **args)
            finally:
                self.queued[sid] -= 1
                if not self.queued[sid]:
                    del self.queued[sid]
            if key:
                self.operations[key].update(
                    state="completed", result={k: v for k, v in result.items() if k != "_image"}
                )
            return result
        except BrowserError as exc:
            result = self._error_response(exc)
            if (
                created_operation
                and key in self.operations
                and self.operations[key]["state"] == "running"
            ):
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
        result = response(
            exc.status,
            session_id=sid,
            tab_id=tid,
            error={
                "code": exc.code,
                "message": exc.message,
                "category": "capacity"
                if exc.code in ("BROWSER_BUSY", "RESOURCE_PRESSURE")
                else "browser",
                "retryable": exc.code
                in (
                    "STALE_NODE",
                    "STALE_REVISION",
                    "STALE_SCREENSHOT",
                    "CURSOR_STALE",
                    "BROWSER_BUSY",
                    "RESOURCE_PRESSURE",
                ),
                "suggested_tool": recovery.get(exc.code, "browser_status"),
            },
            **exc.details,
        )
        # Payload-free correlation: no URLs, identifiers, arguments or credentials.
        # Request IDs in tool responses now have an exact match in operational logs.
        log_capacity(result)
        return result

    @asynccontextmanager
    async def _command_lock(self):
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=self.cfg.command_queue_timeout)
        except TimeoutError as exc:
            raise BrowserError(
                "BROWSER_BUSY",
                "Queued command was not dispatched within its wait budget; poll status",
                busy_reason="queue_timeout",
                retry_after_seconds=2,
            ) from exc
        try:
            yield
        finally:
            self.lock.release()

    async def _serialized_call(self, method, principal, lease_id, **args):
        async with self._command_lock():
            sid, tid = args.get("session_id"), args.get("tab_id")
            before_sessions = set(self.sessions)
            try:
                # Existing leases coexist; only one command touches the worker at a time.
                self._check_control(sid)
                if method == "open" and not sid:
                    await self._reap_expired()
                self.running = (sid, method)
                result = await getattr(self, "_" + method)(**args)
                self._touch(result.get("session_id", sid))
                if method == "open" and principal is not None:
                    created_sid = result["session_id"]
                    if not sid:
                        self.owners[created_sid] = new_ownership(principal)
                        self._remember_session(created_sid, "active", "opened")
                    result["lease_id"] = self.owners[created_sid]["lease_id"]
                if result.get("session_id") in self.sessions:
                    result["expires_at"] = iso(self.sessions[result["session_id"]]["expires"])
                return response(**result)
            except BrowserError as exc:
                created = set(self.sessions) - before_sessions
                if method == "open" and not sid and principal is not None and len(created) == 1:
                    # Even a partial/failed open belongs to its caller, so it can
                    # inspect or close that exact work without global disclosure.
                    failed_sid = created.pop()
                    self.owners[failed_sid] = new_ownership(principal)
                    self._remember_session(failed_sid, "active", "open_failed")
                    return self._error_response(exc, failed_sid) | {
                        "lease_id": self.owners[failed_sid]["lease_id"]
                    }
                return self._error_response(exc, sid, tid)
            finally:
                self.running = None

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
                self._admit(self.cfg.memory_per_tab_mb, "new_tab")
            elif not (await self._rpc("list_tabs", session_id=session_id))["tabs"]:
                self._admit(self.cfg.memory_per_tab_mb, "new_tab")
        else:
            if len(self.sessions) >= self.cfg.max_sessions:
                raise BrowserError(
                    "BROWSER_BUSY",
                    "Work capacity is occupied; keep your own lease, or retry when a work closes. Do not join another work",
                    busy_reason="session_capacity",
                    retry_after_seconds=15,
                    scheduler=self._scheduler(),
                )
            self._admit(self.cfg.memory_per_session_mb, "new_session")
            session_id = "ses_" + secrets.token_urlsafe(18)
            self.store.put("session", session_id, {"state": "active"})
            self.sessions[session_id] = {
                "expires": time.time() + self.cfg.session_ttl,
                "uncertain": False,
                "last_activity": time.time(),
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
        self._admit(64, "navigation")
        return await self._rpc(
            "navigate", session_id=session_id, tab_id=tab_id, operation=operation, url=url
        )

    async def _observe(self, session_id, tab_id, **options):
        self._session(session_id)
        self._check_control(session_id, observation=True)
        resources = self.resources()
        text_budget = admission_state(resources, self.cfg, cost_mb=32, operation="observation")
        constrained = not text_budget["can_admit"] or text_budget["pressure_level"] != "normal"
        if options.get("mode", "auto") == "auto":
            cost = self._capture_cost(session_id, tab_id, options.get("full_page", False))
            capture = admission_state(resources, self.cfg, cost_mb=cost, operation="capture")
            constrained = constrained or not capture["can_admit"]
        if options.get("mode") == "visual":
            self._admit(
                self._capture_cost(session_id, tab_id, options.get("full_page", False)), "capture"
            )
            constrained = False
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

    def _capture_cost(self, sid, tid, full_page=False):
        configuration = self.configurations.get(sid, {}).get(tid, {})
        pixels = (
            self.cfg.max_capture_pixels
            if full_page
            else configuration.get("viewport_width", 1024)
            * configuration.get("viewport_height", 768)
        )
        return 32 + (pixels * 16 + 1048575) // 1048576

    async def _configure(self, session_id, tab_id, options):
        self._session(session_id)
        self._check_control(session_id)
        result = await self._rpc("configure", session_id=session_id, tab_id=tab_id, options=options)
        self.configurations.setdefault(session_id, {}).setdefault(tab_id, {}).update(options)
        return result

    async def _list_page_tools(self, session_id, tab_id):
        self._session(session_id)
        self._check_control(session_id, observation=True)
        self._admit(32, "page_tools")
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

    async def _act(
        self,
        session_id,
        tab_id,
        expected_revision,
        action,
        confirmation_token=None,
        completion=None,
        completion_timeout_ms=5000,
        follow_up=False,
    ):
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
        if completion and result.get("action_result", {}).get("performed"):
            try:
                waited = await self._wait(session_id, tab_id, completion, completion_timeout_ms)
                result["completion"] = waited.get("wait")
            except BrowserError as exc:
                result["completion"] = {
                    "matched": False,
                    "error": {"code": exc.code, "message": exc.message},
                }
        if follow_up and not result.get("dialog"):
            try:
                observed = await self._observe(
                    session_id, tab_id, mode="interactive", max_chars=2000, lightweight=True
                )
                result["follow_up"] = observed.get("observation")
                result["revision"] = observed.get("revision", result.get("revision"))
            except BrowserError as exc:
                result["follow_up"] = {"error": {"code": exc.code, "message": exc.message}}
        return result

    async def _wait(self, session_id, tab_id, condition, timeout_ms=5000):
        self._session(session_id)
        self._check_control(session_id, observation=True)
        if not 0 <= timeout_ms <= 10000:
            raise BrowserError("INVALID_INPUT", "Wait timeout must be 0..10000 ms")
        return await self._rpc(
            "wait", session_id=session_id, tab_id=tab_id, condition=condition, timeout_ms=timeout_ms
        )

    async def _logs(self, session_id, tab_id, after=0, limit=50):
        self._session(session_id)
        self._check_control(session_id, observation=True)
        return await self._rpc(
            "logs", session_id=session_id, tab_id=tab_id, after=after, limit=limit
        )

    async def _artifacts(
        self, session_id, operation="list", artifact_id=None, tab_id=None, format="text"
    ):
        self._session(session_id)
        self._check_control(session_id, observation=operation in ("list", "get", "export"))
        if operation == "export" and format == "image":
            self._admit(self._capture_cost(session_id, tab_id), "capture")
        return await self._rpc(
            "artifacts",
            session_id=session_id,
            operation=operation,
            artifact_id=artifact_id,
            tab_id=tab_id,
            format=format,
        )

    async def private_download(self, session_id, artifact_id):
        async with self._command_lock():
            self._session(session_id)
            self._check_control(session_id, observation=True)
            return await self._rpc(
                "private_download", session_id=session_id, artifact_id=artifact_id
            )

    async def _dialog(
        self,
        session_id,
        tab_id,
        operation="get",
        dialog_id=None,
        text=None,
        confirmation_token=None,
    ):
        self._session(session_id)
        self._check_control(session_id, observation=operation == "get")
        info = await self._rpc("dialog_info", session_id=session_id, tab_id=tab_id)
        if operation == "get":
            return info
        if not dialog_id:
            raise BrowserError("INVALID_INPUT", "A previously observed dialog_id is required")
        return await self._act(
            session_id,
            tab_id,
            info["revision"],
            {"type": "dialog", "operation": operation, "dialog_id": dialog_id, "text": text},
            confirmation_token,
        )

    async def _clipboard(
        self,
        session_id,
        operation,
        text=None,
        tab_id=None,
        node_id=None,
        expected_revision=None,
        confirmation_token=None,
    ):
        self._session(session_id)
        self._check_control(session_id, observation=operation in ("read", "copy"))
        if text is not None and (len(text) > 20000 or TOKEN.search(text)):
            raise BrowserError(
                "SENSITIVE_INPUT",
                "Clipboard text is too large or contains a secret token",
                "blocked",
            )
        if operation == "write":
            self.clipboards[session_id] = text or ""
        elif operation == "clear":
            self.clipboards.pop(session_id, None)
        elif operation in ("paste", "copy"):
            if not tab_id or not node_id or expected_revision is None:
                raise BrowserError(
                    "INVALID_INPUT", "Copy/paste requires a tab, observed node and revision"
                )
            if operation == "paste":
                return await self._act(
                    session_id,
                    tab_id,
                    expected_revision,
                    {
                        "type": "fill",
                        "node_id": node_id,
                        "text": self.clipboards.get(session_id, ""),
                    },
                    confirmation_token,
                )
            copied = await self._rpc(
                "clipboard_read",
                session_id=session_id,
                tab_id=tab_id,
                node_id=node_id,
                expected_revision=expected_revision,
            )
            self.clipboards[session_id] = copied["text"]
        return {
            "session_id": session_id,
            "clipboard": {
                "text": self.clipboards.get(session_id, "") if operation == "read" else None,
                "length": len(self.clipboards.get(session_id, "")),
                "scope": "work-local-text-only",
            },
        }

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
        if len(self.sessions) > 1 and not self.cfg.managed_display:
            raise BrowserError(
                "HANDOFF_UNAVAILABLE",
                "Multiple works require managed isolated displays for private control",
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
        self.clipboards.clear()
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
        try:
            await self._rpc("focus", session_id=session_id, tab_id=tab_id)
        except BrowserError as exc:
            # Focus may already have paused collection or started the private
            # display. Keep the work locked until the administrator cancels or
            # completes it; a bridge startup failure is not permission to observe.
            lease["start_error"] = exc.code
            raise
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
            "automation_paused": lease["state"] in ("active", "returning"),
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
            # An idle work TTL must not trap the user inside protected login forever.
            self._touch(lease["session_id"])
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
                for sid in self.sessions:
                    self._touch(sid)  # Shared private control suspended all work.
                self.tab_cache.clear()
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
            self._touch(lease["session_id"])
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
            self._forget(sid, "closed", "manual_cancel")
            return {"state": "cancelled", "session_id": sid, "session_closed": True}

    def _capabilities(self):
        return {
            "protocol_contract": "0.4-draft",
            "work_leases": "isolated-principal-bound",
            "scheduling": "bounded-fifo-command-queue",
            "work_expiry": "idle-ttl-with-background-reaper",
            "memory_admission": self.cfg.memory_policy,
            "manual_control_scope": "isolated-display-global-dispatch-pause",
            "operation_results": "operation_id-and-browser_status",
            "image_content": True,
            "observation_format": "rendered-main-v1",
            "accessibility": "chromium-ax-with-dom-fallback",
            "select_options": True,
            "scroll_containers": True,
            "history_policy": "observed-get-only",
            "navigation_details": "document-identity-and-same-document-events-v1",
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
            "scoped_observation": True,
            "extended_input": [
                "type",
                "modifiers",
                "right_click",
                "middle_click",
                "drag",
                "select_multiple",
            ],
            "condition_wait": "bounded-10s",
            "dialogs": "explicit-human-approved-responses",
            "logs": "bounded-metadata-no-console-arguments",
            "clipboard": "work-local-text-only",
            "artifacts": "bounded-work-local-downloads-and-safe-exports",
            "display_lifecycle": "on-demand" if self.cfg.managed_display else "operator-managed",
            "control_lifecycle": "on-demand-same-browser"
            if self.cfg.managed_display
            else "operator-managed",
            "installation": "native-systemd"
            if self.cfg.native_config
            else "container-or-development",
            "webmcp_read_allowlist": bool(self.cfg.webmcp_read_allowlist),
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
            "scheduler": self._scheduler(),
        }
        ids = [session_id] if session_id else list(self.sessions)
        for sid in ids:
            # Cached status remains available for expired work, especially while
            # a human still owns authentication/control. Never collect its page.
            state = self.sessions[sid] if sid in self.sessions else self._session(sid)
            lease = self.leases.get(sid)
            item = {
                "session_id": sid,
                "expires_at": iso(state["expires"]),
                "work_lease_expired": state["expires"] <= time.time(),
                "last_activity_at": iso(
                    state.get("last_activity", state["expires"] - self.cfg.session_ttl)
                ),
                "work_state": "user_control"
                if self._active_control()
                else "executing"
                if self.running and self.running[0] == sid
                else "queued"
                if self.queued.get(sid)
                else "expired"
                if state["expires"] <= time.time()
                else "idle",
                "result_uncertain": state["uncertain"],
                "control": self._lease_output(lease) if lease else None,
            }
            if not self._active_control():
                item.update(self.tab_cache.get(sid, {"tabs": None}))
                item["tabs_cached"] = True
            else:
                item["tabs"] = None  # No URL/title collection during authentication.
            result["sessions"].append(item)
        return result

    async def _close(self, session_id, scope, tab_id=None):
        # Closing one's expired work is always available, not gated by the idle TTL.
        if session_id not in self.sessions:
            self._session(session_id)
        self._check_control(session_id)
        result = await self._rpc("close", session_id=session_id, scope=scope, tab_id=tab_id)
        if scope == "session" or result.get("session_closed"):
            self._forget(session_id, "closed", result.get("termination_reason", "explicit_close"))
        return result

    async def reclaim_session(self, session_id):
        """Authenticated private administrator action, never an MCP capability."""
        async with self._command_lock():
            if session_id not in self.sessions:
                raise BrowserError("SESSION_NOT_FOUND", "No active session to reclaim")
            lease = self.leases.get(session_id)
            if lease:
                lease["state"] = "returning"
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(close() for close in list(self.control_disconnectors))), 5
                )
            except (TimeoutError, Exception) as exc:
                if lease:
                    lease["state"] = "active"
                raise BrowserError(
                    "CONTROL_DISCONNECT_FAILED",
                    "Private control could not be disconnected; automation remains paused",
                ) from exc
            try:
                await self._rpc("close", session_id=session_id, scope="session")
            except BrowserError:
                if lease and session_id in self.sessions:
                    lease["state"] = "active"
                raise
            self._forget(session_id, "closed", "administrator_reclaimed")
            return {"session_closed": True, "termination_reason": "administrator_reclaimed"}

    async def shutdown(self):
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
            self.sweeper = None
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await self.worker.shutdown()
        self.uploads.close()
        for sid in self.sessions:
            self._remember_session(sid, "expired", "server_shutdown")
        self.sessions.clear()
