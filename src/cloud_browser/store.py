"""Small durable state store. Tokens are indexed by digest, never stored in plaintext."""

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS kv (kind TEXT, key TEXT, value TEXT, expires REAL, PRIMARY KEY(kind,key))"
        )
        self.lock = threading.RLock()
        self.db.execute("CREATE INDEX IF NOT EXISTS kv_expiry ON kv(expires)")
        self.last_cleanup = 0

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def put(self, kind, key, value, ttl=86400 * 365):
        with self.lock:
            if time.time() - self.last_cleanup > 60:
                self.db.execute("DELETE FROM kv WHERE expires < ?", (time.time(),))
                self.last_cleanup = time.time()
            self.db.execute(
                "INSERT OR REPLACE INTO kv VALUES (?,?,?,?)",
                (kind, digest(key), json.dumps(value), time.time() + ttl),
            )

    def get(self, kind, key):
        with self.lock:
            row = self.db.execute(
                "SELECT value,expires FROM kv WHERE kind=? AND key=?", (kind, digest(key))
            ).fetchone()
            return json.loads(row[0]) if row and row[1] > time.time() else None

    def pop(self, kind, key):
        with self.transaction():
            value = self.get(kind, key)
            self.db.execute("DELETE FROM kv WHERE kind=? AND key=?", (kind, digest(key)))
            return value

    def delete(self, kind, key):
        with self.lock:
            self.db.execute("DELETE FROM kv WHERE kind=? AND key=?", (kind, digest(key)))

    def delete_kind(self, kind):
        with self.lock:
            self.db.execute("DELETE FROM kv WHERE kind=?", (kind,))

    def close(self):
        self.db.close()
