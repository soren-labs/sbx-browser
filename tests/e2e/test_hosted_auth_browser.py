"""Optional Chromium smoke against the real Stage 1 app, with an in-memory email outbox.

Run with `uv run --with playwright pytest tests/e2e/test_hosted_auth_browser.py`.
Install Chromium separately with Playwright; this test never downloads browsers.
"""

from __future__ import annotations

import secrets
import socket
import threading
import time

import pytest
import uvicorn
from control.app import create_app
from control.auth_email import MockEmailSender
from control.auth_store import AuthDatabase, AuthStore
from control.hosted_auth_routes import COOKIE_NAME


def test_hosted_registration_and_login_in_chromium(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    sender = MockEmailSender()
    app = create_app(
        auth_store=AuthStore(AuthDatabase(path=tmp_path / "auth.sqlite3")), email_sender=sender
    )
    # Loopback is a secure context in Chromium, so Secure cookies work locally
    # without weakening the production cookie policy or requiring TLS secrets.
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
        password = secrets.token_urlsafe(24)
        email = "browser@example.test"
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                context = browser.new_context()
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://localhost:{port}/auth")
                page.locator("#register input[name=email]").fill(email)
                page.get_by_role("button", name="Send verification code").click()
                playwright.expect(page.locator("#verify")).to_be_visible()
                page.get_by_label("Verification code").fill(sender.latest_code(email))
                page.get_by_role("button", name="Verify email").click()
                playwright.expect(page.locator("#password")).to_be_visible()
                page.locator("#password input").fill(password)
                page.get_by_role("button", name="Create account", exact=True).click()
                playwright.expect(page.locator("#signed-in")).to_be_visible()
                playwright.expect(page.locator("#identity")).to_have_text(email)
                cookie = next(item for item in context.cookies() if item["name"] == COOKIE_NAME)
                assert cookie["httpOnly"] and cookie["secure"] and cookie["sameSite"] == "Lax"
                assert page.evaluate("document.cookie") == ""
                assert page.evaluate("localStorage.length + sessionStorage.length") == 0
                page.reload()
                playwright.expect(page.locator("#signed-in")).to_be_visible()
                page.get_by_role("button", name="Sign out").click()
                playwright.expect(page.locator("#entry")).to_be_visible()
                page.locator("#login input[name=email]").fill(email)
                page.locator("#login input[name=password]").fill(secrets.token_urlsafe(24))
                page.get_by_role("button", name="Sign in", exact=True).click()
                playwright.expect(page.locator("#status")).to_have_text(
                    "Email or password is incorrect."
                )
                page.locator("#login input[name=password]").fill(password)
                page.get_by_role("button", name="Sign in", exact=True).click()
                playwright.expect(page.locator("#signed-in")).to_be_visible()
                page.get_by_role("button", name="Sign out").click()
                playwright.expect(page.locator("#entry")).to_be_visible()
                assert not any(item["name"] == COOKIE_NAME for item in context.cookies())
                assert not errors
            finally:
                browser.close()
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()
        assert not worker.is_alive()
