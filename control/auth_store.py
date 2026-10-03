"""Durable product identity/session/API-key foundation.

PostgreSQL is required on Modal; SQLite is an isolated local development/test
backend. No Modal Dict TTLs, plaintext credentials, or process-local authority.
Each operation uses a short transaction and closes its connection. Bootstrap
operator access is a separate env-authoritative overlay, never a User.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from control.auth_schema import MIGRATIONS
from control.ports import ApiKey, ApiKeyStore


class AuthStorageUnavailable(RuntimeError):
    """Safe storage error: never include database URLs, SQL parameters or DSNs."""


class IdentityConflict(ValueError):
    """An external identity already belongs to another user."""


@dataclass(frozen=True)
class User:
    id: str
    email: str | None
    display_name: str
    created_at: str


@dataclass(frozen=True)
class UserSession:
    id: str
    user_id: str
    token_hash: str = field(repr=False)
    created_at: str
    expires_at: float
    revoked_at: str | None = None


@dataclass(frozen=True)
class OAuthAccount:
    id: str
    user_id: str
    provider: str
    provider_subject: str
    created_at: str


@dataclass(frozen=True)
class UserApiKey(ApiKey):
    """Extends the frozen public port without changing its response contract.

    A null owner denotes existing operator-issued keys/console handoffs.
    PR2 must pass an authenticated user_id for end-user key management.
    """

    user_id: str | None = None
    expires_at: float | None = None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).isoformat()


def _expiry(now: float, ttl_s: float) -> float:
    expires = now + ttl_s
    if not math.isfinite(ttl_s) or ttl_s <= 0 or not math.isfinite(expires):
        raise ValueError("credential TTL must be finite and positive")
    return expires


class AuthDatabase:
    """Connection factory with transactional, serialized schema versioning.

    Construction is lazy (control.app has an import-time app). Server startup
    requires initialize() to succeed before accepting requests; the method is
    also available explicitly for deployment preflight/migration tooling.
    """

    def __init__(self, *, path: Path | None = None, database_url: str | None = None) -> None:
        if (path is None) == (database_url is None):
            raise ValueError("choose exactly one auth database backend")
        if database_url is not None and not database_url.startswith(
            ("postgres://", "postgresql://")
        ):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        if path is not None and (not path.is_absolute() or str(path) == ":memory:"):
            raise ValueError("auth SQLite storage requires an absolute file path")
        self._path = path
        self._database_url = database_url
        self._initialized = False
        self._init_lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AuthDatabase:
        env = os.environ if env is None else env
        url = env.get("DATABASE_URL")
        if url:
            return cls(database_url=url)
        if env.get("SBX_BACKEND") == "modal":
            raise ValueError("Modal product auth requires DATABASE_URL in a mounted Modal Secret")
        override = env.get("SBX_AUTH_DB_PATH")
        base = Path(
            env.get("XDG_STATE_HOME") or Path(env.get("HOME") or str(Path.home())) / ".local/state"
        )
        return cls(path=Path(override) if override else base / "sbx-browser" / "auth.sqlite3")

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Open privately from the outset; do not create a world-readable
            # file then chmod it after potentially writing credential verifiers.
            fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
            conn = sqlite3.connect(self._path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
        else:
            import psycopg
            from psycopg.rows import dict_row

            try:
                conn = psycopg.connect(
                    self._database_url,
                    row_factory=dict_row,
                    connect_timeout=10,
                    options="-c statement_timeout=10000 -c lock_timeout=10000",
                )
            except psycopg.Error:
                raise AuthStorageUnavailable("auth database connection failed") from None
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
        # Only static application SQL uses '?' placeholders; values are always
        # passed separately. Both dialects use the exact same migrations/DML.
        return conn.execute(sql if self._path is not None else sql.replace("?", "%s"), params)

    def initialize(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            with self._connect() as conn:
                if self._path is not None:
                    conn.execute("BEGIN IMMEDIATE")
                else:
                    # Serialize concurrent cold starts across containers. The
                    # transaction lock releases on commit/rollback/crash.
                    conn.execute("SELECT pg_advisory_xact_lock(1935833185)")
                self.execute(
                    conn,
                    "CREATE TABLE IF NOT EXISTS auth_schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
                )
                versions = [
                    row["version"]
                    for row in self.execute(
                        conn, "SELECT version FROM auth_schema_migrations ORDER BY version"
                    ).fetchall()
                ]
                if versions != list(range(1, len(versions) + 1)) or len(versions) > len(MIGRATIONS):
                    raise AuthStorageUnavailable("unsupported auth schema version")
                for version, statements in enumerate(MIGRATIONS, start=1):
                    if version <= len(versions):
                        continue
                    for statement in statements:
                        self.execute(conn, statement)
                    self.execute(
                        conn,
                        "INSERT INTO auth_schema_migrations (version, applied_at) VALUES (?, ?)",
                        (version, _iso(time.time())),
                    )
            self._initialized = True

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        self.initialize()
        with self._connect() as conn:
            yield conn


class AuthStore:
    """Server-side primitives; caller must establish trust before linking IDs.

    User email is normalized/unique metadata, not a proof of identity. OAuth
    links never auto-merge users by email. Provider subjects are case-sensitive.
    """

    def __init__(self, database: AuthDatabase, *, clock: Callable[[], float] = time.time) -> None:
        self.database = database
        self.clock = clock

    def create_user(self, *, email: str | None = None, display_name: str = "") -> User:
        if email is not None:
            email = email.strip().lower()
            if not email or "\x00" in email:
                raise ValueError("email must be non-empty")
        user = User(f"usr_{uuid.uuid4().hex}", email, display_name, _iso(self.clock()))
        with self.database.transaction() as conn:
            cursor = self.database.execute(
                conn,
                "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(email) DO NOTHING",
                (user.id, user.email, user.display_name, user.created_at),
            )
            if cursor.rowcount != 1:
                raise IdentityConflict("email already belongs to a user")
        return user

    def get_user(self, user_id: str) -> User | None:
        with self.database.transaction() as conn:
            row = self.database.execute(
                conn, "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return User(**dict(row)) if row is not None else None

    def find_user_by_email(self, email: str) -> User | None:
        with self.database.transaction() as conn:
            row = self.database.execute(
                conn, "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
            ).fetchone()
        return User(**dict(row)) if row is not None else None

    def _require_user(self, conn: Any, user_id: str) -> None:
        if (
            self.database.execute(conn, "SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
            is None
        ):
            raise KeyError("user not found")

    @staticmethod
    def _identity(provider: str, subject: str) -> tuple[str, str]:
        provider = provider.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", provider):
            raise ValueError("invalid identity provider")
        if not subject or "\x00" in subject or len(subject.encode("utf-8")) > 1024:
            raise ValueError("invalid identity subject")
        return provider, subject

    def link_oauth_account(self, user_id: str, *, provider: str, subject: str) -> OAuthAccount:
        provider, subject = self._identity(provider, subject)
        record = OAuthAccount(
            f"oauth_{uuid.uuid4().hex}", user_id, provider, subject, _iso(self.clock())
        )
        with self.database.transaction() as conn:
            self._require_user(conn, user_id)
            self.database.execute(
                conn,
                "INSERT INTO oauth_accounts (id, user_id, provider, provider_subject, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, provider_subject) DO NOTHING",
                (record.id, user_id, provider, subject, record.created_at),
            )
            row = self.database.execute(
                conn,
                "SELECT * FROM oauth_accounts WHERE provider = ? AND provider_subject = ?",
                (provider, subject),
            ).fetchone()
            if row["user_id"] != user_id:
                raise IdentityConflict("external identity already belongs to another user")
        return OAuthAccount(**dict(row))

    def lookup_oauth_account(self, *, provider: str, subject: str) -> OAuthAccount | None:
        provider, subject = self._identity(provider, subject)
        with self.database.transaction() as conn:
            row = self.database.execute(
                conn,
                "SELECT * FROM oauth_accounts WHERE provider = ? AND provider_subject = ?",
                (provider, subject),
            ).fetchone()
        return OAuthAccount(**dict(row)) if row is not None else None

    def list_oauth_accounts(self, user_id: str) -> list[OAuthAccount]:
        with self.database.transaction() as conn:
            rows = self.database.execute(
                conn, "SELECT * FROM oauth_accounts WHERE user_id = ? ORDER BY id", (user_id,)
            ).fetchall()
        return [OAuthAccount(**dict(row)) for row in rows]

    def create_session(self, user_id: str, *, ttl_s: float = 86400) -> tuple[UserSession, str]:
        with self.database.transaction() as conn:
            return self._create_session(conn, user_id, ttl_s=ttl_s)

    def _create_session(self, conn: Any, user_id: str, *, ttl_s: float) -> tuple[UserSession, str]:
        """Also used to atomically finish hosted registration with its session."""
        now = self.clock()
        expires = _expiry(now, ttl_s)
        token = f"sbx_session_{secrets.token_urlsafe(32)}"
        record = UserSession(
            f"usess_{uuid.uuid4().hex}", user_id, _digest(token), _iso(now), expires
        )
        self._require_user(conn, user_id)
        self.database.execute(
            conn,
            "INSERT INTO user_sessions "
            "(id, user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (record.id, user_id, record.token_hash, record.created_at, expires),
        )
        return record, token

    def lookup_session(self, token: str) -> UserSession | None:
        if not token.startswith("sbx_session_"):
            return None
        with self.database.transaction() as conn:
            row = self.database.execute(
                conn,
                "SELECT * FROM user_sessions WHERE token_hash = ? "
                "AND revoked_at IS NULL AND expires_at > ?",
                (_digest(token), self.clock()),
            ).fetchone()
        return UserSession(**dict(row)) if row is not None else None

    def revoke_session(self, session_id: str, *, user_id: str) -> bool:
        # Require the owner on session revocation so PR2 does not accidentally
        # expose a cross-user primitive when handling a client-supplied id.
        with self.database.transaction() as conn:
            cursor = self.database.execute(
                conn,
                "UPDATE user_sessions SET revoked_at = ? "
                "WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                (_iso(self.clock()), session_id, user_id),
            )
        return cursor.rowcount == 1


class PersistentApiKeyStore:
    """Frozen ApiKeyStore-compatible durable keys, optionally user owned."""

    def __init__(self, auth: AuthStore) -> None:
        self.auth = auth

    @staticmethod
    def _record(row: Any) -> UserApiKey:
        data = dict(row)
        data["scopes"] = tuple(json.loads(data["scopes"]))
        return UserApiKey(**data)

    def create(
        self,
        *,
        label: str = "",
        scopes: Iterable[str] = ("agents",),
        user_id: str | None = None,
        ttl_s: float | None = None,
    ) -> tuple[UserApiKey, str]:
        now = self.auth.clock()
        expires = _expiry(now, ttl_s) if ttl_s is not None else None
        token = f"sbx_{secrets.token_hex(32)}"
        record = UserApiKey(
            id=f"key_{uuid.uuid4().hex}",
            key_hash=_digest(token),
            label=label,
            scopes=tuple(scopes),
            created_at=_iso(now),
            user_id=user_id,
            expires_at=expires,
        )
        with self.auth.database.transaction() as conn:
            if user_id is not None:
                self.auth._require_user(conn, user_id)
            self.auth.database.execute(
                conn,
                "INSERT INTO api_keys "
                "(id, user_id, key_hash, label, scopes, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    user_id,
                    record.key_hash,
                    label,
                    json.dumps(record.scopes),
                    record.created_at,
                    expires,
                ),
            )
        return record, token

    def list(self, *, user_id: str | None = None) -> list[UserApiKey]:
        with self.auth.database.transaction() as conn:
            sql = "SELECT * FROM api_keys"
            params = ()
            if user_id is not None:
                sql += " WHERE user_id = ?"
                params = (user_id,)
            rows = self.auth.database.execute(conn, sql + " ORDER BY id", params).fetchall()
        return [self._record(row) for row in rows]

    def lookup(self, token: str) -> UserApiKey | None:
        if not token.startswith("sbx_"):
            return None
        with self.auth.database.transaction() as conn:
            row = self.auth.database.execute(
                conn,
                "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (_digest(token), self.auth.clock()),
            ).fetchone()
        return self._record(row) if row is not None else None

    def revoke(self, key_id: str, *, user_id: str | None = None) -> bool:
        with self.auth.database.transaction() as conn:
            sql = "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL"
            params = (_iso(self.auth.clock()), key_id)
            if user_id is not None:
                sql += " AND user_id = ?"
                params += (user_id,)
            cursor = self.auth.database.execute(conn, sql, params)
        return cursor.rowcount == 1


class BootstrapApiKeyStore:
    """Operator-only credential overlay; product keys delegate to persistence.

    Rotation/removal of the deployment Secret immediately changes bootstrap
    authority on reconstruction; old bootstrap hashes are never persisted as
    active product keys. Like the old env seed, revoking the current operator
    key lasts until restart; permanent removal requires rotating the Secret.
    """

    def __init__(self, store: ApiKeyStore, token: str) -> None:
        if not token.startswith("sbx_"):
            raise ValueError("bootstrap API key must use the sbx_ prefix")
        self.store = store
        digest = _digest(token)
        self.bootstrap = ApiKey(
            id=f"key_bootstrap_{digest[:12]}",
            key_hash=digest,
            label="p2.1-gate",
            scopes=("agents", "admin"),
            created_at=_iso(time.time()),
        )

    def create(self, *, label: str = "", scopes: Iterable[str] = ("agents",)) -> tuple[ApiKey, str]:
        return self.store.create(label=label, scopes=scopes)

    def list(self) -> list[ApiKey]:
        return sorted([*self.store.list(), self.bootstrap], key=lambda record: record.id)

    def lookup(self, token: str) -> ApiKey | None:
        if secrets.compare_digest(_digest(token), self.bootstrap.key_hash):
            return self.bootstrap if self.bootstrap.revoked_at is None else None
        return self.store.lookup(token)

    def revoke(self, key_id: str) -> bool:
        if key_id == self.bootstrap.id:
            if self.bootstrap.revoked_at is not None:
                return False
            self.bootstrap = replace(self.bootstrap, revoked_at=_iso(time.time()))
            return True
        return self.store.revoke(key_id)


def configure_auth(app: Any, *, auth: AuthStore | None = None) -> None:
    """Install durable defaults without replacing explicit test/integration seams."""
    if getattr(app.state, "auth_store", None) is None:
        app.state.auth_store = auth if auth is not None else AuthStore(AuthDatabase.from_env())
    if getattr(app.state, "api_key_store", None) is None:
        app.state.api_key_store = PersistentApiKeyStore(app.state.auth_store)
