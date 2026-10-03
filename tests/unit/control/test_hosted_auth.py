"""Stage 1 state transitions, durable limits and concurrency without credentials."""

from __future__ import annotations

import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from control.auth_email import EmailDeliveryUnavailable, MockEmailSender, UnconfiguredEmailSender
from control.auth_store import AuthDatabase, AuthStore
from control.hosted_auth import (
    HASHER,
    OTP_ATTEMPT_LIMIT,
    HostedAuthError,
    HostedAuthService,
)


@pytest.fixture
def hosted(tmp_path):
    now = [1700000000.0]
    auth = AuthStore(AuthDatabase(path=tmp_path / "auth.sqlite3"), clock=lambda: now[0])
    sender = MockEmailSender()
    return HostedAuthService(auth, sender), sender, now


def register(hosted, email="alice@example.test"):
    service, sender, _ = hosted
    challenge = service.register(email, ip="127.0.0.1")
    return challenge, sender.latest_code(email.strip().lower())


def verified(hosted, email="alice@example.test"):
    challenge, code = register(hosted, email)
    return hosted[0].verify(challenge, code, ip="127.0.0.1")


def complete(hosted, email="alice@example.test"):
    grant = verified(hosted, email)
    password = secrets.token_urlsafe(24)
    result = hosted[0].set_password(grant, password, ip="127.0.0.1")
    return result, password


def test_flow_normalizes_email_and_stores_only_verifiers(hosted):
    service, sender, _ = hosted
    challenge, code = register(hosted, " Alice@EXAMPLE.test ")
    assert len(code) == 6 and code.isascii() and code.isdigit()
    assert code not in repr(sender.messages)
    password = secrets.token_urlsafe(24)
    grant = service.verify(challenge, code, ip="127.0.0.1")
    user, session, token = service.set_password(grant, password, ip="127.0.0.1")
    assert user.email == "alice@example.test"
    assert session.user_id == user.id
    assert service.auth.lookup_session(token) == session
    login_user, login_session, login_token = service.login(
        " ALICE@EXAMPLE.TEST ", password, ip="127.0.0.1"
    )
    assert login_user == user and login_session.id != session.id
    assert service.auth.lookup_session(login_token).user_id == user.id
    with service.auth.database.transaction() as conn:
        credential = conn.execute("SELECT * FROM password_credentials").fetchone()
        assert credential["email_verified_at"]
        assert credential["password_hash"].startswith("$argon2id$")
        assert HASHER.verify(credential["password_hash"], password)
        row = conn.execute("SELECT * FROM email_verification_challenges").fetchone()
        assert row["consumed_at"] and row["code_hash"] is None
        assert row["registration_hash"] is None
        dump = "\n".join(conn.iterdump())
    for value in (password, grant, token, login_token):
        assert value not in dump
        assert value.encode() not in service.auth.database._path.read_bytes()


def test_otp_hash_is_salted_and_not_recoverable_from_storage(hosted):
    service, _, _ = hosted
    _, code = register(hosted)
    with service.auth.database.transaction() as conn:
        encoded = conn.execute("SELECT code_hash FROM email_verification_challenges").fetchone()[0]
    assert encoded.startswith("$argon2id$")
    assert HASHER.verify(encoded, code)
    assert HASHER.hash(code) != encoded


def test_wrong_guesses_persist_and_exhaust_the_attempt_limit(hosted):
    service, sender, now = hosted
    challenge, code = register(hosted)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for index in range(OTP_ATTEMPT_LIMIT):
        # Reconstruct every time: failures cannot reset the durable counter.
        restored = HostedAuthService(
            AuthStore(AuthDatabase(path=service.auth.database._path), clock=lambda: now[0]), sender
        )
        with pytest.raises(HostedAuthError, match="invalid_verification"):
            restored.verify(challenge, wrong, ip="127.0.0.1")
        with restored.auth.database.transaction() as conn:
            assert (
                conn.execute("SELECT attempts FROM email_verification_challenges").fetchone()[0]
                == index + 1
            )
    with pytest.raises(HostedAuthError, match="invalid_verification"):
        service.verify(challenge, code, ip="127.0.0.1")


def test_malformed_otp_also_consumes_attempt(hosted):
    service, _, _ = hosted
    challenge, _ = register(hosted)
    with pytest.raises(HostedAuthError, match="invalid_verification"):
        service.verify(challenge, "REDACTED", ip="127.0.0.1")
    with service.auth.database.transaction() as conn:
        assert conn.execute("SELECT attempts FROM email_verification_challenges").fetchone()[0] == 1


def test_otp_expiry_at_exact_boundary(hosted):
    service, _, now = hosted
    challenge, code = register(hosted)
    now[0] += 600
    with pytest.raises(HostedAuthError, match="invalid_verification"):
        service.verify(challenge, code, ip="127.0.0.1")


def test_resend_cooldown_and_old_challenge_invalidation(hosted):
    service, sender, now = hosted
    old, code = register(hosted)
    with pytest.raises(HostedAuthError, match="resend_cooldown") as exc:
        service.register("ALICE@example.test", ip="127.0.0.1")
    assert exc.value.status == 429 and exc.value.retry_after == 60
    assert len(sender.messages) == 1
    now[0] += 60
    new, new_code = register(hosted)
    assert new != old
    with pytest.raises(HostedAuthError, match="invalid_verification"):
        service.verify(old, code, ip="127.0.0.1")
    assert service.verify(new, new_code, ip="127.0.0.1")


def test_resend_invalidates_previously_verified_registration_grant(hosted):
    service, _, now = hosted
    grant = verified(hosted)
    now[0] += 60
    register(hosted)
    with pytest.raises(HostedAuthError, match="invalid_registration"):
        service.set_password(grant, secrets.token_urlsafe(24), ip="127.0.0.1")


def test_otp_and_registration_grant_are_each_one_use(hosted):
    service, _, _ = hosted
    challenge, code = register(hosted)
    grant = service.verify(challenge, code, ip="127.0.0.1")
    with pytest.raises(HostedAuthError, match="invalid_verification"):
        service.verify(challenge, code, ip="127.0.0.1")
    password = secrets.token_urlsafe(24)
    user, _, _ = service.set_password(grant, password, ip="127.0.0.1")
    with pytest.raises(HostedAuthError, match="invalid_registration"):
        service.set_password(grant, secrets.token_urlsafe(24), ip="127.0.0.1")
    assert service.login(user.email, password, ip="127.0.0.1")[0] == user


def test_registration_grant_expires_and_unverified_password_is_rejected(hosted):
    service, _, now = hosted
    challenge, _ = register(hosted)
    password = secrets.token_urlsafe(24)
    with pytest.raises(HostedAuthError, match="invalid_registration"):
        service.set_password(challenge, password, ip="127.0.0.1")
    grant = service.verify(challenge, hosted[1].latest_code("alice@example.test"), ip="127.0.0.1")
    now[0] += 600
    with pytest.raises(HostedAuthError, match="invalid_registration"):
        service.set_password(grant, password, ip="127.0.0.1")
    assert service.auth.find_user_by_email("alice@example.test") is None


@pytest.mark.parametrize("password", ["REDACTED", "REDACTED" * 20])
def test_password_policy_does_not_consume_grant(hosted, password):
    service, _, _ = hosted
    grant = verified(hosted)
    with pytest.raises(HostedAuthError, match="invalid_password"):
        service.set_password(grant, password, ip="127.0.0.1")
    assert service.set_password(grant, secrets.token_urlsafe(24), ip="127.0.0.1")[0]


def test_wrong_password_and_unknown_user_fail_identically(hosted):
    service, _, _ = hosted
    complete(hosted)
    for email in ("alice@example.test", "missing@example.test"):
        with pytest.raises(HostedAuthError) as exc:
            service.login(email, secrets.token_urlsafe(24), ip="127.0.0.1")
        assert exc.value.code == "invalid_credentials" and exc.value.status == 401


def test_registration_cannot_take_over_existing_foundation_user(hosted):
    service, sender, _ = hosted
    user = service.auth.create_user(email="alice@example.test")
    challenge = service.register(user.email, ip="127.0.0.1")
    assert sender.messages == []
    with pytest.raises(HostedAuthError, match="invalid_verification"):
        service.verify(challenge, "REDACTED", ip="127.0.0.1")
    assert service.auth.get_user(user.id) == user
    with service.auth.database.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM password_credentials").fetchone()[0] == 0


def test_email_and_ip_rate_limits_survive_reconstruction(hosted):
    service, sender, now = hosted
    for _ in range(10):
        with pytest.raises(HostedAuthError, match="invalid_credentials"):
            service.login("unknown@example.test", secrets.token_urlsafe(24), ip="127.0.0.1")
    restored = HostedAuthService(
        AuthStore(AuthDatabase(path=service.auth.database._path), clock=lambda: now[0]), sender
    )
    with pytest.raises(HostedAuthError, match="rate_limited") as exc:
        restored.login("unknown@example.test", secrets.token_urlsafe(24), ip="127.0.0.2")
    assert exc.value.status == 429 and exc.value.retry_after > 0
    # A different address cannot evade the IP registration budget.
    for index in range(20):
        restored.register(f"ip-limit-{index}@example.test", ip="192.0.2.1")
    with pytest.raises(HostedAuthError, match="rate_limited"):
        restored.register("ip-limit-next@example.test", ip="192.0.2.1")
    now[0] += 900
    assert restored.register("ip-limit-next@example.test", ip="192.0.2.1")


def test_rate_limit_adapter_is_injectable(hosted):
    calls = []

    class Limiter:
        def check(self, **kwargs):
            calls.append(kwargs)
            raise HostedAuthError("rate_limited", 429, retry_after=1)

    service = HostedAuthService(hosted[0].auth, hosted[1], limiter=Limiter())
    with pytest.raises(HostedAuthError, match="rate_limited"):
        service.register(" ALICE@EXAMPLE.TEST ", ip="127.0.0.1")
    assert calls == [{"action": "register", "email": "alice@example.test", "ip": "127.0.0.1"}]
    assert hosted[1].messages == []


def test_email_failure_is_safe_and_allows_immediate_retry(hosted):
    class FailingSender:
        def send_verification(self, **kwargs):
            raise RuntimeError("provider credential=REDACTED")

    service, sender, _ = hosted
    failing = HostedAuthService(service.auth, FailingSender())
    with pytest.raises(EmailDeliveryUnavailable) as exc:
        failing.register("alice@example.test", ip="127.0.0.1")
    assert "REDACTED" not in str(exc.value)
    assert service.register("alice@example.test", ip="127.0.0.1")
    assert len(sender.messages) == 1
    unconfigured = HostedAuthService(service.auth, UnconfiguredEmailSender())
    with pytest.raises(EmailDeliveryUnavailable):
        unconfigured.register("bob@example.test", ip="127.0.0.1")


@pytest.mark.parametrize("operation", ["verify", "password"])
def test_concurrent_consumption_has_exactly_one_success(hosted, operation):
    service, sender, now = hosted
    challenge, code = register(hosted)
    grant = service.verify(challenge, code, ip="127.0.0.1") if operation == "password" else None
    password = secrets.token_urlsafe(24)

    def consume(_):
        isolated = HostedAuthService(
            AuthStore(AuthDatabase(path=service.auth.database._path), clock=lambda: now[0]), sender
        )
        try:
            if operation == "verify":
                return isolated.verify(challenge, code, ip="127.0.0.1")
            return isolated.set_password(grant, password, ip="127.0.0.1")
        except HostedAuthError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(consume, range(8)))
    assert sum(result is not None for result in results) == 1
    with sqlite3.connect(service.auth.database._path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == (operation == "password")
        assert conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0] == (
            operation == "password"
        )


def test_password_and_session_creation_roll_back_together(hosted, monkeypatch):
    service, _, _ = hosted
    grant = verified(hosted)

    def unavailable(*args, **kwargs):
        raise RuntimeError("session write unavailable")

    monkeypatch.setattr(service.auth, "_create_session", unavailable)
    with pytest.raises(RuntimeError, match="session write unavailable"):
        service.set_password(grant, secrets.token_urlsafe(24), ip="127.0.0.1")
    assert service.auth.find_user_by_email("alice@example.test") is None
    with service.auth.database.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM password_credentials").fetchone()[0] == 0
        assert (
            conn.execute("SELECT consumed_at FROM email_verification_challenges").fetchone()[0]
            is None
        )
