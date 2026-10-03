"""Durable auth behavior against isolated temporary databases, without cloud clients."""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from control.auth_schema import MIGRATIONS
from control.auth_store import (
    AuthDatabase,
    AuthStorageUnavailable,
    AuthStore,
    BootstrapApiKeyStore,
    IdentityConflict,
    PersistentApiKeyStore,
)
from control.ports import ApiKeyStore


@pytest.fixture
def auth(tmp_path):
    return AuthStore(AuthDatabase(path=tmp_path / "auth.sqlite3"), clock=lambda: 1700000000.0)


def reopen(auth, *, now=1700000000.0):
    return AuthStore(AuthDatabase(path=auth.database._path), clock=lambda: now)


def test_user_session_identity_and_api_key_survive_reconstruction(auth):
    user = auth.create_user(email=" Alice@Example.test ", display_name="Alice")
    session, session_token = auth.create_session(user.id, ttl_s=120)
    identity = auth.link_oauth_account(user.id, provider="Google", subject="subject-123")
    keys = PersistentApiKeyStore(auth)
    key, key_token = keys.create(user_id=user.id, label="test", scopes=("agents",))

    restored = reopen(auth)
    assert restored.get_user(user.id) == user
    assert restored.find_user_by_email("ALICE@EXAMPLE.TEST") == user
    assert restored.lookup_session(session_token) == session
    assert restored.lookup_oauth_account(provider="google", subject="subject-123") == identity
    assert restored.list_oauth_accounts(user.id) == [identity]
    restored_keys = PersistentApiKeyStore(restored)
    assert isinstance(restored_keys, ApiKeyStore)
    assert restored_keys.lookup(key_token) == key
    assert restored_keys.list(user_id=user.id) == [key]
    assert user.email == "alice@example.test"


def test_storage_contains_only_credential_hashes(auth):
    user = auth.create_user()
    session, session_token = auth.create_session(user.id)
    key, key_token = PersistentApiKeyStore(auth).create(user_id=user.id)
    assert session.token_hash == hashlib.sha256(session_token.encode()).hexdigest()
    assert key.key_hash == hashlib.sha256(key_token.encode()).hexdigest()
    assert len(session_token.removeprefix("sbx_session_")) >= 43  # 256 random bits
    assert len(key_token.removeprefix("sbx_")) == 64
    with sqlite3.connect(auth.database._path) as conn:
        dump = "\n".join(conn.iterdump())
        assert conn.execute("SELECT token_hash FROM user_sessions").fetchone() == (
            session.token_hash,
        )
        assert conn.execute("SELECT key_hash FROM api_keys").fetchone() == (key.key_hash,)
    assert session_token not in dump
    assert key_token not in dump
    assert session_token.encode() not in auth.database._path.read_bytes()
    assert key_token.encode() not in auth.database._path.read_bytes()
    assert session_token not in repr(session)
    assert key_token not in repr(key)
    assert auth.database._path.stat().st_mode & 0o777 == 0o600


def test_revoke_session_is_owned_idempotent_and_durable(auth):
    user = auth.create_user()
    other = auth.create_user()
    session, token = auth.create_session(user.id)
    assert not auth.revoke_session(session.id, user_id=other.id)
    assert auth.lookup_session(token) is not None
    assert auth.revoke_session(session.id, user_id=user.id)
    assert not auth.revoke_session(session.id, user_id=user.id)
    assert not auth.revoke_session("missing", user_id=user.id)
    assert reopen(auth).lookup_session(token) is None


def test_api_key_revocation_survives_restart_and_preserves_metadata(auth):
    user = auth.create_user()
    other = auth.create_user()
    keys = PersistentApiKeyStore(auth)
    key, token = keys.create(user_id=user.id)
    assert not keys.revoke(key.id, user_id=other.id)
    assert keys.revoke(key.id, user_id=user.id)
    restarted = PersistentApiKeyStore(reopen(auth))
    assert restarted.lookup(token) is None
    assert not restarted.revoke(key.id)
    assert not restarted.revoke("missing")
    assert restarted.list()[0].revoked_at is not None
    assert restarted.list(user_id=other.id) == []


def test_expiry_is_enforced_at_boundary_after_reopen(auth):
    user = auth.create_user()
    _, session_token = auth.create_session(user.id, ttl_s=10)
    _, key_token = PersistentApiKeyStore(auth).create(user_id=user.id, ttl_s=10)
    _, permanent = PersistentApiKeyStore(auth).create()
    before = reopen(auth, now=1700000009.999)
    assert before.lookup_session(session_token) is not None
    assert PersistentApiKeyStore(before).lookup(key_token) is not None
    expired = reopen(auth, now=1700000010)
    assert expired.lookup_session(session_token) is None
    assert PersistentApiKeyStore(expired).lookup(key_token) is None
    assert PersistentApiKeyStore(expired).lookup(permanent) is not None


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan")])
def test_invalid_ttl_is_rejected_without_inserting_credentials(auth, ttl):
    user = auth.create_user()
    with pytest.raises(ValueError, match="TTL"):
        auth.create_session(user.id, ttl_s=ttl)
    keys = PersistentApiKeyStore(auth)
    with pytest.raises(ValueError, match="TTL"):
        keys.create(user_id=user.id, ttl_s=ttl)
    assert keys.list() == []


def test_distinct_credentials_and_unknown_tokens(auth):
    user = auth.create_user()
    sessions = [auth.create_session(user.id) for _ in range(4)]
    keys = [PersistentApiKeyStore(auth).create() for _ in range(4)]
    assert len({token for _, token in sessions}) == 4
    assert len({token for _, token in keys}) == 4
    assert auth.lookup_session("REDACTED") is None
    assert auth.lookup_session("sbx_session_REDACTED") is None
    assert PersistentApiKeyStore(auth).lookup("sbx_REDACTED") is None
    assert auth.lookup_session(keys[0][1]) is None
    assert PersistentApiKeyStore(auth).lookup(sessions[0][1]) is None


def test_identity_link_is_idempotent_and_cannot_move_between_users(auth):
    user, other = auth.create_user(), auth.create_user()
    first = auth.link_oauth_account(user.id, provider="github", subject="123")
    assert reopen(auth).link_oauth_account(user.id, provider="GITHUB", subject="123") == first
    with pytest.raises(IdentityConflict):
        reopen(auth).link_oauth_account(other.id, provider="github", subject="123")
    assert auth.lookup_oauth_account(provider="github", subject="123") == first
    # Provider namespaces and case-sensitive subjects are independent.
    auth.link_oauth_account(other.id, provider="google", subject="123")
    auth.link_oauth_account(other.id, provider="github", subject="ABC")
    auth.link_oauth_account(user.id, provider="github", subject="abc")
    assert len(auth.list_oauth_accounts(user.id)) == 2


def test_email_uniqueness_never_links_an_external_identity_implicitly(auth):
    first = auth.create_user(email="alice@example.test")
    with pytest.raises(IdentityConflict):
        reopen(auth).create_user(email=" ALICE@EXAMPLE.TEST ")
    other = auth.create_user()
    identity = auth.link_oauth_account(other.id, provider="google", subject="alice@example.test")
    assert identity.user_id == other.id
    assert auth.find_user_by_email("alice@example.test") == first
    assert auth.get_user("missing") is None
    assert auth.lookup_oauth_account(provider="github", subject="missing") is None


def test_missing_user_cannot_create_or_link_credentials(auth):
    with pytest.raises(KeyError):
        auth.create_session("missing")
    with pytest.raises(KeyError):
        auth.link_oauth_account("missing", provider="google", subject="123")
    with pytest.raises(KeyError):
        PersistentApiKeyStore(auth).create(user_id="missing")
    # The schema also protects referential integrity against direct SQL writes.
    with pytest.raises(sqlite3.IntegrityError), auth.database.transaction() as conn:
        conn.execute(
            "INSERT INTO oauth_accounts VALUES (?, ?, ?, ?, ?)",
            ("oauth_test", "missing", "google", "subject", "now"),
        )


@pytest.mark.parametrize(
    "provider,subject", [("", "123"), ("google", ""), ("a/b", "123"), ("github", "\x00")]
)
def test_invalid_external_identity_rejected(auth, provider, subject):
    user = auth.create_user()
    with pytest.raises(ValueError):
        auth.link_oauth_account(user.id, provider=provider, subject=subject)


def test_concurrent_reconstruction_and_identity_claims_have_one_owner(auth):
    users = [auth.create_user(), auth.create_user()]

    def claim(index):
        store = reopen(auth)
        try:
            return store.link_oauth_account(users[index % 2].id, provider="github", subject="123")
        except IdentityConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(16)))
    owners = {result.user_id for result in results if result is not None}
    assert len(owners) == 1
    assert any(result is None for result in results)
    assert auth.lookup_oauth_account(provider="github", subject="123").user_id in owners


def test_concurrent_schema_initialization_is_idempotent(tmp_path):
    path = tmp_path / "auth.sqlite3"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: AuthDatabase(path=path).initialize(), range(16)))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM auth_schema_migrations").fetchall() == [
            (n,) for n in range(1, len(MIGRATIONS) + 1)
        ]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables == {
        "users",
        "user_sessions",
        "oauth_accounts",
        "api_keys",
        "auth_schema_migrations",
        "password_credentials",
        "email_verification_challenges",
        "auth_rate_limits",
        "control_records",
        "hosted_connections",
        "connection_authorizations",
    }


def test_newer_or_noncontiguous_schema_is_rejected(auth):
    auth.database.initialize()
    with sqlite3.connect(auth.database._path) as conn:
        conn.execute("INSERT INTO auth_schema_migrations VALUES (?, 'now')", (len(MIGRATIONS) + 1,))
    with pytest.raises(AuthStorageUnavailable, match="schema version"):
        reopen(auth).database.initialize()


def test_stage_one_migration_preserves_existing_users_sessions_and_keys(tmp_path, monkeypatch):
    import control.auth_store as module

    path = tmp_path / "upgrade.sqlite3"
    monkeypatch.setattr(module, "MIGRATIONS", MIGRATIONS[:1])
    original = AuthStore(AuthDatabase(path=path))
    user = original.create_user(email="upgrade@example.test")
    session, session_token = original.create_session(user.id)
    key, key_token = PersistentApiKeyStore(original).create(user_id=user.id)
    monkeypatch.setattr(module, "MIGRATIONS", MIGRATIONS)
    upgraded = AuthStore(AuthDatabase(path=path))
    assert upgraded.get_user(user.id) == user
    assert upgraded.lookup_session(session_token) == session
    assert PersistentApiKeyStore(upgraded).lookup(key_token) == key
    with upgraded.database.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM password_credentials").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM email_verification_challenges").fetchone()[0] == 0


def test_failed_migration_rolls_back_and_can_be_retried(auth, monkeypatch):
    import control.auth_store as module

    migrations = module.MIGRATIONS
    monkeypatch.setattr(module, "MIGRATIONS", (("CREATE TABLE partial (id TEXT)", "INVALID SQL"),))
    with pytest.raises(sqlite3.OperationalError):
        auth.database.initialize()
    with sqlite3.connect(auth.database._path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    monkeypatch.setattr(module, "MIGRATIONS", migrations)
    auth.database.initialize()
    assert auth.create_user() is not None


def test_backend_config_fails_closed_on_modal_and_does_not_echo_url(tmp_path):
    assert (
        AuthDatabase.from_env({"HOME": str(tmp_path)})._path
        == tmp_path / ".local/state/sbx-browser/auth.sqlite3"
    )
    assert (
        AuthDatabase.from_env({"SBX_AUTH_DB_PATH": str(tmp_path / "custom.db")})._path
        == tmp_path / "custom.db"
    )
    with pytest.raises(ValueError, match="requires DATABASE_URL"):
        AuthDatabase.from_env({"SBX_BACKEND": "modal"})
    with pytest.raises(ValueError, match="PostgreSQL URL") as error:
        AuthDatabase.from_env({"SBX_BACKEND": "modal", "DATABASE_URL": "REDACTED"})
    assert "REDACTED" not in str(error.value)
    with pytest.raises(ValueError, match="absolute"):
        AuthDatabase(path=Path("relative.db"))


def test_postgres_connection_failure_is_redacted(monkeypatch):
    import psycopg

    def fail(*args, **kwargs):
        raise psycopg.OperationalError("database_url=REDACTED")

    monkeypatch.setattr(psycopg, "connect", fail)
    db = AuthDatabase(database_url="postgresql://localhost/sbx")
    with pytest.raises(AuthStorageUnavailable) as error:
        db.initialize()
    assert str(error.value) == "auth database connection failed"
    assert "REDACTED" not in str(error.value)


def test_bootstrap_is_operator_only_and_rotation_keeps_product_keys(auth):
    # Random test-only material; never fixture plaintext or real credentials.
    import secrets

    token = f"sbx_{secrets.token_hex(32)}"
    overlay = BootstrapApiKeyStore(PersistentApiKeyStore(auth), token)
    bootstrap = overlay.lookup(token)
    assert bootstrap is not None
    _, product_token = overlay.create(label="product")
    next_token = f"sbx_{secrets.token_hex(32)}"
    rotated = BootstrapApiKeyStore(PersistentApiKeyStore(reopen(auth)), next_token)
    assert rotated.lookup(token) is None
    assert rotated.lookup(next_token) is not None
    assert rotated.lookup(product_token) is not None
    with sqlite3.connect(auth.database._path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 1
        assert (
            conn.execute("SELECT id FROM api_keys WHERE id = ?", (bootstrap.id,)).fetchone() is None
        )
    assert overlay.revoke(bootstrap.id)
    assert overlay.lookup(token) is None
    assert not overlay.revoke(bootstrap.id)
    assert (
        BootstrapApiKeyStore(PersistentApiKeyStore(reopen(auth)), token).lookup(token) is not None
    )


def test_bootstrap_lookup_does_not_depend_on_product_storage(auth, monkeypatch):
    import secrets

    token = f"sbx_{secrets.token_hex(32)}"
    store = PersistentApiKeyStore(auth)
    overlay = BootstrapApiKeyStore(store, token)

    def unavailable(*args):
        raise AuthStorageUnavailable("unavailable")

    monkeypatch.setattr(store, "lookup", unavailable)
    assert overlay.lookup(token) is not None
    with pytest.raises(AuthStorageUnavailable):
        overlay.lookup("sbx_REDACTED")


def test_postgres_adapter_uses_transactions_and_parameter_binding(tmp_path, monkeypatch):
    """Cloud-free driver seam check; SQLite does not substitute for live PG acceptance."""
    import psycopg

    path = tmp_path / "driver.db"
    statements = []
    calls = []

    class Connection:
        def __init__(self):
            self.conn = sqlite3.connect(path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("BEGIN IMMEDIATE")

        def execute(self, sql, params=()):
            statements.append((sql, params))
            if "pg_advisory_xact_lock" in sql:
                return None
            assert "?" not in sql
            return self.conn.execute(sql.replace("%s", "?"), params)

        def commit(self):
            self.conn.commit()

        def rollback(self):
            self.conn.rollback()

        def close(self):
            self.conn.close()

    def connect(*args, **kwargs):
        calls.append(kwargs)
        return Connection()

    monkeypatch.setattr(psycopg, "connect", connect)
    auth = AuthStore(AuthDatabase(database_url="postgresql://localhost/sbx"))
    user = auth.create_user(email="bound@example.test")
    session, token = auth.create_session(user.id)
    auth.link_oauth_account(user.id, provider="github", subject="bound-subject")
    key, key_token = PersistentApiKeyStore(auth).create(user_id=user.id)
    assert auth.lookup_session(token) == session
    assert PersistentApiKeyStore(auth).lookup(key_token) == key
    assert auth.revoke_session(session.id, user_id=user.id)
    assert PersistentApiKeyStore(auth).revoke(key.id)
    assert any("pg_advisory_xact_lock" in sql for sql, _ in statements)
    assert all("bound-subject" not in sql for sql, _ in statements)
    assert all(call["connect_timeout"] == 10 for call in calls)
    assert all("statement_timeout" in call["options"] for call in calls)
