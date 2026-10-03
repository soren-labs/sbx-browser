"""Stage 1 email/password auth on the existing durable identity foundation."""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from control.auth_email import EmailDeliveryUnavailable, EmailSender
from control.auth_store import AuthStore, User, UserSession, _digest, _iso

# Argon2id: OWASP minimum of 19 MiB, two iterations and one lane. Explicit
# parameters keep container memory bounded and the policy stable across upgrades.
HASHER = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)
OTP_TTL_S = 600
RESEND_COOLDOWN_S = 60
OTP_ATTEMPT_LIMIT = 5
REGISTRATION_TTL_S = 600
SESSION_TTL_S = 7 * 86400


class HostedAuthError(ValueError):
    def __init__(self, code: str, status: int = 400, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if len(email) > 254 or not re.fullmatch(
        r"[^\s@\x00-\x1f]+@[^\s@\x00-\x1f]+\.[^\s@\x00-\x1f]+", email
    ):
        raise HostedAuthError("invalid_email")
    return email


def _matches(encoded: str, value: str) -> bool:
    try:
        return HASHER.verify(encoded, value)
    except (VerificationError, InvalidHashError):
        return False


@contextmanager
def _write(auth: AuthStore, scope: str) -> Iterator[Any]:
    """Serialize state transitions across processes, including SQLite dev mode.

    PostgreSQL uses a scoped transaction lock; challenge reads additionally lock
    the row so resends and consumption cannot race using different lookup keys.
    """
    with auth.database.transaction() as conn:
        if auth.database._path is not None:
            conn.execute("BEGIN IMMEDIATE")
        else:
            key = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], signed=True)
            auth.database.execute(conn, "SELECT pg_advisory_xact_lock(?)", (key,))
        yield conn


class AuthRateLimiter(Protocol):
    def check(self, *, action: str, email: str, ip: str) -> None:
        """Consume email/IP budget or raise HostedAuthError; replaceable by an edge limiter."""


class DatabaseAuthRateLimiter:
    """Durable fixed-window limits; no raw IP addresses or credentials stored.

    The peer IP comes from the ASGI connection, never client-supplied forwarding
    headers. A trusted deployment proxy must supply that peer IP correctly.
    """

    LIMITS = {"register": (5, 20), "verify": (20, 100), "password": (10, 50), "login": (10, 50)}
    WINDOW_S = 900

    def __init__(self, auth: AuthStore) -> None:
        self.auth = auth

    def check(self, *, action: str, email: str, ip: str) -> None:
        now = self.auth.clock()
        window = math.floor(now / self.WINDOW_S) * self.WINDOW_S
        limited = False
        for kind, value, limit in zip(("email", "ip"), (email, ip), self.LIMITS[action]):
            bucket = _digest(f"{action}:{kind}:{value}")
            with _write(self.auth, bucket) as conn:
                row = self.auth.database.execute(
                    conn,
                    "INSERT INTO auth_rate_limits (bucket_hash, window_start, attempts) "
                    "VALUES (?, ?, 1) ON CONFLICT(bucket_hash) DO UPDATE SET "
                    "attempts = CASE WHEN auth_rate_limits.window_start = excluded.window_start "
                    "THEN auth_rate_limits.attempts + 1 ELSE 1 END, "
                    "window_start = excluded.window_start RETURNING attempts",
                    (bucket, window),
                ).fetchone()
                limited |= row["attempts"] > limit
        if limited:
            raise HostedAuthError(
                "rate_limited", 429, retry_after=max(1, math.ceil(window + self.WINDOW_S - now))
            )


class HostedAuthService:
    def __init__(
        self, auth: AuthStore, sender: EmailSender, *, limiter: AuthRateLimiter | None = None
    ) -> None:
        self.auth = auth
        self.sender = sender
        self.limiter = limiter if limiter is not None else DatabaseAuthRateLimiter(auth)
        # Unknown users still incur a full password verification. Lazily create
        # the dummy verifier to keep app construction free of hashing work.
        self._dummy_hash: str | None = None

    def _challenge(self, conn: Any, field: str, value: str) -> Any:
        assert field in {"id", "email", "registration_hash"}
        lock = " FOR UPDATE" if self.auth.database._path is None else ""
        return self.auth.database.execute(
            conn, f"SELECT * FROM email_verification_challenges WHERE {field} = ?{lock}", (value,)
        ).fetchone()

    def register(self, email: str, *, ip: str) -> str:
        email = normalize_email(email)
        self.limiter.check(action="register", email=email, ip=ip)
        challenge_id = f"evc_{uuid.uuid4().hex}"
        code = f"{secrets.randbelow(1_000_000):06d}"
        encoded = HASHER.hash(code)
        with _write(self.auth, f"register:{email}") as conn:
            previous = self._challenge(conn, "email", email)
            now = self.auth.clock()
            if previous is not None and previous["resend_after"] > now:
                raise HostedAuthError(
                    "resend_cooldown", 429, retry_after=math.ceil(previous["resend_after"] - now)
                )
            # Registration never claims pre-existing identities or resets a
            # password. Return the same response but do not send them an OTP.
            exists = self.auth.database.execute(
                conn, "SELECT id FROM users WHERE email = ?", (email,)
            ).fetchone()
            self.auth.database.execute(
                conn,
                "INSERT INTO email_verification_challenges "
                "(email, id, code_hash, created_at, expires_at, resend_after) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET "
                "id = excluded.id, code_hash = excluded.code_hash, "
                "created_at = excluded.created_at, expires_at = excluded.expires_at, "
                "resend_after = excluded.resend_after, attempts = 0, verified_at = NULL, "
                "registration_hash = NULL, registration_expires_at = NULL, consumed_at = NULL",
                (
                    email,
                    challenge_id,
                    encoded if exists is None else None,
                    now,
                    now + OTP_TTL_S,
                    now + RESEND_COOLDOWN_S,
                ),
            )
        if exists is None:
            try:
                self.sender.send_verification(email=email, code=code, expires_in_s=OTP_TTL_S)
            except Exception:
                # Do not expose or log a provider exception. Remove only our
                # challenge so failure permits immediate retry without deleting
                # a newer resend from another process.
                with _write(self.auth, f"register:{email}") as conn:
                    self.auth.database.execute(
                        conn,
                        "DELETE FROM email_verification_challenges WHERE id = ?",
                        (challenge_id,),
                    )
                raise EmailDeliveryUnavailable("verification email delivery unavailable") from None
        return challenge_id

    def verify(self, challenge_id: str, code: str, *, ip: str) -> str:
        # Unknown IDs have the same failure and IP limiting as a wrong OTP.
        with self.auth.database.transaction() as conn:
            row = self.auth.database.execute(
                conn,
                "SELECT email FROM email_verification_challenges WHERE id = ?",
                (challenge_id,),
            ).fetchone()
        email = row["email"] if row else "unknown"
        self.limiter.check(action="verify", email=email, ip=ip)
        token: str | None = None
        with _write(self.auth, f"verify:{challenge_id}") as conn:
            row = self._challenge(conn, "id", challenge_id)
            now = self.auth.clock()
            if (
                row is not None
                and row["code_hash"] is not None
                and row["expires_at"] > now
                and row["attempts"] < OTP_ATTEMPT_LIMIT
                and row["verified_at"] is None
                and row["consumed_at"] is None
            ):
                # Commit failed attempts before raising; rolling back would
                # make the attempt limit ineffective.
                self.auth.database.execute(
                    conn,
                    "UPDATE email_verification_challenges SET attempts = attempts + 1 WHERE id = ?",
                    (challenge_id,),
                )
                if re.fullmatch(r"[0-9]{6}", code) and _matches(row["code_hash"], code):
                    token = secrets.token_urlsafe(32)
                    self.auth.database.execute(
                        conn,
                        "UPDATE email_verification_challenges SET verified_at = ?, "
                        "code_hash = NULL, registration_hash = ?, registration_expires_at = ? "
                        "WHERE id = ?",
                        (_iso(now), _digest(token), now + REGISTRATION_TTL_S, challenge_id),
                    )
        if token is None:
            raise HostedAuthError("invalid_verification")
        return token

    def set_password(
        self, registration_token: str, password: str, *, ip: str
    ) -> tuple[User, UserSession, str]:
        if not 12 <= len(password) <= 128:
            raise HostedAuthError("invalid_password")
        digest = _digest(registration_token)
        with self.auth.database.transaction() as conn:
            row = self.auth.database.execute(
                conn,
                "SELECT email FROM email_verification_challenges WHERE registration_hash = ?",
                (digest,),
            ).fetchone()
        self.limiter.check(action="password", email=row["email"] if row else "unknown", ip=ip)
        encoded = HASHER.hash(password)
        with _write(self.auth, f"password:{digest}") as conn:
            row = self._challenge(conn, "registration_hash", digest)
            now = self.auth.clock()
            if (
                row is None
                or row["verified_at"] is None
                or row["consumed_at"] is not None
                or row["registration_expires_at"] <= now
            ):
                raise HostedAuthError("invalid_registration")
            user = User(f"usr_{uuid.uuid4().hex}", row["email"], "", _iso(now))
            cursor = self.auth.database.execute(
                conn,
                "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(email) DO NOTHING",
                (user.id, user.email, user.display_name, user.created_at),
            )
            if cursor.rowcount != 1:
                raise HostedAuthError("invalid_registration")
            self.auth.database.execute(
                conn,
                "INSERT INTO password_credentials "
                "(user_id, password_hash, email_verified_at, created_at) VALUES (?, ?, ?, ?)",
                (user.id, encoded, row["verified_at"], _iso(now)),
            )
            self.auth.database.execute(
                conn,
                "UPDATE email_verification_challenges SET consumed_at = ?, "
                "registration_hash = NULL WHERE id = ?",
                (_iso(now), row["id"]),
            )
            session, token = self.auth._create_session(conn, user.id, ttl_s=SESSION_TTL_S)
        return user, session, token

    def login(self, email: str, password: str, *, ip: str) -> tuple[User, UserSession, str]:
        email = normalize_email(email)
        self.limiter.check(action="login", email=email, ip=ip)
        with self.auth.database.transaction() as conn:
            row = self.auth.database.execute(
                conn,
                "SELECT users.*, password_credentials.password_hash FROM users "
                "JOIN password_credentials ON password_credentials.user_id = users.id "
                "WHERE users.email = ?",
                (email,),
            ).fetchone()
        if self._dummy_hash is None:
            self._dummy_hash = HASHER.hash(secrets.token_urlsafe(32))
        valid = _matches(row["password_hash"] if row else self._dummy_hash, password)
        if not valid or row is None:
            raise HostedAuthError("invalid_credentials", 401)
        user = User(row["id"], row["email"], row["display_name"], row["created_at"])
        if HASHER.check_needs_rehash(row["password_hash"]):
            encoded = HASHER.hash(password)
            with self.auth.database.transaction() as conn:
                self.auth.database.execute(
                    conn,
                    "UPDATE password_credentials SET password_hash = ? "
                    "WHERE user_id = ? AND password_hash = ?",
                    (encoded, user.id, row["password_hash"]),
                )
        session, token = self.auth.create_session(user.id, ttl_s=SESSION_TTL_S)
        return user, session, token
