"""Durable user-owned connections, authenticated encryption and OAuth state."""

from __future__ import annotations

import base64
import json
import os
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from control.auth_store import AuthStore, _digest, _iso
from control.hosted_auth import HostedAuthError, _write


class SecretVault:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("connection encryption key must contain 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def from_env(cls) -> SecretVault | None:
        value = os.environ.get("SBX_CONNECTION_ENCRYPTION_KEY")
        if not value:
            return None
        try:
            return cls(base64.urlsafe_b64decode(value))
        except Exception:
            raise ValueError("invalid connection encryption key") from None

    def seal(self, value: dict[str, Any], *, context: str) -> str:
        nonce = secrets.token_bytes(12)
        encoded = self._cipher.encrypt(nonce, json.dumps(value).encode(), context.encode())
        return base64.urlsafe_b64encode(nonce + encoded).decode()

    def open(self, value: str, *, context: str) -> dict[str, Any]:
        try:
            raw = base64.urlsafe_b64decode(value)
            return json.loads(self._cipher.decrypt(raw[:12], raw[12:], context.encode()))
        except Exception:
            raise HostedAuthError("connection_unavailable", 503) from None


@dataclass
class Connection:
    id: str
    user_id: str
    provider: str
    state: str
    credential_cipher: str | None = field(repr=False)
    metadata: dict[str, Any]
    version: int
    updated_at: str

    @property
    def context(self) -> str:
        return f"{self.user_id}:{self.provider}:{self.id}"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "state": self.state,
            "metadata": self.metadata,
            "version": self.version,
            "updated_at": self.updated_at,
        }


class ConnectionStore:
    def __init__(self, auth: AuthStore, vault: SecretVault | None) -> None:
        self.auth, self.vault = auth, vault

    def get(self, user_id: str, provider: str, *, conn: Any = None) -> Connection | None:
        if conn is None:
            with self.auth.database.transaction() as connection:
                return self.get(user_id, provider, conn=connection)
        row = self.auth.database.execute(
            conn,
            "SELECT * FROM hosted_connections WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["metadata"] = json.loads(data["metadata"])
        return Connection(**data)

    def save(self, record: Connection, *, conn: Any = None) -> Connection:
        if conn is None:
            with self.auth.database.transaction() as connection:
                return self.save(record, conn=connection)
        record.updated_at = _iso(self.auth.clock())
        cursor = self.auth.database.execute(
            conn,
            "UPDATE hosted_connections SET state = ?, credential_cipher = ?, metadata = ?, "
            "version = version + 1, updated_at = ? WHERE id = ? AND user_id = ? AND version = ?",
            (
                record.state,
                record.credential_cipher,
                json.dumps(record.metadata),
                record.updated_at,
                record.id,
                record.user_id,
                record.version,
            ),
        )
        if cursor.rowcount != 1:
            raise HostedAuthError("connection_changed", 409)
        record.version += 1
        return record

    def connect(self, user_id: str, provider: str, credentials: dict[str, Any]) -> Connection:
        if self.vault is None:
            raise HostedAuthError("encryption_not_configured", 503)
        with _write(self.auth, f"connection:{user_id}:{provider}") as conn:
            self.auth._require_user(conn, user_id)
            record = self.get(user_id, provider, conn=conn)
            if record is None:
                record = Connection(
                    f"conn_{uuid.uuid4().hex}",
                    user_id,
                    provider,
                    "connected",
                    None,
                    {},
                    1,
                    _iso(self.auth.clock()),
                )
                record.credential_cipher = self.vault.seal(credentials, context=record.context)
                self.auth.database.execute(
                    conn,
                    "INSERT INTO hosted_connections "
                    "(id, user_id, provider, state, credential_cipher, metadata, "
                    "version, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.id,
                        user_id,
                        provider,
                        record.state,
                        record.credential_cipher,
                        json.dumps(record.metadata),
                        record.version,
                        record.updated_at,
                    ),
                )
            else:
                record.credential_cipher = self.vault.seal(credentials, context=record.context)
                record.state, record.metadata = "connected", {}
                self.save(record, conn=conn)
        return record

    def credentials(self, record: Connection) -> dict[str, Any]:
        if self.vault is None or record.credential_cipher is None:
            raise HostedAuthError("connection_unavailable", 503)
        return self.vault.open(record.credential_cipher, context=record.context)

    def begin_authorization(self, user_id: str, provider: str) -> str:
        state = secrets.token_urlsafe(32)
        with self.auth.database.transaction() as conn:
            self.auth._require_user(conn, user_id)
            self.auth.database.execute(
                conn,
                "INSERT INTO connection_authorizations "
                "(state_hash, user_id, provider, expires_at) VALUES (?, ?, ?, ?)",
                (_digest(state), user_id, provider, self.auth.clock() + 600),
            )
        return state

    def consume_authorization(self, user_id: str, provider: str, state: str) -> None:
        with self.auth.database.transaction() as conn:
            cursor = self.auth.database.execute(
                conn,
                "UPDATE connection_authorizations SET consumed_at = ? WHERE state_hash = ? "
                "AND user_id = ? AND provider = ? AND expires_at > ? AND consumed_at IS NULL",
                (_iso(self.auth.clock()), _digest(state), user_id, provider, self.auth.clock()),
            )
            if cursor.rowcount != 1:
                raise HostedAuthError("invalid_authorization", 400)
