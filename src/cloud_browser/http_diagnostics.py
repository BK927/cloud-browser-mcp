"""Opt-in, payload-free public ASGI lifecycle diagnostics. Never an access log."""

import asyncio
import json
import logging
import secrets
import threading
import time
from collections import deque
from datetime import UTC, datetime

METHODS = frozenset({"GET", "POST", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"})
PATHS = frozenset(
    {
        "/mcp",
        "/mcp/",
        "/authorize",
        "/token",
        "/revoke",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    }
)
MAX_PATH_LENGTH = max(map(len, PATHS))
MAX_EVENTS_PER_MINUTE = 120
MAX_ELAPSED_MS = 86_400_000
MAX_RECORD_BYTES = 512


class _QuietHandler(logging.StreamHandler):
    def flush(self):
        try:
            super().flush()
        except Exception:
            # Also quiet during logging shutdown or a stream replacement.
            pass

    def handleError(self, record):
        # Logging failures must not print traceback/exception/record locals.
        pass


def diagnostic_logger():
    # Standalone logger: no root/uvicorn handlers, access log, inherited formatter
    # or disable_existing_loggers setting. uvicorn may remain at WARNING.
    logger = logging.Logger("cloud_browser.http_diagnostics", level=logging.INFO)
    logger.propagate = False
    handler = _QuietHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


class HTTPDiagnostics:
    """Wrap outside PublicGuard only when explicitly enabled by the operator.

    Logs ASGI dispatch, successful send(start), successful final body/trailers,
    and cancellation/failure. No receive interception, message mutation, retries,
    background tasks, request storage or response correlation headers.
    """

    def __init__(self, app, *, logger=None, clock=time.monotonic):
        self.app = app
        self.logger = logger if logger is not None else diagnostic_logger()
        self.clock = clock
        self.events = deque()
        self.rate_lock = threading.Lock()

    def _emit(self, event, request_id, method, path, status, started):
        try:
            now = self.clock()
            with self.rate_lock:
                while self.events and self.events[0] <= now - 60:
                    self.events.popleft()
                if len(self.events) >= MAX_EVENTS_PER_MINUTE:
                    return False
                self.events.append(now)
            record = json.dumps(
                {
                    "ts": datetime.now(UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    "event": event,
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "status": status,
                    "elapsed_ms": max(0, min(MAX_ELAPSED_MS, int((now - started) * 1000))),
                },
                separators=(",", ":"),
                ensure_ascii=True,
            )
            if len(record) <= MAX_RECORD_BYTES:
                self.logger.info(record)
                return True
        except Exception:
            # Diagnostic output is best-effort, never an exception report.
            pass
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method")
        path = scope.get("path")
        method = (
            method if type(method) is str and len(method) <= 7 and method in METHODS else "unknown"
        )
        path = (
            path
            if type(path) is str and len(path) <= MAX_PATH_LENGTH and path in PATHS
            else "unknown"
        )
        started = self.clock()
        request_id = secrets.token_hex(6)  # Server randomness, not any caller ID or data hash.
        if not self._emit("request_received", request_id, method, path, None, started):
            return await self.app(scope, receive, send)
        status = None
        has_started = False
        completed = False
        trailers = False

        def emit(event):
            self._emit(event, request_id, method, path, status, started)

        async def observed_send(message):
            nonlocal status, has_started, completed, trailers
            kind = message["type"]
            await send(message)  # Identical object/bytes; failures propagate unchanged.
            if kind == "http.response.start" and not has_started:
                value = message.get("status")
                status = value if type(value) is int and 100 <= value <= 599 else None
                has_started = True
                trailers = bool(message.get("trailers", False))
                emit("response_started")
            elif (
                has_started
                and not completed
                and (
                    (
                        kind == "http.response.body"
                        and not message.get("more_body", False)
                        and not trailers
                    )
                    or (
                        kind == "http.response.trailers"
                        and trailers
                        and not message.get("more_trailers", False)
                    )
                )
            ):
                completed = True
                emit("response_completed")

        try:
            result = await self.app(scope, receive, observed_send)
        except asyncio.CancelledError:
            emit("request_cancelled")
            raise
        except BaseException:
            emit("request_failed")
            raise
        if not completed:
            emit("request_failed")
        return result
