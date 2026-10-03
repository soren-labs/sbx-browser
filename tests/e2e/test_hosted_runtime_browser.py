"""Browser creates a Session and reads live native events from a distinct sandbox origin."""

import secrets
import sys
from pathlib import Path

from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore
from control.connections import SecretVault
from tests.e2e.hosted_fixture import hosted_browser


def test_browser_session_receives_direct_runtime_events(tmp_path, monkeypatch):
    monkeypatch.setenv("SBX_CONNECTIONS_MODE", "mock")
    monkeypatch.setenv("CODEX_BIN", str(Path("tests/fakes/fake_codex.py").resolve()))
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    auth = AuthStore(AuthDatabase(path=tmp_path / "auth.db"))
    user = auth.create_user(email="runtime-browser@example.test")
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
    token = auth.create_session(user.id)[1]
    with hosted_browser(app, token) as (page, base, playwright):
        responses = []
        errors = []
        page.on("response", lambda response: responses.append(response))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base)
        page.get_by_role("textbox", name="Session task").fill("Create hello.txt")
        with page.expect_response(
            lambda response: (
                "/sessions/" in response.url
                and response.url.endswith("/events")
                and not response.url.startswith(base)
            ),
            timeout=15000,
        ) as direct:
            page.get_by_role("button", name="Start session", exact=True).click()
        assert direct.value.status == 200
        playwright.expect(
            page.get_by_text("Created hello.txt in the workspace.", exact=False).first
        ).to_be_visible(timeout=15000)
        assert any(
            response.url.startswith(base + "/hosted/sessions/") and response.status == 200
            for response in responses
        )
        assert not errors
        session_id = page.url.rsplit("/", 1)[-1]
        task = app.state.task_store.get(session_id)
        assert task.owner == user.id
        assert app.state.run_store.list(task.agent_id)
        page.reload()
        playwright.expect(
            page.get_by_text("Created hello.txt in the workspace.", exact=False).first
        ).to_be_visible(timeout=15000)
