"""Independent processes share atomic auth rows; optional loopback PostgreSQL.

Set SBX_TEST_AUTH_POSTGRES_URL only to a disposable, passwordless local
sbx_auth_test_* database. The normal test lane uses SQLite with no server.
"""

from __future__ import annotations

import multiprocessing
import os
import secrets
from concurrent.futures import ProcessPoolExecutor
from urllib.parse import urlsplit

import pytest
from control.user_auth.service import AuthService
from control.user_auth.store import SqlAuthStore


def open_store(target, backend):
    if backend == "postgres":
        return SqlAuthStore(database_url=target)
    return SqlAuthStore(target)


def claim_user(args):
    target, backend, email, index = args
    store = open_store(target, backend)
    return store.add(
        "user:" + email,
        {"kind": "user", "owner": f"usr_test_{index}", "email": email, "password_hash": "REDACTED"},
    )


def claim_rate(args):
    target, backend, identity = args
    service = AuthService(open_store(target, backend), clock=lambda: 1000)
    from control.api_v1.errors import V1ApiError

    try:
        service.limit("test", identity, 3)
        return True
    except V1ApiError as error:
        assert error.status_code == 429
        return False


@pytest.fixture(params=["sqlite", "postgres"])
def shared_store(request, tmp_path):
    backend = request.param
    if backend == "sqlite":
        return tmp_path / "auth.sqlite3", backend
    target = os.environ.get("SBX_TEST_AUTH_POSTGRES_URL")
    if not target:
        pytest.skip("optional disposable local PostgreSQL not configured")
    parsed = urlsplit(target)
    if (
        parsed.hostname != "127.0.0.1"
        or parsed.password
        or not parsed.path.startswith("/sbx_auth_test_")
    ):
        pytest.fail("PostgreSQL auth tests require a disposable passwordless loopback database")
    return target, backend


def test_atomic_registration_and_rate_slots_across_processes(shared_store):
    target, backend = shared_store
    email = secrets.token_hex(12) + "@example.com"
    with ProcessPoolExecutor(4, mp_context=multiprocessing.get_context("spawn")) as pool:
        claimed = list(pool.map(claim_user, [(target, backend, email, i) for i in range(8)]))
        attempts = list(pool.map(claim_rate, [(target, backend, email)] * 12))
    assert sum(claimed) == 1
    assert sum(attempts) == 3
    assert open_store(target, backend).get("user:" + email)["email"] == email


def test_auth_lifecycle_survives_new_store_instance(shared_store):
    target, backend = shared_store
    now = [1000.0]
    first = AuthService(open_store(target, backend), clock=lambda: now[0])
    second = AuthService(open_store(target, backend), clock=lambda: now[0])
    email = secrets.token_hex(12) + "@example.com"
    password = secrets.token_urlsafe(24)
    user = first.register(email, password)
    token, session = first.new_session(user, 300)
    key_row, key = second.create_key(user["owner"], "integration")
    assert second.session(token) == session
    assert first.lookup_key(key).id == user["owner"]
    assert second.login(email.upper(), password)["owner"] == user["owner"]
    assert second.revoke_key(user["owner"], key_row["id"])
    assert first.lookup_key(key) is None
    assert second.revoke_session(token, session)
    assert first.session(token) is None
    now[0] = session["expires"] + 1
    second.store.prune(now[0])
    assert first.user(user["owner"]) is not None
    assert first.lookup_key(key) is None
