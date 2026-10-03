"""Real hosted HTTP flow with mock email and persistent isolated local storage."""

from __future__ import annotations

import secrets

import pytest
from control.app import create_app
from control.auth_email import MockEmailSender, UnconfiguredEmailSender
from control.auth_store import AuthDatabase, AuthStore
from control.hosted_auth import SESSION_TTL_S
from control.hosted_auth_routes import COOKIE_NAME
from fastapi.testclient import TestClient


@pytest.fixture
def browser(tmp_path):
    now = [1700000000.0]
    sender = MockEmailSender()
    path = tmp_path / "auth.sqlite3"

    def app():
        return create_app(
            auth_store=AuthStore(AuthDatabase(path=path), clock=lambda: now[0]), email_sender=sender
        )

    return app, sender, now


def register(client, sender, email="alice@example.test"):
    response = client.post("/auth/register", json={"email": email})
    assert response.status_code == 202
    assert set(response.json()) == {"challenge_id", "expires_in_s", "resend_after_s"}
    challenge = response.json()["challenge_id"]
    code = sender.latest_code(email.strip().lower())
    response = client.post("/auth/verify", json={"challenge_id": challenge, "code": code})
    assert response.status_code == 200
    return response.json()["registration_token"]


def complete(client, sender, email="alice@example.test"):
    grant = register(client, sender, email)
    password = secrets.token_urlsafe(24)
    response = client.post(
        "/auth/password", json={"registration_token": grant, "password": password}
    )
    assert response.status_code == 201
    return response, password


def test_registration_verification_password_login_logout_and_restart(browser):
    app, sender, _ = browser
    with TestClient(app(), base_url="https://testserver") as client:
        assert client.get("/auth/me").status_code == 401
        response, password = complete(client, sender)
        user = response.json()["user"]
        cookie = response.headers["set-cookie"]
        assert COOKIE_NAME in cookie
        for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
            assert attribute in cookie
        assert "Domain=" not in cookie
        assert password not in response.text
        assert client.get("/auth/me").json() == {"user": user}
        token = client.cookies.get(COOKIE_NAME)
        assert client.post("/auth/logout", json={}).status_code == 204
        assert not client.cookies.get(COOKIE_NAME)
        assert client.get("/auth/me").status_code == 401
        wrong = client.post(
            "/auth/login", json={"email": user["email"], "password": secrets.token_urlsafe(24)}
        )
        assert wrong.status_code == 401 and wrong.json() == {"error": "invalid_credentials"}
        response = client.post(
            "/auth/login", json={"email": " ALICE@EXAMPLE.TEST ", "password": password}
        )
        assert response.status_code == 200 and response.json() == {"user": user}
        new_token = client.cookies.get(COOKIE_NAME)
        assert new_token != token
    with TestClient(app(), base_url="https://testserver") as client:
        client.cookies.set(COOKIE_NAME, new_token)
        assert client.get("/auth/me").json() == {"user": user}
        assert client.post("/auth/logout", json={}).status_code == 204
    with TestClient(app(), base_url="https://testserver") as client:
        client.cookies.set(COOKIE_NAME, new_token)
        assert client.get("/auth/me").status_code == 401
        assert client.post("/auth/logout", json={}).status_code == 204


def test_pending_challenge_and_verified_grant_survive_app_reconstruction(browser):
    app, sender, _ = browser
    with TestClient(app(), base_url="https://testserver") as client:
        challenge = client.post("/auth/register", json={"email": "restart@example.test"}).json()[
            "challenge_id"
        ]
    with TestClient(app(), base_url="https://testserver") as client:
        response = client.post(
            "/auth/verify",
            json={
                "challenge_id": challenge,
                "code": sender.latest_code("restart@example.test"),
            },
        )
        assert response.status_code == 200
        grant = response.json()["registration_token"]
    with TestClient(app(), base_url="https://testserver") as client:
        response = client.post(
            "/auth/password",
            json={
                "registration_token": grant,
                "password": secrets.token_urlsafe(24),
            },
        )
        assert response.status_code == 201
        assert client.get("/auth/me").status_code == 200


def test_browser_identity_is_user_scoped_and_cannot_authorize_operator_routes(browser, monkeypatch):
    bootstrap = f"sbx_{secrets.token_hex(32)}"
    monkeypatch.setenv("SBX_V1_BOOTSTRAP_KEY", bootstrap)
    app, sender, _ = browser
    with TestClient(app(), base_url="https://testserver") as alice:
        alice_response, alice_password = complete(alice, sender)
        alice_user = alice_response.json()["user"]
        alice_token = alice.cookies.get(COOKIE_NAME)
        with TestClient(app(), base_url="https://testserver") as bob:
            bob_response, _ = complete(bob, sender, "bob@example.test")
            bob_user = bob_response.json()["user"]
            assert bob_user["id"] != alice_user["id"]
            assert bob.get("/auth/me").json()["user"] == bob_user
            assert bob.post("/auth/logout", json={}).status_code == 204
        assert alice.get("/auth/me").json()["user"] == alice_user
        assert alice.get("/v1/api-keys").status_code == 401
        assert alice.get("/v2/sessions").status_code == 401
        assert alice.get("/api/sessions").status_code == 401
        assert (
            alice.get("/v1/me", headers={"Authorization": f"Bearer {alice_token}"}).status_code
            == 401
        )
        # Signing in rotates and revokes this browser's old session.
        assert (
            alice.post(
                "/auth/login", json={"email": alice_user["email"], "password": alice_password}
            ).status_code
            == 200
        )
        assert alice.app.state.auth_store.lookup_session(alice_token) is None
    with TestClient(app(), base_url="https://testserver") as operator:
        assert (
            operator.get("/v1/me", headers={"Authorization": f"Bearer {bootstrap}"}).status_code
            == 200
        )
        assert (
            operator.get("/auth/me", headers={"Authorization": f"Bearer {bootstrap}"}).status_code
            == 401
        )
        assert operator.get("/auth/me", auth=("sbx", "sbx")).status_code == 401


def test_browser_session_expires_after_restart(browser):
    app, sender, now = browser
    with TestClient(app(), base_url="https://testserver") as client:
        complete(client, sender)
        token = client.cookies.get(COOKIE_NAME)
    now[0] += SESSION_TTL_S
    with TestClient(app(), base_url="https://testserver") as client:
        client.cookies.set(COOKIE_NAME, token)
        assert client.get("/auth/me").status_code == 401


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Origin": "https://attacker.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "https://sibling.testserver"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Content-Type": "text/plain"}, 415),
    ],
)
def test_cross_origin_json_and_form_mutations_are_rejected(browser, headers, status):
    app, sender, _ = browser
    with TestClient(app(), base_url="https://testserver") as client:
        response = client.post(
            "/auth/register", json={"email": "csrf@example.test"}, headers=headers
        )
        assert response.status_code == status
        assert sender.messages == []
        complete(client, sender)
        response = client.post("/auth/logout", json={}, headers=headers)
        assert response.status_code == status
        assert client.get("/auth/me").status_code == 200
        assert client.post("/auth/register", data={"email": "form@example.test"}).status_code == 415


def test_same_origin_requests_and_secure_cookie_transport(browser):
    app, sender, _ = browser
    with TestClient(
        app(), base_url="https://testserver", headers={"Origin": "https://testserver"}
    ) as client:
        complete(client, sender)
        assert client.get("http://testserver/auth/me").status_code == 401
        assert client.get("/auth/me").status_code == 200


def test_auth_validation_does_not_echo_credentials_or_allow_client_owner(browser):
    app, _, _ = browser
    password = secrets.token_urlsafe(150)
    with TestClient(app(), base_url="https://testserver") as client:
        for path, payload in (
            ("/auth/login", {"email": "alice@example.test", "password": password}),
            ("/auth/verify", {"challenge_id": "REDACTED", "code": password}),
            ("/auth/password", {"registration_token": password, "password": password}),
            (
                "/auth/login",
                {"email": "alice@example.test", "password": "REDACTED", "user_id": "REDACTED"},
            ),
        ):
            response = client.post(path, json=payload)
            assert response.status_code == 422
            assert response.json() == {"error": "invalid_request"}
            assert password not in response.text and "REDACTED" not in response.text
        response = client.post(
            "/auth/login",
            content='{"password": "REDACTED",',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422 and response.json() == {"error": "invalid_request"}


def test_email_unavailable_and_retry_after_are_safe(browser):
    app, sender, _ = browser
    application = app()
    application.state.hosted_auth.sender = UnconfiguredEmailSender()
    with TestClient(application, base_url="https://testserver") as client:
        assert (
            client.post("/auth/register", json={"email": "alice@example.test"}).status_code == 503
        )
        application.state.hosted_auth.sender = sender
        assert (
            client.post("/auth/register", json={"email": "alice@example.test"}).status_code == 202
        )
        response = client.post("/auth/register", json={"email": "alice@example.test"})
        assert response.status_code == 429 and response.headers["Retry-After"] == "60"


def test_browser_page_and_auth_responses_are_not_cached(browser):
    app, _, _ = browser
    with TestClient(app(), base_url="https://testserver") as client:
        for path in ("/auth", "/auth/app.js", "/auth/me"):
            response = client.get(path)
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        page = client.get("/auth")
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
        assert 'autocomplete="one-time-code"' in page.text
        script = client.get("/auth/app.js").text
        assert "localStorage" not in script and "sessionStorage" not in script


def test_browser_otp_wrong_expired_and_reused_fail(browser):
    app, sender, now = browser
    with TestClient(app(), base_url="https://testserver") as client:
        challenge = client.post("/auth/register", json={"email": "alice@example.test"}).json()[
            "challenge_id"
        ]
        code = sender.latest_code("alice@example.test")
        assert (
            client.post(
                "/auth/verify", json={"challenge_id": challenge, "code": "REDACTED"}
            ).status_code
            == 400
        )
        now[0] += 600
        assert (
            client.post("/auth/verify", json={"challenge_id": challenge, "code": code}).status_code
            == 400
        )
        grant = register(client, sender)
        fresh = sender.messages[-1]
        with client.app.state.auth_store.database.transaction() as conn:
            row = conn.execute("SELECT id FROM email_verification_challenges").fetchone()
        assert (
            client.post(
                "/auth/verify", json={"challenge_id": row["id"], "code": fresh.code}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/auth/password",
                json={"registration_token": grant, "password": secrets.token_urlsafe(24)},
            ).status_code
            == 201
        )
