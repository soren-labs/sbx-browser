"""Local identities, opaque sessions and optional user-owned developer keys."""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from control.api_v1.errors import V1ApiError
from control.ports import ApiKey
from control.user_auth.errors import AuthRateLimit
from control.user_auth.store import SqlAuthStore

# Explicit Argon2id parameters (64 MiB, 3 passes, 1 lane). Bound concurrent
# password work per process as well as attempts in the shared durable store.
PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
_PASSWORD_SLOTS = threading.BoundedSemaphore(4)
_DUMMY_HASH: str | None = None
_DUMMY_LOCK = threading.Lock()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def normalize_email(value: str) -> str:
    """ASCII mailbox policy: trim edges, case-insensitive, no alias rewriting.

    Reject Unicode local parts rather than introducing ambiguous Unicode
    folding. IDNs may be supplied as ASCII punycode. Preserve dots and +tags.
    """
    value = value.strip()
    if len(value) > 254 or not value.isascii() or value.count("@") != 1:
        raise ValueError("invalid email")
    local, domain = value.split("@")
    if (
        not 1 <= len(local) <= 64
        or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+", local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not domain
        or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in domain.split(".")
        )
        or len(domain) > 253
    ):
        raise ValueError("invalid email")
    return value.lower()


def valid_password(value: str) -> bool:
    # No trimming, case conversion or normalization of the actual password.
    return 12 <= len(value) <= 256 and not any(unicodedata.category(c) == "Cc" for c in value)


@contextmanager
def password_work():
    if not _PASSWORD_SLOTS.acquire(blocking=False):
        raise AuthRateLimit(1)
    try:
        yield
    finally:
        _PASSWORD_SLOTS.release()


def dummy_hash() -> str:
    global _DUMMY_HASH
    with _DUMMY_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))
        return _DUMMY_HASH


def verify_password(verifier: str, password: str) -> bool:
    try:
        return PASSWORDS.verify(verifier, password)
    except (VerificationError, InvalidHashError):
        return False


class AuthService:
    def __init__(self, store: SqlAuthStore, *, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock

    def limit(self, category: str, identity: str, maximum: int, window: int = 900) -> None:
        now = self.clock()
        bucket = int(now // window)
        prefix = f"rate:{category}:{digest(identity)}:{bucket}:"
        for slot in range(maximum):
            if self.store.add(
                prefix + str(slot),
                {"kind": "rate", "owner": "", "expires": (bucket + 2) * window},
            ):
                return
        raise AuthRateLimit((bucket + 1) * window - now)

    def register(self, email: str, password: str) -> dict[str, Any]:
        try:
            email = normalize_email(email)
        except ValueError:
            raise V1ApiError(400, "invalid_request", "invalid email or password") from None
        if not valid_password(password):
            raise V1ApiError(400, "invalid_request", "invalid email or password")
        with password_work():
            verifier = PASSWORDS.hash(password)
        user = {
            "kind": "user",
            "owner": "usr_" + uuid.uuid4().hex,
            "email": email,
            "password_hash": verifier,
            "created_at": iso(self.clock()),
        }
        if not self.store.add("user:" + digest(email), user):
            raise V1ApiError(400, "invalid_request", "unable to register with these credentials")
        return user

    def login(self, email: str, password: str) -> dict[str, Any]:
        try:
            email = normalize_email(email)
        except ValueError:
            email = "invalid"
        self.limit("login-email", email, 10)
        user = self.store.get("user:" + digest(email))
        with password_work():
            valid = verify_password(user["password_hash"] if user else dummy_hash(), password)
        if user is None or not valid:
            raise V1ApiError(401, "unauthorized", "invalid email or password")
        return user

    def user(self, owner: str) -> dict[str, Any] | None:
        rows = self.store.list("user", owner)
        return rows[0] if rows else None

    def new_session(self, user: dict[str, Any], ttl: int) -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(32)
        row = {
            "kind": "session",
            "owner": user["owner"],
            "csrf": secrets.token_urlsafe(32),
            "created_at": iso(self.clock()),
            "expires": self.clock() + ttl,
        }
        if not self.store.add("session:" + digest(token), row):
            raise RuntimeError("session identifier collision")
        return token, row

    def session(self, token: str | None) -> dict[str, Any] | None:
        if not token or len(token) > 128:
            return None
        key = "session:" + digest(token)
        row = self.store.get(key)
        if not row or row["expires"] <= self.clock() or self.store.get("revoked:" + key):
            return None
        return row if self.user(row["owner"]) else None

    def revoke_session(self, token: str, row: dict[str, Any]) -> bool:
        return self.store.add(
            "revoked:session:" + digest(token),
            {"kind": "revocation", "owner": row["owner"], "expires": row["expires"]},
        )

    @staticmethod
    def principal(owner: str) -> ApiKey:
        # This is a principal view, never a persisted or automatically minted key.
        return ApiKey(id=owner, key_hash="", label="user", scopes=("agents",))

    def create_key(self, owner: str, label: str) -> tuple[dict[str, Any], str]:
        key_id = "uk_" + uuid.uuid4().hex
        token = "sbx_" + key_id + "_" + secrets.token_urlsafe(32)
        row = {
            "kind": "key",
            "owner": owner,
            "id": key_id,
            "key_hash": digest(token),
            "label": label,
            "scopes": ["agents"],
            "created_at": iso(self.clock()),
        }
        if not self.store.add("key:" + key_id, row):
            raise RuntimeError("API key identifier collision")
        return row, token

    def lookup_key(self, token: str) -> ApiKey | None:
        match = re.fullmatch(r"sbx_(uk_[a-f0-9]{32})_[A-Za-z0-9_-]{43}", token)
        if not match:
            return None
        row = self.store.get("key:" + match[1])
        if (
            not row
            or not secrets.compare_digest(row["key_hash"], digest(token))
            or self.store.get("revoked:key:" + row["id"])
            or not self.user(row["owner"])
        ):
            return None
        return self.principal(row["owner"])

    def keys(self, owner: str) -> list[dict[str, Any]]:
        return [self.key_public(row) for row in self.store.list("key", owner)]

    def key_public(self, row: dict[str, Any]) -> dict[str, Any]:
        revoked = self.store.get("revoked:key:" + row["id"])
        return {
            **{k: row[k] for k in ("id", "label", "scopes", "created_at")},
            "revoked_at": revoked["created_at"] if revoked else None,
        }

    def revoke_key(self, owner: str, key_id: str) -> bool:
        row = self.store.get("key:" + key_id)
        if not row or row["owner"] != owner:
            return False
        self.store.add(
            "revoked:key:" + key_id,
            {"kind": "revocation", "owner": owner, "created_at": iso(self.clock())},
        )
        return True
