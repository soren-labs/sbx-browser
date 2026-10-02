"""Durable authentication rows with atomic insert-if-absent semantics.

SQLite is for a single local host. PostgreSQL is required on Modal / multiple
hosts. Modal Dict's inactivity expiry makes it unsuitable for user identities.
Rows are immutable: revocation uses separate tombstones, never read/modify/write.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = """CREATE TABLE IF NOT EXISTS sbx_auth (
    key TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
    expires DOUBLE PRECISION, payload TEXT NOT NULL
)"""


class SqlAuthStore:
    def __init__(self, path: Path | None = None, *, database_url: str | None = None):
        self.database_url = database_url
        self.path = path
        self._ready = False
        self._lock = threading.Lock()

    def _prepare_local(self) -> None:
        if not self.database_url:
            path = self.path
            assert path is not None
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Open with restrictive permissions before SQLite creates any data.
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)

    @contextmanager
    def _connection(self):
        if self.database_url:
            import psycopg

            db = psycopg.connect(self.database_url, connect_timeout=5)
        else:
            self._prepare_local()
            db = sqlite3.connect(self.path, timeout=10)
        try:
            with self._lock:
                if not self._ready:
                    if self.database_url:
                        # Serialize initial DDL across containers; IF NOT EXISTS
                        # alone does not serialize PostgreSQL catalog creation.
                        db.execute("SELECT pg_advisory_xact_lock(738942101)")
                    db.execute(_SCHEMA)
                    db.execute("CREATE INDEX IF NOT EXISTS sbx_auth_owner ON sbx_auth(kind, owner)")
                    db.execute("CREATE INDEX IF NOT EXISTS sbx_auth_expiry ON sbx_auth(expires)")
                    db.commit()
                    self._ready = True
            with db:
                yield db
        finally:
            db.close()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.database_url else sql

    def add(self, key: str, row: dict[str, Any]) -> bool:
        with self._connection() as db:
            result = db.execute(
                self._sql(
                    "INSERT INTO sbx_auth(key, kind, owner, expires, payload) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(key) DO NOTHING"
                ),
                (key, row["kind"], row["owner"], row.get("expires"), json.dumps(row)),
            )
            return result.rowcount == 1

    def get(self, key: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                self._sql("SELECT payload FROM sbx_auth WHERE key=?"), (key,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def list(self, kind: str, owner: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                self._sql("SELECT payload FROM sbx_auth WHERE kind=? AND owner=? ORDER BY key"),
                (kind, owner),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]

    def prune(self, now: float) -> None:
        """Only expired sessions, their tombstones, and rate slots are deleted.

        Users, keys and key revocations have no expiry and are never pruned.
        """
        with self._connection() as db:
            db.execute(self._sql("DELETE FROM sbx_auth WHERE expires <= ?"), (now,))


def select_store() -> SqlAuthStore:
    url = os.environ.get("SBX_AUTH_DATABASE_URL")
    if url:
        if not url.startswith(("postgresql://", "postgres://")):
            raise ValueError("SBX_AUTH_DATABASE_URL must be a PostgreSQL URL")
        return SqlAuthStore(database_url=url)
    if os.environ.get("SBX_BACKEND") == "modal":
        raise ValueError("Modal authentication requires SBX_AUTH_DATABASE_URL in a Secret")
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    path = Path(os.environ.get("SBX_AUTH_SQLITE_PATH", str(root / "sbx/auth.sqlite3")))
    return SqlAuthStore(path)
