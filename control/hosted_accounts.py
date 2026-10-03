"""Owned connection views for the existing account scheduler; credentials stay brokered."""

from __future__ import annotations

from typing import Any

from control.codex_broker import CodexBroker
from control.ports import Account
from control.postgres_state import DatabaseRecords
from control.scheduler import AccountScheduler, session_running_source


class HostedAccounts:
    def __init__(self, broker: CodexBroker, owner: str | None = None):
        self.broker, self.owner = broker, owner
        self.records = DatabaseRecords(broker.store.auth.database)
        self._running = lambda _: 0

    def scoped(self, owner):
        view = HostedAccounts(self.broker, owner)
        view._running = self._running
        return view

    def _record(self, account_id):
        auth = self.broker.store.auth
        with auth.database.transaction() as conn:
            sql = "SELECT user_id FROM hosted_connections WHERE provider = 'codex' AND id = ?"
            params = (account_id,)
            if self.owner:
                sql += " AND user_id = ?"
                params += (self.owner,)
            row = auth.database.execute(conn, sql, params).fetchone()
        return self.broker.store.get(row["user_id"], "codex") if row else None

    def _account(self, record):
        health = self.records.get("connection_health", record.id, owner=record.user_id) or {}
        status = health.get("status", "active")
        if record.state in {"reauth_required", "disabled"}:
            status = "invalid" if record.state == "reauth_required" else "disabled"
        return Account(
            record.id,
            "codex",
            "My Codex connection",
            status=status,
            max_concurrent=3,
            models=tuple(record.metadata.get("models", ["gpt-5.6-luna"])),
            created_at=record.updated_at,
            last_used_at=health.get("last_used_at"),
            cooldown_until=health.get("cooldown_until"),
            last_error=health.get("last_error"),
        )

    def list(self, provider=None):
        if provider not in (None, "codex"):
            return []
        auth = self.broker.store.auth
        with auth.database.transaction() as conn:
            sql = "SELECT id FROM hosted_connections WHERE provider = 'codex'"
            params = ()
            if self.owner:
                sql += " AND user_id = ?"
                params = (self.owner,)
            rows = auth.database.execute(conn, sql, params).fetchall()
        return [account for row in rows if (account := self.get(row["id"])) is not None]

    def get(self, account_id):
        record = self._record(account_id)
        return self._account(record) if record else None

    def get_credential_blob(self, account_id):
        record = self._record(account_id)
        if record is None or record.state in {"disabled", "reauth_required"}:
            return None
        return self.broker.lease(record.user_id).blob()

    def put_credential_blob(self, account_id, blob):
        raise PermissionError("hosted refresh state is owned by the VPS credential broker")

    def put(self, account):
        raise PermissionError("connect through the hosted provider authorization")

    def mark_status(self, account_id, status, *, cooldown_until=None, last_error=None):
        record = self._record(account_id)
        if record is None:
            raise KeyError(account_id)
        health = self.records.get("connection_health", account_id, owner=record.user_id) or {}
        # Only catalogued scheduler codes belong in durable public health metadata.
        from control.scheduler import failure_status

        safe_error = last_error if last_error and failure_status(last_error) else None
        health.update(
            {"status": status, "cooldown_until": cooldown_until, "last_error": safe_error}
        )
        self.records.put_owned("connection_health", account_id, record.user_id, health)
        return self._account(record)

    def touch(self, account_id, used_at):
        record = self._record(account_id)
        if record is None:
            raise KeyError(account_id)
        health = self.records.get("connection_health", account_id, owner=record.user_id) or {}
        health["last_used_at"] = used_at
        self.records.put_owned("connection_health", account_id, record.user_id, health)

    def remove(self, account_id):
        record = self._record(account_id)
        if record is None:
            raise KeyError(account_id)
        self.broker.disable(record.user_id)

    def running_count(self, account_id):
        return self._running(account_id) if self.get(account_id) else 0

    def bind_running(self, running):
        self._running = running


class HostedScheduling:
    """Reuse atomic scheduler leases/cooldowns, with an independent five-slot user cap."""

    def __init__(self, accounts: HostedAccounts, sessions):
        import threading

        self.accounts, self.sessions = accounts, sessions
        self._items: dict[str, Any] = {}
        self._lock = threading.Lock()

    def for_user(self, owner):
        with self._lock:
            if owner not in self._items:
                self._items[owner] = AccountScheduler(
                    self.accounts.scoped(owner),
                    max_global=5,
                    external_running=session_running_source(self.sessions),
                )
            return self._items[owner]
