"""The runtime only accepts signed, bounded, session/owner/audience-scoped read grants."""

import secrets

import jwt
import pytest
from fastapi.testclient import TestClient
from runtime.http_service import create_runtime_app


@pytest.fixture
def setup(tmp_path):
    now = [1700000000.0]
    key = secrets.token_urlsafe(32)
    app = create_runtime_app(
        root=tmp_path,
        key=key,
        sandbox_id="sandbox-a",
        owner="user-a",
        agent_id="agent-a",
        origins=["https://sbx-agent.com"],
        clock=lambda: now[0],
    )
    return app, key, now


def token(key, **changes):
    claims = {
        "sub": "user-a",
        "sid": "agent-a",
        "aud": "sbx-runtime:sandbox-a",
        "scope": "events",
        "iat": 1700000000,
        "exp": 1700000060,
    }
    return jwt.encode({**claims, **changes}, key, algorithm="HS256")


@pytest.mark.parametrize(
    "changes",
    [
        {"sub": "user-b"},
        {"sid": "agent-b"},
        {"aud": "sbx-runtime:sandbox-b"},
        {"scope": "terminal"},
        {"exp": 1700000000},
        {"exp": 1700000061},
        {"iat": 1700000100},
    ],
)
def test_wrong_owner_session_audience_scope_and_expiry_fail(setup, changes):
    app, key, _ = setup
    with TestClient(app) as client:
        assert (
            client.get(
                "/sessions/agent-a/events",
                headers={"Authorization": f"Bearer {token(key, **changes)}"},
            ).status_code
            == 401
        )


def test_wrong_signing_key_wrong_path_and_exact_expiry(setup):
    app, key, now = setup
    with TestClient(app) as client:
        assert (
            client.get(
                "/sessions/agent-a/events",
                headers={"Authorization": f"Bearer {token(secrets.token_urlsafe(32))}"},
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/sessions/agent-b/events", headers={"Authorization": f"Bearer {token(key)}"}
            ).status_code
            == 404
        )
        now[0] += 60
        assert (
            client.get(
                "/sessions/agent-a/events", headers={"Authorization": f"Bearer {token(key)}"}
            ).status_code
            == 401
        )


def test_runtime_cors_is_explicit_and_terminal_seam_is_disabled(setup):
    app, _, _ = setup
    with TestClient(app) as client:
        headers = {
            "Origin": "https://sbx-agent.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization, Last-Event-ID",
        }
        allowed = client.options("/sessions/agent-a/events", headers=headers)
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "https://sbx-agent.com"
        assert "access-control-allow-credentials" not in allowed.headers
        headers["Origin"] = "https://attacker.example"
        assert client.options("/sessions/agent-a/events", headers=headers).status_code == 400
        assert client.get("/capabilities").json() == {
            "transport": "sse",
            "terminal": False,
            "websocket": False,
        }
        assert client.get("/sessions/agent-a/terminal").status_code == 404
