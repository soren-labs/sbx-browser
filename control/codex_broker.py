"""Control-plane-only Codex rotation with durable claims, CAS and short access leases."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from control.auth_store import _digest
from control.connections import ConnectionStore
from control.hosted_auth import HostedAuthError, _write


class InvalidGrant(Exception):
    pass


class ProviderUnauthorized(Exception):
    pass


class TransientProviderError(Exception):
    def __init__(self, retry_after=30):
        super().__init__("provider temporarily unavailable")
        self.retry_after = max(1, min(int(retry_after), 3600))


class CodexProvider(Protocol):
    configured: bool
    mock: bool

    def authorization_url(self, state: str) -> str: ...
    def exchange(self, owner: str, code: str) -> dict[str, Any]: ...
    def refresh(self, owner: str, refresh_token: str) -> dict[str, Any]: ...


class UnconfiguredCodexProvider:
    configured = False
    mock = False

    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise HostedAuthError("codex_not_configured", 503)

        return unavailable


class FakeCodexProvider:
    """Rotating fake upstream stores only hashes; reconstructing preserves grant validity."""

    configured = True
    mock = True

    def __init__(self, connections: ConnectionStore):
        self.connections = connections
        self.calls = 0
        self.calls_by_owner: dict[str, int] = {}
        self.revoked = False
        self.transient = False
        self.omit_refresh = False
        self.before_refresh = None

    def authorization_url(self, state):
        return "/integrations?codex_authorization=mock"

    def _tokens(self, owner, epoch, generation):
        identity = f"{owner}:{epoch}:{generation}"
        refresh_hash = hashlib.sha256((identity + ":refresh").encode()).hexdigest()
        return {
            "access_token": f"fake-access-{hashlib.sha256(identity.encode()).hexdigest()}",
            "refresh_token": f"fake-refresh-{refresh_hash}",
            "expires_at": self.connections.auth.clock() + 300,
            "account_id": owner,
            "models": ["gpt-5.6-luna", "gpt-6.1-sol"],
        }

    def exchange(self, owner, code):
        if code != f"mock:{owner}":
            raise InvalidGrant()
        auth = self.connections.auth
        epoch = secrets.token_hex(16)
        credentials = self._tokens(owner, epoch, 0)
        with _write(auth, f"fake-codex:{owner}") as conn:
            auth.database.execute(
                conn,
                "INSERT INTO control_records (namespace, id, owner, payload) "
                "VALUES ('fake_codex_grants', ?, ?, ?) ON CONFLICT(namespace, id) "
                "DO UPDATE SET payload = excluded.payload",
                (owner, owner, self._encode(epoch, 0, credentials["refresh_token"])),
            )
        return credentials

    @staticmethod
    def _encode(epoch, generation, refresh_token):
        import json

        return json.dumps(
            {"epoch": epoch, "generation": generation, "refresh_hash": _digest(refresh_token)}
        )

    def refresh(self, owner, refresh_token):
        if self.before_refresh:
            self.before_refresh()
        auth = self.connections.auth
        self.calls += 1
        self.calls_by_owner[owner] = self.calls_by_owner.get(owner, 0) + 1
        if self.revoked:
            raise InvalidGrant()
        if self.transient:
            raise TransientProviderError()
        import json

        with _write(auth, f"fake-codex:{owner}") as conn:
            row = auth.database.execute(
                conn,
                "SELECT payload FROM control_records "
                "WHERE namespace = 'fake_codex_grants' AND id = ? AND owner = ?",
                (owner, owner),
            ).fetchone()
            state = json.loads(row["payload"]) if row else {}
            if not secrets.compare_digest(state.get("refresh_hash", ""), _digest(refresh_token)):
                raise InvalidGrant()
            generation = state["generation"] + 1
            tokens = self._tokens(owner, state["epoch"], generation)
            if self.omit_refresh:
                tokens.pop("refresh_token")
            auth.database.execute(
                conn,
                "UPDATE control_records SET payload = ? "
                "WHERE namespace = 'fake_codex_grants' AND id = ? AND owner = ?",
                (
                    self._encode(
                        state["epoch"], generation, tokens.get("refresh_token", refresh_token)
                    ),
                    owner,
                    owner,
                ),
            )
        return tokens


@dataclass(frozen=True)
class AccessLease:
    connection_id: str
    credential_version: int
    expires_at: float
    access_token: str = field(repr=False)
    account_id: str = ""
    id_token: str | None = field(default=None, repr=False)

    def blob(self):
        import json

        tokens = {"access_token": self.access_token, "account_id": self.account_id}
        if self.id_token:
            tokens["id_token"] = self.id_token
        return {"provider": "codex", "files": {".codex/auth.json": json.dumps({"tokens": tokens})}}


class CodexBroker:
    def __init__(self, store: ConnectionStore, provider: CodexProvider, *, wait_seconds=20):
        self.store, self.provider, self.wait_seconds = store, provider, wait_seconds
        self._stop = threading.Event()
        self._worker = None

    def authorize(self, owner):
        if not self.provider.configured:
            raise HostedAuthError("codex_not_configured", 503)
        state = self.store.begin_authorization(owner, "codex")
        return {
            "state": state,
            "authorization_url": self.provider.authorization_url(state),
            "mock": self.provider.mock,
        }

    def callback(self, owner, state, code):
        self.store.consume_authorization(owner, "codex", state)
        try:
            credentials = self.provider.exchange(owner, code)
            self._validate(credentials)
        except Exception:
            raise HostedAuthError("codex_authorization_failed", 400) from None
        return self.store.connect(
            owner,
            "codex",
            credentials,
            metadata_factory=lambda version: {
                "credential_version": version,
                "expires_at": credentials["expires_at"],
                "models": credentials.get("models", ["gpt-5.6-luna"]),
            },
        )

    def _validate(self, credentials):
        if not credentials.get("access_token") or not credentials.get("refresh_token"):
            raise ValueError("incomplete provider grant")
        if float(credentials["expires_at"]) <= self.store.auth.clock():
            raise ValueError("expired provider grant")

    def lease(self, owner, *, rejected_version=None):
        auth = self.store.auth
        deadline = time.monotonic() + self.wait_seconds
        while True:
            error = None
            with _write(auth, f"connection:{owner}:codex") as conn:
                record = self.store.get(owner, "codex", conn=conn)
                if record is None or record.state in {"reauth_required", "disabled"}:
                    raise HostedAuthError("codex_connection_required", 409)
                now = auth.clock()
                if record.state == "refreshing":
                    if record.metadata.get("refresh_until", 0) <= now:
                        # Rotation may have succeeded before a process crash.
                        # Never replay an ambiguous refresh token automatically.
                        record.state = "reauth_required"
                        record.metadata["error"] = "refresh_interrupted"
                        self.store.save(record, conn=conn)
                        error = HostedAuthError("codex_reauth_required", 409)
                elif record.metadata.get("retry_at", 0) > now:
                    raise HostedAuthError(
                        "codex_refresh_cooldown",
                        503,
                        retry_after=int(record.metadata["retry_at"] - now) + 1,
                    )
                else:
                    credentials = self.store.credentials(record)
                    version = record.metadata.get("credential_version", 1)
                    reactive = rejected_version is not None and rejected_version == version
                    if not reactive and float(credentials["expires_at"]) > now + 60:
                        return self._lease(record, credentials)
                    record.state = "refreshing"
                    record.metadata.update(
                        {"refresh_claim": secrets.token_hex(16), "refresh_until": now + 60}
                    )
                    self.store.save(record, conn=conn)
                    break
            if error:
                raise error
            if time.monotonic() >= deadline:
                raise HostedAuthError("codex_refresh_in_progress", 503, retry_after=1)
            time.sleep(0.02)
        try:
            replacement = self.provider.refresh(owner, credentials["refresh_token"])
            merged = {**credentials, **replacement}
            # Providers may omit the replacement refresh token, but may not
            # return an empty one and silently erase the usable old grant.
            if not replacement.get("refresh_token"):
                merged["refresh_token"] = credentials["refresh_token"]
            self._validate(merged)
        except InvalidGrant:
            error = HostedAuthError("codex_reauth_required", 409)
            record.state = "reauth_required"
            record.metadata["error"] = "invalid_grant"
        except Exception as exc:
            delay = exc.retry_after if isinstance(exc, TransientProviderError) else 30
            error = HostedAuthError("codex_refresh_unavailable", 503, retry_after=delay)
            record.state = "connected"
            record.metadata["retry_at"] = auth.clock() + delay
            record.metadata["error"] = "refresh_unavailable"
        else:
            error = None
            record.state = "connected"
            record.credential_cipher = self.store.vault.seal(merged, context=record.context)
            record.metadata.update(
                {"credential_version": version + 1, "expires_at": merged["expires_at"]}
            )
            record.metadata.pop("error", None)
            record.metadata.pop("retry_at", None)
        record.metadata.pop("refresh_claim", None)
        record.metadata.pop("refresh_until", None)
        # CAS refuses a late response after reconnect/disable, including errors.
        self.store.save(record)
        if error:
            raise error
        return self._lease(record, merged)

    def _lease(self, record, credentials):
        return AccessLease(
            record.id,
            record.metadata.get("credential_version", 1),
            min(float(credentials["expires_at"]), self.store.auth.clock() + 300),
            credentials["access_token"],
            credentials.get("account_id", ""),
            credentials.get("id_token"),
        )

    def execute(self, owner, operation):
        lease = self.lease(owner)
        try:
            return operation(lease)
        except ProviderUnauthorized:
            # Exactly one retry; concurrent 401s join the already-rotated version.
            fresh = self.lease(owner, rejected_version=lease.credential_version)
            return operation(fresh)

    def disable(self, owner):
        with _write(self.store.auth, f"connection:{owner}:codex") as conn:
            record = self.store.get(owner, "codex", conn=conn)
            if record is None:
                raise HostedAuthError("not_found", 404)
            record.state, record.credential_cipher, record.metadata = "disabled", None, {}
            return self.store.save(record, conn=conn)

    def refresh_due(self):
        with self.store.auth.database.transaction() as conn:
            rows = self.store.auth.database.execute(
                conn,
                "SELECT user_id FROM hosted_connections "
                "WHERE provider = 'codex' AND state IN ('connected', 'refreshing')",
            ).fetchall()
        for row in rows:
            try:
                self.lease(row["user_id"])
            except HostedAuthError:
                pass

    def start(self):
        self._stop.clear()

        def run():
            while not self._stop.is_set():
                try:
                    self.refresh_due()
                except Exception:
                    pass  # Never log upstream exceptions/credential material.
                self._stop.wait(15)

        self._worker = threading.Thread(target=run, name="hosted-codex-refresh", daemon=True)
        self._worker.start()

    def stop(self):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=self.wait_seconds + 20)
