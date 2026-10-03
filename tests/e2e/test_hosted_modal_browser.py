"""Browser onboarding through the real API and built hosted console, no Modal credentials."""

import secrets
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore
from control.connections import SecretVault
from control.hosted_auth_routes import COOKIE_NAME
from control.modal_connection import FakeModalProvider


def test_browser_connects_modal_and_github(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    if not Path("console/dist/index.html").is_file():
        pytest.skip("build the console with VITE_HOSTED=1 before this optional smoke")
    auth = AuthStore(AuthDatabase(path=tmp_path / "auth.db"))
    monkeypatch.setenv("SBX_CONNECTIONS_MODE", "mock")
    user = auth.create_user(email="modal-browser@example.test")
    token = auth.create_session(user.id)[1]
    app = create_app(
        auth_store=auth,
        hosted=True,
        state_backend="postgres",
        connection_vault=SecretVault(secrets.token_bytes(32)),
        modal_provider=FakeModalProvider(),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                context = browser.new_context()
                context.add_cookies(
                    [
                        {
                            "name": COOKIE_NAME,
                            "value": token,
                            "domain": "localhost",
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                            "sameSite": "Lax",
                        }
                    ]
                )
                page = context.new_page()
                page.goto(f"http://localhost:{port}/integrations")
                page.get_by_role("button", name="Connect with Modal authorization").click()
                playwright.expect(page.get_by_role("status")).to_have_text("Ready")
                playwright.expect(page.get_by_text("smoke: complete")).to_be_visible()
                page.reload()
                playwright.expect(page.get_by_role("status")).to_have_text("Ready")
                page.get_by_role("button", name="Connect GitHub", exact=True).click()
                account = app.state.github_connections.for_user(user.id)._client.login
                playwright.expect(page.get_by_text(f"Connected: {account}")).to_be_visible()
                playwright.expect(page.get_by_text(f"{account}/alpha", exact=True)).to_be_visible()
                page.get_by_role("button", name="Connect Codex", exact=True).click()
                section = page.get_by_role("region", name="Codex connection")
                playwright.expect(section.get_by_text("Connected", exact=True)).to_be_visible()
                app.state.codex_broker.provider.revoked = True
                app.state.auth_store.clock = lambda: time.time() + 250
                page.get_by_role("button", name="Check Codex connection", exact=True).click()
                playwright.expect(
                    section.get_by_text("Reauth required", exact=True)
                ).to_be_visible()
                page.get_by_role("button", name="Disable Codex", exact=True).click()
                playwright.expect(section.get_by_text("Disabled", exact=True)).to_be_visible()
            finally:
                browser.close()
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()
        assert not worker.is_alive()
