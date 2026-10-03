"""Real Session engine with durable stores and stable hosted user ownership."""

from __future__ import annotations

import pytest
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from control.hosted_auth_routes import COOKIE_NAME
from fastapi.testclient import TestClient
from tests.unit.api_v2.conftest import create_session, wait_session


@pytest.fixture
def hosted_app(credentialed, tmp_path, stub_runner):
    auth = AuthStore(AuthDatabase(path=tmp_path / "hosted.sqlite3"))
    users = [auth.create_user(email=f"user-{i}@example.test") for i in range(2)]
    keys = PersistentApiKeyStore(auth)
    tokens = [keys.create(user_id=user.id)[1] for user in users]
    second = keys.create(user_id=users[0].id)[1]

    def factory():
        import sys

        app = create_app(
            backend=credentialed.backend,
            auth_store=AuthStore(AuthDatabase(path=auth.database._path)),
            state_backend="postgres",
            hosted=True,
            runner_cmd=[sys.executable, str(stub_runner)],
            max_concurrent=64,
        )
        app.state.account_registry = credentialed.registry
        app.state.scheduler = credentialed.scheduler
        return app

    return factory, auth, users, tokens, second


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_two_keys_and_browser_share_one_users_resources_and_restart(hosted_app):
    factory, auth, users, tokens, second = hosted_app
    with TestClient(factory(), base_url="https://testserver") as client:
        session = create_session(client, headers(tokens[0]))["session"]
        session_id = session["id"]
        result = wait_session(client, headers(second), session_id, "finished", "failed")
        assert result["session"]["status"] == "finished", result
        record = client.app.state.task_store.get(session_id)
        assert record.owner == users[0].id
        assert client.app.state.plane.store.get(record.agent_id).owner == users[0].id
        assert client.get("/v2/sessions", headers=headers(second)).json()["total"] == 1
        browser_token = auth.create_session(users[0].id)[1]
        client.cookies.set(COOKIE_NAME, browser_token)
        assert client.get(f"/v2/sessions/{session_id}").status_code == 200
        assert client.get("/v2/sessions").json()["total"] == 1
        assert (
            client.post(
                f"/v2/sessions/{session_id}/messages",
                json={"text": "continue"},
                headers={"Origin": "https://attacker.example"},
            ).status_code
            == 403
        )
        client.cookies.clear()
        assert client.get("/v2/sessions", headers=headers(tokens[1])).json()["total"] == 0
        for path in (
            f"/v2/sessions/{session_id}",
            f"/v2/sessions/{session_id}/changes",
            f"/v2/sessions/{session_id}/history",
            f"/v1/agents/{record.agent_id}",
            f"/v1/agents/{record.agent_id}/revisions",
        ):
            assert client.get(path, headers=headers(tokens[1])).status_code == 404
    with TestClient(factory(), base_url="https://testserver") as client:
        assert client.get(f"/v2/sessions/{session_id}", headers=headers(second)).status_code == 200
        assert client.get("/v2/sessions", headers=headers(second)).json()["total"] == 1
        record = client.app.state.task_store.get(session_id)
        assert client.app.state.run_store.list(record.agent_id)
        assert client.app.state.plane.store.get(record.agent_id).owner == users[0].id
        assert client.app.state.workspace_store.get(record.agent_id) is None


def test_hosted_product_rejects_ownerless_operator_keys(hosted_app):
    factory, auth, _, _, _ = hosted_app
    operator = PersistentApiKeyStore(auth).create(scopes=("agents", "admin"))[1]
    with TestClient(factory()) as client:
        assert client.get("/v2/sessions", headers=headers(operator)).status_code == 403
        assert client.get("/v1/me", headers=headers(operator)).status_code == 200


def test_hosted_state_requires_durable_database_and_is_separate_from_compute(monkeypatch):
    monkeypatch.setenv("SBX_HOSTED", "1")
    with pytest.raises(ValueError, match="DATABASE_URL"):
        create_app()
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        create_app(state_backend="legacy")
