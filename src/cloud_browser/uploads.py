"""Private-console staging. MCP clients receive handles, never filesystem paths."""

import hashlib
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

from starlette.responses import JSONResponse

from .models import BrowserError
from .security import redact


def verify_file(item):
    """Check trusted staging metadata again, including immediately before browser input."""
    path = Path(item["path"])
    try:
        if (
            path.is_symlink()
            or path.parent.is_symlink()
            or path.parent.parent.is_symlink()
            or datetime.fromisoformat(item["expires_at"]).timestamp() <= time.time()
        ):
            raise BrowserError("UPLOAD_CHANGED", "Staged file changed or expired")
        if path.stat().st_size != item["size"]:
            raise BrowserError("UPLOAD_CHANGED", "Staged file size changed")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != item["sha256"]:
            raise BrowserError("UPLOAD_CHANGED", "Staged file content changed")
    except OSError as exc:
        raise BrowserError("UPLOAD_NOT_FOUND", "Staged file is no longer readable") from exc


class BodyLimit:
    def __init__(self, app, upload_limit):
        self.app, self.upload_limit = app, upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.upload_limit + 65536 if scope["path"] == "/uploads" else 128 * 1024
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                return await JSONResponse({"error": "UPLOAD_TOO_LARGE"}, status_code=413)(
                    scope, receive, send
                )
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return await self.app(scope, bounded_receive, send)


class Uploads:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.data_dir / "uploads"
        self.items = {}

    def _path(self, item):
        path = self.root / item["upload_id"] / item["filename"]
        if self.root.is_symlink() or path.parent.is_symlink() or path.is_symlink():
            raise BrowserError(
                "POLICY_BLOCKED", "Upload storage must not contain symlinks", "blocked"
            )
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise BrowserError("POLICY_BLOCKED", "Invalid upload storage path", "blocked")
        return path

    def discard(self, upload_id):
        item = self.items.get(upload_id)
        if item:
            path = self._path(item)
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except FileNotFoundError:
                pass
            del self.items[upload_id]

    def expire(self):
        for key, item in list(self.items.items()):
            if item["expires"] <= time.time():
                self.discard(key)

    def list(self):
        self.expire()
        return [self.public(item) for item in self.items.values()]

    @staticmethod
    def public(item):
        return {
            key: (redact(value) if key in ("filename", "display_name") else value)
            for key, value in item.items()
            if key != "expires"
        } | {"expires_at": datetime.fromtimestamp(item["expires"], UTC).isoformat()}

    async def stage(self, source):
        self.expire()
        if len(self.items) >= self.cfg.max_staged_uploads:
            raise BrowserError(
                "RESOURCE_PRESSURE", "Staged file limit reached; remove a file first"
            )
        original = Path((source.filename or "upload.bin").replace("\\", "/")).name
        name = (
            "".join(c if c.isalnum() or c in ".-_ " else "_" for c in original)[:120].strip(" .")
            or "upload.bin"
        )
        if name.split(".")[0].upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(10)),
            *(f"LPT{i}" for i in range(10)),
        }:
            name = "file_" + name
        key = "upload_" + secrets.token_hex(16)
        item = {
            "upload_id": key,
            "filename": name,
            "display_name": redact(name),
            "size": 0,
            "sha256": "",
            "expires": time.time() + self.cfg.upload_ttl,
        }
        path = self._path(item)
        path.parent.mkdir(parents=True, mode=0o750)
        self.items[key] = item
        try:
            if os.name == "posix" and not self.cfg.development:
                import grp

                gid = grp.getgrnam("browser").gr_gid
                os.chown(self.root, -1, gid)
                self.root.chmod(0o750)
                os.chown(path.parent, -1, gid)
            digest = hashlib.sha256()
            with path.open("xb") as output:
                while chunk := await source.read(65536):
                    item["size"] += len(chunk)
                    if item["size"] > self.cfg.max_upload_mb * 1048576:
                        raise BrowserError(
                            "UPLOAD_TOO_LARGE", "File exceeds the operator upload budget"
                        )
                    digest.update(chunk)
                    output.write(chunk)
            if os.name == "posix":
                if not self.cfg.development:
                    os.chown(path, -1, gid)
                path.chmod(0o440)
            item["sha256"] = digest.hexdigest()
            return self.public(item)
        except BaseException:
            # Windows read-only mode is not set on failed uploads.
            self.discard(key)
            raise

    def resolve(self, ids):
        self.expire()
        if len(set(ids)) != len(ids):
            raise BrowserError("INVALID_INPUT", "Duplicate staged file handles are not allowed")
        files = []
        for key in ids:
            item = self.items.get(key)
            if not item:
                raise BrowserError(
                    "UPLOAD_NOT_FOUND",
                    "File is missing or expired; prepare it in the private console",
                )
            path = self._path(item)
            if not path.is_file():
                raise BrowserError("UPLOAD_NOT_FOUND", "Staged file is no longer available")
            resolved = self.public(item) | {"path": str(path.resolve())}
            verify_file(resolved)
            files.append(resolved)
        return files

    def close(self):
        for key in list(self.items):
            self.discard(key)
