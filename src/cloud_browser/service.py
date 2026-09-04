import asyncio
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime

from .config import Settings
from .models import BrowserError, response
from .resources import memory_state
from .security import TOKEN, origin, redact, validate_url
from .store import Store
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
            raise BrowserError(code, "Session is not active; state has not been silently recreated")
        if state["expires"] <= time.time():
            raise BrowserError("SESSION_EXPIRED", "Session expired; explicitly open a new session")
        return state

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

    async def _rpc(self, method, **args):
        try:
            return await self.worker.call(method, **args)
        except asyncio.CancelledError:
            # A cancelled caller must never leave a pending worker reply for a
            # later command. Invalidate rather than resume uncertain browser state.
            for sid in self.sessions:
                self.store.put("session", sid, {"state": "expired"})
            self.sessions.clear()
            self.leases.clear()
            await asyncio.shield(self.worker.shutdown())
            raise
        except BrowserError as exc:
            if exc.code in ("SESSION_EXPIRED", "WORKER_TIMEOUT"):
                for sid in list(self.sessions):
                    self.store.put("session", sid, {"state": "expired"})
                self.sessions.clear()
                self.leases.clear()
                await self.worker.shutdown()
            elif exc.code == "RESULT_UNCERTAIN":
                sid = args.get("session_id")
                if sid in self.sessions:
                    self.sessions[sid]["uncertain"] = True
            raise

    async def call(self, method, **args):
        async with self.lock:
            sid, tid = args.get("session_id"), args.get("tab_id")
            try:
                # Reap timed-out sessions without exposing/observing their pages.
                for old_sid, state in list(self.sessions.items()):
                    if state["expires"] <= time.time():
                        try:
                            await self.worker.call("close", session_id=old_sid, scope="session")
                        except BrowserError:
                            pass
                        self.sessions.pop(old_sid, None)
                        self.leases.pop(old_sid, None)
                        self.store.put("session", old_sid, {"state": "expired"})
                result = await getattr(self, "_" + method)(**args)
                return response(**result)
            except BrowserError as exc:
                details = dict(exc.details)
                return response(
                    exc.status,
                    session_id=sid,
                    tab_id=tid,
                    error={
                        "code": exc.code,
                        "message": exc.message,
                        "retryable": exc.code in ("STALE_NODE", "STALE_SCREENSHOT", "CURSOR_STALE"),
                        "suggested_tool": "browser_observe"
                        if exc.code in ("STALE_NODE", "STALE_SCREENSHOT", "CURSOR_STALE")
                        else "browser_status",
                    },
                    **details,
                )

    async def _open(self, session_id=None, url=None, new_tab=True):
        if url:
            validate_url(url)
        if session_id:
            self._session(session_id)
            self._check_control(session_id)
            if new_tab:
                self._admit(self.cfg.memory_per_tab_mb)
        else:
            if len(self.sessions) >= self.cfg.max_sessions:
                raise BrowserError(
                    "RESOURCE_PRESSURE",
                    "Operator session budget reached; reuse the existing session",
                    resources=self.resources(),
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
        self._admit()
        return await self._rpc(
            "navigate", session_id=session_id, tab_id=tab_id, operation=operation, url=url
        )

    async def _observe(self, session_id, tab_id, **options):
        self._session(session_id)
        self._check_control(session_id, observation=True)
        self._admit()
        return await self._rpc("observe", session_id=session_id, tab_id=tab_id, **options)

    async def _configure(self, session_id, tab_id, options):
        self._session(session_id)
        self._check_control(session_id)
        return await self._rpc("configure", session_id=session_id, tab_id=tab_id, options=options)

    async def _act(self, session_id, tab_id, expected_revision, action, confirmation_token=None):
        state = self._session(session_id)
        self._check_control(session_id)
        if state["uncertain"]:
            raise BrowserError(
                "RESULT_UNCERTAIN",
                "Resolve the previous action through manual control before further actions",
            )
        if TOKEN.search(json.dumps(action)):
            raise BrowserError(
                "SENSITIVE_INPUT",
                "Credentials and secret tokens must not be sent through MCP",
                "blocked",
            )
        binding = hashlib.sha256(
            json.dumps([session_id, tab_id, expected_revision, action], sort_keys=True).encode()
        ).hexdigest()
        if confirmation_token:
            record = self.store.get("approval", confirmation_token)
            if not record or record["binding"] != binding:
                raise BrowserError(
                    "CONFIRMATION_STALE", "Approval expired or does not match this exact action"
                )
            if record["state"] == "consumed":
                raise BrowserError(
                    "CONFIRMATION_USED",
                    "Approval was already consumed; the action will not run twice",
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
                action=action,
            )
        except BrowserError as exc:
            if confirmation_token and exc.code in (
                "STALE_NODE",
                "STALE_SCREENSHOT",
                "NODE_NOT_FOUND",
            ):
                raise BrowserError(
                    "CONFIRMATION_STALE",
                    "Approved page or target changed; observe and request new approval",
                ) from exc
            raise
        if prepared["requires_confirmation"] and not confirmation_token:
            # Remove expired proposals before admission; a client cannot grow memory
            # unboundedly by requesting confirmations without using them.
            self.pending = {
                key: item for key, item in self.pending.items() if item["expires"] > time.time()
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
                "destination": prepared["page"]["url"],
                "data_sent": [key for key in ("text", "value", "checked", "keys") if key in action],
                "expires_at": iso(expires),
                "control_url": self.cfg.control_origin + "/",
            }
            self.store.put(
                "approval", token, {"binding": binding, "state": "pending"}, self.cfg.approval_ttl
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
        self._admit()
        if confirmation_token:
            # Consume before dispatch, including when dispatch returns an uncertain result.
            with self.store.transaction():
                record = self.store.get("approval", confirmation_token)
                if not record or record["state"] != "approved":
                    raise BrowserError("CONFIRMATION_USED", "Approval cannot be reused")
                record["state"] = "consumed"
                self.store.put("approval", confirmation_token, record, 86400)
            self.pending = {
                key: item
                for key, item in self.pending.items()
                if item["token"] != confirmation_token
            }
        return await self._rpc(
            "act",
            session_id=session_id,
            tab_id=tab_id,
            expected_revision=expected_revision,
            action=action,
        )

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
            if not approved:
                self.pending.pop(review_id, None)

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
            # Close the lease first, terminating gated remote desktop connections.
            lease["state"] = "completed"
            await asyncio.gather(
                *(close() for close in list(self.control_disconnectors)), return_exceptions=True
            )
            lease["verification"] = "unverified" if lease["kind"] == "auth" else "not_applicable"
            self.sessions[lease["session_id"]]["uncertain"] = False
            try:
                result = await self._rpc(
                    "resume", session_id=lease["session_id"], tab_id=lease["tab_id"]
                )
                lease["result"] = result
            except BrowserError as exc:
                lease["result"] = {"error": {"code": exc.code, "message": exc.message}}
            return self._lease_output(lease)

    async def _status(self, session_id=None):
        result = {
            "session_id": session_id,
            "resources": self.resources(),
            "sessions": [],
            "capabilities": {
                "manual_control": self.cfg.manual_control_enabled,
                "webmcp": False,
                "iframe_automation": False,
                "authentication_verification": False,
            },
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
                item.update(await self._rpc("list_tabs", session_id=sid))
            else:
                item["tabs"] = None  # No URL/title collection during authentication.
            result["sessions"].append(item)
        return result

    async def _close(self, session_id, scope, tab_id=None):
        self._session(session_id)
        self._check_control(session_id)
        result = await self._rpc("close", session_id=session_id, scope=scope, tab_id=tab_id)
        if scope == "session":
            self.sessions.pop(session_id, None)
            self.leases.pop(session_id, None)
            self.store.put("session", session_id, {"state": "closed"})
        return result

    async def shutdown(self):
        await self.worker.shutdown()
        for sid in self.sessions:
            self.store.put("session", sid, {"state": "expired"})
        self.sessions.clear()
