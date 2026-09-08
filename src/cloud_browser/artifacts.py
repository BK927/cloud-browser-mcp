"""Bounded work-local artifact store. No client path or executable HTML is accepted."""

import base64
import hashlib
import os
import secrets
import threading
import time
from pathlib import Path

from .models import BrowserError
from .security import redact


class Artifacts:
    def __init__(self, root: Path, *, max_bytes=64 * 1048576, file_bytes=16 * 1048576, ttl=1800):
        self.root = root
        self.max_bytes, self.file_bytes, self.ttl = max_bytes, file_bytes, ttl
        self.items = {}
        self.lock = threading.RLock()
        if root.is_symlink() or root.parent.is_symlink():
            raise BrowserError("POLICY_BLOCKED", "Artifact storage cannot be a symlink", "blocked")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key):
        # Every path component comes from an internal registry, never from an MCP path.
        if key not in self.items:
            raise BrowserError(
                "ARTIFACT_NOT_FOUND",
                "Artifact is missing, expired or belongs to another work lease",
            )
        path = self.root / self.items[key]["storage_name"]
        if (
            path.is_symlink()
            or self.root.is_symlink()
            or not path.resolve().is_relative_to(self.root.resolve())
        ):
            raise BrowserError("POLICY_BLOCKED", "Artifact storage changed", "blocked")
        return path

    def reserve(self, name, mime_type, *, storage_name=None, kind="download"):
        with self.lock:
            self.expire()
            if (
                len(self.items) >= 32
                or sum(i["size"] for i in self.items.values()) >= self.max_bytes
            ):
                raise BrowserError(
                    "RESOURCE_PRESSURE", "Artifact budget reached; clean this work's artifacts"
                )
            key = "artifact_" + secrets.token_hex(16)
            storage_name = storage_name or secrets.token_hex(24)
            # Chromium download GUIDs are internal but still constrain their syntax.
            if not storage_name or any(c not in "0123456789abcdefABCDEF-" for c in storage_name):
                raise BrowserError("ARTIFACT_FAILED", "Invalid internal download identifier")
            self.items[key] = dict(
                artifact_id=key,
                storage_name=storage_name,
                name=redact(Path(str(name).replace("\\", "/")).name[:160]),
                mime_type=mime_type,
                kind=kind,
                state="in_progress",
                size=0,
                created_at=time.time(),
                expires_at=time.time() + self.ttl,
            )
            return key

    def progress(self, key, size, state):
        with self.lock:
            if key not in self.items:
                return False
            item = self.items[key]
            item["size"] = max(0, int(size))
            total = sum(i["size"] for i in self.items.values())
            if item["size"] > self.file_bytes or total > self.max_bytes:
                item["state"] = "size_limit"
                return False
            item["state"] = state
            return True

    def put(self, data: bytes, name, mime_type, *, kind="export"):
        if len(data) > self.file_bytes:
            raise BrowserError("RESOURCE_PRESSURE", "Export exceeds artifact file budget")
        with self.lock:
            key = self.reserve(name, mime_type, kind=kind)
            if not self.progress(key, len(data), "in_progress"):
                self.delete(key)
                raise BrowserError("RESOURCE_PRESSURE", "Export exceeds artifact storage budget")
            try:
                with self._path(key).open("xb") as output:
                    output.write(data)
                if os.name == "posix":
                    self._path(key).chmod(0o600)
                self.items[key]["state"] = "completed"
                self.items[key]["sha256"] = hashlib.sha256(data).hexdigest()
                return self.public(self.items[key])
            except BaseException:
                self.delete(key)
                raise

    @staticmethod
    def public(item):
        return {k: v for k, v in item.items() if k != "storage_name"}

    def list(self):
        with self.lock:
            self.expire()
            return [self.public(i) for i in self.items.values()]

    def get(self, key):
        with self.lock:
            self.expire()
            path = self._path(key)
            item = self.items[key]
            if item["state"] != "completed":
                raise BrowserError(
                    "ARTIFACT_NOT_READY", "Download is not complete; inspect artifact state"
                )
            if path.stat().st_size > self.file_bytes:
                raise BrowserError("RESOURCE_PRESSURE", "Artifact file exceeds read budget")
            with path.open("rb") as source:
                data = source.read(
                    self.file_bytes + 1
                    if item["kind"] == "export" and item["mime_type"].startswith("image/")
                    else 400004
                )
            public = self.public(item)
            # Only images exported through the browser's privacy pipeline are emitted.
            if item["kind"] == "export" and item["mime_type"] in ("image/jpeg", "image/png"):
                return {
                    "artifact": public,
                    "_image": {
                        "mimeType": item["mime_type"],
                        "data": base64.b64encode(data).decode(),
                    },
                }
            try:
                import codecs

                text = codecs.getincrementaldecoder("utf-8")().decode(
                    data, final=path.stat().st_size <= len(data)
                )
            except UnicodeError as exc:
                raise BrowserError(
                    "ARTIFACT_BINARY",
                    "Binary download is retained privately; automatic binary/image disclosure is not supported",
                ) from exc
            if "\x00" in text:
                raise BrowserError("ARTIFACT_BINARY", "Binary download is not exposed as text")
            return {
                "artifact": public,
                "text": redact(text[:100000]),
                "truncated": len(text) > 100000 or path.stat().st_size > len(data),
                "untrusted": True,
            }

    def private_download(self, key):
        with self.lock:
            self.expire()
            path = self._path(key)
            if self.items[key]["state"] != "completed":
                raise BrowserError("ARTIFACT_NOT_READY", "Download is not complete")
            if path.stat().st_size > self.file_bytes:
                raise BrowserError("RESOURCE_PRESSURE", "Download exceeds size budget")
            return {"filename": self.items[key]["name"], "bytes": path.read_bytes()}

    def delete(self, key):
        with self.lock:
            path = self._path(key)
            path.unlink(missing_ok=True)
            partial = path.with_name(path.name + ".crdownload")
            if partial.is_symlink():
                raise BrowserError("POLICY_BLOCKED", "Download partial file changed", "blocked")
            partial.unlink(missing_ok=True)
            del self.items[key]

    def expire(self):
        with self.lock:
            for key, item in list(self.items.items()):
                if item["expires_at"] <= time.time():
                    self.delete(key)

    def close(self):
        with self.lock:
            for key in list(self.items):
                self.delete(key)
