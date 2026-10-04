import asyncio
import hashlib
import json
import secrets
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from pydantic import ValidationError

from .approval import PASSIVE_ACTIONS
from .authentication import MANUAL_METHODS
from .config import Settings
from .error_policy import PAGE_LIMIT_MESSAGE, error_metadata
from .models import BrowserError, ObservationQuery, WaitCondition, response
from .navigation_pacing import NavigationPacer
from .operation_diagnostics import log_capacity
from .ownership import check_ownership, durable_owner, new_ownership
from .resources import admission_state, memory_state
from .security import SENSITIVE, TOKEN, origin, reader_safe_url, redact, validate_url
from .store import Store
from .uploads import Uploads
from .worker import Worker


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


_error_context = ContextVar("browser_error_context", default=None)


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
        self.navigations = {}
        self.reader_sid = None
        self.reader_tid = None
        self.reader_lock = asyncio.Lock()
        self.read_cache = {}
        self.navigation_pacer = NavigationPacer(settings)
        self.handoff_registered = False
        self.recent_errors = OrderedDict()
        self.diagnosed_errors = OrderedDict()

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
            ttl = self.cfg.reader_idle_ttl if sid == self.reader_sid else self.cfg.session_ttl
            self.sessions[sid].update(expires=now + ttl, last_activity=now)

    def _forget(self, sid, state, reason):
        nav = self.navigations.get(sid)
        if nav:
            self._finish_navigation(
                sid,
                nav,
                self._error_response(
                    BrowserError("NAVIGATION_CANCELLED", "Work closed during navigation"),
                    sid,
                    nav["tab_id"],
                ),
            )
        if sid == self.reader_sid:
            self.reader_sid = self.reader_tid = None
        else:
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
        self._expire_reads()
        self.navigation_pacer.expire()
        if self._active_control():
            return  # Never disturb the shared private desktop, even after expiry.
        for sid, state in list(self.sessions.items()):
            if (
                state["expires"] > time.time()
                or (sid == self.reader_sid and self.reader_lock.locked())
                or self.queued.get(sid)
                or sid in self.navigations
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
            works = [sid for sid in self.sessions if sid != self.reader_sid]
            if session_id is None and len(works) == 1:
                session_id = works[0]
            if session_id is None and works:
                raise BrowserError(
                    "SESSION_REQUIRED", "Choose the work that should receive this file"
                )
            if session_id:
                if session_id == self.reader_sid:
                    raise BrowserError("LEASE_INVALID", "The reader is reserved for public reading")
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
        if state["expires"] <= time.time() and sid not in self.navigations:
            raise BrowserError("SESSION_EXPIRED", "Session expired; explicitly open a new session")
        return state

    def _remember_session(self, sid, state, reason):
        if sid == self.reader_sid:
            return  # Internal reader work never acquires a durable owner/lease record.
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
            if exc.code == "SESSION_EXPIRED" and exc.details.get("failure_scope") == "session":
                sid = args.get("session_id")
                if sid in self.sessions:
                    self._forget(sid, "expired", "browser_disconnected")
                # Only this browser process disappeared; the IPC worker and
                # other isolated works remain valid and are not restarted.
            elif exc.code in ("SESSION_EXPIRED", "WORKER_TIMEOUT"):
                for nav_sid, nav in list(self.navigations.items()):
                    self._finish_navigation(
                        nav_sid,
                        nav,
                        self._error_response(
                            BrowserError(exc.code, "Navigation interrupted by worker failure"),
                            nav_sid,
                            nav["tab_id"],
                        ),
                    )
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
        context = {"deadline": time.monotonic() + 5} if method in ("open", "navigate") else {}
        task = asyncio.create_task(
            self._call_diagnosed(method, _principal, lease_id, operation_id, args, context)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        # Retrieve exceptions even if the HTTP client has gone away.
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        if method not in ("open", "navigate"):
            return await asyncio.shield(task)
        try:
            return await asyncio.wait_for(asyncio.shield(task), 5)
        except TimeoutError:
            if context.get("result"):
                return context["result"]
            # Still queued or validating: abandon before dispatch, without
            # cancelling cleanup/IPC and invalidating another work's browser.
            context["abandoned"] = True
            result = self._error_response(
                BrowserError(
                    "BROWSER_BUSY",
                    "Navigation was not dispatched within the initial response budget; poll status",
                    busy_reason="queue_timeout",
                )
            )
            self._diagnose_result(result, _principal, "browser_" + method)
            return result

    async def _call_owned(self, method, principal, lease_id, operation_id, args, context=None):
        context = context if context is not None else {}
        call_started = time.monotonic()
        sid = args.get("session_id")
        key = None
        created_operation = False
        try:
            if sid is not None and sid == self.reader_sid:
                raise BrowserError("LEASE_INVALID", "The reader is reserved for public reading")
            if method == "read":
                if args.get("read_id") is not None:
                    return await self._read(
                        principal, **args
                    )  # Cached slices do not queue for Chromium.
                if (
                    len(self.tasks) > 32
                    or self.queued.get("reader", 0) >= self.cfg.max_queued_per_work
                ):
                    raise BrowserError(
                        "BROWSER_BUSY", "Reader queue is full", busy_reason="queue_capacity"
                    )
                self.queued["reader"] = self.queued.get("reader", 0) + 1
                try:
                    return await self._read(principal, **args)
                finally:
                    self.queued["reader"] -= 1
                    if not self.queued["reader"]:
                        del self.queued["reader"]
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
                        approvals=self._approval_summaries(principal=principal),
                        staged_uploads=[],
                        capabilities=self._capabilities(),
                        reader=self._reader_status(),
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
                return record.get(
                    "progress_result",
                    response("no_change", operation=self._operation_output(record)),
                ) | {"replayed": True}
            if len(self.tasks) > 32 or self.queued.get(sid, 0) >= self.cfg.max_queued_per_work:
                raise BrowserError(
                    "BROWSER_BUSY",
                    "Command queue is full; poll status before retrying",
                    busy_reason="queue_capacity",
                    retry_after_seconds=2,
                )
            needed = int(bool(key)) + int(method in ("open", "navigate"))
            self._reserve_operations(needed)
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
                result = await self._serialized_call(
                    method, principal, lease_id, _context=context, **args
                )
            finally:
                self.queued[sid] -= 1
                if not self.queued[sid]:
                    del self.queued[sid]
            navigation = result.get("navigation", {})
            context["result"] = result
            if navigation.get("pending"):
                nav_sid = result["session_id"]
                nav = self.navigations[nav_sid]
                nav.update(
                    principal=principal,
                    lease_id=result.get("lease_id", lease_id),
                    result=result,
                    tool="browser_" + method,
                )
                nav_key = (principal, nav["lease_id"], nav["operation_id"])
                if key:
                    nav["keys"].add(key)
                nav["keys"].add(nav_key)
                for journal_key in nav["keys"]:
                    self.operations[journal_key] = {
                        "digest": digest,
                        "state": "running",
                        "progress_result": result,
                    }
                task = asyncio.create_task(self._drive_navigation(nav_sid, nav))
                nav["task"] = task
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                try:
                    await asyncio.wait_for(
                        nav["done"].wait(),
                        timeout=max(0.001, 5 - (time.monotonic() - call_started)),
                    )
                except TimeoutError:
                    pass
                return nav["result"]
            if key:
                self.operations[key].update(
                    state="completed", result={k: v for k, v in result.items() if k != "_image"}
                )
                self.operations[key].pop("progress_result", None)
            if context.get("key") in self.operations:
                self.operations[context["key"]].update(state="completed", result=result)
                self.operations[context["key"]].pop("progress_result", None)
            return result
        except BrowserError as exc:
            result = self._error_response(exc)
            if (
                created_operation
                and key in self.operations
                and self.operations[key]["state"] == "running"
            ):
                self.operations[key].update(state="completed", result=result)
                self.operations[key].pop("progress_result", None)
            if context.get("key") in self.operations:
                self.operations[context["key"]].update(state="completed", result=result)
                self.operations[context["key"]].pop("progress_result", None)
            context["result"] = result
            return result

    def _reader_status(self):
        state = self.sessions.get(self.reader_sid)
        return {"active": bool(state), "idle_expires_at": iso(state["expires"]) if state else None}

    def _expire_reads(self):
        now = time.time()
        self.read_cache = {
            rid: item for rid, item in self.read_cache.items() if item["expires"] > now
        }

    def _read_slice(self, rid, item, offset, max_chars):
        cached = item["read"]
        end = min(len(cached["text"]), offset + max_chars)
        return response(
            page=item["page"],
            notices=item["notices"],
            read=cached
            | {
                "read_id": rid,
                "text": cached["text"][offset:end],
                "offset": offset,
                "next_offset": end if end < len(cached["text"]) else None,
                "links": cached["links"] if offset == 0 else [],
            },
        )

    async def _read(
        self, principal, url=None, read_id=None, offset=0, max_chars=None, selector=None
    ):
        if not self.cfg.reader_enabled:
            raise BrowserError("INVALID_INPUT", "Public reading is disabled")
        if (url is None) == (read_id is None):
            raise BrowserError("INVALID_INPUT", "Exactly one of url and read_id is required")
        max_chars = 20000 if max_chars is None else max_chars
        if type(offset) is not int or offset < 0:
            raise BrowserError("INVALID_INPUT", "offset must be a nonnegative integer")
        if type(max_chars) is not int or not 1000 <= max_chars <= 100000:
            raise BrowserError("INVALID_INPUT", "max_chars must be 1000..100000")
        try:
            query = (
                ObservationQuery(selector=selector).model_dump(exclude_none=True)
                if selector is not None
                else None
            )
        except ValidationError as exc:
            raise BrowserError("INVALID_INPUT", "Invalid observation selector") from exc
        self._expire_reads()
        self._check_control(None)
        if read_id is not None:
            item = self.read_cache.get(read_id)
            if not item or item["principal"] != principal:
                raise BrowserError(
                    "READ_NOT_FOUND", "Read is unknown, expired or belongs to another caller"
                )
            return self._read_slice(read_id, item, offset, max_chars)

        await self._validate_navigation_url(url)
        # Pacing precedes both locks; loading still serializes only reader pages.
        async with (
            self.navigation_pacer.pace(url) as pacing,
            self._command_lock_for_reader(),
        ):
            try:
                async with self._command_lock():
                    self._check_control(None)
                    await self._reap_expired()
                    if self.reader_sid not in self.sessions:
                        if len(self.sessions) >= self.cfg.max_sessions:
                            raise BrowserError(
                                "BROWSER_BUSY",
                                "Work capacity is occupied; retry when a work closes",
                                busy_reason="session_capacity",
                                retry_after_seconds=15,
                                scheduler=self._scheduler(),
                            )
                        self._admit(self.cfg.memory_per_session_mb, "new_session")
                        self.reader_sid = "ses_" + secrets.token_urlsafe(18)
                        self.reader_tid = None
                        self.sessions[self.reader_sid] = {
                            "expires": time.time() + self.cfg.reader_idle_ttl,
                            "last_activity": time.time(),
                            "uncertain": False,
                            "approval_epoch": 0,
                        }
                    sid = self.reader_sid
                    if self.reader_tid is None:
                        opened = await self._rpc(
                            "open", session_id=sid, new_tab=False, profile="reader"
                        )
                        self.reader_tid = opened["tab_id"]
                    tid = self.reader_tid
                    # A private-control interruption may have left the worker job paused.
                    await self._cancel_navigation(sid)
                    await self._rpc("navigation_cancel", session_id=sid, tab_id=tid)
                    self._admit(64, "navigation")
                    result = await self._begin_navigation(
                        sid,
                        tid,
                        "goto",
                        url,
                        int(self.cfg.reader_timeout * 1000),
                        pacing=pacing,
                    )
                    nav = self.navigations.get(sid)
                    if nav:
                        nav["reader"] = True
                if nav:
                    await self._drive_navigation(sid, nav)
                    result = nav["result"]
                error = result.get("error")
                complete = not error
                if error and error["code"] != "NAVIGATION_TIMEOUT":
                    raise BrowserError(error["code"], error["message"], result["status"])
                async with self._command_lock():
                    observed = await self._observe(
                        sid,
                        tid,
                        mode="semantic",
                        max_chars=self.cfg.reader_max_text_chars,
                        query=query,
                        reader_options={
                            "collect_links": True,
                            "max_text_chars": self.cfg.reader_max_text_chars,
                        },
                    )
                    self._touch(sid)
                obs = observed["observation"]
                text = obs.get("semantic_snapshot", "")[: self.cfg.reader_max_text_chars]
                notices = list(observed.get("notices", []))
                if not complete:
                    notices.append(
                        "NAVIGATION_TIMEOUT: loading stopped; returning partial page text"
                    )
                rid = "read_" + secrets.token_urlsafe(24)
                item = {
                    "principal": principal,
                    "expires": time.time() + 600,
                    "page": {
                        "url": reader_safe_url(observed["page"]["url"]),
                        "title": observed["page"]["title"],
                    },
                    "notices": notices,
                    "read": {
                        "complete": complete,
                        "text": text,
                        "total_chars": len(text),
                        "text_capped": bool(
                            obs.get("semantic_truncated") or obs.get("semantic_source_truncated")
                        ),
                        "links": [
                            {
                                "text": redact(link["text"])[:200],
                                "url": reader_safe_url(link["url"]),
                            }
                            for link in obs.get("links", [])
                        ],
                        "links_truncated": bool(obs.get("links_truncated")),
                        "resource_limited": bool(obs.get("resource_limited")),
                        "protected_regions_omitted": bool(obs.get("protected_regions_omitted")),
                        **(
                            {"frame_reading_truncated": obs["frame_reading_truncated"]}
                            if "frame_reading_truncated" in obs
                            else {}
                        ),
                    },
                }
                owned = [
                    key for key, value in self.read_cache.items() if value["principal"] == principal
                ]
                for old in owned[: max(0, len(owned) - 3)]:
                    del self.read_cache[old]
                self.read_cache[rid] = item
                return self._read_slice(rid, item, offset, max_chars)
            except BrowserError as exc:
                if exc.code in ("CAPTCHA_REQUIRED", "BOT_BLOCKED", "PRIVACY_INSPECTION_INCOMPLETE"):
                    exc.details["notices"] = [
                        "The reader profile needs a human check later; manual reader control is planned"
                    ]
                raise
            finally:
                self._touch(self.reader_sid)

    @asynccontextmanager
    async def _command_lock_for_reader(self):
        try:
            await asyncio.wait_for(self.reader_lock.acquire(), self.cfg.command_queue_timeout)
        except TimeoutError as exc:
            raise BrowserError(
                "BROWSER_BUSY", "Reader queue wait budget exceeded", busy_reason="queue_timeout"
            ) from exc
        try:
            yield
        finally:
            self.reader_lock.release()

    @staticmethod
    def _operation_output(record):
        if not record:
            return {"state": "not_found"}
        result = {k: v for k, v in record.items() if k not in ("digest", "progress_result")}
        source = record.get("progress_result", record.get("result", {}))
        if source.get("navigation"):
            result["navigation"] = source["navigation"]
        return result

    def _reserve_operations(self, needed):
        for key in list(self.operations):
            if len(self.operations) + needed <= 128:
                break
            if self.operations[key]["state"] == "completed":
                del self.operations[key]
        if len(self.operations) + needed > 128:
            raise BrowserError("BROWSER_BUSY", "Execution result budget is full")

    def _navigation_timeout(self, sid=None, tid=None, requested=None):
        value = (
            requested
            if requested is not None
            else self.configurations.get(sid, {})
            .get(tid, {})
            .get("navigation_timeout_ms", int(self.cfg.navigation_timeout * 1000))
        )
        if type(value) is not int or not 1000 <= value <= self.cfg.navigation_max_timeout * 1000:
            raise BrowserError(
                "INVALID_INPUT", "timeout_ms must be 1000..operator navigation ceiling"
            )
        return value

    def _check_navigation(self, sid, tid=None):
        nav = self.navigations.get(sid)
        if nav and (tid is None or tid == nav["tab_id"]):
            raise BrowserError(
                "NAVIGATION_IN_PROGRESS",
                "Navigation is pending; poll browser_status instead of redispatching",
                operation_id=nav["operation_id"],
            )

    async def _begin_navigation(
        self, sid, tid, operation, url, timeout_ms, operation_id=None, pacing=None
    ):
        self._check_navigation(sid)
        result = await self._rpc(
            "navigation_begin",
            session_id=sid,
            tab_id=tid,
            operation=operation,
            url=url,
            timeout_ms=timeout_ms,
        )
        if pacing:
            pacing()
        if not result.get("navigation", {}).get("pending"):
            return result
        op = operation_id or "nav_" + secrets.token_urlsafe(18)
        result["navigation"]["operation_id"] = op
        result["operation_id"] = op
        result.setdefault("notices", []).append(
            "Navigation pending is not a completed load; poll browser_status with this lease and operation_id"
        )
        self.navigations[sid] = {
            "tab_id": tid,
            "operation_id": op,
            "result": result,
            "keys": set(),
            "done": asyncio.Event(),
            "started": time.monotonic(),
            "timeout_ms": timeout_ms,
        }
        return result

    def _finish_navigation(self, sid, nav, result):
        self._touch(sid)  # Active execution, not status polling, renews idle TTL.
        self._diagnose_result(result, nav.get("principal"), nav.get("tool"))
        nav["result"] = result | {"operation_id": nav["operation_id"]}
        for key in nav["keys"]:
            if key in self.operations:
                self.operations[key].update(state="completed", result=nav["result"])
                self.operations[key].pop("progress_result", None)
        if self.navigations.get(sid) is nav:
            self.navigations.pop(sid)
        nav["done"].set()

    async def _drive_navigation(self, sid, nav):
        while not nav["done"].is_set():
            await asyncio.sleep(0.5)
            try:
                remaining = (
                    max(0.01, nav["started"] + nav["timeout_ms"] / 1000 - time.monotonic())
                    if nav.get("reader")
                    else None
                )
                async with self._command_lock(
                    **({"timeout": remaining} if nav.get("reader") else {})
                ):
                    if nav["done"].is_set():
                        break
                    if nav.get("reader"):
                        self._check_control(sid)
                    if self._active_control():
                        continue  # Even readiness probes collect no DOM during private control.
                    try:
                        poll_options = {"readiness": "interactive"} if nav.get("reader") else {}
                        data = await self._rpc(
                            "navigation_poll", session_id=sid, tab_id=nav["tab_id"], **poll_options
                        )
                        result = response(**data)
                    except BrowserError as exc:
                        result = self._error_response(exc, sid, nav["tab_id"])
                    result["operation_id"] = nav["operation_id"]
                    if nav.get("lease_id"):
                        result["lease_id"] = nav["lease_id"]
                    if result.get("navigation", {}).get("pending"):
                        result["navigation"]["operation_id"] = nav["operation_id"]
                        result["notices"].append(
                            "Navigation is pending; this is not load completion"
                        )
                        nav["result"] = result
                        for key in nav["keys"]:
                            if key in self.operations:
                                self.operations[key]["progress_result"] = result
                    else:
                        # A final open result must retain its already issued lease.
                        if nav.get("lease_id"):
                            result["lease_id"] = nav["lease_id"]
                        self._finish_navigation(sid, nav, result)
            except BrowserError as exc:
                if (
                    exc.code == "BROWSER_BUSY"
                    and exc.details.get("busy_reason") == "queue_timeout"
                    and not nav.get("reader")
                ):
                    # The page command was already sent. A delayed readiness probe
                    # is neither a failed navigation nor authority to abandon its
                    # worker job. Keep the journal pending; its next probe enforces
                    # the original deadline without dispatching navigation again.
                    continue
                self._finish_navigation(sid, nav, self._error_response(exc, sid, nav["tab_id"]))

    async def _cancel_navigation(self, sid, tid=None):
        nav = self.navigations.get(sid)
        if nav and (tid is None or tid == nav["tab_id"]):
            try:
                await self._rpc("navigation_cancel", session_id=sid, tab_id=nav["tab_id"])
            except BrowserError as exc:
                if exc.code not in ("TAB_NOT_FOUND", "SESSION_EXPIRED"):
                    raise
            self._finish_navigation(
                sid,
                nav,
                self._error_response(
                    BrowserError(
                        "NAVIGATION_CANCELLED",
                        "Pending navigation cancelled for cleanup or private control",
                    ),
                    sid,
                    nav["tab_id"],
                ),
            )

    async def _call_diagnosed(self, method, principal, lease_id, operation_id, args, context):
        token = _error_context.set((principal, "browser_" + method))
        try:
            result = await self._call_owned(
                method, principal, lease_id, operation_id, args, context
            )
            self._diagnose_result(result)
            if method == "status":
                result["recent_errors"] = list(self.recent_errors.get(principal, ()))
            return result
        finally:
            _error_context.reset(token)

    def _error_response(self, exc, sid=None, tid=None):
        page_limit = exc.code == "PRIVACY_INSPECTION_INCOMPLETE"
        result = response(
            "error" if page_limit else exc.status,
            session_id=sid,
            tab_id=tid,
            error={
                "code": exc.code,
                "message": PAGE_LIMIT_MESSAGE if page_limit else exc.message,
                **error_metadata(
                    exc.code,
                    handoff_available=self.cfg.manual_control_enabled and self.handoff_registered,
                ),
            },
            **exc.details,
        )
        return result

    def _diagnose_result(self, result, principal=None, tool=None):
        if tool is None and (context := _error_context.get()) is not None:
            principal, tool = context
        # Visit only protocol error slots, never arbitrary page-tool JSON or payloads.
        slots = [result]
        for name in ("completion", "follow_up", "wait", "observation"):
            value = result.get(name)
            if isinstance(value, dict):
                slots.append(value)
                if isinstance(value.get("screenshot_omitted"), dict):
                    slots.append({"error": value["screenshot_omitted"]})
        for slot in slots:
            error = slot.get("error")
            if not isinstance(error, dict) or not error.get("code"):
                continue
            error.update(
                error_metadata(
                    error["code"],
                    handoff_available=self.cfg.manual_control_enabled and self.handoff_registered,
                )
            )
            key = (result["request_id"], error["code"])
            if key in self.diagnosed_errors:
                continue  # Replayed operation results are the same error event.
            if len(self.diagnosed_errors) >= 512:
                self.diagnosed_errors.popitem(last=False)
            self.diagnosed_errors[key] = None
            diagnostic = result | {"error": error}
            log_capacity(diagnostic)
            if tool is not None:
                self._remember_error(principal, tool, diagnostic)

    def _remember_error(self, principal, tool, result):
        if principal not in self.recent_errors:
            # Cap principals as well as each principal's history (LRU).
            if len(self.recent_errors) >= 128:
                self.recent_errors.popitem(last=False)
            self.recent_errors[principal] = deque(maxlen=20)
        self.recent_errors.move_to_end(principal)
        error = result["error"]
        self.recent_errors[principal].append(
            {
                "tool": tool,
                "code": error["code"],
                "category": error["category"],
                "request_id": result["request_id"],
                "at": iso(time.time()),
            }
        )

    @asynccontextmanager
    async def _command_lock(self, timeout=None):
        try:
            await asyncio.wait_for(
                self.lock.acquire(),
                timeout=min(self.cfg.command_queue_timeout, timeout)
                if timeout is not None
                else self.cfg.command_queue_timeout,
            )
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

    async def _validate_navigation_url(self, url):
        await asyncio.to_thread(
            validate_url,
            url,
            dns_proxy=self.cfg.browser_proxy if self.cfg.network_isolated else None,
        )

    async def _serialized_call(self, method, principal, lease_id, _context=None, **args):
        self._check_control(args.get("session_id"))
        url = None
        if method == "open" or (method == "navigate" and args.get("operation") == "goto"):
            url = args.get("url")
            if method == "navigate" and not url:
                raise BrowserError("INVALID_URL", "goto requires a URL", reason="missing")
            if url:
                await self._validate_navigation_url(url)
        elif method == "navigate" and args.get("operation") == "reload":
            url = next(
                (
                    tab.get("url")
                    for tab in self.tab_cache.get(args.get("session_id"), {}).get("tabs", [])
                    if tab["tab_id"] == args.get("tab_id")
                ),
                None,
            )
        async with (
            self.navigation_pacer.pace(url, (_context or {}).get("deadline")) as pacing,
            self._command_lock(),
        ):
            sid, tid = args.get("session_id"), args.get("tab_id")
            before_sessions = set(self.sessions)
            try:
                # Existing leases coexist; only one command touches the worker at a time.
                self._check_control(sid)
                if (_context or {}).get("abandoned"):
                    raise BrowserError(
                        "BROWSER_BUSY",
                        "Initial request was abandoned before dispatch",
                        busy_reason="queue_timeout",
                    )
                if method not in (
                    "open",
                    "navigate",
                    "close",
                    "handoff",
                    "auth_request",
                    "list_tabs",
                ):
                    self._check_navigation(sid, tid)
                if method == "open" and not sid:
                    await self._reap_expired()
                self.running = (sid, method)
                if method == "open":
                    result = await self._open(
                        **args, _context=_context, _principal=principal, _pacing=pacing
                    )
                elif method == "navigate":
                    result = await self._navigate(
                        **args,
                        _context=_context,
                        _principal=principal,
                        _lease_id=lease_id,
                        _pacing=pacing,
                    )
                else:
                    result = await getattr(self, "_" + method)(**args)
                self._touch(result.get("session_id", sid))
                if method == "open" and principal is not None:
                    created_sid = result["session_id"]
                    if not sid and created_sid not in self.owners:
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
                    self.owners.setdefault(failed_sid, new_ownership(principal))
                    self._remember_session(failed_sid, "active", "open_failed")
                    return self._error_response(exc, failed_sid) | {
                        "lease_id": self.owners[failed_sid]["lease_id"]
                    }
                return self._error_response(exc, sid, tid)
            finally:
                self.running = None

    def _provisional_navigation(
        self, context, sid, tid, principal, lease_id, operation, timeout_ms
    ):
        if context is None:
            return
        if context.get("abandoned"):
            raise BrowserError(
                "BROWSER_BUSY",
                "Initial request was abandoned before dispatch",
                busy_reason="queue_timeout",
            )
        if context.get("result"):
            return
        self._reserve_operations(1)
        op = "nav_" + secrets.token_urlsafe(18)
        result = response(
            "no_change",
            session_id=sid,
            tab_id=tid,
            lease_id=lease_id,
            operation_id=op,
            navigation={
                "operation": operation,
                "pending": True,
                "phase": "command_response",
                "elapsed_ms": 0,
                "timeout_ms": timeout_ms,
                "operation_id": op,
            },
            notices=[
                "Browser initialization/navigation is pending, not load completion; poll browser_status"
            ],
        )
        key = (principal, lease_id, op)
        context.update(result=result, operation_id=op, key=key)
        self.operations[key] = {
            "digest": "initialization",
            "state": "running",
            "progress_result": result,
        }

    async def _open(
        self,
        session_id=None,
        url=None,
        new_tab=True,
        timeout_ms=None,
        _context=None,
        _principal=None,
        _pacing=None,
    ):
        self._navigation_timeout(requested=timeout_ms)
        self._check_navigation(session_id)
        if (_context or {}).get("abandoned"):
            raise BrowserError(
                "BROWSER_BUSY",
                "Initial request was abandoned before dispatch",
                busy_reason="queue_timeout",
            )
        if session_id:
            self._session(session_id)
            self._check_control(session_id)
            if url:
                self._check_uncertain(session_id)
            self._provisional_navigation(
                _context,
                session_id,
                None,
                _principal,
                self.owners.get(session_id, {}).get("lease_id"),
                "goto",
                self._navigation_timeout(requested=timeout_ms),
            )
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
                "approval_epoch": 0,
            }
            if _principal is not None:
                self.owners[session_id] = new_ownership(_principal)
                self._remember_session(session_id, "active", "opening")
        self._provisional_navigation(
            _context,
            session_id,
            None,
            _principal,
            self.owners.get(session_id, {}).get("lease_id"),
            "goto",
            self._navigation_timeout(requested=timeout_ms),
        )
        try:
            result = await self._rpc(
                "open",
                session_id=session_id,
                url=None,
                new_tab=new_tab,
            )
        except BrowserError:
            # Preserve a possibly created session so status can diagnose it.
            raise
        result["expires_at"] = iso(self.sessions[session_id]["expires"])
        if url:
            result = await self._begin_navigation(
                session_id,
                result["tab_id"],
                "goto",
                url,
                self._navigation_timeout(session_id, result["tab_id"], timeout_ms),
                operation_id=(_context or {}).get("operation_id"),
                pacing=_pacing,
            )
        return result

    async def _list_tabs(self, session_id):
        self._session(session_id)
        self._check_control(session_id, observation=True)
        return await self._rpc("list_tabs", session_id=session_id)

    async def _navigate(
        self,
        session_id,
        tab_id,
        operation,
        url=None,
        timeout_ms=None,
        _context=None,
        _principal=None,
        _lease_id=None,
        _pacing=None,
    ):
        self._session(session_id)
        self._check_control(session_id)
        self._check_uncertain(session_id)
        budget = self._navigation_timeout(session_id, tab_id, timeout_ms)
        self._check_navigation(session_id)
        self._admit(64, "navigation")
        self._provisional_navigation(
            _context, session_id, tab_id, _principal, _lease_id, operation, budget
        )
        return await self._begin_navigation(
            session_id,
            tab_id,
            operation,
            url,
            budget,
            operation_id=(_context or {}).get("operation_id"),
            pacing=_pacing,
        )

    async def _observe(self, session_id, tab_id, **options):
        self._session(session_id)
        self._check_control(session_id, observation=True)
        resources = self.resources()
        text_budget = admission_state(resources, self.cfg, cost_mb=32, operation="observation")
        constrained = not text_budget["can_admit"] or text_budget["pressure_level"] != "normal"
        # The worker admits capture immediately before collecting pixels, using
        # actual page dimensions. A possible image must not suppress fresh text.
        if constrained:
            options.update(max_chars=min(options.get("max_chars") or 4000, 4000), lightweight=True)
        result = await self._rpc("observe", session_id=session_id, tab_id=tab_id, **options)
        if constrained:
            result.setdefault("notices", []).append(
                "RESOURCE_PRESSURE: bounded fresh observation; broad scanning reduced"
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
        if options.get("navigation_timeout_ms") is not None:
            self._navigation_timeout(requested=options["navigation_timeout_ms"])
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
        session = self._session(session_id)
        self._check_control(session_id)
        self._check_uncertain(session_id)
        if completion is not None:
            try:
                completion = WaitCondition.model_validate(completion).model_dump(exclude_none=True)
            except ValidationError as exc:
                raise BrowserError("INVALID_INPUT", "Invalid completion condition") from exc
            if not 0 <= completion_timeout_ms <= 10000:
                raise BrowserError("INVALID_INPUT", "Completion timeout must be 0..10000 ms")
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
            json.dumps(
                [session_id, tab_id, session.get("approval_epoch", 0), action], sort_keys=True
            ).encode()
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
                **({"completion": completion} if completion is not None else {}),
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
                        "confirmation": item["confirmation"] | {"approval_state": record["state"]},
                        "notices": [
                            "The user approved this exact action. Call browser_act again with the same arguments plus confirmation_token before expires_at."
                        ]
                        if record["state"] == "approved"
                        else [],
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
                "approval_state": "pending",
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
                    "session_id": session_id,
                    "approval_epoch": session.get("approval_epoch", 0),
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
        approval_record = None
        approval_expires = None
        removed_pending = {}
        execution_recorded = False
        if confirmation_token:
            # Consume before dispatch, including when dispatch returns an uncertain result.
            with self.store.transaction():
                record = self.store.get("approval", confirmation_token)
                if not record or record["state"] != "approved":
                    raise BrowserError("CONFIRMATION_USED", "Approval cannot be reused")
                approval_record = record.copy()
                approval_expires = self.store.expires_at("approval", confirmation_token)
                record["state"] = "consumed"
                self.store.put("approval", confirmation_token, record, 86400)
                self.store.put(
                    "execution", binding, {"dispatched": True}, max(86400, self.cfg.session_ttl)
                )
                execution_recorded = True
            removed_pending = {
                key: item
                for key, item in self.pending.items()
                if item["token"] == confirmation_token
            }
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
            execution_recorded = True
        try:
            result = await self._rpc(
                "act",
                session_id=session_id,
                tab_id=tab_id,
                expected_revision=expected_revision,
                action=engine_action,
            )
        except BrowserError as exc:
            if (
                exc.code != "RESULT_UNCERTAIN"
                and exc.details.get("action_result", {}).get("performed") is False
            ):
                # Only an explicit worker guarantee of no dispatch permits retry.
                # Never renew the approved window or undo a later revocation.
                with self.store.transaction():
                    if execution_recorded:
                        self.store.delete("execution", binding)
                    if approval_record is not None:
                        current = self.store.get("approval", confirmation_token)
                        remaining = approval_expires - time.time()
                        if current == record and remaining > 0:
                            self.store.put(
                                "approval", confirmation_token, approval_record, remaining
                            )
                            self.pending.update(removed_pending)
            raise
        if "action_policy" in prepared:
            result["action_policy"] = prepared["action_policy"]
        if completion and result.get("action_result", {}).get("performed"):
            try:
                deadline = time.monotonic() + completion_timeout_ms / 1000
                while True:
                    try:
                        waited = await self._wait(
                            session_id,
                            tab_id,
                            completion,
                            max(0, int((deadline - time.monotonic()) * 1000)),
                        )
                        break
                    except BrowserError as exc:
                        remaining = deadline - time.monotonic()
                        if (
                            exc.code
                            not in (
                                "BROWSER_ERROR",
                                "OBSERVATION_FAILED",
                                "NAVIGATION_IN_PROGRESS",
                                "SCREEN_CHANGED",
                            )
                            or remaining <= 0
                        ):
                            raise
                        await asyncio.sleep(min(0.2, remaining))
                        if time.monotonic() >= deadline:
                            raise
                result["completion"] = waited.get("wait")
                if not result["completion"].get("matched"):
                    uncertain = bool(result["completion"].get("partial"))
                    result["status"] = "error"
                    result["error"] = self._error_response(
                        BrowserError(
                            "RESULT_UNCERTAIN" if uncertain else "ACTION_GOAL_NOT_MET",
                            "Action was dispatched but its requested completion condition was not met; observe before any new action",
                        )
                    )["error"]
                    if uncertain:
                        self.sessions[session_id]["uncertain"] = True
            except BrowserError as exc:
                result["completion"] = {
                    "matched": False,
                    "error": {"code": exc.code, "message": exc.message},
                }
                result["status"] = "error"
                result["error"] = self._error_response(
                    BrowserError(
                        "RESULT_UNCERTAIN",
                        "Action was dispatched but completion could not be observed; do not repeat it",
                    )
                )["error"]
                self.sessions[session_id]["uncertain"] = True
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
            session = self._session(item["session_id"])
            if record.get("session_id") != item["session_id"] or record.get(
                "approval_epoch"
            ) != session.get("approval_epoch", 0):
                raise BrowserError("CONFIRMATION_STALE", "Manual control invalidated this approval")
            record["state"] = "approved" if approved else "denied"
            if approved:
                item["expires"] = time.time() + self.cfg.approval_ttl
                item["confirmation"]["expires_at"] = iso(item["expires"])
                item["confirmation"]["approval_state"] = "approved"
                self.store.put("approval", token, record, self.cfg.approval_ttl)
            else:
                self.store.put("approval", token, record, max(1, item["expires"] - time.time()))
            # Keep the bounded record until expiry so browser_status reports denials.

    def _invalidate_approvals(self, session_id):
        """A private-control transition revokes only this work's outstanding approvals."""
        session = self._session(session_id)
        session["approval_epoch"] = session.get("approval_epoch", 0) + 1
        for review_id, item in list(self.pending.items()):
            if item["session_id"] == session_id:
                self.store.delete("approval", item["token"])
                self.pending.pop(review_id)
        # The generation also rejects persisted tokens no longer in pending. Never
        # clear execution records: private control does not make a dispatch retry-safe.

    async def _start_handoff(self, session_id, tab_id, kind, reason, site_origin=None):
        self._session(session_id)
        self._check_control(session_id)
        await self._cancel_navigation(session_id)
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
        # Revoke before focus may pause/start private control. Startup failure must
        # not restore consent granted for the previous automation state.
        self._invalidate_approvals(session_id)
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
            self._invalidate_approvals(lease["session_id"])
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
            "browser_language": self.cfg.browser_language,
            "browser_timezone": self.cfg.browser_timezone,
            "observation_format": "rendered-main-v1",
            "accessibility": "chromium-ax-with-dom-fallback",
            "targeted_query": "bounded-semantic-targets-v1",
            "select_options": True,
            "scroll_containers": True,
            "history_policy": "observed-get-only",
            "navigation_details": "document-identity-and-same-document-events-v1",
            "navigation_default_timeout_ms": int(self.cfg.navigation_timeout * 1000),
            "navigation_max_timeout_ms": int(self.cfg.navigation_max_timeout * 1000),
            "navigation_min_interval_ms": self.cfg.navigation_min_interval_ms,
            "navigation_per_host_per_minute": self.cfg.navigation_per_host_per_minute,
            "navigation_progress": "server-operation-id-browser-status",
            "navigation_poll_max_hz": 2,
            "duplicate_action_policy": "exact-session-tab-revision-action",
            "pagination": "revision-bound-complete-nodes",
            "approval_policy": "strict-per-action"
            if self.cfg.approval_policy == "strict"
            else "balanced-v3",
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
            "observation_privacy": "protected-fields-and-owning-forms",
            "optional_capture_admission": "worker-actual-geometry",
            "node_registry": "session-bounded-backend-metadata",
            "node_registry_bytes": self.cfg.node_registry_bytes,
            "target_state_verification": ["fill", "select", "select_multiple", "check"],
            "typing_events": "ascii-key-events-unicode-text-insertion",
            "ime_composition": False,
            "selection_events": "synthetic-input-change",
            "shadow_dom": "open-composed-tree",
            "closed_shadow_dom": False,
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

    def _approval_summaries(self, session_id=None, *, principal=None):
        self.pending = {
            key: item for key, item in self.pending.items() if item["expires"] > time.time()
        }
        approvals = []
        for item in self.pending.values():
            if session_id and item["session_id"] != session_id:
                continue
            if (
                principal is not None
                and self.owners.get(item["session_id"], {}).get("principal") != principal
            ):
                continue
            record = self.store.get("approval", item["token"])
            if record:
                summary = {
                    "session_id": item["session_id"],
                    "tab_id": item["tab_id"],
                    "state": record["state"],
                    "summary": item["confirmation"]["summary"],
                    "expires_at": iso(item["expires"]),
                }
                if "approval_state" in item["confirmation"]:
                    summary["approval_state"] = record["state"]
                approvals.append(summary)
        return approvals

    async def _status(self, session_id=None):
        approvals = self._approval_summaries(session_id)
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
            "reader": self._reader_status(),
        }
        result["navigations"] = [
            {
                "session_id": sid,
                "tab_id": nav["tab_id"],
                "operation_id": nav["operation_id"],
                **nav["result"].get("navigation", {}),
                "elapsed_ms": max(0, int((time.monotonic() - nav["started"]) * 1000)),
                "timeout_ms": nav["timeout_ms"],
            }
            for sid, nav in self.navigations.items()
            if sid != self.reader_sid and (not session_id or sid == session_id)
        ]
        ids = (
            [session_id] if session_id else [sid for sid in self.sessions if sid != self.reader_sid]
        )
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
        await self._cancel_navigation(session_id, None if scope == "session" else tab_id)
        result = await self._rpc("close", session_id=session_id, scope=scope, tab_id=tab_id)
        if scope == "session" or result.get("session_closed"):
            self._forget(session_id, "closed", result.get("termination_reason", "explicit_close"))
        return result

    async def reclaim_session(self, session_id):
        """Authenticated private administrator action, never an MCP capability."""
        async with self._command_lock():
            if session_id not in self.sessions:
                raise BrowserError("SESSION_NOT_FOUND", "No active session to reclaim")
            await self._cancel_navigation(session_id)
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
        async with self._command_lock():
            for sid in list(self.navigations):
                await self._cancel_navigation(sid)
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
        self.read_cache.clear()
        self.recent_errors.clear()
        self.diagnosed_errors.clear()
