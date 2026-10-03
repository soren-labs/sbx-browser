"""Real Git/runner/owned API delivery and independent-review acceptance."""

import secrets
import sys
import time
from pathlib import Path

import pytest
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from control.connections import SecretVault
from fastapi.testclient import TestClient
from tests.unit.api_v2.conftest import create_session, wait_session


@pytest.fixture
def workflow_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SBX_CONNECTIONS_MODE", "mock")
    monkeypatch.setenv("CODEX_BIN", str(Path("tests/fakes/hosted_codex.py").resolve()))
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    auth = AuthStore(AuthDatabase(path=tmp_path / "workflow.db"))
    user = auth.create_user(email="workflow@example.test")
    app = create_app(
        auth_store=auth,
        hosted=True,
        state_backend="postgres",
        connection_vault=SecretVault(secrets.token_bytes(32)),
        runner_cmd=[sys.executable, "-m", "runtime.runner"],
    )
    app.state.connections.connect(
        user.id, "modal", {"token_id": "REDACTED", "token_secret": "REDACTED"}
    )
    app.state.modal_connections.provision(user.id)
    state = app.state.codex_broker.authorize(user.id)["state"]
    app.state.codex_broker.callback(user.id, state, f"mock:{user.id}")
    github = app.state.github_connections.for_user(user.id)
    github.complete_authorization(
        github._client.installation_id, github.begin_authorization()["state"]
    )
    token = PersistentApiKeyStore(auth).create(user_id=user.id)[1]
    return app, user, {"Authorization": f"Bearer {token}"}, github._client.repositories[0]


def completed_review(client, headers, session_id):
    result = client.post(f"/hosted/sessions/{session_id}/review-sessions", json={}, headers=headers)
    assert result.status_code == 201, result.text
    reviewer_id = result.json()["session_id"]
    wait_session(client, headers, reviewer_id, "finished", "failed")
    result = client.get(f"/hosted/review-sessions/{reviewer_id}", headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed", result.text
    return reviewer_id, result.json()


def test_hosted_coding_delivery_fix_review_merge(workflow_app):
    app, user, headers, repo = workflow_app
    with TestClient(app, base_url="https://testserver") as client:
        author = create_session(client, headers, repository={"repo": repo, "ref": "main"})[
            "session"
        ]["id"]
        result = wait_session(client, headers, author, "finished", "failed")
        assert result["session"]["status"] == "finished", result
        diff = client.get(f"/v2/sessions/{author}/changes/diff", headers=headers)
        assert diff.status_code == 200, diff.text
        assert diff.json()["files"]
        delivered = client.post(
            f"/v2/sessions/{author}/deliver",
            json={"pull_request": {"draft": True}},
            headers=headers,
        )
        assert delivered.status_code == 200, delivered.text
        reviewer, review = completed_review(client, headers, author)
        assert review["review"]["verdict"] == "request_changes"
        assert review["review"]["independent"]
        assert client.post(f"/v1/tasks/{author}/merge", json={}, headers=headers).status_code == 409
        follow = client.post(
            f"/v2/sessions/{author}/messages",
            json={"prompt": "Fix the requested greeting"},
            headers=headers,
        )
        assert follow.status_code == 202, follow.text
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = client.get(f"/v2/sessions/{author}", headers=headers).json()
            if state["session"]["status"] == "finished" and state["session"]["turns"] == 2:
                break
            time.sleep(0.05)
        else:
            pytest.fail(str(state))
        delivered = client.post(
            f"/v2/sessions/{author}/deliver",
            json={"pull_request": {"draft": False}},
            headers=headers,
        )
        assert delivered.status_code == 200, delivered.text
        reviewer2, review2 = completed_review(client, headers, author)
        assert reviewer != reviewer2
        assert review2["review"]["verdict"] == "approve"
        assert review2["review"]["reviewed_head_sha"] != review["review"]["reviewed_head_sha"]
        assert client.get(f"/hosted/review-sessions/{reviewer}", headers=headers).json()["review"][
            "stale"
        ]
        merged = client.post(f"/v1/tasks/{author}/merge", json={}, headers=headers)
        assert merged.status_code == 200, merged.text
        assert merged.json()["revision"]["delivery"]["merged"]
        assert app.state.task_store.get(reviewer2).owner == user.id


def test_review_routes_are_owned_and_idempotent_after_reconstruction(workflow_app):
    app, user, headers, repo = workflow_app
    auth = app.state.auth_store
    other = auth.create_user(email="other-review@example.test")
    other_headers = {
        "Authorization": f"Bearer {PersistentApiKeyStore(auth).create(user_id=other.id)[1]}"
    }
    with TestClient(app, base_url="https://testserver") as client:
        author = create_session(client, headers, repository={"repo": repo, "ref": "main"})[
            "session"
        ]["id"]
        wait_session(client, headers, author, "finished", "failed")
        assert (
            client.post(
                f"/hosted/sessions/{author}/review-sessions", json={}, headers=headers
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/v2/sessions/{author}/deliver", json={"pull_request": {}}, headers=headers
            ).status_code
            == 200
        )
        reviewer, status = completed_review(client, headers, author)
        for path in (
            f"/hosted/sessions/{author}/review-sessions",
            f"/hosted/review-sessions/{reviewer}",
        ):
            assert client.get(path, headers=other_headers).status_code == 404
        assert (
            client.post(
                f"/hosted/sessions/{author}/review-sessions", json={}, headers=other_headers
            ).status_code
            == 404
        )
        for _ in range(2):
            assert (
                client.get(f"/hosted/review-sessions/{reviewer}", headers=headers).json()["review"][
                    "id"
                ]
                == status["review"]["id"]
            )
    restored = create_app(
        backend=app.state.compute_provider.source,
        auth_store=AuthStore(AuthDatabase(path=auth.database._path)),
        hosted=True,
        state_backend="postgres",
        connection_vault=app.state.connections.vault,
        runner_cmd=[sys.executable, "-m", "runtime.runner"],
    )
    with TestClient(restored, base_url="https://testserver") as client:
        result = client.get(f"/hosted/review-sessions/{reviewer}", headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["review"]["id"] == status["review"]["id"]
        task = restored.state.task_store.get(author)
        assert len(restored.state.revision_store.list_reviews(agent_id=task.agent_id)) == 1
