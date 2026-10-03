"""Isolated loopback server and browser for hosted acceptance; no remote credentials."""

import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn


@contextmanager
def hosted_browser(app, browser_token=None):
    from control.hosted_auth_routes import COOKIE_NAME

    playwright = pytest.importorskip("playwright.sync_api")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, timeout_graceful_shutdown=1)
    )
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
                if browser_token:
                    context.add_cookies(
                        [
                            {
                                "name": COOKIE_NAME,
                                "value": browser_token,
                                "domain": "localhost",
                                "path": "/",
                                "secure": True,
                                "httpOnly": True,
                                "sameSite": "Lax",
                            }
                        ]
                    )
                yield context.new_page(), f"http://localhost:{port}", playwright
            finally:
                browser.close()
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()
        assert not worker.is_alive()
