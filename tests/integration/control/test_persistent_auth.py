"""Real /v1 authentication across app reconstruction with isolated SQLite storage."""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import asynccontextmanager

import pytest
from control.api_v1.deps import get_key_store
from control.app import create_app
from control.auth_store import (
    AuthDatabase,
    AuthStorageUnavailable,
    AuthStore,
    PersistentApiKeyStore,
)
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request


async def _run_lifespan(app, messages, on_started=None):
    """Exercise the ASGI readiness/shutdown protocol without a TestClient portal."""
    incoming = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def receive():
        return next(incoming)

    async def send(message):
        messages.append(message["type"])
        if message["type"] == "lifespan.startup.complete" and on_started is not None:
            on_started()

    await app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send)


def test_api_created_key_and_revocation_survive_control_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("SBX_AUTH_DB_PATH", str(tmp_path / "auth.sqlite3"))
    bootstrap = f"sbx_{secrets.token_hex(32)}"
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", bootstrap)
    headers = {"Authorization": f"Bearer {bootstrap}"}

    with TestClient(create_app()) as client:
        response = client.post("/v1/api-keys", headers=headers, json={"label": "persistent"})
        assert response.status_code == 201
        key = response.json()
        assert set(key) == {"id", "label", "scopes", "created_at", "revoked_at", "key"}
        token = key["key"]
    with TestClient(create_app()) as client:
        assert client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        listed = client.get("/v1/api-keys", headers=headers).json()["api_keys"]
        assert key["id"] in {record["id"] for record in listed}
        assert token not in str(listed)
        assert all("key_hash" not in record and "key" not in record for record in listed)
        assert client.delete(f"/v1/api-keys/{key['id']}", headers=headers).status_code == 204
    with TestClient(create_app()) as client:
        response = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


def test_console_grant_keeps_one_time_operator_semantics_and_durable_key(tmp_path, monkeypatch):
    path = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("SBX_AUTH_DB_PATH", str(path))
    token = f"sbx_{secrets.token_hex(32)}"
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", token)
    with TestClient(create_app()) as client:
        grant = client.post("/v1/console/grant", headers={"Authorization": f"Bearer {token}"})
        assert grant.status_code == 201
        payload = {"grant": grant.json()["grant"]}
        exchange = client.post("/v1/console/exchange", json=payload)
        assert exchange.status_code == 201
        assert set(exchange.json()["scopes"]) == {"agents", "admin"}
        console_token = exchange.json()["key"]
        assert client.post("/v1/console/exchange", json=payload).status_code == 401
    with TestClient(create_app()) as client:
        assert (
            client.get(
                "/v1/api-keys", headers={"Authorization": f"Bearer {console_token}"}
            ).status_code
            == 200
        )
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert conn.execute("SELECT user_id FROM api_keys").fetchall() == [(None,)]


def test_product_key_survives_bootstrap_rotation_and_removal(tmp_path, monkeypatch):
    monkeypatch.setenv("SBX_AUTH_DB_PATH", str(tmp_path / "auth.sqlite3"))
    old = f"sbx_{secrets.token_hex(32)}"
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", old)
    app = create_app()
    _, product = app.state.api_key_store.create()
    new = f"sbx_{secrets.token_hex(32)}"
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", new)
    with TestClient(create_app()) as client:
        assert client.get("/v1/me", headers={"Authorization": f"Bearer {old}"}).status_code == 401
        assert client.get("/v1/me", headers={"Authorization": f"Bearer {new}"}).status_code == 200
        assert (
            client.get("/v1/me", headers={"Authorization": f"Bearer {product}"}).status_code == 200
        )
    monkeypatch.delenv("SBX_V1_BOOTSTRAP_KEY")
    with TestClient(create_app()) as client:
        assert client.get("/v1/me", headers={"Authorization": f"Bearer {new}"}).status_code == 401
        assert (
            client.get("/v1/me", headers={"Authorization": f"Bearer {product}"}).status_code == 200
        )


async def test_auth_dependency_fallback_is_durable_and_explicit_injection_is_preserved(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SBX_AUTH_DB_PATH", str(tmp_path / "auth.sqlite3"))
    app = FastAPI()
    request = Request({"type": "http", "app": app})
    keys = get_key_store(request)
    assert isinstance(keys, PersistentApiKeyStore)
    _, token = keys.create()
    next_app = FastAPI()
    assert get_key_store(Request({"type": "http", "app": next_app})).lookup(token) is not None
    auth = AuthStore(AuthDatabase(path=tmp_path / "injected.sqlite3"))
    injected = create_app(auth_store=auth)
    assert injected.state.auth_store is auth
    assert injected.state.api_key_store.auth is auth
    assert not (tmp_path / "injected.sqlite3").exists()  # lazy construction
    messages = []
    await _run_lifespan(injected, messages)
    assert messages == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert (tmp_path / "injected.sqlite3").exists()  # mandatory startup initialization


async def test_durable_auth_storage_failure_does_not_fall_back_to_memory(tmp_path):
    path = tmp_path / "auth.sqlite3"
    auth = AuthStore(AuthDatabase(path=path))
    PersistentApiKeyStore(auth).create()
    # Reconstruct against a corrupt file: product auth must fail closed.
    path.write_bytes(b"invalid database")
    app = create_app(auth_store=AuthStore(AuthDatabase(path=path)))
    messages = []
    with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
        await _run_lifespan(app, messages)
    assert messages == ["lifespan.startup.failed"]
    assert isinstance(app.state.api_key_store, PersistentApiKeyStore)


@pytest.mark.parametrize("failure", ["connection", "migration_privileges", "unsupported_schema"])
async def test_startup_rejects_auth_init_failure_despite_valid_bootstrap(
    tmp_path, monkeypatch, failure
):
    import psycopg

    bootstrap = f"sbx_{secrets.token_hex(32)}"
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", bootstrap)
    database = AuthDatabase(path=tmp_path / "auth.sqlite3")
    if failure == "connection":
        database = AuthDatabase(database_url="postgresql://localhost/sbx")

        def unreachable(*args, **kwargs):
            raise psycopg.OperationalError("connection unavailable")

        monkeypatch.setattr(psycopg, "connect", unreachable)
        expected = AuthStorageUnavailable
    elif failure == "migration_privileges":

        def denied(*args, **kwargs):
            raise psycopg.errors.InsufficientPrivilege("schema migration denied")

        monkeypatch.setattr(database, "execute", denied)
        expected = psycopg.errors.InsufficientPrivilege
    else:
        database.initialize()
        with sqlite3.connect(tmp_path / "auth.sqlite3") as conn:
            conn.execute("INSERT INTO auth_schema_migrations VALUES (999, 'now')")
        database = AuthDatabase(path=tmp_path / "auth.sqlite3")
        expected = AuthStorageUnavailable

    app = create_app(auth_store=AuthStore(database))
    # The operator overlay itself deliberately does not consult product storage.
    assert app.state.api_key_store.lookup(bootstrap) is not None
    messages = []
    with pytest.raises(expected):
        await _run_lifespan(app, messages)
    # ASGI servers wait for startup.complete before serving the bootstrap probe.
    assert messages == ["lifespan.startup.failed"]
    assert getattr(app.state.plane, "credential_refresher", None) is None


@pytest.mark.parametrize("refresh_enabled", [False, True])
async def test_auth_ready_before_startup_and_refresher_stops_on_repeated_shutdown(
    tmp_path, monkeypatch, refresh_enabled
):
    from control.auth_schema import MIGRATIONS

    monkeypatch.setenv("SBX_CRED_REFRESH", "1" if refresh_enabled else "0")
    bootstrap = f"sbx_{secrets.token_hex(32)}"
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", bootstrap)
    path = tmp_path / "auth.sqlite3"
    app = create_app(auth_store=AuthStore(AuthDatabase(path=path)))
    assert not path.exists()  # construction neither opens the DB nor starts a worker
    assert getattr(app.state.plane, "credential_refresher", None) is None
    workers = []

    def ready():
        # Check persisted schema at the exact ASGI startup acknowledgement.
        with sqlite3.connect(path) as conn:
            versions = conn.execute("SELECT version FROM auth_schema_migrations").fetchall()
            assert versions == [(n,) for n in range(1, len(MIGRATIONS) + 1)]
        assert app.state.api_key_store.lookup(bootstrap) is not None
        worker = getattr(app.state.plane, "credential_refresher", None)
        if refresh_enabled:
            assert worker._thread.is_alive()
            workers.append(worker)
        else:
            assert worker is None

    for _ in range(2):
        messages = []
        await _run_lifespan(app, messages, ready)
        assert messages == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
        assert all(not worker._thread.is_alive() for worker in workers)
    if refresh_enabled:
        assert workers[0] is not workers[1]


@pytest.mark.parametrize("failure_phase", ["startup", "shutdown"])
async def test_auth_lifespan_stops_refresher_when_nested_lifespan_fails(
    tmp_path, monkeypatch, failure_phase
):
    monkeypatch.setenv("SBX_CRED_REFRESH", "1")
    app = create_app(auth_store=AuthStore(AuthDatabase(path=tmp_path / "auth.sqlite3")))
    workers = []

    @asynccontextmanager
    async def failing_lifespan(app):
        worker = app.state.plane.credential_refresher
        assert worker._thread.is_alive()
        workers.append(worker)
        if failure_phase == "startup":
            raise RuntimeError("nested startup failed")
        yield
        raise RuntimeError("nested shutdown failed")

    app.include_router(APIRouter(lifespan=failing_lifespan))
    messages = []
    with pytest.raises(RuntimeError, match=f"nested {failure_phase} failed"):
        await _run_lifespan(app, messages)
    expected = (
        ["lifespan.startup.failed"]
        if failure_phase == "startup"
        else ["lifespan.startup.complete", "lifespan.shutdown.failed"]
    )
    assert messages == expected
    assert len(workers) == 1
    assert not workers[0]._thread.is_alive()
