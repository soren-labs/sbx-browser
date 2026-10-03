"""Request-scoped views over the shared engine; workers retain trusted global stores."""

from __future__ import annotations

from typing import Any

from control.artifacts import ArtifactNotFoundError, page_manifests


def request_user_id(request: Any) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        key = request.app.state.api_key_store.lookup(token) if scheme.lower() == "bearer" else None
        return getattr(key, "user_id", None)
    if getattr(request.app.state, "hosted_mode", False):
        from control.hosted_auth_routes import COOKIE_NAME

        session = request.app.state.auth_store.lookup_session(request.cookies.get(COOKIE_NAME, ""))
        return session.user_id if session else None
    return None


class ScopedSessionStore:
    def __init__(self, source: Any, user_id: str) -> None:
        self.source, self.user_id = source, user_id

    def get(self, session_id: str) -> Any:
        record = self.source.get(session_id)
        return record if record is not None and record.owner == self.user_id else None

    def list_all(self) -> list[Any]:
        return [record for record in self.source.list_all() if record.owner == self.user_id]

    def put(self, record: Any) -> None:
        if record.owner != self.user_id:
            raise KeyError("session not found")
        self.source.put(record)

    def delete(self, session_id: str) -> None:
        if self.get(session_id) is not None:
            self.source.delete(session_id)


class ScopedControlPlane:
    def __init__(self, source: Any, user_id: str) -> None:
        self.source = source
        self.store = ScopedSessionStore(source.store, user_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)

    def get(self, session_id: str) -> Any:
        if self.store.get(session_id) is None:
            return None
        return self.source.get(session_id)


class ScopedArtifactStore:
    def __init__(self, source: Any, sessions: ScopedSessionStore) -> None:
        self.source, self.sessions = source, sessions

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)

    def manifest(self, artifact_id: str) -> Any:
        manifest = self.source.manifest(artifact_id)
        if self.sessions.get(manifest.producer_agent_id) is None:
            raise ArtifactNotFoundError("artifact not found")
        return manifest

    def open(self, artifact_id: str) -> Any:
        self.manifest(artifact_id)
        return self.source.open(artifact_id)

    def read(self, artifact_id: str, member: str) -> bytes:
        self.manifest(artifact_id)
        return self.source.read(artifact_id, member)

    def list(self, *, agent_id: str | None = None) -> list[Any]:
        return [
            m
            for m in self.source.list(agent_id=agent_id)
            if self.sessions.get(m.producer_agent_id) is not None
        ]

    def list_page(self, *, agent_id=None, cursor=None, limit=None):
        return page_manifests(self.list(agent_id=agent_id), cursor=cursor, limit=limit)

    def delete(self, artifact_id: str) -> None:
        self.manifest(artifact_id)
        self.source.delete(artifact_id)
