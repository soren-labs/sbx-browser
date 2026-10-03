from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from control.auth_store import AuthDatabase, AuthStore, IdentityConflict
from control.postgres_state import DatabaseRecords, PostgresRevisionStore, PostgresWorkflowStore
from control.revisions import Revision
from control.workflow_store import WorkflowTaskRecord


def test_owned_records_survive_reconstruction_and_cannot_change_owner(tmp_path):
    path = tmp_path / "state.sqlite3"
    auth = AuthStore(AuthDatabase(path=path))
    alice, bob = auth.create_user(), auth.create_user()
    records = DatabaseRecords(auth.database)
    records.put_owned("connections", "connection", alice.id, {"status": "connected"})
    restored = DatabaseRecords(AuthDatabase(path=path))
    assert restored.get("connections", "connection", owner=bob.id) is None
    assert restored.get("connections", "connection", owner=alice.id) == {"status": "connected"}
    with pytest.raises(IdentityConflict):
        restored.put_owned("connections", "connection", bob.id, {"status": "changed"})
    restored.delete("connections", "connection", owner=bob.id)
    assert restored.rows("connections", owner=alice.id)


def test_concurrent_workflow_bindings_survive_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    auth = AuthStore(AuthDatabase(path=path))
    alice = auth.create_user()

    def attach(index):
        store = PostgresWorkflowStore(DatabaseRecords(AuthDatabase(path=path)))
        store.attach(
            WorkflowTaskRecord(alice.id, "workflow", str(index), "author", f"agent-{index}")
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attach, range(16)))
    restored = PostgresWorkflowStore(DatabaseRecords(AuthDatabase(path=path)))
    assert len(restored.list_workflow(alice.id, "workflow")) == 16
    assert restored.list_workflow("other", "workflow") == []


def test_revision_metadata_survives_reconstruction(tmp_path):
    path = tmp_path / "state.sqlite3"
    records = DatabaseRecords(AuthDatabase(path=path))
    revision = Revision("rev-0123456789abcdef", "agent", 1, repo="https://github.com/mock/repo")
    PostgresRevisionStore(records).put_revision(revision)
    restored = PostgresRevisionStore(DatabaseRecords(AuthDatabase(path=path)))
    assert restored.get_revision(revision.revision_id) == revision
    assert restored.list_revisions("other") == []


def test_scoped_miss_does_not_release_another_users_live_lease():
    from types import SimpleNamespace

    from control.api_v1.state import V1State
    from control.ownership import ScopedSessionStore

    class Store:
        def get(self, _):
            return SimpleNamespace(owner="other", status="running")

    class Lease:
        def release(self):
            raise AssertionError("another user's live lease was released")

    state = V1State()
    state.set_lease("session", Lease())
    assert state.reconcile_leases(ScopedSessionStore(Store(), "user"), interval_s=0) == 0
