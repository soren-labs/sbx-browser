"""User-context compute adapter; local fake uses the existing backend and real HTTP data plane."""

from __future__ import annotations

import secrets
import socket
import threading
import time
from dataclasses import replace
from typing import Any, Protocol

import jwt
import uvicorn
from runtime.http_service import create_runtime_app

from control.backend import LocalProcessBackend, SandboxHandle, SandboxSpec
from control.connections import ConnectionStore
from control.hosted_auth import HostedAuthError
from control.modal_connection import ModalContext
from control.postgres_state import DatabaseRecords


class ComputeProvider(Protocol):
    """Production implementations must construct every client from this user's context."""

    def create(self, context: ModalContext, spec: SandboxSpec, runtime: dict) -> SandboxHandle: ...
    def exec(self, context: ModalContext, handle: SandboxHandle, argv: list[str], env: dict): ...
    def poll(self, context: ModalContext, handle: SandboxHandle): ...
    def terminate(self, context: ModalContext, handle: SandboxHandle): ...
    def list(self, context: ModalContext, tags: dict): ...
    def endpoint(self, context: ModalContext, handle: SandboxHandle, key: str) -> str: ...


class UnconfiguredComputeProvider:
    def __getattr__(self, name):
        if name == "stop":
            return lambda: None

        def unavailable(*args, **kwargs):
            raise HostedAuthError("modal_compute_not_configured", 503)

        return unavailable


class FakeComputeProvider:
    def __init__(self, source=None, *, clock=time.time):
        self.source, self.clock = source or LocalProcessBackend(), clock
        self._servers: dict[str, tuple[Any, Any, Any, str]] = {}
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []

    def _check(self, context, handle):
        if (
            handle.tags.get("owner") != context.user_id
            or handle.tags.get("modal_connection") != context.connection_id
        ):
            raise HostedAuthError("sandbox_not_found", 404)

    def create(self, context, spec, runtime):
        self.calls.append(("create", context.user_id))
        return self.source.create(spec)

    def exec(self, context, handle, argv, env):
        self._check(context, handle)
        return self.source.exec(handle, argv, env=env)

    def poll(self, context, handle):
        self._check(context, handle)
        return self.source.poll(handle)

    def list(self, context, tags):
        return self.source.list(
            tags={**tags, "owner": context.user_id, "modal_connection": context.connection_id}
        )

    def endpoint(self, context, handle, key):
        self._check(context, handle)
        with self._lock:
            if handle.id in self._servers:
                return self._servers[handle.id][3]
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            app = create_runtime_app(
                root=handle.root,
                key=key,
                sandbox_id=handle.id,
                owner=context.user_id,
                agent_id=handle.tags["session_id"],
                origins=[],
                mock=True,
                clock=self.clock,
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app, log_level="critical", access_log=False, timeout_graceful_shutdown=1
                )
            )
            worker = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            worker.start()
            deadline = time.monotonic() + 5
            while not server.started and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not server.started:
                server.should_exit = True
                worker.join(timeout=5)
                listener.close()
                raise HostedAuthError("sandbox_runtime_unavailable", 503)
            url = f"http://localhost:{port}"
            self._servers[handle.id] = (server, worker, listener, url)
            return url

    def _close(self, sandbox_id):
        with self._lock:
            running = self._servers.pop(sandbox_id, None)
        if running:
            server, worker, listener, _ = running
            server.should_exit = True
            worker.join(timeout=5)
            listener.close()

    def terminate(self, context, handle):
        self._check(context, handle)
        self._close(handle.id)
        self.source.terminate(handle)

    def stop(self):
        for sandbox_id in list(self._servers):
            self._close(sandbox_id)


class HostedModalBackend:
    def __init__(self, connections: ConnectionStore, provider: ComputeProvider):
        self.connections, self.provider = connections, provider
        self.records = DatabaseRecords(connections.auth.database)

    def _context(self, owner):
        record = self.connections.get(owner, "modal")
        if record is None or record.state != "ready":
            raise HostedAuthError("modal_runtime_required", 409)
        return ModalContext(owner, record.id, self.connections.credentials(record)), record.metadata

    def _handle_context(self, handle):
        context, runtime = self._context(handle.tags.get("owner", ""))
        if (
            handle.tags.get("modal_connection") != context.connection_id
            or handle.tags.get("modal_workspace") != runtime["workspace"]
        ):
            raise HostedAuthError("sandbox_not_found", 404)
        data = self.records.get("hosted_sandboxes", handle.id, owner=context.user_id)
        if data is None or data["agent_id"] != handle.tags.get("session_id"):
            raise HostedAuthError("sandbox_not_found", 404)
        return context, runtime, data

    def create(self, spec):
        context, runtime = self._context(spec.tags.get("owner", ""))
        if spec.tags.get("provider", "codex") != "codex":
            raise HostedAuthError("hosted_provider_not_supported", 400)
        key = secrets.token_urlsafe(32)
        spec = replace(
            spec,
            secrets=[],
            resource_secrets=[],
            tags={
                **spec.tags,
                "hosted": "1",
                "modal_connection": context.connection_id,
                "modal_workspace": runtime["workspace"],
                "runtime_image": runtime["image"],
            },
            env={
                **spec.env,
                "SBX_RUNTIME_CONNECT_KEY": key,
                "SBX_RUNTIME_OWNER": context.user_id,
                "SBX_RUNTIME_AGENT_ID": spec.tags["session_id"],
            },
        )
        handle = self.provider.create(context, spec, runtime)
        cipher = self.connections.vault.seal({"key": key}, context=f"{context.user_id}:{handle.id}")
        try:
            self.records.put_owned(
                "hosted_sandboxes",
                handle.id,
                context.user_id,
                {
                    "agent_id": spec.tags["session_id"],
                    "key_cipher": cipher,
                    "runtime_version": runtime["runtime_version"],
                    "image": runtime["image"],
                    "workspace": runtime["workspace"],
                    "connection_id": context.connection_id,
                    "state": "live",
                },
            )
        except Exception:
            self.provider.terminate(context, handle)
            raise
        return handle

    def exec(self, handle, argv, env=None):
        context, _, _ = self._handle_context(handle)
        return self.provider.exec(context, handle, argv, dict(env or {}))

    def poll(self, handle):
        context, _, _ = self._handle_context(handle)
        return self.provider.poll(context, handle)

    def list(self, tags=None):
        tags = tags or {}
        if tags.get("owner"):
            owners = [tags["owner"]]
        else:
            with self.connections.auth.database.transaction() as conn:
                owners = [
                    row["user_id"]
                    for row in self.connections.auth.database.execute(
                        conn,
                        "SELECT user_id FROM hosted_connections "
                        "WHERE provider = 'modal' AND state = 'ready'",
                    ).fetchall()
                ]
        handles = []
        for owner in owners:
            try:
                context, _ = self._context(owner)
            except HostedAuthError:
                continue
            handles.extend(self.provider.list(context, tags))
        return handles

    def terminate(self, handle):
        context, _, data = self._handle_context(handle)
        self.provider.terminate(context, handle)
        data["state"] = "released"
        self.records.put_owned("hosted_sandboxes", handle.id, context.user_id, data)

    def connect(self, handle):
        context, _, data = self._handle_context(handle)
        if data["state"] != "live" or not self.provider.poll(context, handle).alive:
            raise HostedAuthError("sandbox_unavailable", 409)
        key = self.connections.vault.open(
            data["key_cipher"], context=f"{context.user_id}:{handle.id}"
        )["key"]
        url = self.provider.endpoint(context, handle, key)
        now = self.connections.auth.clock()
        token = jwt.encode(
            {
                "sub": context.user_id,
                "sid": data["agent_id"],
                "aud": f"sbx-runtime:{handle.id}",
                "scope": "events",
                "iat": now,
                "exp": now + 60,
                "jti": secrets.token_hex(16),
            },
            key,
            algorithm="HS256",
        )
        return {
            "url": f"{url}/sessions/{data['agent_id']}/events",
            "grant": token,
            "expires_at": now + 60,
            "transport": "sse",
            "terminal": False,
        }
