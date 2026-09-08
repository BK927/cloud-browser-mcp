"""Small event journal. Authentication disables collection and clears records."""

import hashlib
import secrets
import threading
import time
from collections import deque

from .security import SENSITIVE, TOKEN, redact, safe_url


class Events:
    def __init__(self):
        self.lock = threading.RLock()
        self.enabled = True
        self.dialog = None
        self.chooser = None
        self.records = deque(maxlen=64)
        self.sequence = 0

    def append(self, kind, level="info", url=None):
        with self.lock:
            if not self.enabled:
                return
            self.sequence += 1
            self.records.append(
                dict(
                    sequence=self.sequence,
                    timestamp=time.time(),
                    kind=kind,
                    level=level,
                    url=safe_url(url) if url else None,
                )
            )

    def opened(self, message, type, url=None, **kwargs):
        with self.lock:
            if not self.enabled:
                return
            protected = bool(SENSITIVE.search(message) or TOKEN.search(message))
            self.dialog = {
                "dialog_id": "dialog_" + secrets.token_hex(16),
                "type": type,
                "message": "[Protected prompt: use private authentication]"
                if protected
                else redact(message[:2000]),
                "sensitive": protected,
                "url": safe_url(url or ""),
                "_binding": hashlib.sha256(
                    (str(type) + "\0" + message + "\0" + str(url)).encode()
                ).hexdigest(),
            }
            self.append("dialog_opened")

    def closed(self, **kwargs):
        with self.lock:
            self.dialog = None
            self.append("dialog_closed")

    def file_chooser(self, backendNodeId=None, frameId=None, mode=None, **kwargs):
        with self.lock:
            if self.enabled:
                self.chooser = {"backend": backendNodeId, "frame": frameId, "mode": mode}
                self.chooser["node_id"] = "node_" + secrets.token_urlsafe(16)
                self.append("file_chooser")

    def console(self, type=None, **kwargs):
        # Site console arguments/stack locals can contain arbitrary credentials.
        # Retain bounded event-level diagnostics, not uninspectable payloads.
        self.append("console", str(type or "log")[:40])

    def exception(self, exceptionDetails=None, **kwargs):
        self.append("javascript_exception", "error", (exceptionDetails or {}).get("url"))

    def read(self, after=0, limit=50):
        with self.lock:
            records = list(self.records)
            selected = [r for r in records if r["sequence"] > after]
            return dict(
                records=selected[:limit],
                next_sequence=selected[min(limit, len(selected)) - 1]["sequence"]
                if selected
                else after,
                truncated=len(selected) > limit,
                lost_before=records[0]["sequence"]
                if records and after < records[0]["sequence"] - 1
                else None,
                payload_policy="console arguments and exception locals withheld",
            )

    def pause(self):
        with self.lock:
            self.enabled = False
            self.dialog = self.chooser = None
            self.records.clear()
