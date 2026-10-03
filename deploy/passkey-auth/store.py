"""Persistent, single-use ceremonies and device credentials for one MCP owner."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS temporary (
                    kind TEXT, id TEXT, value TEXT, expires INTEGER,
                    PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY, public_key TEXT NOT NULL,
                    sign_count INTEGER NOT NULL, label TEXT NOT NULL,
                    device_type TEXT NOT NULL, backed_up INTEGER NOT NULL,
                    created INTEGER NOT NULL, last_used INTEGER NOT NULL DEFAULT 0);
            """)
        os.chmod(path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def put(self, kind: str, value: dict, ttl: int) -> str:
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("DELETE FROM temporary WHERE expires <= ?", (int(time.time()),))
            if db.execute("SELECT count(*) FROM temporary").fetchone()[0] >= 2000:
                raise ValueError("pending_operation_limit")
            db.execute("INSERT INTO temporary VALUES (?,?,?,?)",
                       (kind, digest(token), json.dumps(value), int(time.time()) + ttl))
        return token

    def get(self, kind: str, token: str, consume: bool = False) -> dict | None:
        with self.db() as db:
            if consume:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value,expires FROM temporary WHERE kind=? AND id=?",
                             (kind, digest(token))).fetchone()
            if consume:
                db.execute("DELETE FROM temporary WHERE kind=? AND id=?", (kind, digest(token)))
            if not row or row["expires"] <= int(time.time()):
                return None
            return json.loads(row["value"])

    def session(self, token: str) -> dict | None:
        value = self.get("session", token)
        if value and value.get("credential") and not self.credential(value["credential"]):
            return None
        return value

    def credentials(self) -> list[dict]:
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM credentials ORDER BY created,id")]

    def credential(self, credential_id: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM credentials WHERE id=?", (credential_id,)).fetchone()
            return dict(row) if row else None

    def add_credential(self, credential: dict):
        with self.db() as db:
            if db.execute("SELECT count(*) FROM credentials").fetchone()[0] >= 10:
                raise ValueError("device_limit")
            db.execute("INSERT INTO credentials(id,public_key,sign_count,label,device_type,backed_up,created) "
                       "VALUES (:id,:public_key,:sign_count,:label,:device_type,:backed_up,:created)", credential)

    def update_counter(self, credential_id: str, old: int, new: int) -> bool:
        with self.db() as db:
            return db.execute("UPDATE credentials SET sign_count=?,last_used=? WHERE id=? AND sign_count=?",
                              (new, int(time.time()), credential_id, old)).rowcount == 1

    def delete_credential(self, credential_id: str):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM credentials").fetchone()[0] <= 1:
                raise ValueError("last_device")
            db.execute("DELETE FROM credentials WHERE id=?", (credential_id,))
            for row in db.execute("SELECT id,value FROM temporary WHERE kind='session'").fetchall():
                if json.loads(row["value"]).get("credential") == credential_id:
                    db.execute("DELETE FROM temporary WHERE kind='session' AND id=?", (row["id"],))
