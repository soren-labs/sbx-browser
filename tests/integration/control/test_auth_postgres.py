"""Optional real PostgreSQL verification using a throwaway local Unix-socket server.

Set SBX_TEST_POSTGRES_BIN to the directory containing initdb/pg_ctl. No existing
database, cloud service, password or DATABASE_URL is used. The default suite
skips these checks when local PostgreSQL binaries are unavailable.
"""

from __future__ import annotations

import os
import pwd
import secrets
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from control.app import create_app
from control.auth_email import MockEmailSender
from control.auth_schema import MIGRATIONS
from control.auth_store import AuthDatabase, AuthStore, IdentityConflict, PersistentApiKeyStore
from control.hosted_auth import HostedAuthError, HostedAuthService
from control.hosted_auth_routes import COOKIE_NAME
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def postgres():
    configured = os.environ.get("SBX_TEST_POSTGRES_BIN")
    found = shutil.which("initdb")
    if not configured and not found:
        pytest.skip("local PostgreSQL binaries unavailable; set SBX_TEST_POSTGRES_BIN")
    binaries = Path(configured) if configured else Path(found).parent
    assert (binaries / "initdb").is_file()
    assert (binaries / "pg_ctl").is_file()
    with TemporaryDirectory(prefix="sbx-pr1-pg-") as directory:
        root = Path(directory)
        data, socket, home = root / "data", root / "socket", root / "home"
        socket.mkdir()
        home.mkdir()
        env = {
            "HOME": str(home),
            "PATH": f"{binaries}:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LD_LIBRARY_PATH": str(binaries.parents[2] / "x86_64-linux-gnu"),
        }

        def run(*args):
            subprocess.run(args, check=True, env=env, capture_output=True, timeout=30)

        run(str(binaries / "initdb"), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8")

        def start():
            run(
                str(binaries / "pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(root / "postgres.log"),
                "-o",
                f"-k {socket} -h '' -p 55432",
                "-w",
                "start",
            )

        def stop():
            run(str(binaries / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop")

        start()
        try:
            # The test cluster uses the local OS role over its private socket.
            user = pwd.getpwuid(os.getuid()).pw_name
            yield f"postgresql://{user}@/postgres?host={socket}&port=55432", stop, start
        finally:
            stop()


def test_postgres_records_and_hash_verifiers_survive_database_restart(postgres):
    url, stop, start = postgres
    # A fresh database initialized concurrently by independent containers.
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: AuthDatabase(database_url=url).initialize(), range(16)))
    auth = AuthStore(AuthDatabase(database_url=url), clock=lambda: 1700000000)
    user = auth.create_user(email="postgres-restart@example.test")
    session, session_token = auth.create_session(user.id, ttl_s=30)
    identity = auth.link_oauth_account(user.id, provider="github", subject="restart-subject")
    key, token = PersistentApiKeyStore(auth).create(user_id=user.id, ttl_s=30)
    stop()
    start()
    restored = AuthStore(AuthDatabase(database_url=url), clock=lambda: 1700000001)
    assert restored.get_user(user.id) == user
    assert restored.lookup_session(session_token) == session
    assert restored.lookup_oauth_account(provider="github", subject="restart-subject") == identity
    assert PersistentApiKeyStore(restored).lookup(token) == key
    with restored.database.transaction() as conn:
        assert (
            conn.execute(
                "SELECT token_hash FROM user_sessions WHERE id = %s", (session.id,)
            ).fetchone()["token_hash"]
            == session.token_hash
        )
        assert (
            conn.execute("SELECT key_hash FROM api_keys WHERE id = %s", (key.id,)).fetchone()[
                "key_hash"
            ]
            == key.key_hash
        )
    assert restored.revoke_session(session.id, user_id=user.id)
    assert PersistentApiKeyStore(restored).revoke(key.id, user_id=user.id)
    again = AuthStore(AuthDatabase(database_url=url), clock=lambda: 1700000001)
    assert again.lookup_session(session_token) is None
    assert PersistentApiKeyStore(again).lookup(token) is None


def test_postgres_concurrent_migrations_and_identity_linking(postgres):
    url, _, _ = postgres
    auth = AuthStore(AuthDatabase(database_url=url))
    users = [auth.create_user(), auth.create_user()]

    def link(index):
        store = AuthStore(AuthDatabase(database_url=url))
        try:
            return store.link_oauth_account(users[index % 2].id, provider="google", subject="race")
        except IdentityConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(link, range(16)))
    assert len({result.user_id for result in results if result is not None}) == 1
    assert any(result is None for result in results)
    with auth.database.transaction() as conn:
        assert [
            row["version"] for row in conn.execute("SELECT version FROM auth_schema_migrations")
        ] == list(range(1, len(MIGRATIONS) + 1))


def test_postgres_email_uniqueness_and_exact_expiry(postgres):
    url, _, _ = postgres
    auth = AuthStore(AuthDatabase(database_url=url), clock=lambda: 1700000000)
    user = auth.create_user(email="pg-expiry@example.test")
    with pytest.raises(IdentityConflict):
        auth.create_user(email=" PG-EXPIRY@EXAMPLE.TEST ")
    _, session_token = auth.create_session(user.id, ttl_s=10)
    _, token = PersistentApiKeyStore(auth).create(user_id=user.id, ttl_s=10)
    expired = AuthStore(AuthDatabase(database_url=url), clock=lambda: 1700000010)
    assert expired.lookup_session(session_token) is None
    assert PersistentApiKeyStore(expired).lookup(token) is None
    with pytest.raises(KeyError):
        auth.link_oauth_account("missing", provider="github", subject="missing-owner")


def test_postgres_hosted_browser_flow_survives_database_restarts(postgres):
    url, stop, start = postgres
    sender = MockEmailSender()
    email = "pg-hosted@example.test"
    password = secrets.token_urlsafe(24)

    def app():
        return create_app(auth_store=AuthStore(AuthDatabase(database_url=url)), email_sender=sender)

    with TestClient(app(), base_url="https://testserver") as client:
        response = client.post("/auth/register", json={"email": email})
        assert response.status_code == 202
        challenge = response.json()["challenge_id"]
    stop()
    start()
    with TestClient(app(), base_url="https://testserver") as client:
        response = client.post(
            "/auth/verify",
            json={
                "challenge_id": challenge,
                "code": sender.latest_code(email),
            },
        )
        assert response.status_code == 200
        grant = response.json()["registration_token"]
    stop()
    start()
    with TestClient(app(), base_url="https://testserver") as client:
        response = client.post(
            "/auth/password",
            json={
                "registration_token": grant,
                "password": password,
            },
        )
        assert response.status_code == 201
        user = response.json()["user"]
        token = client.cookies.get(COOKIE_NAME)
    stop()
    start()
    with TestClient(app(), base_url="https://testserver") as client:
        client.cookies.set(COOKIE_NAME, token)
        assert client.get("/auth/me").json()["user"] == user
        assert client.post("/auth/logout", json={}).status_code == 204
        assert (
            client.post(
                "/auth/login", json={"email": email, "password": secrets.token_urlsafe(24)}
            ).status_code
            == 401
        )
        assert (
            client.post("/auth/login", json={"email": email, "password": password}).status_code
            == 200
        )
        with client.app.state.auth_store.database.transaction() as conn:
            credential = conn.execute(
                "SELECT password_hash FROM password_credentials WHERE user_id = %s", (user["id"],)
            ).fetchone()
            assert credential["password_hash"].startswith("$argon2id$")
            challenge_row = conn.execute(
                "SELECT * FROM email_verification_challenges WHERE id = %s", (challenge,)
            ).fetchone()
            assert challenge_row["consumed_at"] is not None
            assert challenge_row["code_hash"] is None and challenge_row["registration_hash"] is None
    assert AuthStore(AuthDatabase(database_url=url)).lookup_session(token) is None


@pytest.mark.parametrize("operation", ["verify", "password"])
def test_postgres_hosted_concurrent_consumption_has_one_winner(postgres, operation):
    url, _, _ = postgres
    sender = MockEmailSender()
    service = HostedAuthService(AuthStore(AuthDatabase(database_url=url)), sender)
    email = f"pg-race-{operation}@example.test"
    challenge = service.register(email, ip="192.0.2.2")
    code = sender.latest_code(email)
    grant = service.verify(challenge, code, ip="192.0.2.2") if operation == "password" else None
    password = secrets.token_urlsafe(24)

    def consume(_):
        isolated = HostedAuthService(AuthStore(AuthDatabase(database_url=url)), sender)
        try:
            if operation == "verify":
                return isolated.verify(challenge, code, ip="192.0.2.2")
            return isolated.set_password(grant, password, ip="192.0.2.2")
        except HostedAuthError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(consume, range(8)))
    assert sum(result is not None for result in results) == 1


def test_postgres_business_state_and_owner_survive_database_restart(postgres):
    from control.postgres_state import DatabaseRecords, PostgresWorkflowStore
    from control.workflow_store import WorkflowTaskRecord

    url, stop, start = postgres
    auth = AuthStore(AuthDatabase(database_url=url))
    user = auth.create_user(email="pg-business-state@example.test")
    records = DatabaseRecords(auth.database)
    records.put_owned("connections", "pg-connection", user.id, {"status": "connected"})
    store = PostgresWorkflowStore(records)
    store.attach(WorkflowTaskRecord(user.id, "pg-workflow", "task", "author", "pg-agent"))
    stop()
    start()
    restored = DatabaseRecords(AuthDatabase(database_url=url))
    assert restored.get("connections", "pg-connection", owner=user.id) == {"status": "connected"}
    assert restored.get("connections", "pg-connection", owner="other") is None
    assert len(PostgresWorkflowStore(restored).list_workflow(user.id, "pg-workflow")) == 1


def test_postgres_modal_installation_and_cipher_survive_restart(postgres):
    from control.connections import ConnectionStore, SecretVault
    from control.modal_connection import FakeModalProvider, ModalConnectionService

    url, stop, start = postgres
    auth = AuthStore(AuthDatabase(database_url=url))
    user = auth.create_user(email="pg-modal@example.test")
    vault = SecretVault(secrets.token_bytes(32))
    store = ConnectionStore(auth, vault)
    credential = {"token_id": "REDACTED", "token_secret": secrets.token_urlsafe(32)}
    store.connect(user.id, "modal", credential)
    ready = ModalConnectionService(store, FakeModalProvider()).provision(user.id)
    stop()
    start()
    restored = ConnectionStore(AuthStore(AuthDatabase(database_url=url)), vault)
    assert restored.credentials(restored.get(user.id, "modal")) == credential
    assert ModalConnectionService(restored, FakeModalProvider()).provision(user.id) == ready


def test_postgres_codex_rotation_three_contenders_survives_restart(postgres):
    from control.codex_broker import CodexBroker, FakeCodexProvider
    from control.connections import ConnectionStore, SecretVault

    url, stop, start = postgres
    now = [1700000000.0]
    auth = AuthStore(AuthDatabase(database_url=url), clock=lambda: now[0])
    user = auth.create_user(email="pg-codex@example.test")
    vault = SecretVault(secrets.token_bytes(32))
    store = ConnectionStore(auth, vault)
    provider = FakeCodexProvider(store)
    broker = CodexBroker(store, provider)
    state = broker.authorize(user.id)["state"]
    broker.callback(user.id, state, f"mock:{user.id}")
    now[0] += 250
    with ThreadPoolExecutor(max_workers=3) as pool:
        leases = list(pool.map(lambda _: broker.lease(user.id), range(3)))
    assert provider.calls == 1 and len({lease.access_token for lease in leases}) == 1
    stop()
    start()
    restored = ConnectionStore(
        AuthStore(AuthDatabase(database_url=url), clock=lambda: now[0]), vault
    )
    assert CodexBroker(restored, FakeCodexProvider(restored)).lease(user.id) == leases[0]
