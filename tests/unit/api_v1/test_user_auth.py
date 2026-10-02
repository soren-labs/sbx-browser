"""Browser identity and sessions alongside unchanged legacy Bearer clients."""

from __future__ import annotations

import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest
from control.api_v1.errors import V1ApiError
from control.user_auth.routes import AuthConfig
from control.user_auth.service import AuthService, digest, normalize_email, verify_password
from control.user_auth.store import SqlAuthStore, select_store
from fastapi.testclient import TestClient

ORIGIN = "https://testserver"


@pytest.fixture
def browser(v1_env):
    with TestClient(v1_env.app, base_url=ORIGIN) as client:
        yield client


def register(browser, email="alice@example.com", password=None):
    password = password or secrets.token_urlsafe(24)
    response = browser.post(
        "/auth/register", json={"email": email, "password": password}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 201
    return response, password


def csrf(response):
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["session"]["csrf_token"]}


def test_register_login_logout_me(browser, v1_env):
    response, password = register(browser, " Alice@EXAMPLE.com ")
    user = response.json()["user"]
    assert user["email"] == "alice@example.com"
    assert user["id"].startswith("usr_")
    assert browser.get("/auth/me").json()["user"] == user
    assert browser.get("/auth/api-keys").json() == {"api_keys": []}
    assert len(v1_env.keys.list()) == 2  # only the fixture's legacy keys
    assert "no-store" in response.headers["cache-control"]
    cookie = response.headers["set-cookie"]
    for flag in ("__Host-sbx_session=", "HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert flag in cookie
    assert "Domain=" not in cookie
    token = browser.cookies.get("__Host-sbx_session")
    row = v1_env.app.state.user_auth.store.get("session:" + digest(token))
    assert token not in repr(row) and token not in response.text
    principal = browser.get("/v1/me").json()
    assert principal["key_id"] == user["id"] and principal["scopes"] == ["agents"]
    assert browser.get("/v1/models").status_code == 200
    assert browser.get("/v2/sessions").status_code == 200
    assert browser.get("/v1/accounts").status_code == 403
    assert browser.post("/auth/logout", headers=csrf(response)).status_code == 204
    assert browser.get("/auth/me").status_code == 401
    browser.cookies.set("__Host-sbx_session", token)
    assert browser.get("/auth/me").status_code == 401  # replay fails server-side
    browser.cookies.clear()
    login = browser.post(
        "/auth/login",
        json={"email": "ALICE@example.com", "password": password},
        headers={"Origin": ORIGIN},
    )
    assert login.status_code == 200
    assert login.json()["user"] == user
    assert browser.cookies.get("__Host-sbx_session") != token


def test_normalization_and_duplicate(browser, v1_env):
    _, password = register(browser)
    duplicate = browser.post(
        "/auth/register",
        json={"email": " ALICE@EXAMPLE.COM ", "password": password},
        headers={"Origin": ORIGIN},
    )
    assert duplicate.status_code == 400
    assert "exists" not in duplicate.text
    store = v1_env.app.state.user_auth.store
    user = store.get("user:" + digest("alice@example.com"))
    verifier = user["password_hash"]
    assert verifier.startswith("$argon2id$")
    assert password not in repr(user)
    assert verify_password(verifier, password)
    assert not verify_password(verifier, secrets.token_urlsafe(24))
    assert not verify_password(verifier, verifier)
    assert normalize_email("A.B+tag@Example.com") == "a.b+tag@example.com"
    for email in (
        "a..b@example.com",
        "a@-example.com",
        "a b@example.com",
        "а@example.com",
        "a@",
        "a@a..com",
        "a\n@example.com",
    ):
        with pytest.raises(ValueError):
            normalize_email(email)


def test_password_policy_and_validation_redaction(browser):
    password = secrets.token_urlsafe(24)
    for body in (
        {"email": "bad", "password": password},
        {"email": "alice@example.com", "password": "REDACTED"},
        {"email": "alice@example.com", "password": password, "extra": "REDACTED"},
    ):
        response = browser.post("/auth/register", json=body, headers={"Origin": ORIGIN})
        assert response.status_code == 400
        assert password not in response.text and "REDACTED" not in response.text


def test_generic_login_failure_and_rate_limit(browser):
    _, password = register(browser)
    wrong = secrets.token_urlsafe(24)
    bodies = []
    for email in ("alice@example.com", "nobody@example.com"):
        response = browser.post(
            "/auth/login", json={"email": email, "password": wrong}, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 401
        bodies.append(response.json())
    assert bodies[0] == bodies[1]
    for _ in range(9):
        response = browser.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": password},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 401
    response = browser.post(
        "/auth/login",
        json={"email": "NOBODY@example.com", "password": password},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 429


def test_csrf(browser):
    response, password = register(browser)
    for headers in (
        {},
        {"Origin": "https://evil.example"},
        {"Origin": ORIGIN},
        {"Origin": ORIGIN, "X-CSRF-Token": "REDACTED"},
        {**csrf(response), "Sec-Fetch-Site": "cross-site"},
    ):
        assert browser.post("/auth/logout", headers=headers).status_code == 403
        assert browser.post("/v1/agents", json={}, headers=headers).status_code == 403
    # Login/registration CSRF are blocked even before a cookie exists.
    body = {"email": "bob@example.com", "password": password}
    for headers in ({}, {"Origin": "null"}, {"Origin": "https://evil.example"}):
        assert browser.post("/auth/login", json=body, headers=headers).status_code == 403
        assert browser.post("/auth/register", json=body, headers=headers).status_code == 403
    assert browser.post("/auth/api-keys", json={}, headers=csrf(response)).status_code == 201


def test_expiry_and_rotation(browser, v1_env):
    now = [1000.0]
    v1_env.app.state.user_auth.clock = lambda: now[0]
    response, _ = register(browser)
    old = browser.cookies.get("__Host-sbx_session")
    rotated = browser.post("/auth/session/rotate", headers=csrf(response))
    assert rotated.status_code == 200
    assert browser.cookies.get("__Host-sbx_session") != old
    assert v1_env.app.state.user_auth.session(old) is None
    assert browser.post("/auth/logout", headers=csrf(response)).status_code == 403
    now[0] = rotated.json()["session"]["expires_at"]
    assert browser.get("/auth/me").status_code == 401
    assert browser.get("/v1/agents").status_code == 401


def test_keys_and_owner_isolation(browser, v1_env):
    alice, _ = register(browser)
    a_headers = csrf(alice)
    created = browser.post(
        "/v1/agents",
        json={"agent": {"provider": "codex"}, "prompt": {"text": "test"}},
        headers=a_headers,
    )
    assert created.status_code == 201
    agent = created.json()["agent"]["id"]
    key_response = browser.post("/auth/api-keys", json={"label": "developer"}, headers=a_headers)
    assert key_response.status_code == 201
    key = key_response.json()
    token = key["key"]
    auth = {"Authorization": "Bearer " + token}
    assert browser.get("/v1/me", headers=auth).json()["key_id"] == alice.json()["user"]["id"]
    assert browser.get(f"/v1/agents/{agent}", headers=auth).status_code == 200
    listed = browser.get("/auth/api-keys").json()["api_keys"]
    assert listed == [{k: v for k, v in key.items() if k != "key"}]
    assert token not in repr(listed) and "key_hash" not in repr(listed)
    row = v1_env.app.state.user_auth.store.get("key:" + key["id"])
    assert token not in repr(row) and row["key_hash"] == digest(token)
    assert v1_env.app.state.user_auth.lookup_key(row["key_hash"]) is None
    # User keys cannot mint admin credentials or reach operator APIs.
    assert browser.get("/v1/accounts", headers=auth).status_code == 403
    assert browser.post("/v1/api-keys", json={}, headers=auth).status_code == 403
    browser.cookies.clear()
    bob, _ = register(browser, "bob@example.com")
    assert browser.get(f"/v1/agents/{agent}").status_code == 404
    assert browser.get("/v1/agents").json()["agents"] == []
    assert browser.get("/auth/api-keys").json()["api_keys"] == []
    assert browser.delete(f"/auth/api-keys/{key['id']}", headers=csrf(bob)).status_code == 404
    assert browser.get(f"/v1/agents/{agent}", headers=auth).status_code == 200
    browser.cookies.clear()
    login = browser.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": secrets.token_urlsafe(24)},
        headers={"Origin": ORIGIN},
    )
    assert login.status_code == 401
    # Revoke through the service as the original owner, then ensure no cookie fallback.
    assert v1_env.app.state.user_auth.revoke_key(alice.json()["user"]["id"], key["id"])
    register(browser, "carol@example.com")
    assert browser.get("/v1/me", headers=auth).status_code == 401
    for authorization in ("Bearer sbx_unknown", "Basic REDACTED"):
        assert browser.get("/v1/me", headers={"Authorization": authorization}).status_code == 401


def test_key_revoke_endpoint(browser):
    response, _ = register(browser)
    headers = csrf(response)
    key = browser.post("/auth/api-keys", json={}, headers=headers).json()
    auth = {"Authorization": "Bearer " + key["key"]}
    assert browser.delete(f"/auth/api-keys/{key['id']}", headers=headers).status_code == 204
    assert browser.get("/v1/me", headers=auth).status_code == 401
    listed = browser.get("/auth/api-keys").json()["api_keys"][0]
    assert listed["revoked_at"] is not None and "key" not in listed


def test_legacy_bearer_without_csrf(browser, auth, admin_auth, v1_env):
    register(browser)
    assert browser.get("/v1/me", headers=auth).json()["key_id"] == v1_env.agents_key_id
    assert (
        browser.post(
            "/v1/agents",
            json={"agent": {"provider": "codex"}, "prompt": {"text": "test"}},
            headers=auth,
        ).status_code
        == 201
    )
    assert browser.get("/v1/accounts", headers=admin_auth).status_code == 200
    assert browser.get("/v1/api-keys", headers=auth).status_code == 403


def test_shared_store_concurrency_and_reopen(tmp_path):
    path = tmp_path / "auth.sqlite3"
    services = [AuthService(SqlAuthStore(path)) for _ in range(2)]
    password = secrets.token_urlsafe(24)

    def register_once(service):
        try:
            return service.register("same@example.com", password)
        except V1ApiError as error:
            assert error.status_code == 400
            return None

    with ThreadPoolExecutor(2) as pool:
        users = list(pool.map(register_once, services))
    assert sum(user is not None for user in users) == 1
    user = next(user for user in users if user)
    token, session = services[0].new_session(user, 300)
    assert services[1].session(token) == session
    _, key = services[1].create_key(user["owner"], "test")
    assert services[0].lookup_key(key).id == user["owner"]
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda s: s.revoke_session(token, session), services))
    assert sorted(outcomes) == [False, True]
    assert services[1].session(token) is None
    assert services[1].revoke_key(user["owner"], services[0].keys(user["owner"])[0]["id"])
    reopened = AuthService(SqlAuthStore(path))
    assert reopened.lookup_key(key) is None
    assert reopened.login("SAME@EXAMPLE.COM", password)["owner"] == user["owner"]
    reopened.store.prune(session["expires"] + 1)
    assert reopened.store.get("session:" + digest(token)) is None
    assert reopened.user(user["owner"]) is not None
    assert reopened.lookup_key(key) is None
    assert path.stat().st_mode & 0o777 == 0o600


def test_config_and_modal_require_durable_database(monkeypatch):
    assert AuthConfig.from_env().secure
    monkeypatch.setenv("SBX_AUTH_COOKIE_SECURE", "false")
    assert not AuthConfig.from_env().secure
    monkeypatch.setenv("SBX_BACKEND", "modal")
    with pytest.raises(ValueError, match="secure"):
        AuthConfig.from_env()
    monkeypatch.setenv("SBX_AUTH_COOKIE_SECURE", "true")
    with pytest.raises(ValueError, match="requires SBX_AUTH_DATABASE_URL"):
        select_store()
    monkeypatch.setenv("SBX_AUTH_DATABASE_URL", "postgresql://REDACTED")
    assert select_store().database_url == "postgresql://REDACTED"
    monkeypatch.setenv("SBX_AUTH_SESSION_TTL_S", "999999")
    with pytest.raises(ValueError, match="between"):
        AuthConfig.from_env()


def test_login_rotates_old_cookie_and_secure_local_configuration(browser, v1_env):
    response, password = register(browser)
    old = browser.cookies.get("__Host-sbx_session")
    login = browser.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": password},
        headers={"Origin": ORIGIN},
    )
    assert login.status_code == 200
    assert v1_env.app.state.user_auth.session(old) is None
    assert login.json()["session"]["csrf_token"] != response.json()["session"]["csrf_token"]
    v1_env.app.state.auth_config = AuthConfig(secure=False)
    browser.cookies.clear()
    with TestClient(v1_env.app, base_url="http://testserver") as local:
        login = local.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": password},
            headers={"Origin": "http://testserver"},
        )
        assert login.status_code == 200
        cookie = login.headers["set-cookie"]
        assert "sbx_session=" in cookie and "Secure" not in cookie
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert local.get("/auth/me").status_code == 200


def test_v2_session_and_cross_user_write_guards(browser, v1_env):
    v1_env.registry.put_credential_blob(
        "acct-codex-1", {"provider": "codex", "files": {".codex/auth.json": "{}"}}
    )
    alice, _ = register(browser)
    headers = csrf(alice)
    response = browser.post(
        "/v2/sessions", json={"prompt": "test", "execution": {"provider": "codex"}}, headers=headers
    )
    assert response.status_code == 201
    session_id = response.json()["session"]["id"]
    key = browser.post("/auth/api-keys", json={}, headers=headers).json()["key"]
    bearer = {"Authorization": "Bearer " + key}
    assert browser.get(f"/v2/sessions/{session_id}", headers=bearer).status_code == 200
    agent_response = browser.post(
        "/v1/agents",
        json={"agent": {"provider": "codex"}, "prompt": {"text": "test"}},
        headers=headers,
    )
    assert agent_response.status_code == 201
    agent_id = agent_response.json()["agent"]["id"]
    browser.cookies.clear()
    bob, _ = register(browser, "bob@example.com")
    assert browser.get(f"/v2/sessions/{session_id}").status_code == 404
    assert (
        browser.post(f"/v2/sessions/{session_id}/cancel", json={}, headers=csrf(bob)).status_code
        == 404
    )
    for method, suffix, body in (
        ("get", "/runs", None),
        ("get", "/usage", None),
        ("get", "/workspace", None),
        ("get", "/runs/run-1/stream", None),
        ("post", "/runs", {"prompt": {"text": "test"}}),
        ("delete", "", None),
    ):
        result = browser.request(
            method, f"/v1/agents/{agent_id}{suffix}", json=body, headers=csrf(bob)
        )
        assert result.status_code == 404
    assert browser.get("/v1/agents/summary").json()["total"] == 0
    # Legacy agents credentials cannot read a normal user's resources either.
    legacy = {"Authorization": "Bearer " + v1_env.agents_token}
    assert browser.get(f"/v1/agents/{agent_id}", headers=legacy).status_code == 404
    assert browser.get("/v1/agents", headers=legacy).json()["agents"] == []


def test_deployment_secret_allowlist_and_canonical_origin(browser, v1_env):
    from control.config import app_secret_names, remote_env_overlay

    env = {
        "SBX_AUTH_SECRET_NAME": "auth-test",
        "SBX_AUTH_DATABASE_URL": "REDACTED",
        "SBX_AUTH_ORIGIN": "https://sbx.example",
    }
    assert "auth-test" in app_secret_names(env)
    remote = remote_env_overlay(env)
    assert remote["SBX_AUTH_ORIGIN"] == "https://sbx.example"
    assert "SBX_AUTH_DATABASE_URL" not in remote
    v1_env.app.state.auth_config = AuthConfig(origin="https://sbx.example")
    body = {"email": "alice@example.com", "password": secrets.token_urlsafe(24)}
    assert browser.post("/auth/register", json=body, headers={"Origin": ORIGIN}).status_code == 403
    assert (
        browser.post(
            "/auth/register", json=body, headers={"Origin": "https://sbx.example.evil.test"}
        ).status_code
        == 403
    )
    assert (
        browser.post(
            "/auth/register", json=body, headers={"Origin": "https://sbx.example"}
        ).status_code
        == 201
    )


def test_artifact_owner_isolation_and_handoff(browser, v1_env):
    from types import SimpleNamespace

    from control.artifacts import ArtifactManifest

    alice, _ = register(browser)
    created = browser.post(
        "/v1/agents",
        json={"agent": {"provider": "codex"}, "prompt": {"text": "test"}},
        headers=csrf(alice),
    )
    assert created.status_code == 201
    agent_id = created.json()["agent"]["id"]
    artifact_id = "a" * 64
    manifest = ArtifactManifest(artifact_id=artifact_id, producer_agent_id=agent_id)
    v1_env.app.state.artifact_store = SimpleNamespace(
        manifest=lambda _: manifest, list=lambda **_: [manifest], read=lambda *_: b"test"
    )
    assert browser.get(f"/v1/artifacts/{artifact_id}").status_code == 200
    browser.cookies.clear()
    bob, _ = register(browser, "bob@example.com")
    assert browser.get(f"/v1/artifacts/{artifact_id}").status_code == 404
    assert browser.get(f"/v1/artifacts/{artifact_id}/download").status_code == 404
    assert browser.get("/v1/artifacts").json()["artifacts"] == []
    assert (
        browser.get("/v1/artifacts", params={"agent_id": agent_id, "limit": 1}).json()["artifacts"]
        == []
    )
    body = {
        "agent": {"provider": "codex"},
        "prompt": {"text": "test"},
        "workspace": {
            "repo": "https://github.com/example/repo.git",
            "base_ref": "main",
            "base_sha": "0" * 40,
        },
        "handoff": {"artifact_id": artifact_id},
    }
    assert browser.post("/v1/agents", json=body, headers=csrf(bob)).status_code == 404
    assert (
        browser.post("/auth/api-keys", json={"scopes": ["admin"]}, headers=csrf(bob)).status_code
        == 400
    )
