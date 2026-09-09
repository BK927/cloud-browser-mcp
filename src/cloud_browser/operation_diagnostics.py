"""Bounded, payload-free capacity diagnostics correlated with MCP request IDs."""

import json
import re
import threading
import time
from collections import deque

from .http_diagnostics import diagnostic_logger

_logger = diagnostic_logger()
_events = deque()
_lock = threading.Lock()
_codes = {
    "BROWSER_BUSY",
    "RESOURCE_PRESSURE",
    "CLEANUP_REQUIRED",
    "USER_CONTROL_ACTIVE",
    "AUTH_IN_PROGRESS",
}
_reasons = {"session_capacity", "queue_capacity", "queue_timeout", "user_control"}


def log_capacity(result):
    try:
        code = (result.get("error") or {}).get("code")
        request_id = result.get("request_id", "")
        if code not in _codes or not re.fullmatch(r"req_[A-Za-z0-9_-]{1,64}", request_id):
            return
        with _lock:
            now = time.monotonic()
            while _events and _events[0] <= now - 60:
                _events.popleft()
            if len(_events) >= 120:
                return
            _events.append(now)
        reason = result.get("busy_reason")
        _logger.info(
            json.dumps(
                {
                    "event": "browser_capacity",
                    "request_id": request_id,
                    "code": code,
                    "reason": reason if reason in _reasons else None,
                }
            )
        )
    except Exception:
        pass  # Diagnostics must never print locals or break the browser result.
