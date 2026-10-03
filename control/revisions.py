"""SOR-225: durable Revision / Review / Delivery lifecycle.

A *Revision* is the durable result of a successful code-changing Run: the
B1 artifact package (files + ``patch.diff`` / ``repo.bundle`` payloads,
per-file sha256, secret scan) plus the workspace identity it was produced
from — ``repo``, ``base_sha``, ``head_sha``, producer agent/run, and its
own ``delivery`` record. Revisions persist in their own store, so the
deliverable output of a run survives sandbox teardown and control-plane
restarts.

* *Materialization* — ``RevisionService.materialize`` snapshots a live
  workspace into the artifact store and records a revision. Runs that
  changed nothing (same head, clean tree) produce no revision. A new
  revision automatically marks every prior review on the agent's earlier
  revisions ``stale`` — a review is only ever valid against the revision
  it pinned.

* *Delivery* — ``RevisionService.deliver`` operates on the revision, not
  the sandbox: it rebuilds the payload host-side (``control.github_remote``)
  and pushes the recorded branch, then opens/updates the pull request via
  the server-side GitHub client (env PAT or GitHub App installation token).
  Failure is a first-class revision fact — ``delivery.status="failed"``
  with a clipped code/message — never swallowed into
  ``workspace.publish_error`` alone (it is mirrored there only for legacy
  compatibility).

* *Review* — a durable resource carrying reviewer identity / agent / run /
  verdict / findings and the pinned ``reviewed_head_sha``. ``independent``
  is computed at write: a review whose agent or run produced the revision
  can never satisfy the merge gate.

* *Merge* — ``RevisionService.merge`` requires a non-stale ``approve``
  review that is independent and pinned to the exact head being merged;
  the remote PR ref must still resolve to the pushed head, and the GitHub
  merge call carries that sha as the server-side required-head pin.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from control import github, github_remote
from control.artifact_ops import BUNDLE_MEMBER, snapshot_workspace_artifact
from control.artifacts import ArtifactStore
from control.github_remote import RemoteGitHub, RemoteGitHubError, push_payload
from control.workspace import (
    CHECKOUT_FAILED,
    HEAD_SHA_MISMATCH,
    REPO_UNAVAILABLE,
    REVIEW_REQUIRED,
    WORKSPACE_INVALID,
    WorkspaceError,
    WorkspaceService,
    create_issue_comment,
    git_head,
    is_commit_sha,
    is_safe_ref,
    run_git,
)

REVISIONS_DICT_NAME = "sbx-revisions"
REVISIONS_DICT_ENV = "SBX_REVISIONS_DICT"
REVISION_STORE_DIR_ENV = "SBX_REVISION_STORE_DIR"

# Error codes surfaced on the revision/review/delivery surface. Where a
# WorkspaceError code already exists the same string is reused so the /v1
# error mapping stays uniform.
REVISION_NOT_FOUND = "revision_not_found"
REVISION_NOT_READY = "revision_not_ready"
REVIEW_STALE = "review_stale"
INDEPENDENCE_VIOLATION = "independence_violation"
DELIVERY_FAILED = "delivery_failed"
DELIVERY_NOT_FOUND = "delivery_not_found"
MERGE_NOT_ALLOWED = "merge_not_allowed"

REVIEW_VERDICTS = ("approve", "request_changes", "comment")

_REVISION_ID_RE = re.compile(r"^rev-[0-9a-f]{8,32}$")
_REVIEW_ID_RE = re.compile(r"^rvw-[0-9a-f]{8,32}$")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


class RevisionError(Exception):
    """Canonical revision/review/delivery failure (code + message)."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Revision
# ---------------------------------------------------------------------------


@dataclass
class Revision:
    """One durable run result row.

    ``n`` is the per-agent sequence (1, 2, 3, …) — ``latest`` is the row with
    the highest ``n``. ``delivery`` is ``None`` until a deliver attempt; then
    ``{status, branch, pushed_head_sha, pull_request, error, delivered_at,
    merged, merge_commit_sha, merged_at}``.
    """

    revision_id: str
    agent_id: str
    n: int
    task_id: str | None = None
    run_id: str | None = None
    artifact_id: str | None = None
    repo: str = ""
    base_sha: str = ""
    head_sha: str = ""
    status: str = "ready"  # "ready" | "materialization_failed"
    error: dict[str, str] | None = None
    delivery: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.revision_id,
            "n": self.n,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "artifact_id": self.artifact_id,
            "repo": github.redact_url_credentials(self.repo) if self.repo else "",
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "status": self.status,
            "error": dict(self.error) if self.error else None,
            "delivery": dict(self.delivery) if self.delivery else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def new_revision_id() -> str:
    return f"rev-{uuid.uuid4().hex[:16]}"


def revision_to_dict(revision: Revision) -> dict[str, Any]:
    return {
        "revision_id": revision.revision_id,
        "agent_id": revision.agent_id,
        "n": revision.n,
        "task_id": revision.task_id,
        "run_id": revision.run_id,
        "artifact_id": revision.artifact_id,
        "repo": revision.repo,
        "base_sha": revision.base_sha,
        "head_sha": revision.head_sha,
        "status": revision.status,
        "error": revision.error,
        "delivery": revision.delivery,
        "created_at": revision.created_at,
        "updated_at": revision.updated_at,
    }


def revision_from_dict(data: Any) -> Revision:
    if not isinstance(data, dict):
        raise ValueError("revision record is not a dict")
    for key in ("revision_id", "agent_id"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"revision record missing {key}")
    n = data.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("revision record n must be a positive int")
    for key in ("task_id", "run_id", "artifact_id", "repo", "base_sha", "head_sha"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"revision record field {key} must be a string")
    error = data.get("error")
    if error is not None and not isinstance(error, dict):
        raise ValueError("revision record field error must be an object")
    delivery = data.get("delivery")
    if delivery is not None and not isinstance(delivery, dict):
        raise ValueError("revision record field delivery must be an object")
    return Revision(
        revision_id=data["revision_id"],
        agent_id=data["agent_id"],
        n=n,
        task_id=data.get("task_id"),
        run_id=data.get("run_id"),
        artifact_id=data.get("artifact_id"),
        repo=str(data.get("repo") or ""),
        base_sha=str(data.get("base_sha") or ""),
        head_sha=str(data.get("head_sha") or ""),
        status=str(data.get("status") or "ready"),
        error=dict(error) if error is not None else None,
        delivery=dict(delivery) if delivery is not None else None,
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
    )


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


@dataclass
class Review:
    """One durable review row.

    ``reviewed_head_sha`` pins the exact revision head the verdict covers.
    ``independent`` is computed at write time — a review produced by the
    revision's own agent or run is never independent. ``stale`` flips when
    a newer revision materializes for the same agent (or when the review
    was written against a non-latest revision in the first place).
    """

    review_id: str
    revision_id: str
    agent_id: str  # the revision's author agent (denormalized)
    reviewer_identity: str
    reviewer_agent_id: str | None = None
    reviewer_run_id: str | None = None
    verdict: str = "comment"
    findings: list[dict[str, Any]] = field(default_factory=list)
    reviewed_head_sha: str = ""
    independent: bool = True
    stale: bool = False
    comment_url: str | None = None
    created_at: str = ""
    # Durable idempotency pin {key_id, key, fingerprint} — a replayed
    # ``Idempotency-Key`` resolves to this review instead of writing a
    # duplicate (same bound shape as RunRecord.idempotency).
    idempotency: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.review_id,
            "revision_id": self.revision_id,
            "agent_id": self.agent_id,
            "reviewer": {
                "identity": self.reviewer_identity,
                "agent_id": self.reviewer_agent_id,
                "run_id": self.reviewer_run_id,
            },
            "verdict": self.verdict,
            "findings": [dict(f) for f in self.findings],
            "reviewed_head_sha": self.reviewed_head_sha,
            "independent": self.independent,
            "stale": self.stale,
            "comment_url": self.comment_url,
            "created_at": self.created_at,
        }


def new_review_id() -> str:
    return f"rvw-{uuid.uuid4().hex[:16]}"


def review_to_dict(review: Review) -> dict[str, Any]:
    return {
        "review_id": review.review_id,
        "revision_id": review.revision_id,
        "agent_id": review.agent_id,
        "reviewer_identity": review.reviewer_identity,
        "reviewer_agent_id": review.reviewer_agent_id,
        "reviewer_run_id": review.reviewer_run_id,
        "verdict": review.verdict,
        "findings": review.findings,
        "reviewed_head_sha": review.reviewed_head_sha,
        "independent": review.independent,
        "stale": review.stale,
        "comment_url": review.comment_url,
        "created_at": review.created_at,
        "idempotency": review.idempotency,
    }


def review_from_dict(data: Any) -> Review:
    if not isinstance(data, dict):
        raise ValueError("review record is not a dict")
    for key in ("review_id", "revision_id", "agent_id", "reviewer_identity"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"review record missing {key}")
    findings = data.get("findings") or []
    if not isinstance(findings, list) or any(not isinstance(f, dict) for f in findings):
        raise ValueError("review record findings must be a list of objects")
    for key in ("reviewer_agent_id", "reviewer_run_id", "comment_url"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ValueError(f"review record field {key} must be a string")
    idempotency = data.get("idempotency")
    if idempotency is not None and not isinstance(idempotency, dict):
        raise ValueError("review record field idempotency must be a dict")
    verdict = str(data.get("verdict") or "comment")
    if verdict not in REVIEW_VERDICTS:
        raise ValueError(f"review verdict must be one of {REVIEW_VERDICTS}: {verdict!r}")
    return Review(
        review_id=data["review_id"],
        revision_id=data["revision_id"],
        agent_id=data["agent_id"],
        reviewer_identity=data["reviewer_identity"],
        reviewer_agent_id=data.get("reviewer_agent_id"),
        reviewer_run_id=data.get("reviewer_run_id"),
        verdict=verdict,
        findings=[dict(f) for f in findings],
        reviewed_head_sha=str(data.get("reviewed_head_sha") or ""),
        independent=bool(data.get("independent", True)),
        stale=bool(data.get("stale", False)),
        comment_url=data.get("comment_url"),
        created_at=str(data.get("created_at") or ""),
        idempotency=dict(idempotency) if idempotency is not None else None,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@runtime_checkable
class RevisionStore(Protocol):
    """Durable store for revisions + their reviews."""

    def put_revision(self, revision: Revision) -> None: ...

    def get_revision(self, revision_id: str) -> Revision | None: ...

    def list_revisions(self, agent_id: str) -> list[Revision]: ...

    def put_review(self, review: Review) -> None: ...

    def get_review(self, review_id: str) -> Review | None: ...

    def list_reviews(
        self, agent_id: str | None = None, revision_id: str | None = None
    ) -> list[Review]: ...


class InMemoryRevisionStore:
    """Thread-safe dict store (tests, ephemeral deployments)."""

    def __init__(self) -> None:
        self._revisions: dict[str, dict[str, Any]] = {}
        self._reviews: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put_revision(self, revision: Revision) -> None:
        with self._lock:
            self._revisions[revision.revision_id] = revision_to_dict(revision)

    def get_revision(self, revision_id: str) -> Revision | None:
        with self._lock:
            raw = self._revisions.get(revision_id)
        return revision_from_dict(raw) if raw is not None else None

    def list_revisions(self, agent_id: str) -> list[Revision]:
        with self._lock:
            rows = list(self._revisions.values())
        out = [revision_from_dict(raw) for raw in rows if raw.get("agent_id") == agent_id]
        return sorted(out, key=lambda r: r.n)

    def put_review(self, review: Review) -> None:
        with self._lock:
            self._reviews[review.review_id] = review_to_dict(review)

    def get_review(self, review_id: str) -> Review | None:
        with self._lock:
            raw = self._reviews.get(review_id)
        return review_from_dict(raw) if raw is not None else None

    def list_reviews(
        self, agent_id: str | None = None, revision_id: str | None = None
    ) -> list[Review]:
        with self._lock:
            rows = list(self._reviews.values())
        out = []
        for raw in rows:
            rec = review_from_dict(raw)
            if agent_id is not None and rec.agent_id != agent_id:
                continue
            if revision_id is not None and rec.revision_id != revision_id:
                continue
            out.append(rec)
        return sorted(out, key=lambda r: r.created_at)


class FileRevisionStore:
    """Local durable store: ``root/revisions/<id>.json`` + ``root/reviews/<id>.json``.

    Atomic writes (tmp + os.replace); survives control-plane restarts —
    same re-open semantics as the task store.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def _put(self, subdir: str, name: str, data: dict[str, Any]) -> None:
        import json

        directory = self._root / subdir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _get(self, subdir: str, name: str) -> dict[str, Any] | None:
        import json

        path = self._root / subdir / f"{name}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _iter(self, subdir: str) -> list[dict[str, Any]]:
        directory = self._root / subdir
        if not directory.is_dir():
            return []
        out = []
        for path in sorted(directory.glob("*.json")):
            raw = self._get(subdir, path.stem)
            if raw is not None:
                out.append(raw)
        return out

    def put_revision(self, revision: Revision) -> None:
        with self._lock:
            self._put("revisions", revision.revision_id, revision_to_dict(revision))

    def get_revision(self, revision_id: str) -> Revision | None:
        raw = self._get("revisions", revision_id)
        if raw is None:
            return None
        try:
            return revision_from_dict(raw)
        except ValueError:
            return None

    def list_revisions(self, agent_id: str) -> list[Revision]:
        out = []
        for raw in self._iter("revisions"):
            try:
                rec = revision_from_dict(raw)
            except ValueError:
                continue
            if rec.agent_id == agent_id:
                out.append(rec)
        return sorted(out, key=lambda r: r.n)

    def put_review(self, review: Review) -> None:
        with self._lock:
            self._put("reviews", review.review_id, review_to_dict(review))

    def get_review(self, review_id: str) -> Review | None:
        raw = self._get("reviews", review_id)
        if raw is None:
            return None
        try:
            return review_from_dict(raw)
        except ValueError:
            return None

    def list_reviews(
        self, agent_id: str | None = None, revision_id: str | None = None
    ) -> list[Review]:
        out = []
        for raw in self._iter("reviews"):
            try:
                rec = review_from_dict(raw)
            except ValueError:
                continue
            if agent_id is not None and rec.agent_id != agent_id:
                continue
            if revision_id is not None and rec.revision_id != revision_id:
                continue
            out.append(rec)
        return sorted(out, key=lambda r: r.created_at)


class ModalDictRevisionStore:
    """``modal.Dict``-backed durable store for deployed control planes.

    Keys: ``revision/<id>`` → row, ``revisions/<agent_id>`` → id index,
    ``review/<id>`` → row, ``reviews/<agent_id>`` → id index,
    ``__agents__`` → agent-id index for agent-agnostic review lookups
    (``current_review``'s revision-only read depends on it).
    """

    def __init__(self, name: str = REVISIONS_DICT_NAME) -> None:
        import modal

        self._dict = modal.Dict.from_name(name, create_if_missing=True)
        self._index_ready = False

    def _d(self) -> Any:
        return self._dict

    def put_revision(self, revision: Revision) -> None:
        d = self._d()
        exists = self.get_revision(revision.revision_id) is not None
        d.put(f"revision/{revision.revision_id}", revision_to_dict(revision))
        if not exists:
            key = f"revisions/{revision.agent_id}"
            ids = list(d.get(key) or [])
            ids.append(revision.revision_id)
            d.put(key, ids)

    def get_revision(self, revision_id: str) -> Revision | None:
        raw = self._d().get(f"revision/{revision_id}")
        if raw is None:
            return None
        try:
            return revision_from_dict(raw)
        except ValueError:
            return None

    def list_revisions(self, agent_id: str) -> list[Revision]:
        out = []
        seen: set[str] = set()
        for revision_id in list(self._d().get(f"revisions/{agent_id}") or []):
            # The index is append-on-miss, not atomic — a raced double-put
            # can record the same id twice; never return the row twice.
            rid = str(revision_id)
            if rid in seen:
                continue
            seen.add(rid)
            rec = self.get_revision(rid)
            if rec is not None:
                out.append(rec)
        return sorted(out, key=lambda r: r.n)

    def put_review(self, review: Review) -> None:
        d = self._d()
        exists = self.get_review(review.review_id) is not None
        d.put(f"review/{review.review_id}", review_to_dict(review))
        if not exists:
            key = f"reviews/{review.agent_id}"
            ids = list(d.get(key) or [])
            ids.append(review.review_id)
            d.put(key, ids)
            agents = list(d.get("__agents__") or [])
            if review.agent_id not in agents:
                agents.append(review.agent_id)
                d.put("__agents__", agents)

    def _ensure_agent_index(self) -> None:
        """Backfill ``__agents__`` once for Dicts written before the index
        existed — a bounded ``keys()`` pass over ``reviews/`` index keys,
        then the write-maintained index is trusted (the SOR-199 listing
        contract still holds on the warm path)."""
        if self._index_ready:
            return
        d = self._d()
        if d.get("__agents__") is None:
            agents = sorted(
                str(key).split("/", 1)[1]
                for key in list(d.keys())
                if str(key).startswith("reviews/")
            )
            d.put("__agents__", agents)
        self._index_ready = True

    def get_review(self, review_id: str) -> Review | None:
        raw = self._d().get(f"review/{review_id}")
        if raw is None:
            return None
        try:
            return review_from_dict(raw)
        except ValueError:
            return None

    def list_reviews(
        self, agent_id: str | None = None, revision_id: str | None = None
    ) -> list[Review]:
        if agent_id is None:
            self._ensure_agent_index()
            agent_ids = list(self._d().get("__agents__") or [])
            out: list[Review] = []
            for aid in agent_ids:
                out.extend(self.list_reviews(agent_id=str(aid)))
            if revision_id is not None:
                out = [r for r in out if r.revision_id == revision_id]
            return sorted(out, key=lambda r: r.created_at)
        out = []
        for review_id in list(self._d().get(f"reviews/{agent_id}") or []):
            rec = self.get_review(str(review_id))
            if rec is not None and (revision_id is None or rec.revision_id == revision_id):
                out.append(rec)
        return sorted(out, key=lambda r: r.created_at)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int = 300) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _revision_rank(revision: Revision) -> tuple[Any, ...]:
    """Representative pick for stored rows sharing one ``run_id``: the
    ``ready`` row, then the one carrying delivery state, then the most
    recently updated — falling back to the lowest ``n`` so the winner is
    deterministic."""
    return (
        revision.status == "ready",
        bool(revision.delivery),
        revision.updated_at or "",
        revision.created_at or "",
        -revision.n,
        revision.revision_id,
    )


def _error_status(code: str) -> int:
    """HTTP-ish status for a canonical revision/delivery error code."""
    if code in (REVISION_NOT_FOUND, DELIVERY_NOT_FOUND, "artifact_not_found"):
        return 404
    if code in (WORKSPACE_INVALID, "artifact_invalid"):
        return 400
    if code == REPO_UNAVAILABLE:
        return 502
    return 409


class RevisionService:
    """Materialize / deliver / review / merge durable revisions.

    ``remote`` is a ``Callable[[str], RemoteGitHub] | RemoteGitHub | None``
    seam — tests inject a fake; production builds per-call from the env so
    App token refresh applies to every request.
    """

    def __init__(
        self,
        store: RevisionStore,
        artifacts: ArtifactStore,
        *,
        workspaces: WorkspaceService | None = None,
        env: Mapping[str, str] | None = None,
        remote: Any = None,
        clock: Callable[[], datetime] | None = None,
        env_for_repo: Any = None,
        push_payload_fn: Any = None,
        ls_remote_fn: Any = None,
    ) -> None:
        self._store = store
        self._artifacts = artifacts
        self._workspaces = workspaces
        self._env_map = env
        self._env_for_repo = env_for_repo
        self._push_payload = push_payload_fn
        self._ls_remote = ls_remote_fn
        self._remote = remote
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    # -- internals ---------------------------------------------------------

    def _now(self) -> str:
        return self._clock().isoformat()

    def _env(self, repo: str | None = None) -> Mapping[str, str]:
        if self._env_for_repo is not None and repo is not None:
            return self._env_for_repo(repo)
        return self._env_map if self._env_map is not None else os.environ

    def _remote_for(self, repo: str, *, require_token: bool = False) -> RemoteGitHub | None:
        """Server-side GitHub client, or ``None`` when no seam is configured.

        ``remote=None`` builds one from the env token source (unauthenticated
        when no credential exists — public repos still answer);
        ``require_token`` returns ``None`` instead when no credential
        resolves, so write operations fail closed with a clear error. A
        ``callable`` remote seam is invoked per repo.
        """
        remote = self._remote
        if remote is None:
            token = github_remote.server_token(repo, self._env(repo))
            if require_token and token is None:
                return None
            return RemoteGitHub(token)
        if callable(remote) and not isinstance(remote, RemoteGitHub):
            return remote(repo)
        return remote

    def _next_n(self, agent_id: str) -> int:
        existing = self.list(agent_id)
        return (existing[-1].n if existing else 0) + 1

    # -- reads --------------------------------------------------------------

    def get(self, revision_id: str) -> Revision:
        rec = self._store.get_revision(revision_id)
        if rec is None:
            raise RevisionError(
                REVISION_NOT_FOUND, f"unknown revision {revision_id!r}", status_code=404
            )
        return rec

    def list(self, agent_id: str) -> list[Revision]:
        """The agent's revisions — one row per logical revision.

        Revision identity is its producing ``run_id``: a run materializes
        at most one revision, so stored rows that share a non-empty
        ``run_id`` are the same logical revision (a duplicate commit from
        a settle/reconcile race, or a failed materialization later
        re-run). Each run group collapses to one representative — ``ready``
        beats ``materialization_failed``, then a recorded delivery, then
        the most recent update — so delivery state always travels with the
        surviving row. Ordering is ``(n, created_at, revision_id)``:
        stable and deterministic even when legacy duplicates share an ``n``.
        """
        by_id: dict[str, Revision] = {}
        for row in self._store.list_revisions(agent_id):
            by_id.setdefault(row.revision_id, row)
        by_run: dict[str, list[Revision]] = {}
        out: list[Revision] = []
        for row in by_id.values():
            if row.run_id:
                by_run.setdefault(row.run_id, []).append(row)
            else:
                out.append(row)
        for rows in by_run.values():
            out.append(max(rows, key=_revision_rank))
        return sorted(out, key=lambda r: (r.n, r.created_at, r.revision_id))

    def latest(self, agent_id: str) -> Revision | None:
        rows = self.list(agent_id)
        return rows[-1] if rows else None

    def resolve(self, agent_id: str, ref: str | None) -> Revision:
        """``ref``: ``None``/``"latest"`` → newest; ``rev-…`` → by id; an int
        string → per-agent sequence ``n``."""
        if ref is None or ref == "latest":
            rec = self.latest(agent_id)
            if rec is None:
                raise RevisionError(
                    REVISION_NOT_FOUND, f"agent {agent_id} has no revisions", status_code=404
                )
            return rec
        if _REVISION_ID_RE.fullmatch(ref):
            rec = self._store.get_revision(ref)
            if rec is None or rec.agent_id != agent_id:
                raise RevisionError(
                    REVISION_NOT_FOUND,
                    f"unknown revision {ref!r} for agent {agent_id}",
                    status_code=404,
                )
            return rec
        if re.fullmatch(r"[0-9]+", ref):
            n = int(ref)
            for rec in self.list(agent_id):
                if rec.n == n:
                    return rec
        raise RevisionError(
            REVISION_NOT_FOUND,
            f"revision ref {ref!r} does not resolve for agent {agent_id}",
            status_code=404,
        )

    def reviews(self, agent_id: str | None = None, revision_id: str | None = None) -> list[Review]:
        return self._store.list_reviews(agent_id=agent_id, revision_id=revision_id)

    def resolve_pr_url(self, pr_url: str) -> tuple[str, str]:
        """Resolve a GitHub pull URL to ``(ref, head_sha)`` — SOR-225 PR-URL
        handoff without caller-supplied ref/SHA.

        Uses the control-plane GitHub client when a credential resolves and
        falls back to ``git ls-remote`` on ``refs/pull/<n>/head`` — the same
        ref ``prepare_from_pull_request`` then fetches.
        """
        parsed = github_remote.parse_pull_url(pr_url)
        if parsed is None:
            raise RevisionError(
                WORKSPACE_INVALID,
                f"not a GitHub pull URL: {pr_url!r}",
                status_code=400,
            )
        slug, number = parsed
        remote = self._remote_for(f"https://github.com/{slug}")
        if remote is not None:
            try:
                data = remote.get_pull(slug, number)
            except RemoteGitHubError:
                data = None
            head = (data or {}).get("head") or {}
            sha = head.get("sha")
            if is_commit_sha(sha):
                return f"refs/pull/{number}/head", sha
        ref = f"refs/pull/{number}/head"
        sha = (self._ls_remote or github_remote.ls_remote)(
            f"https://github.com/{slug}", ref, env=self._env(f"https://github.com/{slug}")
        )
        if not is_commit_sha(sha):
            raise RevisionError(
                REPO_UNAVAILABLE,
                f"pull request ref {ref} not found on github.com:{slug}",
            )
        return ref, sha

    # -- materialization ----------------------------------------------------

    def materialize(
        self,
        backend: Any,
        handle: Any,
        agent_id: str,
        *,
        run_id: str | None = None,
        run_n: int | None = None,
        task_id: str | None = None,
        forbidden_values: Sequence[bytes | str] = (),
        ledger: Any = None,
    ) -> Revision | None:
        """Snapshot a live workspace into a durable revision.

        Returns ``None`` for a run that changed no code (same head, clean
        worktree) — only code-changing runs produce a revision. A snapshot
        failure is itself durable: the revision lands with
        ``status="materialization_failed"`` and the clipped error, so a
        secret-scan refusal or collection error is visible after teardown
        instead of vanishing with the sandbox.

        The git/snapshot work runs unlocked; ``_commit`` re-checks and
        inserts under the identity lock, so a concurrent settle/reconcile
        re-entry for the same run replays the committed row instead of
        writing a duplicate.
        """
        if self._workspaces is None:
            return None
        if run_id:
            # A run materializes at most one ready revision — a re-entrant
            # finish (control-plane reconcile settling the same turn from
            # evidence) replays the recorded one instead of duplicating.
            for existing in self.list(agent_id):
                if existing.run_id == run_id and existing.status == "ready":
                    return existing
        record = self._workspaces.get(agent_id)
        if record is None or not record.prepared or record.checkout_sha is None:
            return None
        try:
            status = run_git(backend, handle, ["status", "--porcelain"], cwd=record.workdir)
            if status.code != 0:
                raise WorkspaceError(
                    CHECKOUT_FAILED,
                    f"git status failed in workdir {record.workdir} (exit {status.code})",
                )
            dirty = any(line.strip() for line in status.lines)
            head = git_head(backend, handle, record.workdir)
            if head is None:
                raise WorkspaceError(
                    CHECKOUT_FAILED, f"no HEAD in workdir {record.workdir} for agent {agent_id}"
                )
            changed = dirty or head != record.checkout_sha
            if record.dirty != dirty:
                record.dirty = dirty
                self._workspaces.save(record)
        except WorkspaceError as exc:
            return self._record_failure(
                agent_id,
                run_id=run_id,
                task_id=task_id,
                code=exc.code,
                message=exc.message,
                repo=record.repo,
                base_sha=record.checkout_sha,
            )
        if not changed:
            return None
        try:
            manifest = snapshot_workspace_artifact(
                backend=backend,
                handle=handle,
                workspaces=self._workspaces,
                store=self._artifacts,
                agent_id=agent_id,
                run_id=run_id,
                forbidden_values=forbidden_values,
                ledger=ledger,
                run_n=run_n,
                clock=self._clock,
            )
        except Exception as exc:
            return self._record_failure(
                agent_id,
                run_id=run_id,
                task_id=task_id,
                code="artifact_invalid",
                message=str(exc),
                repo=record.repo,
                base_sha=record.checkout_sha,
            )
        return self._commit(
            agent_id,
            run_id=run_id,
            task_id=task_id,
            status="ready",
            error=None,
            repo=record.repo,
            base_sha=record.checkout_sha,
            artifact_id=manifest.artifact_id,
            head_sha=manifest.head_sha,
        )

    def _commit(
        self,
        agent_id: str,
        *,
        run_id: str | None,
        task_id: str | None,
        status: str,
        error: dict[str, str] | None,
        repo: str = "",
        base_sha: str = "",
        artifact_id: str | None = None,
        head_sha: str = "",
    ) -> Revision:
        """Allocate ``n`` + insert the revision under the identity lock.

        A ``ready`` row already committed for the same ``run_id`` is
        replayed — success supersedes an earlier failure and a re-entrant
        finish must never land a second row. A repeat failure verdict for
        the same run replays the recorded failure too; only a recovery
        (existing failed row + fresh ``ready`` outcome) appends, and the
        read-side collapse prefers the ``ready`` representative.
        """
        with self._lock:
            if run_id:
                for existing in self.list(agent_id):
                    if existing.run_id != run_id:
                        continue
                    if existing.status == "ready" or status == "materialization_failed":
                        return existing
            now = self._now()
            revision = Revision(
                revision_id=new_revision_id(),
                agent_id=agent_id,
                n=self._next_n(agent_id),
                task_id=task_id,
                run_id=run_id,
                repo=repo,
                base_sha=base_sha,
                artifact_id=artifact_id,
                head_sha=head_sha,
                status=status,
                error=error,
                created_at=now,
                updated_at=now,
            )
            self._store.put_revision(revision)
        # A newer revision exists — every earlier review no longer applies
        # to current state.
        self._mark_stale(agent_id)
        return revision

    def _record_failure(
        self,
        agent_id: str,
        *,
        run_id: str | None,
        task_id: str | None,
        code: str,
        message: str,
        repo: str = "",
        base_sha: str = "",
    ) -> Revision:
        return self._commit(
            agent_id,
            run_id=run_id,
            task_id=task_id,
            status="materialization_failed",
            error={"code": code, "message": _clip(message)},
            repo=repo,
            base_sha=base_sha,
        )

    def _mark_stale(self, agent_id: str) -> None:
        for review in self._store.list_reviews(agent_id=agent_id):
            if not review.stale:
                review.stale = True
                self._store.put_review(review)

    def void_for_run(self, agent_id: str, run_id: str, *, code: str, message: str) -> int:
        """Demote every ``ready`` revision produced by ``run_id``.

        A run whose durable verdict settled non-FINISHED (a cancel won the
        finish race) after its workspace already materialized must not keep
        a deliverable revision — cancelled work never reaches a branch/PR.
        The row is demoted to ``materialization_failed`` rather than
        dropped, so the artifact stays auditable while ``deliver`` refuses
        it (non-ready revisions raise ``revision_not_ready``).

        Runs under the identity lock over the raw store — every stored row
        for the run is demoted, including duplicates the read-side
        collapse keeps out of ``list``.
        """
        count = 0
        with self._lock:
            for revision in self._store.list_revisions(agent_id):
                if revision.run_id == run_id and revision.status == "ready":
                    revision.status = "materialization_failed"
                    revision.error = {"code": code, "message": _clip(message)}
                    revision.updated_at = self._now()
                    self._store.put_revision(revision)
                    count += 1
        return count

    def _payload(self, revision: Revision) -> tuple[str, bytes]:
        """``(kind, bytes)`` for the revision's artifact — bundle when the
        run committed cleanly, patch otherwise. Absent/corrupt is explicit."""
        if not revision.artifact_id:
            raise RevisionError(
                REVISION_NOT_READY,
                f"revision {revision.revision_id} has no artifact",
            )
        try:
            manifest = self._artifacts.manifest(revision.artifact_id)
        except Exception as exc:
            raise RevisionError(
                "artifact_not_found",
                f"revision {revision.revision_id} artifact "
                f"{revision.artifact_id} is unavailable: {_clip(str(exc))}",
                status_code=404,
            )
        member = BUNDLE_MEMBER if BUNDLE_MEMBER in manifest.payloads else "patch.diff"
        if member not in manifest.payloads:
            raise RevisionError(
                "artifact_invalid",
                f"revision {revision.revision_id} artifact has no patch/bundle payload",
            )
        data = self._artifacts.read(revision.artifact_id, member)
        if data is None:
            raise RevisionError(
                "artifact_invalid",
                f"revision {revision.revision_id} payload {member} is missing",
            )
        return ("bundle" if member == BUNDLE_MEMBER else "patch"), data

    # -- delivery -------------------------------------------------------------

    def _apply_pull_overrides(
        self,
        revision: Revision,
        repo: str,
        pr_data: dict[str, Any],
        pr_req: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply caller ``pull_request`` overrides onto a tracked open PR.

        ``deliver`` is declared "open/update the pull request": a caller
        that re-delivers with ``pull_request`` fields expects them on the
        existing request — ``title``/``body``/``target`` PATCH upstream,
        ``draft: false`` marks ready for review (GraphQL; REST cannot).
        The remote's post-update shape is merged into the durable record;
        a refusal surfaces as the canonical error instead of a silent drop.
        """
        slug = github.repo_slug(repo)
        remote = self._remote_for(repo, require_token=True) if slug else None
        update = getattr(remote, "update_pull", None) if remote is not None else None
        if slug is None or not callable(update):
            raise RevisionError(
                REPO_UNAVAILABLE,
                f"pull request overrides on revision {revision.revision_id} need a "
                "GitHub credential with pull-request write access on the control plane",
            )
        try:
            data = update(
                slug,
                int(pr_data.get("number")),
                title=pr_req.get("title"),
                body=pr_req.get("body"),
                base=(str(pr_req["target"]) if pr_req.get("target") else None),
                draft=(bool(pr_req["draft"]) if "draft" in pr_req else None),
            )
        except RemoteGitHubError as exc:
            code = getattr(exc, "code", REPO_UNAVAILABLE)
            raise RevisionError(
                code,
                _clip(getattr(exc, "message", str(exc))),
                status_code=_error_status(code),
            ) from exc
        out = dict(pr_data)
        if isinstance(data, dict) and data:
            url = data.get("html_url") or data.get("url")
            if isinstance(url, str) and url:
                out["url"] = url
            if isinstance(data.get("state"), str):
                out["state"] = data["state"]
            live_base = data.get("base")
            if isinstance(live_base, dict):
                live_base = live_base.get("ref")
            if isinstance(live_base, str) and live_base:
                out["base"] = live_base
            elif pr_req.get("target"):
                out["base"] = str(pr_req["target"])
            if isinstance(data.get("draft"), bool):
                out["draft"] = data["draft"]
        return out

    def _delivery_policy(
        self, revision: Revision, record: Any, overrides: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """The merged git/delivery policy a deliver attempt executes."""
        policy: dict[str, Any] = dict((record.git if record else {}) or {})
        overrides = dict(overrides or {})
        pr = overrides.get("pull_request")
        if overrides.get("branch"):
            policy["branch"] = overrides["branch"]
        if pr is not None:
            policy["auto_create_pr"] = True
            for key in ("title", "body", "target"):
                if pr.get(key):
                    policy[key] = pr[key]
            if "draft" in pr:
                policy["draft"] = bool(pr["draft"])
        # An explicit deliver call is itself the push authorization.
        policy["push"] = True
        return policy

    def deliver(
        self,
        revision: Revision,
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> Revision:
        """Deliver the revision: push its payload to the delivery branch and
        open/update the pull request — entirely control-plane-side.

        The payload is rebuilt from the durable artifact, so delivery works
        identically before and after the author sandbox is gone. Failure is
        first-class: ``delivery.status="failed"`` with the canonical error
        is persisted on the revision and raised to the caller — it is
        mirrored onto ``workspace.publish_error`` only as the legacy
        compatibility view.
        """
        if revision.status != "ready" or not revision.head_sha:
            raise RevisionError(
                REVISION_NOT_READY,
                f"revision {revision.revision_id} is not deliverable (status {revision.status})",
            )
        record = self._workspaces.get(revision.agent_id) if self._workspaces else None
        policy = self._delivery_policy(revision, record, overrides)
        repo = revision.repo or (record.repo if record else "")
        if not repo:
            raise RevisionError(
                WORKSPACE_INVALID,
                f"revision {revision.revision_id} has no repo",
                status_code=400,
            )
        branch = policy.get("branch") or (record.branch if record else None)
        branch = branch or f"sbx/{revision.agent_id}"
        if not is_safe_ref(branch):
            raise RevisionError(
                WORKSPACE_INVALID, f"unsafe branch name: {branch!r}", status_code=400
            )
        existing_delivery = revision.delivery or {}
        recorded_pr = existing_delivery.get("pull_request")
        pr_satisfied = (not policy.get("auto_create_pr")) or (
            isinstance(recorded_pr, dict)
            and recorded_pr.get("number") is not None
            and recorded_pr.get("state") not in ("merged", "closed")
        )
        if (
            existing_delivery.get("status") == "delivered"
            and existing_delivery.get("branch") == branch
            and existing_delivery.get("pushed_head_sha")
            and pr_satisfied
        ):
            # Idempotent replay: this exact revision payload already landed
            # on the resolved branch — return the durable record rather than
            # re-pushing (a patch-kind re-commit would mint a fresh sha and
            # non-FF fail against the delivered remote head). A replay that
            # asks for a pull request the recorded delivery never opened —
            # or whose recorded PR went terminal — is NOT a replay: fall
            # through so the PR leg runs (the push converges on the live tip).
            pr_req = (overrides or {}).get("pull_request")
            if (
                isinstance(pr_req, dict)
                and isinstance(recorded_pr, dict)
                and isinstance(recorded_pr.get("number"), int)
            ):
                # ``deliver`` is "open/update the PR": caller overrides on a
                # replayed delivery apply to the recorded open pull request
                # (title/body/base via PATCH, draft→ready via GraphQL) and
                # persist on the durable delivery record.
                updated = self._apply_pull_overrides(revision, repo, recorded_pr, pr_req)
                revision.delivery = {**existing_delivery, "pull_request": updated}
                revision.updated_at = self._now()
                self._store.put_revision(revision)
                if record is not None and self._workspaces is not None:
                    record.pull_request = updated
                    self._workspaces.save(record)
            return revision
        try:
            kind, payload = self._payload(revision)
            base_ref = (
                str(policy.get("target") or (record.base_ref if record else "") or "") or None
            )
            pushed = (self._push_payload or push_payload)(
                repo,
                branch,
                kind=kind,
                payload=payload,
                base_sha=revision.base_sha,
                head_sha=revision.head_sha,
                base_ref=base_ref,
                env=self._env(repo),
                # Pin the delivery commit's dates so a retry after a
                # crash between push and record re-mints the identical
                # sha and converges instead of non-FF failing.
                commit_date=revision.created_at or None,
            )
            pr_data: dict[str, Any] | None = dict((record.pull_request if record else None) or {})
            if pr_data.get("state") in ("merged", "closed"):
                # A terminal pull request is never carried onto a new
                # delivery — merge() itself leaves record.pull_request
                # merged, so the next revision must re-anchor instead.
                pr_data = None
            if policy.get("auto_create_pr"):
                existing = record.pull_request if record else None
                if existing and existing.get("state") in ("merged", "closed"):
                    # Recorded-terminal PRs are never reused: drop it so
                    # the find-or-create leg below re-anchors.
                    existing = None
                if existing:
                    # The locally-recorded state may lag upstream — refresh
                    # it when a remote resolves so a merged/closed PR is
                    # never carried forward as if still open.
                    state = existing.get("state")
                    number = existing.get("number")
                    # The recorded PR only applies when its head branch is
                    # the branch just pushed — a deliver to another branch
                    # must not graft that PR's number onto this revision
                    # (merge would then drift-check the wrong PR).
                    head_ref = existing.get("head_branch") or (record.branch if record else None)
                    slug = github.repo_slug(repo)
                    remote = self._remote_for(repo) if slug and isinstance(number, int) else None
                    if remote is not None:
                        try:
                            live = remote.get_pull(slug, number)
                            if live.get("merged") is True:
                                state = "merged"
                            elif isinstance(live.get("state"), str):
                                state = live["state"]
                            live_ref = (live.get("head") or {}).get("ref")
                            if isinstance(live_ref, str) and live_ref:
                                head_ref = live_ref
                        except RemoteGitHubError:
                            pass  # unreachable → trust the recorded state
                    if state in ("merged", "closed") or (
                        head_ref is not None and head_ref != branch
                    ):
                        existing = None
                    else:
                        # The tracked PR rides the pushed branch — head moved.
                        pr_data = dict(existing)
                        pr_data["head_sha"] = pushed
                        pr_data["head_branch"] = branch
                        pr_req = (overrides or {}).get("pull_request")
                        if isinstance(pr_req, dict):
                            pr_data = self._apply_pull_overrides(revision, repo, pr_data, pr_req)
                if existing is None:
                    slug = github.repo_slug(repo)
                    if slug is None:
                        raise RevisionError(
                            WORKSPACE_INVALID,
                            f"pull_request delivery requires a github.com repo: "
                            f"{github.redact_url_credentials(repo)!r}",
                            status_code=400,
                        )
                    remote = self._remote_for(repo, require_token=True)
                    if remote is None:
                        raise RevisionError(
                            REPO_UNAVAILABLE,
                            "no GitHub credential configured for control-plane "
                            "pull requests — set GH_TOKEN/GITHUB_TOKEN or "
                            "authorize the GitHub App",
                        )
                    # Find-or-create: a PR for this head branch may already
                    # exist upstream even when the local record tracks a
                    # different branch (or was never recorded).
                    data = None
                    find_pull = getattr(remote, "find_pull", None)
                    if callable(find_pull):
                        data = find_pull(slug, branch)
                    if data is None:
                        data = remote.create_pull(
                            slug,
                            head=branch,
                            base=str(
                                policy.get("target") or (record.base_ref if record else "main")
                            ),
                            title=str(policy.get("title") or f"sbx {revision.agent_id}"),
                            body=str(policy.get("body") or ""),
                            draft=bool(policy.get("draft")),
                        )
                    number = data.get("number")
                    pr_data = {
                        "number": number if isinstance(number, int) else None,
                        "url": data.get("html_url"),
                        "state": data.get("state") or "open",
                        "ref": f"refs/pull/{number}/head" if isinstance(number, int) else None,
                        "head_sha": pushed,
                        "head_branch": branch,
                        "base": str(
                            policy.get("target") or (record.base_ref if record else "main")
                        ),
                        "draft": bool(data.get("draft") or policy.get("draft")),
                    }
            elif pr_data and (record is None or record.branch in (None, branch)):
                pr_data["head_sha"] = pushed
            elif pr_data:
                # The recorded PR tracks a different branch — carrying it
                # forward would mis-link this revision's delivery to a pull
                # request the push never touched.
                pr_data = None
        except (RevisionError, RemoteGitHubError, WorkspaceError) as exc:
            code = getattr(exc, "code", "repo_unavailable")
            message = _clip(getattr(exc, "message", str(exc)))
            self._record_delivery_failure(revision, record, branch, code, message)
            if isinstance(exc, RevisionError):
                raise
            raise RevisionError(code, message, status_code=_error_status(code)) from exc
        now = self._now()
        revision.delivery = {
            "status": "delivered",
            "branch": branch,
            "pushed_head_sha": pushed,
            "pull_request": pr_data or None,
            "delivered_at": now,
        }
        revision.updated_at = now
        self._store.put_revision(revision)
        if record is not None and self._workspaces is not None:
            record.branch = branch
            record.pushed_head_sha = pushed
            if pr_data:
                record.pull_request = pr_data
            record.publish_error = None
            self._workspaces.save(record)
        return revision

    def _record_delivery_failure(
        self,
        revision: Revision,
        record: Any,
        branch: str,
        code: str,
        message: str,
    ) -> None:
        """Persist the failure on the revision (first-class) and mirror the
        legacy ``workspace.publish_error`` view when a record exists."""
        now = self._now()
        revision.delivery = {
            "status": "failed",
            "branch": branch,
            "pull_request": dict((record.pull_request if record else None) or {}) or None,
            "error": {"code": code, "message": message},
            "failed_at": now,
        }
        revision.updated_at = now
        self._store.put_revision(revision)
        if record is not None and self._workspaces is not None:
            record.publish_error = f"{code}: {message}"
            self._workspaces.save(record)

    def sync_delivery(self, revision: Revision, record: Any) -> Revision:
        """Mirror a sandbox-side publish outcome onto the revision (SOR-225):
        the legacy ``workspaces.publish`` path stays, but its result is
        durable revision state too — success records the delivered branch/PR,
        failure records first-class ``delivery.status="failed"``."""
        now = self._now()
        if record.publish_error:
            revision.delivery = {
                "status": "failed",
                "pull_request": dict(record.pull_request or {}) or None,
                "error": {
                    "code": record.publish_error.split(":", 1)[0] or "repo_unavailable",
                    "message": _clip(record.publish_error),
                },
                "failed_at": now,
            }
        elif record.pushed_head_sha:
            revision.delivery = {
                "status": "delivered",
                "branch": record.branch,
                "pushed_head_sha": record.pushed_head_sha,
                "pull_request": dict(record.pull_request or {}) or None,
                "delivered_at": now,
            }
        else:
            return revision
        revision.updated_at = now
        self._store.put_revision(revision)
        return revision

    # -- review ---------------------------------------------------------------

    def add_review(
        self,
        revision: Revision,
        *,
        reviewer_identity: str,
        reviewer_agent_id: str | None = None,
        reviewer_run_id: str | None = None,
        verdict: str,
        findings: Sequence[Mapping[str, Any]] = (),
        idempotency: dict[str, Any] | None = None,
        comment_url: str | None = None,
    ) -> Review:
        """Record a durable review pinned to the revision's current head.

        ``independent`` is computed — never caller-supplied: a reviewer that
        is the revision's own agent or run is marked dependent and can never
        satisfy the merge gate. A review on anything but the agent's latest
        revision is born stale.
        """
        if verdict not in REVIEW_VERDICTS:
            raise RevisionError(
                WORKSPACE_INVALID,
                f"verdict must be one of {REVIEW_VERDICTS}: {verdict!r}",
                status_code=400,
            )
        independent = True
        if reviewer_agent_id is not None and reviewer_agent_id == revision.agent_id:
            independent = False
        if reviewer_run_id is not None and reviewer_run_id == revision.run_id:
            independent = False
        latest = self.latest(revision.agent_id)
        stale = latest is not None and latest.revision_id != revision.revision_id
        review = Review(
            review_id=new_review_id(),
            revision_id=revision.revision_id,
            agent_id=revision.agent_id,
            reviewer_identity=reviewer_identity,
            reviewer_agent_id=reviewer_agent_id,
            reviewer_run_id=reviewer_run_id,
            verdict=verdict,
            findings=[dict(f) for f in findings],
            reviewed_head_sha=revision.head_sha,
            independent=independent,
            stale=stale,
            created_at=self._now(),
            idempotency=dict(idempotency) if idempotency is not None else None,
            comment_url=comment_url,
        )
        self._store.put_review(review)
        return review

    def save_review(self, review: Review) -> None:
        """Persist a mutated review row (e.g. a resolved ``comment_url``)."""
        self._store.put_review(review)

    def find_review_by_idempotency(self, agent_id: str, key_id: str, key: str) -> Review | None:
        """The review durably pinned to ``(api key, Idempotency-Key)``, if any.

        Same durable bound as the run/session pins: a replay that lands after
        a control-plane restart still resolves to the original review instead
        of writing a duplicate.
        """
        for review in self._store.list_reviews(agent_id=agent_id):
            pin = review.idempotency or {}
            if pin.get("key_id") == key_id and pin.get("key") == key:
                return review
        return None

    def current_review(self, revision: Revision) -> Review | None:
        """The gating review: newest non-stale verdict on this revision."""
        reviews = self._store.list_reviews(revision_id=revision.revision_id)
        for review in reversed(reviews):
            if not review.stale:
                return review
        return None

    # -- merge ------------------------------------------------------------------

    def merge(self, revision: Revision) -> Revision:
        """Merge the revision's delivered pull request — review-gated.

        Fail-closed chain: delivery must be delivered with a PR; the current
        non-stale review must exist (``review_required``), be independent
        (``independence_violation``), approve (``review_required``), and pin
        the exact revision head; the remote PR ref must still resolve to the
        pushed head (``head_sha_mismatch`` → re-review); then GitHub merges
        with the sha pin — a second server-side closed gate.
        """
        if revision.status != "ready":
            raise RevisionError(
                REVISION_NOT_READY, f"revision {revision.revision_id} is not mergeable"
            )
        delivery = revision.delivery or {}
        pr = delivery.get("pull_request") or {}
        number = pr.get("number")
        if delivery.get("status") != "delivered" or not isinstance(number, int):
            raise RevisionError(
                DELIVERY_NOT_FOUND,
                f"revision {revision.revision_id} has no delivered pull request",
            )
        if delivery.get("merged") is True:
            # Idempotent replay: the recorded merge already committed — the
            # remote PR now reads merged/closed, so re-running the drift
            # check would fail closed on its own success. Return the
            # durable record as-is.
            return revision
        record = self._workspaces.get(revision.agent_id) if self._workspaces else None
        pushed = delivery.get("pushed_head_sha") or revision.head_sha
        review = self.current_review(revision)
        if review is None:
            raise RevisionError(
                REVIEW_REQUIRED,
                f"revision {revision.revision_id} has no current review — "
                "an independent review must pin it first",
            )
        if not review.independent:
            raise RevisionError(
                INDEPENDENCE_VIOLATION,
                f"review {review.review_id} is not independent of revision "
                f"{revision.revision_id} (self-review)",
            )
        if review.verdict != "approve":
            raise RevisionError(
                REVIEW_REQUIRED,
                f"review {review.review_id} verdict {review.verdict!r} does not "
                "satisfy the merge gate",
            )
        if review.reviewed_head_sha != revision.head_sha:
            raise RevisionError(
                HEAD_SHA_MISMATCH,
                f"reviewed head {review.reviewed_head_sha} does not match "
                f"revision head {revision.head_sha} — re-review required",
            )
        repo = revision.repo or (record.repo if record else "")
        slug = github.repo_slug(repo)
        if slug is None:
            raise RevisionError(
                WORKSPACE_INVALID,
                f"merge requires a github.com repo: {github.redact_url_credentials(repo)!r}",
            )
        remote = self._remote_for(repo, require_token=True)
        if remote is None:
            raise RevisionError(
                REPO_UNAVAILABLE,
                "no GitHub credential configured for control-plane merge — "
                "set GH_TOKEN/GITHUB_TOKEN or authorize the GitHub App",
            )
        # Remote drift check: the live PR head must still be what was
        # delivered — and therefore what was reviewed.
        remote_sha: str | None = None
        pull_draft = False
        try:
            pull = remote.get_pull(slug, number)
            head = pull.get("head") or {}
            remote_sha = head.get("sha") if isinstance(head.get("sha"), str) else None
            if pull.get("merged") is True or pull.get("state") == "closed":
                raise RevisionError(
                    HEAD_SHA_MISMATCH,
                    f"PR #{number} is already {pull.get('state') or 'merged'}",
                )
            pull_draft = pull.get("draft") is True
        except RemoteGitHubError:
            # API unreachable (e.g. local dev): ls-remote is the same truth.
            remote_sha = (self._ls_remote or github_remote.ls_remote)(
                repo, f"refs/pull/{number}/head", env=self._env(repo)
            )
        if pull_draft:
            # A draft PR can never merge — say so in canonical shape instead
            # of letting GitHub's 405 escape as an unstructured 500.
            raise RevisionError(
                MERGE_NOT_ALLOWED,
                f"PR #{number} is a draft — mark it ready for review before merge",
            )
        if remote_sha != pushed:
            raise RevisionError(
                HEAD_SHA_MISMATCH,
                f"remote PR #{number} resolves to {remote_sha}, not the "
                f"reviewed/delivered head {pushed} — re-review required",
            )
        try:
            data = remote.merge_pull(slug, number, sha=pushed)
        except RemoteGitHubError as exc:
            detail = _clip(exc.message)
            if exc.status == 405:
                # GitHub's merge refusal for draft/blocked PRs is not a
                # transport failure — surface it as a structured conflict.
                raise RevisionError(
                    MERGE_NOT_ALLOWED,
                    f"GitHub refused merge of PR #{number}: {detail}",
                ) from exc
            if exc.status == 409:
                raise RevisionError(
                    HEAD_SHA_MISMATCH,
                    f"PR #{number} head moved during merge ({detail}) — re-review required",
                ) from exc
            raise RevisionError(
                REPO_UNAVAILABLE,
                f"merge of PR #{number} failed: {detail}",
                status_code=502,
            ) from exc
        if not data.get("merged"):
            detail = _clip(str(data.get("message") or ""))[:200]
            raise RevisionError(
                REPO_UNAVAILABLE,
                f"GitHub refused merge of PR #{number}" + (f": {detail}" if detail else ""),
                status_code=502,
            )
        now = self._now()
        merge_commit_sha = data.get("sha")
        delivery = dict(delivery)
        delivery["merged"] = True
        delivery["merge_commit_sha"] = merge_commit_sha if is_commit_sha(merge_commit_sha) else None
        delivery["merged_at"] = now
        revision.delivery = delivery
        revision.updated_at = now
        self._store.put_revision(revision)
        if record is not None and self._workspaces is not None:
            record.merge = {
                "merged": True,
                "merge_commit_sha": (merge_commit_sha if is_commit_sha(merge_commit_sha) else None),
                "head_sha": pushed,
                "merged_at": now,
            }
            record_pr = dict(record.pull_request or {})
            record_pr["state"] = "merged"
            record.pull_request = record_pr
            record.reviewed_head_sha = revision.head_sha
            self._workspaces.save(record)
        return revision

    def post_review_comment(
        self, revision: Revision, body: str, *, handle: Any = None
    ) -> str | None:
        """Post a machine-readable review comment on the delivered PR.

        Control-plane first: the server-side GitHub client posts when a
        credential resolves; a live sandbox (legacy injection bridge) is the
        fallback. Deliberately an issue comment — never a formal ``/reviews``
        approval, which would read as the author approving their own PR under
        the shared GitHub identity. Returns the comment URL when known.
        """
        if not isinstance(body, str) or not body:
            raise RevisionError(
                WORKSPACE_INVALID, "review comment body must be non-empty", status_code=400
            )
        record = self._workspaces.get(revision.agent_id) if self._workspaces else None
        delivery = revision.delivery or {}
        pr = delivery.get("pull_request") or (record.pull_request if record else None) or {}
        number = pr.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise RevisionError(
                DELIVERY_NOT_FOUND,
                f"revision {revision.revision_id} has no delivered pull request to comment on",
                status_code=404,
            )
        repo = revision.repo or (record.repo if record else "")
        slug = github.repo_slug(repo)
        if slug is None:
            raise RevisionError(
                WORKSPACE_INVALID,
                f"review comment requires a github.com repo: "
                f"{github.redact_url_credentials(repo)!r}",
            )
        remote = self._remote_for(repo, require_token=True)
        data: dict[str, Any] | None = None
        if remote is not None:
            data = remote.create_comment(slug, number, body=body)
        elif handle is not None and self._workspaces is not None and record is not None:
            data = create_issue_comment(
                self._workspaces.backend, handle, record.repo, number=number, body=body
            )
        else:
            raise RevisionError(
                REPO_UNAVAILABLE,
                "no GitHub credential configured for control-plane review "
                "comments — set GH_TOKEN/GITHUB_TOKEN or authorize the "
                "GitHub App",
            )
        url = data.get("html_url") if data else None
        if record is not None and self._workspaces is not None:
            record_pr = dict(record.pull_request or {})
            if isinstance(url, str) and url:
                record_pr["review_comment_url"] = url
            record.pull_request = record_pr
            self._workspaces.save(record)
        return url if isinstance(url, str) else None


__all__ = [
    "DELIVERY_FAILED",
    "DELIVERY_NOT_FOUND",
    "FileRevisionStore",
    "INDEPENDENCE_VIOLATION",
    "InMemoryRevisionStore",
    "MERGE_NOT_ALLOWED",
    "ModalDictRevisionStore",
    "REVISIONS_DICT_ENV",
    "REVISIONS_DICT_NAME",
    "REVISION_NOT_FOUND",
    "REVISION_NOT_READY",
    "REVISION_STORE_DIR_ENV",
    "REVIEW_STALE",
    "REVIEW_VERDICTS",
    "Revision",
    "RevisionError",
    "RevisionService",
    "RevisionStore",
    "Review",
    "new_review_id",
    "new_revision_id",
    "review_from_dict",
    "review_to_dict",
    "revision_from_dict",
    "revision_to_dict",
]
