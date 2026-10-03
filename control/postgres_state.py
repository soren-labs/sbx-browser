"""PostgreSQL business state behind existing store interfaces (SQLite test dialect).

Typed record serializers and lifecycle logic are reused from the existing stores.
Only their mapping primitives change: every read/write hits the database, with
no process-local authority, Modal Dict, filesystem or read-through cache.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator, MutableMapping
from typing import Any

from control.artifacts import (
    InMemoryArtifactStore,
    manifest_dumps,
    manifest_loads,
    page_manifests,
)
from control.auth_store import AuthDatabase, AuthStore, IdentityConflict
from control.checkpoint import InMemoryCheckpointStore
from control.environment import InMemoryEnvironmentStore
from control.revisions import InMemoryRevisionStore
from control.run_activity import InMemoryRunActivityStore
from control.run_store import InMemoryRunStore
from control.store import InMemoryStore
from control.tasks import InMemoryTaskStore
from control.workflow_store import InMemoryWorkflowStore
from control.workspace import InMemoryWorkspaceStore


class DatabaseRecords:
    def __init__(self, database: AuthDatabase) -> None:
        self.database = database

    def get(self, namespace: str, key: str, *, owner: str | None = None) -> Any:
        with self.database.transaction() as conn:
            sql = "SELECT payload FROM control_records WHERE namespace = ? AND id = ?"
            params = (namespace, key)
            if owner is not None:
                sql += " AND owner = ?"
                params += (owner,)
            row = self.database.execute(conn, sql, params).fetchone()
        return json.loads(row["payload"]) if row else None

    def put(self, namespace: str, key: str, value: Any, *, owner: str | None = None) -> None:
        with self.database.transaction() as conn:
            cursor = self.database.execute(
                conn,
                "INSERT INTO control_records (namespace, id, owner, payload) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(namespace, id) DO UPDATE SET payload = excluded.payload, "
                "owner = excluded.owner, "
                "version = control_records.version + 1 "
                "WHERE control_records.owner = excluded.owner "
                "OR control_records.owner IS NULL",
                (namespace, key, owner, json.dumps(value, ensure_ascii=False)),
            )
            if cursor.rowcount != 1:
                raise IdentityConflict("resource ownership is immutable")

    def delete(self, namespace: str, key: str, *, owner: str | None = None) -> None:
        with self.database.transaction() as conn:
            sql = "DELETE FROM control_records WHERE namespace = ? AND id = ?"
            params = (namespace, key)
            if owner is not None:
                sql += " AND owner = ?"
                params += (owner,)
            self.database.execute(conn, sql, params)

    def rows(self, namespace: str, *, owner: str | None = None) -> list[tuple[str, Any]]:
        with self.database.transaction() as conn:
            sql = "SELECT id, payload FROM control_records WHERE namespace = ?"
            params = (namespace,)
            if owner is not None:
                sql += " AND owner = ?"
                params += (owner,)
            rows = self.database.execute(conn, sql + " ORDER BY id", params).fetchall()
        return [(row["id"], json.loads(row["payload"])) for row in rows]

    def put_owned(self, namespace: str, key: str, user_id: str, value: Any) -> None:
        with self.database.transaction() as conn:
            AuthStore(self.database)._require_user(conn, user_id)
        self.put(namespace, key, value, owner=user_id)


class DatabaseMapping(MutableMapping):
    """Durable mapping used by existing typed adapters; values are detached JSON."""

    def __init__(self, records: DatabaseRecords, namespace: str) -> None:
        self.records, self.namespace = records, namespace

    @staticmethod
    def _key(key: Any) -> str:
        return json.dumps(key, separators=(",", ":"))

    def __getitem__(self, key: Any) -> Any:
        value = self.records.get(self.namespace, self._key(key))
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: Any, value: Any) -> None:
        owner = value.get("owner") if isinstance(value, dict) else None
        if owner is None:
            agent_id = value.get("agent_id") if isinstance(value, dict) else None
            agent_id = agent_id or (str(key).split("/", 1)[0] if isinstance(key, str) else None)
            parent = self.records.get("sessions", self._key(agent_id)) if agent_id else None
            owner = parent.get("owner") if parent else None
        self.records.put(self.namespace, self._key(key), value, owner=owner)

    def __delitem__(self, key: Any) -> None:
        if key not in self:
            raise KeyError(key)
        self.records.delete(self.namespace, self._key(key))

    def __iter__(self) -> Iterator[Any]:
        for key, _ in self.records.rows(self.namespace):
            decoded = json.loads(key)
            yield tuple(decoded) if isinstance(decoded, list) else decoded

    def __len__(self) -> int:
        return len(self.records.rows(self.namespace))

    def values(self) -> list[Any]:
        return [value for _, value in self.records.rows(self.namespace)]

    def items(self) -> list[tuple[Any, Any]]:
        out = []
        for key, value in self.records.rows(self.namespace):
            decoded = json.loads(key)
            out.append((tuple(decoded) if isinstance(decoded, list) else decoded, value))
        return out


class PostgresSessionStore(InMemoryStore):
    def __init__(self, records: DatabaseRecords) -> None:
        super().__init__()
        self._items = DatabaseMapping(records, "sessions")


class PostgresRunStore(InMemoryRunStore):
    def __init__(self, records: DatabaseRecords) -> None:
        super().__init__()
        self._items = DatabaseMapping(records, "runs")


class PostgresTaskStore(InMemoryTaskStore):
    def __init__(self, records: DatabaseRecords) -> None:
        super().__init__()
        self._items = DatabaseMapping(records, "tasks")


class PostgresWorkspaceStore(InMemoryWorkspaceStore):
    def __init__(self, records: DatabaseRecords) -> None:
        super().__init__()
        self._items = DatabaseMapping(records, "workspaces")


class PostgresActivityStore(InMemoryRunActivityStore):
    def __init__(self, records: DatabaseRecords) -> None:
        super().__init__()
        self._items = DatabaseMapping(records, "activity")


class PostgresCheckpointStore(InMemoryCheckpointStore):
    def __init__(self, records: DatabaseRecords) -> None:
        super().__init__()
        self._items = DatabaseMapping(records, "checkpoints")


class PostgresEnvironmentStore(InMemoryEnvironmentStore):
    def __init__(self, records: DatabaseRecords) -> None:
        super().__init__()
        self._items = DatabaseMapping(records, "environments")


class PostgresRevisionStore(InMemoryRevisionStore):
    def __init__(self, records: DatabaseRecords) -> None:
        super().__init__()
        self._revisions = DatabaseMapping(records, "revisions")
        self._reviews = DatabaseMapping(records, "reviews")


class PostgresWorkflowStore(InMemoryWorkflowStore):
    def __init__(self, records: DatabaseRecords) -> None:
        super().__init__()
        self._agents = DatabaseMapping(records, "workflow_bindings")
        self._indexes = DatabaseMapping(records, "workflow_indexes")


class ArtifactMapping(DatabaseMapping):
    def __getitem__(self, key: Any) -> Any:
        value = super().__getitem__(key)
        return (
            manifest_dumps(manifest_loads(value["manifest"].encode())),
            {name: base64.b64decode(raw) for name, raw in value["members"].items()},
        )

    def __setitem__(self, key: Any, value: Any) -> None:
        manifest, members = value
        decoded = manifest_loads(manifest)
        super().__setitem__(
            key,
            {
                "agent_id": decoded.producer_agent_id,
                "manifest": manifest.decode(),
                "members": {name: base64.b64encode(raw).decode() for name, raw in members.items()},
            },
        )

    def values(self) -> list[Any]:
        return [self[key] for key in self]


class PostgresArtifactStore(InMemoryArtifactStore):
    def __init__(self, records: DatabaseRecords) -> None:
        super().__init__()
        self._items = ArtifactMapping(records, "artifacts")

    def list(self, *, agent_id: str | None = None) -> list[Any]:
        manifests = super().list()
        return [m for m in manifests if agent_id is None or m.producer_agent_id == agent_id]

    def list_page(self, *, agent_id=None, cursor=None, limit=None):
        return page_manifests(self.list(agent_id=agent_id), cursor=cursor, limit=limit)
