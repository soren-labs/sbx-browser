"""Pure FastAPI session API. Tests import this module; Modal decorators live in modal_app.py."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette._utils import create_collapsing_task_group
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Receive, Scope, Send

from control.api_v1 import router as api_v1_router
from control.api_v2 import router as api_v2_router
from control.auth_email import EmailSender, MockEmailSender, UnconfiguredEmailSender
from control.auth_store import AuthStore, configure_auth
from control.backend import LocalProcessBackend, SandboxBackend
from control.config import (
    DEFAULT_MODEL,
    RUN_ACTIVITY_DICT_NAME,
    RUNS_DICT_NAME,
    SESSIONS_DICT_NAME,
    SSE_KEEPALIVE_S,
    WORKFLOWS_DICT_NAME,
    basic_credentials,
    default_runner_cmd,
    env_float,
    env_int,
    env_str,
    lifecycle_config,
)
from control.hosted_auth import AuthRateLimiter, HostedAuthService
from control.hosted_auth_routes import router as hosted_auth_router
from control.run_activity import FileRunActivityStore, InMemoryRunActivityStore, RunActivityStore
from control.run_store import RunLedger, RunStore
from control.sandbox_io import sandbox_env
from control.scheduler import DEFAULT_MAX_GLOBAL
from control.service import (
    ConcurrencyLimit,
    ControlPlane,
    SessionConflict,
    format_sse,
    release_lease,
)
from control.store import InMemoryStore, SessionRecord, SessionStore
from control.workflow_store import WorkflowStore

security = HTTPBasic(auto_error=False)


class CreateSessionRequest(BaseModel):
    title: str | None = None
    model: str | None = None


class PostMessageRequest(BaseModel):
    text: str = Field(min_length=1)


def _http_error(code: int, error: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail={"error": error, "code": code},
        headers=headers,
    )


class DisconnectAwareStreamingResponse(StreamingResponse):
    """Watch ``http.disconnect`` even on ASGI spec ≥ 2.4 (uvicorn).

    Starlette's ``StreamingResponse`` only listens for disconnect on older spec
    versions, so a ``tail -F`` generator would never run ``finally`` / ``kill``.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await super().__call__(scope, receive, send)
            return

        try:
            async with create_collapsing_task_group() as task_group:

                async def wrap(func: Callable[[], Awaitable[None]]) -> None:
                    try:
                        await func()
                    except OSError:
                        pass
                    except anyio.get_cancelled_exc_class():
                        pass
                    finally:
                        task_group.cancel_scope.cancel()

                task_group.start_soon(wrap, partial(self.stream_response, send))
                await wrap(partial(self.listen_for_disconnect, receive))
        except anyio.get_cancelled_exc_class():
            pass
        finally:
            with anyio.CancelScope(shield=True):
                aclose = getattr(self.body_iterator, "aclose", None)
                if callable(aclose):
                    await aclose()
            if self.background is not None:
                await self.background()


class SPAStaticFiles(StaticFiles):
    """Vite/React Router build: real files plus ``index.html`` history fallback.

    Fallback rules: a missed request serves ``index.html`` only for GET/HEAD
    paths with no file extension — so ``/sessions`` or ``/settings`` deep
    links land in the React app, a missing ``/assets/*.js`` stays a real 404
    (a page must never be served in place of a script), and API prefixes
    (``/v1``/``/v2``/``/api``/``/.well-known``) never fall back to HTML.
    Content-hashed ``assets/*`` cache immutably; everything else revalidates.
    """

    _API_PREFIXES = ("v1", "v2", "api", "auth", "hosted", ".well-known")

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not self._spa_fallback(path, scope):
                raise
            return await self._index(scope)
        if response.status_code == 404 and self._spa_fallback(path, scope):
            return await self._index(scope)
        name = Path(path).name
        if name != "index.html" and path.startswith("assets/") and Path(path).suffix:
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response

    async def _index(self, scope: Scope) -> Response:
        response = await super().get_response("index.html", scope)
        response.headers["Cache-Control"] = "no-cache"
        return response

    def _spa_fallback(self, path: str, scope: Scope) -> bool:
        if scope["method"] not in ("GET", "HEAD"):
            return False
        first = path.split("/", 1)[0]
        if first in self._API_PREFIXES:
            return False
        return not Path(path).suffix


def _select_backend() -> SandboxBackend:
    kind = os.environ.get("SBX_BACKEND", "local")
    if kind == "modal":
        from control.backends.modal import ModalBackend

        return ModalBackend()
    return LocalProcessBackend()


def _select_store() -> SessionStore:
    kind = os.environ.get("SBX_BACKEND", "local")
    if kind == "modal":
        from control.store import ModalDictStore

        # ``SBX_SESSIONS_DICT`` lets a parallel deploy keep its own durable
        # Dict; the contract default is unchanged when unset.
        return ModalDictStore(env_str("SBX_SESSIONS_DICT", SESSIONS_DICT_NAME))
    return InMemoryStore()


def _select_run_store() -> RunStore:
    """SOR-82/A1: the durable run ledger's backing store.

    Production keeps run records in a ``modal.Dict`` (``sbx-runs``) so they
    survive control-plane restarts and sandbox teardown. Locally the ledger
    lives on disk under ``$SBX_RUN_STORE_DIR`` (or
    ``$XDG_STATE_HOME/sbx-browser/runs``) — same re-open semantics.
    """
    kind = os.environ.get("SBX_BACKEND", "local")
    if kind == "modal":
        from control.run_store import ModalDictRunStore

        return ModalDictRunStore(env_str("SBX_RUNS_DICT", RUNS_DICT_NAME))
    from pathlib import Path

    from control.run_store import FileRunStore

    override = os.environ.get("SBX_RUN_STORE_DIR")
    if override:
        return FileRunStore(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return FileRunStore(base / "sbx-browser" / "runs")


def _select_run_activity_store(run_store: RunStore) -> RunActivityStore:
    """Durable run transcripts live beside the run ledger's backing store."""
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.run_activity import ModalDictRunActivityStore

        return ModalDictRunActivityStore(env_str("SBX_RUN_ACTIVITY_DICT", RUN_ACTIVITY_DICT_NAME))
    from control.run_store import FileRunStore

    if isinstance(run_store, FileRunStore):
        return FileRunActivityStore(run_store.root.parent / "run-activity")
    return InMemoryRunActivityStore()


def _xdg_state_dir(name: str) -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "sbx-browser" / name


def _select_artifact_store() -> Any:
    """SOR-83: durable artifact package store (survives sandbox teardown)."""
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.artifacts import ARTIFACTS_DICT_NAME, ModalDictArtifactStore

        return ModalDictArtifactStore(env_str("SBX_ARTIFACTS_DICT", ARTIFACTS_DICT_NAME))
    from control.artifacts import FileArtifactStore

    override = os.environ.get("SBX_ARTIFACT_STORE_DIR")
    return FileArtifactStore(override or _xdg_state_dir("artifacts"))


def _select_workspace_store() -> Any:
    """SOR-83: durable workspace record store."""
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.workspace import WORKSPACES_DICT_NAME, ModalDictWorkspaceStore

        return ModalDictWorkspaceStore(env_str("SBX_WORKSPACES_DICT", WORKSPACES_DICT_NAME))
    from control.workspace import FileWorkspaceStore

    override = os.environ.get("SBX_WORKSPACE_STORE_DIR")
    return FileWorkspaceStore(override or _xdg_state_dir("workspaces"))


def _select_environment_store() -> Any:
    """SOR-127: durable environment build-record store."""
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.config import ENVIRONMENTS_DICT_NAME
        from control.environment import ModalDictEnvironmentStore

        return ModalDictEnvironmentStore(env_str("SBX_ENVIRONMENTS_DICT", ENVIRONMENTS_DICT_NAME))
    from control.environment import FileEnvironmentStore

    override = os.environ.get("SBX_ENV_STORE_DIR")
    return FileEnvironmentStore(override or _xdg_state_dir("environments"))


def _select_checkpoint_store() -> Any:
    """SOR-180: durable agent-checkpoint record store."""
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.checkpoint import ModalDictCheckpointStore
        from control.config import CHECKPOINTS_DICT_NAME

        return ModalDictCheckpointStore(env_str("SBX_CHECKPOINTS_DICT", CHECKPOINTS_DICT_NAME))
    from control.checkpoint import FileCheckpointStore

    override = os.environ.get("SBX_CHECKPOINT_STORE_DIR")
    return FileCheckpointStore(override or _xdg_state_dir("checkpoints"))


def _select_snapshot_provider(backend: SandboxBackend) -> Any:
    """SOR-127/SOR-180: filesystem snapshot/restore seam.

    Modal uses the native ``Sandbox.snapshot_filesystem`` primitive (image
    ids as refs); local dev/tests get directory copies under the snapshot
    root. The worker sandbox never holds Modal control credentials — both
    directions are driven control-plane-side. Shared by the environment
    cache (opt-in) and the per-agent checkpoint service (always on).
    """
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.backends.modal import ModalSnapshotProvider
        from control.config import ENV_SNAPSHOT_TIMEOUT_S, ENV_SNAPSHOT_TTL_S

        return ModalSnapshotProvider(
            backend,
            timeout_s=env_int("SBX_ENV_SNAPSHOT_TIMEOUT_S", ENV_SNAPSHOT_TIMEOUT_S),
            ttl_s=env_int("SBX_ENV_SNAPSHOT_TTL_S", ENV_SNAPSHOT_TTL_S),
        )
    from control.environment import LocalSnapshotProvider

    override = os.environ.get("SBX_ENV_SNAPSHOT_DIR")
    return LocalSnapshotProvider(backend, override or _xdg_state_dir("env-snapshots"))


def _select_workflow_store() -> WorkflowStore:
    """SOR-84 C1: the durable workflow/task metadata index.

    Production keeps the ``(owner, workflow_id) → tasks`` index in a
    ``modal.Dict`` (``sbx-workflows``) so it survives control-plane
    restarts. Locally it lives on disk under ``$SBX_WORKFLOW_STORE_DIR``
    (or ``$XDG_STATE_HOME/sbx-browser/workflows``) — same re-open
    semantics as the run ledger.
    """
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.workflow_store import ModalDictWorkflowStore

        return ModalDictWorkflowStore(env_str("SBX_WORKFLOWS_DICT", WORKFLOWS_DICT_NAME))
    from control.workflow_store import FileWorkflowStore

    override = os.environ.get("SBX_WORKFLOW_STORE_DIR")
    return FileWorkflowStore(override or _xdg_state_dir("workflows"))


def _select_task_store() -> Any:
    """SOR-222/223: durable public-Task store (requested vs resolved).

    Production keeps task records in a ``modal.Dict`` (``sbx-tasks``) so
    they survive control-plane restarts; locally they live under
    ``$SBX_TASK_STORE_DIR`` (or ``$XDG_STATE_HOME/sbx-browser/tasks``) —
    same re-open semantics as the workspace store.
    """
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.tasks import TASKS_DICT_ENV, TASKS_DICT_NAME, ModalDictTaskStore

        return ModalDictTaskStore(env_str(TASKS_DICT_ENV, TASKS_DICT_NAME))
    from control.tasks import TASK_STORE_DIR_ENV, FileTaskStore

    override = os.environ.get(TASK_STORE_DIR_ENV)
    return FileTaskStore(override or _xdg_state_dir("tasks"))


def _select_revision_store() -> Any:
    """SOR-225: durable Revision/Review store.

    Production keeps revision + review records in a ``modal.Dict``
    (``sbx-revisions``) so delivery/review survive sandbox teardown and
    control-plane restarts; locally they live under
    ``$SBX_REVISION_STORE_DIR`` (or ``$XDG_STATE_HOME/sbx-browser/revisions``).
    """
    if os.environ.get("SBX_BACKEND", "local") == "modal":
        from control.revisions import (
            REVISIONS_DICT_ENV,
            REVISIONS_DICT_NAME,
            ModalDictRevisionStore,
        )

        return ModalDictRevisionStore(env_str(REVISIONS_DICT_ENV, REVISIONS_DICT_NAME))
    from control.revisions import REVISION_STORE_DIR_ENV, FileRevisionStore

    override = os.environ.get(REVISION_STORE_DIR_ENV)
    return FileRevisionStore(override or _xdg_state_dir("revisions"))


def create_app(
    *,
    backend: SandboxBackend | None = None,
    store: SessionStore | None = None,
    run_store: RunStore | None = None,
    artifact_store: Any | None = None,
    workspace_store: Any | None = None,
    run_activity_store: RunActivityStore | None = None,
    workflow_store: WorkflowStore | None = None,
    task_store: Any | None = None,
    revision_store: Any | None = None,
    auth_store: AuthStore | None = None,
    email_sender: EmailSender | None = None,
    auth_rate_limiter: AuthRateLimiter | None = None,
    state_backend: str | None = None,
    hosted: bool | None = None,
    connection_vault: Any = None,
    modal_provider: Any = None,
    github_factory: Any = None,
    codex_provider: Any = None,
    compute_provider: Any = None,
    runner_cmd: list[str] | None = None,
    basic_user: str | None = None,
    basic_password: str | None = None,
    clock: Any | None = None,
    keepalive_s: float | None = None,
    max_concurrent: int | None = None,
    default_model: str | None = None,
    idle_timeout_s: int | None = None,
    turn_max_seconds: int | None = None,
) -> FastAPI:
    backend_kind = os.environ.get("SBX_BACKEND", "local")
    hosted = hosted if hosted is not None else os.environ.get("SBX_HOSTED") == "1"
    state_backend = state_backend or os.environ.get(
        "SBX_STATE_BACKEND", "postgres" if hosted else "legacy"
    )
    if state_backend not in {"postgres", "legacy"}:
        raise ValueError("SBX_STATE_BACKEND must be postgres or legacy")
    if hosted and state_backend != "postgres":
        raise ValueError("hosted mode requires PostgreSQL state")
    database_records = None
    if state_backend == "postgres":
        from control.auth_store import AuthDatabase
        from control.postgres_state import (
            DatabaseRecords,
            PostgresActivityStore,
            PostgresArtifactStore,
            PostgresRevisionStore,
            PostgresRunStore,
            PostgresSessionStore,
            PostgresTaskStore,
            PostgresWorkflowStore,
            PostgresWorkspaceStore,
        )

        if auth_store is None:
            if not os.environ.get("DATABASE_URL"):
                raise ValueError("PostgreSQL state requires DATABASE_URL")
            auth_store = AuthStore(AuthDatabase.from_env())
        database_records = DatabaseRecords(auth_store.database)
        store = store if store is not None else PostgresSessionStore(database_records)
        run_store = run_store if run_store is not None else PostgresRunStore(database_records)
        run_activity_store = (
            run_activity_store
            if run_activity_store is not None
            else PostgresActivityStore(database_records)
        )
        artifact_store = (
            artifact_store
            if artifact_store is not None
            else PostgresArtifactStore(database_records)
        )
        workspace_store = (
            workspace_store
            if workspace_store is not None
            else PostgresWorkspaceStore(database_records)
        )
        workflow_store = (
            workflow_store
            if workflow_store is not None
            else PostgresWorkflowStore(database_records)
        )
        task_store = task_store if task_store is not None else PostgresTaskStore(database_records)
        revision_store = (
            revision_store
            if revision_store is not None
            else PostgresRevisionStore(database_records)
        )
    backend = backend or _select_backend()
    store = store or _select_store()
    run_store = run_store or _select_run_store()
    run_activity_store = run_activity_store or _select_run_activity_store(run_store)
    artifact_store = artifact_store or _select_artifact_store()
    workspace_store = workspace_store or _select_workspace_store()
    workflow_store = workflow_store or _select_workflow_store()
    task_store = task_store or _select_task_store()
    revision_store = revision_store or _select_revision_store()
    runner_cmd = runner_cmd or default_runner_cmd(backend_kind=backend_kind)
    user_default, pass_default = basic_credentials()
    basic_user = basic_user if basic_user is not None else user_default
    basic_password = basic_password if basic_password is not None else pass_default
    keepalive = (
        keepalive_s
        if keepalive_s is not None
        else env_float("SBX_SSE_KEEPALIVE_SECONDS", SSE_KEEPALIVE_S)
    )
    from control.artifact_ops import HandoffStoreView, credential_forbidden_values
    from control.handoff import HandoffService
    from control.workspace import WorkspaceService

    # SOR-132/SOR-134 + SOR-135: one resolved lifecycle chain — the values
    # here are the same ones the reaper and ``Sandbox.create`` resolve
    # (``plane.idle_timeout_s`` is the post-session retention only; the
    # sandbox's native bound is ``lifecycle.sandbox_idle_timeout_s``).
    lifecycle = lifecycle_config()
    hosted_connections = None
    github_connections = None
    if hosted:
        from control.connections import ConnectionStore, SecretVault
        from control.hosted_github import GitHubScopedBackend, HostedGitHub

        hosted_connections = ConnectionStore(
            auth_store, connection_vault if connection_vault is not None else SecretVault.from_env()
        )
        github_connections = HostedGitHub(
            hosted_connections,
            mock=os.environ.get("SBX_CONNECTIONS_MODE") == "mock",
            factory=github_factory,
        )
        from control.hosted_compute import (
            FakeComputeProvider,
            HostedModalBackend,
            UnconfiguredComputeProvider,
        )

        compute_provider = compute_provider or (
            FakeComputeProvider(backend, clock=lambda: auth_store.clock())
            if os.environ.get("SBX_CONNECTIONS_MODE") == "mock"
            else UnconfiguredComputeProvider()
        )
        backend = HostedModalBackend(hosted_connections, compute_provider)
        backend = GitHubScopedBackend(backend, github_connections, workspace_store)
    workspaces = WorkspaceService(backend, workspace_store, clock=clock)
    handoffs = HandoffService(workspaces, HandoffStoreView(artifact_store))
    plane = ControlPlane(
        backend,
        store,
        runner_cmd,
        clock=clock,
        # SOR-271 round-4: the plane cap and the scheduler's global cap are
        # the same design knob (v2 §3.3) — resolve both from
        # ``SBX_MAX_CONCURRENT`` with the same ``DEFAULT_MAX_GLOBAL``
        # fallback. The old ``MAX_CONCURRENT=2`` fallback silently held the
        # production plane at 2 live agents whenever the env var was absent
        # remotely (the ~2-live ``concurrency_limit`` wedge).
        max_concurrent=max_concurrent
        if max_concurrent is not None
        else env_int("SBX_MAX_CONCURRENT", DEFAULT_MAX_GLOBAL),
        default_model=default_model or os.environ.get("SBX_DEFAULT_MODEL", DEFAULT_MODEL),
        idle_timeout_s=idle_timeout_s if idle_timeout_s is not None else lifecycle.idle_timeout_s,
        turn_max_seconds=turn_max_seconds
        if turn_max_seconds is not None
        else lifecycle.turn_max_seconds,
        run_ledger=RunLedger(run_store, clock=clock),
        workspaces=workspaces,
        handoffs=handoffs,
    )

    credential_refresher_factory: Callable[[], Any] | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Construction stays lazy for the import-time app, but the server
        # cannot serve even bootstrap /v1/me until durable auth is ready.
        # Run bounded, synchronous initialization before starting the refresher
        # or yielding readiness; startup needs no thread-pool round trip.
        app.state.auth_store.database.initialize()
        if hosted:
            app.state.codex_broker.start()
        refresher = credential_refresher_factory() if credential_refresher_factory else None
        if refresher is not None:
            plane.credential_refresher = refresher
        try:
            if refresher is not None:
                refresher.start()
            yield
        finally:
            if hosted:
                app.state.codex_broker.stop()
                stop_compute = getattr(compute_provider, "stop", None)
                if stop_compute:
                    stop_compute()
            if refresher is not None:
                refresher.stop()

    app = FastAPI(title="sbx-control", version="0.1.1", lifespan=lifespan)
    app.include_router(api_v1_router)  # empty shell until P2-D (SOR-64)
    app.include_router(api_v2_router)  # Session-first facade (SOR-256)
    app.include_router(hosted_auth_router)
    from control.hosted_routes import router as hosted_router

    app.include_router(hosted_router)
    app.state.plane = plane
    app.state.hosted_mode = hosted
    app.state.database_records = database_records
    app.state.compute_provider = compute_provider
    app.state.run_store = run_store
    app.state.run_ledger = plane.run_ledger
    plane.run_activity = run_activity_store
    app.state.run_activity = run_activity_store
    app.state.artifact_store = artifact_store
    app.state.workspace_store = workspace_store
    app.state.workspaces = workspaces
    app.state.handoffs = handoffs

    def _snapshot_on_close(rec: Any, handle: Any) -> None:
        """SOR-83: persist the declared workspace artifact before teardown.

        Best-effort — the close path swallows failures; agents without a
        declared workspace simply skip (``workspace_not_found``).
        """
        from control.artifact_ops import snapshot_workspace_artifact

        account_id = (rec.sandbox_tags or {}).get("account_id")
        registry = getattr(app.state, "account_registry", None)
        get_blob = getattr(registry, "get_credential_blob", None)
        blob = None
        if callable(get_blob) and account_id and account_id != "auto":
            try:
                blob = get_blob(account_id)
            except Exception:
                pass
        run_n = rec.turns or None
        snapshot_workspace_artifact(
            backend=backend,
            handle=handle,
            workspaces=workspaces,
            store=artifact_store,
            agent_id=rec.id,
            run_id=f"run-{run_n}" if run_n else None,
            forbidden_values=credential_forbidden_values(blob),
            ledger=plane.run_ledger,
            run_n=run_n,
        )

    plane.snapshot_hook = _snapshot_on_close

    # SOR-225: every successful code-changing run materializes a durable
    # Revision while its sandbox is still readable — the revision (artifact
    # + repo/base/head + delivery) is the object delivery/review/merge then
    # operate on after teardown.
    from control.revisions import RevisionService

    revisions = RevisionService(revision_store, artifact_store, workspaces=workspaces)
    app.state.revision_store = revision_store
    app.state.revisions = revisions
    plane.revisions = revisions

    def _revision_on_finish(rec: Any, handle: Any, n: int) -> None:
        """Materialize the revision for a finished run; task_id resolved
        from the durable task store; credential blobs feed the same
        forbidden-value secret scan as artifact snapshots."""
        from control.artifact_ops import credential_forbidden_values

        account_id = (rec.sandbox_tags or {}).get("account_id")
        registry = getattr(app.state, "account_registry", None)
        get_blob = getattr(registry, "get_credential_blob", None)
        blob = None
        if callable(get_blob) and account_id and account_id != "auto":
            try:
                blob = get_blob(account_id)
            except Exception:
                pass
        task = None
        try:
            task = task_store.find_by_agent(rec.id)
        except Exception:
            pass
        revisions.materialize(
            backend,
            handle,
            rec.id,
            run_id=f"run-{n}",
            run_n=n,
            task_id=task.id if task is not None else None,
            forbidden_values=credential_forbidden_values(blob),
            ledger=plane.run_ledger,
        )

    plane.revision_hook = _revision_on_finish

    # SOR-180: same-agent checkpoint / suspend / recovery — always armed
    # (not opt-in): an idle agent past its retention is checkpointed +
    # released to a recoverable ``suspended`` state by the reaper sweep,
    # and a follow-up message restores the same Agent id, filesystem and
    # native provider session. Shares the snapshot seam with the env
    # cache; credentials are scrubbed pre-snapshot and re-attached
    # in-sandbox on restore — never durable product state.
    from control.checkpoint import CheckpointService

    snapshot_provider = _select_snapshot_provider(backend)
    checkpoint_store = _select_checkpoint_store() if database_records is None else None
    if database_records is not None:
        from control.postgres_state import PostgresCheckpointStore

        checkpoint_store = PostgresCheckpointStore(database_records)
    app.state.checkpoints = CheckpointService(
        backend,
        checkpoint_store,
        snapshots=snapshot_provider,
        workspaces=workspaces,
        clock=clock,
    )
    plane.checkpoints = app.state.checkpoints

    # SOR-127 environment build/snapshot cache — opt-in (``SBX_ENV_CACHE=1``).
    # When armed, the /v1 worker resolves the workspace's last-known-good
    # build record before provisioning (restore instead of cold clone) and
    # fills the cache after a successful cold prepare. Snapshot/restore run
    # control-plane-side; build sandboxes carry no credentials.
    app.state.environments = None
    if os.environ.get("SBX_ENV_CACHE") == "1":
        from control.environment import EnvironmentService

        environment_store = _select_environment_store() if database_records is None else None
        if database_records is not None:
            from control.postgres_state import PostgresEnvironmentStore

            environment_store = PostgresEnvironmentStore(database_records)
        environments = EnvironmentService(
            backend,
            environment_store,
            snapshots=snapshot_provider,
            setup=os.environ.get("SBX_ENV_SETUP") or "",
            clock=clock,
        )
        plane.snapshot_provider = snapshot_provider
        plane.environments = environments
        app.state.environments = environments

    app.state.workflow_store = workflow_store
    app.state.task_store = task_store
    # SOR-82 integration: the durable run ledger is the source of truth, and
    # the /v1 run-state seam (begin/get/list/transition) binds to it by
    # default. Tests may still inject a substitute on app.state.run_states or
    # app.state.v1_state.
    from control.api_v1.lifecycle import LedgerRunStates
    from control.api_v1.state import V1State

    app.state.run_states = LedgerRunStates(plane.run_ledger)
    app.state.v1_state = V1State(run_states=app.state.run_states)
    app.state.basic_user = basic_user
    app.state.basic_password = basic_password
    app.state.keepalive_s = keepalive

    # Product credentials always use durable storage. The operator bootstrap
    # Secret adds a separate credential overlay and provider account seeding.
    from control.api_v1.bootstrap import configure_v1_bootstrap

    configure_auth(app, auth=auth_store)
    if email_sender is None:
        # Mock delivery is explicit on Modal; local dev uses it by default.
        # Until a production adapter is configured, cloud registration fails closed.
        email_mode = os.environ.get(
            "SBX_AUTH_EMAIL_MODE", "disabled" if backend_kind == "modal" else "mock"
        )
        if email_mode not in {"mock", "disabled"}:
            raise ValueError("SBX_AUTH_EMAIL_MODE must be mock or disabled")
        email_sender = MockEmailSender() if email_mode == "mock" else UnconfiguredEmailSender()
    app.state.hosted_auth = HostedAuthService(
        app.state.auth_store, email_sender, limiter=auth_rate_limiter
    )
    from control.connections import ConnectionStore, SecretVault
    from control.modal_connection import (
        FakeModalProvider,
        ModalConnectionService,
        UnconfiguredModalProvider,
    )

    app.state.github_connections = github_connections
    app.state.connections = hosted_connections or ConnectionStore(
        app.state.auth_store,
        connection_vault if connection_vault is not None else SecretVault.from_env(),
    )
    app.state.modal_connections = ModalConnectionService(
        app.state.connections,
        modal_provider
        if modal_provider is not None
        else (
            FakeModalProvider()
            if os.environ.get("SBX_CONNECTIONS_MODE") == "mock"
            else UnconfiguredModalProvider()
        ),
    )
    from control.codex_broker import CodexBroker, FakeCodexProvider, UnconfiguredCodexProvider

    app.state.codex_broker = CodexBroker(
        app.state.connections,
        codex_provider
        if codex_provider is not None
        else (
            FakeCodexProvider(app.state.connections)
            if os.environ.get("SBX_CONNECTIONS_MODE") == "mock"
            else UnconfiguredCodexProvider()
        ),
    )
    if hosted:
        from control.hosted_accounts import HostedAccounts, HostedScheduling

        app.state.hosted_accounts = HostedAccounts(app.state.codex_broker)
        app.state.hosted_scheduling = HostedScheduling(app.state.hosted_accounts, plane.store)
        backend.codex_broker = app.state.codex_broker
        plane.max_concurrent = min(plane.max_concurrent, 5)
    configure_v1_bootstrap(app)
    if hosted:
        app.state.account_registry = app.state.hosted_accounts

    def reserve_recovery(rec: SessionRecord) -> Callable[[bool], None]:
        from control.api_v1.deps import get_scheduler
        from control.scheduler import ScheduleRefused

        state = app.state.v1_state
        with state.lock:
            existing = state.leases.get(rec.id)
            if existing is not None:
                state.recovering_leases.add(rec.id)
                state.lease_generations[rec.id] = state.lease_generations.get(rec.id, 0) + 1
        account_id = rec.sandbox_tags.get("account_id", "auto")
        if existing is not None:

            def finish_existing(_success: bool) -> None:
                with state.lock:
                    state.recovering_leases.discard(rec.id)
                    state.lease_generations[rec.id] = state.lease_generations.get(rec.id, 0) + 1

            return finish_existing
        if account_id == "auto":
            return lambda _success: None
        scheduler = (
            app.state.hosted_scheduling.for_user(rec.owner)
            if hosted
            else get_scheduler(Request({"type": "http", "app": app}))
        )
        acquire = getattr(scheduler, "acquire", None)
        if not callable(acquire):
            decision = scheduler.decide(
                provider=rec.sandbox_tags.get("provider", "codex"), account=account_id
            )
            if decision.error:
                raise SessionConflict(decision.error)
            return lambda _success: None
        try:
            lease = acquire(provider=rec.sandbox_tags.get("provider", "codex"), account=account_id)
        except ScheduleRefused as exc:
            raise SessionConflict(exc.error, exc.code) from None

        def finish(success: bool) -> None:
            if success:
                state.set_lease(rec.id, lease)
            else:
                lease.release()

        return finish

    plane.recovery_reserve = reserve_recovery

    # SOR-147 (WP-H1): automatic OAuth credential write-back. The registry is
    # resolved lazily — bootstrap seeds ``app.state.account_registry`` above,
    # tests may install one later — so the sync is inert until a registry
    # exists. Modal deployments also refresh the managed ``<prefix><id>``
    # Secret in place (no redeploy); local writes stay in the account store.
    from control.credlifecycle import (
        CredentialLifecycleService,
        CredentialRefresher,
        worker_enabled,
    )
    from control.credsync import CredentialSync, ModalCredentialSecretWriter

    # SOR-176: the credential lifecycle is independent of local CLI files —
    # the cloud lane owns the store blob + managed Secret; local auth files
    # remain import sources only and are never overwritten by refresh.
    plane.credential_lifecycle = CredentialLifecycleService(
        lambda: getattr(app.state, "account_registry", None)
    )
    app.state.credential_lifecycle = plane.credential_lifecycle
    plane.credential_sync = CredentialSync(
        lambda: getattr(app.state, "account_registry", None),
        secret_writer=(ModalCredentialSecretWriter() if backend_kind == "modal" else None),
        lifecycle=plane.credential_lifecycle,
    )
    if hosted:
        # Hosted refresh never executes or accepts write-back in a sandbox.
        plane.credential_sync = None
        plane.credential_lifecycle = None
    if not hosted and worker_enabled(backend_kind):
        # Proactive OAuth refresh: a per-account claim + the official CLI's
        # own refresh path inside a throwaway sandbox, committed via the
        # SOR-147 CAS write-back (store blob + managed Secret).
        # Each server lifespan owns a fresh worker. Merely constructing an
        # app (including the import-time app and reaper cron) starts no refresher.
        def credential_refresher_factory() -> CredentialRefresher:
            return CredentialRefresher(
                registry_source=lambda: getattr(app.state, "account_registry", None),
                backend=backend,
                runner_cmd=runner_cmd,
                sync=plane.credential_sync,
                lifecycle=plane.credential_lifecycle,
                default_model=getattr(plane, "default_model", None) or "gpt-5.6-luna",
            )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.detail,
                headers=exc.headers,
            )
        code = exc.status_code
        return JSONResponse(
            status_code=code,
            content={"error": str(exc.detail), "code": code},
            headers=exc.headers,
        )

    def require_basic(
        credentials: HTTPBasicCredentials | None = Depends(security),
    ) -> str:
        import secrets

        if credentials is None:
            raise _http_error(
                401,
                "unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )
        user_ok = secrets.compare_digest(credentials.username, app.state.basic_user)
        pass_ok = secrets.compare_digest(credentials.password, app.state.basic_password)
        if not (user_ok and pass_ok):
            raise _http_error(
                401,
                "unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

    @app.post("/api/sessions", status_code=201)
    def create_session(
        body: CreateSessionRequest | None = None,
        owner: str = Depends(require_basic),
    ) -> dict[str, str]:
        body = body or CreateSessionRequest()
        try:
            session_id = plane.create_session(owner=owner, title=body.title, model=body.model)
        except ConcurrencyLimit as exc:
            raise _http_error(exc.code, exc.error) from exc
        return {"session_id": session_id}

    @app.get("/api/sessions")
    def list_sessions(_: str = Depends(require_basic)) -> list[dict[str, Any]]:
        return plane.list_sessions()

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str, _: str = Depends(require_basic)) -> dict[str, Any]:
        rec = plane.get(sid)
        if rec is None:
            raise _http_error(404, "not_found")
        return plane.public(rec)

    @app.post("/api/sessions/{sid}/messages", status_code=202)
    def post_message(
        sid: str,
        body: PostMessageRequest,
        _: str = Depends(require_basic),
    ) -> dict[str, str]:
        try:
            turn_id = plane.post_message(sid, body.text)
        except KeyError:
            raise _http_error(404, "not_found") from None
        except SessionConflict as exc:
            raise _http_error(exc.code, exc.error) from exc
        return {"turn_id": turn_id}

    @app.post("/api/sessions/{sid}/stop", status_code=202)
    def stop_session(sid: str, _: str = Depends(require_basic)) -> dict[str, str]:
        try:
            status_name = plane.stop(sid)
        except KeyError:
            raise _http_error(404, "not_found") from None
        return {"status": status_name}

    @app.delete("/api/sessions/{sid}")
    def delete_session(sid: str, _: str = Depends(require_basic)) -> dict[str, Any]:
        try:
            rec = plane.close(sid)
        except KeyError:
            raise _http_error(404, "not_found") from None
        # SOR-80: sessions may hold a /v1 scheduler lease even when closed
        # through the internal API — release it idempotently.
        release_lease(getattr(app.state, "v1_state", None), sid)
        return plane.public(rec)

    @app.get("/api/sessions/{sid}/events")
    async def session_events(
        sid: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        _: str = Depends(require_basic),
    ) -> DisconnectAwareStreamingResponse:
        # SOR-271 round 2: Dict get + sandbox poll run off the event
        # loop — a synchronous remote call inside ``async def`` stalls
        # every other in-flight request while it blocks.
        rec = await asyncio.to_thread(plane.get, sid)
        if rec is None:
            raise _http_error(404, "not_found")
        try:
            last_id = int(last_event_id) if last_event_id else 0
        except ValueError:
            last_id = 0
        start_line = max(1, last_id + 1)

        handle = rec.handle()
        poll = await asyncio.to_thread(plane.backend.poll, handle) if handle is not None else None
        keepalive_s: float = app.state.keepalive_s

        async def gen() -> AsyncIterator[str]:
            proc: Any = None
            try:
                if handle is not None and poll is not None and poll.alive:
                    proc = await asyncio.to_thread(
                        plane.backend.exec,
                        handle,
                        ["tail", "-n", f"+{start_line}", "-F", str(handle.root / "events.jsonl")],
                        sandbox_env(handle),
                    )
                    line_q: queue.Queue[tuple[str, str | None]] = queue.Queue()

                    def _reader() -> None:
                        try:
                            for line in proc.stdout:
                                line_q.put(("line", line))
                        except Exception:
                            pass
                        finally:
                            line_q.put(("eof", None))

                    threading.Thread(target=_reader, daemon=True, name="sbx-sse-tail").start()
                else:
                    line_q = None

                yield ": keepalive\n\n"
                if proc is None or line_q is None:
                    while True:
                        await asyncio.sleep(keepalive_s)
                        yield ": keepalive\n\n"
                    return

                lineno = start_line
                next_ka = time.monotonic() + keepalive_s
                while True:
                    try:
                        kind, payload = line_q.get_nowait()
                    except queue.Empty:
                        now = time.monotonic()
                        if now >= next_ka:
                            yield ": keepalive\n\n"
                            next_ka = now + keepalive_s
                        await asyncio.sleep(0.05)
                        continue
                    if kind == "eof":
                        break
                    raw = payload or ""
                    current = lineno
                    lineno += 1
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw)
                        if not isinstance(obj, dict):
                            obj = {"type": "error", "message": raw}
                    except json.JSONDecodeError:
                        obj = {"type": "error", "message": "bad json in event stream"}
                    yield format_sse(current, obj)
                    now = time.monotonic()
                    if now >= next_ka:
                        yield ": keepalive\n\n"
                        next_ka = now + keepalive_s
            finally:
                if proc is not None:
                    # Must not await: this finally often runs under a cancelled
                    # cancel-scope (client disconnect), which would abort kill().
                    try:
                        proc.kill()
                    except Exception:
                        pass

        return DisconnectAwareStreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # SOR-199: warm the durable listing indexes at startup so the one-time
    # migration rebuild on pre-index Dicts happens off the request path.
    # Each store's ``_build_lock`` makes a request that lands mid-warmup
    # share the same rebuild rather than starting a second enumeration.
    if backend_kind == "modal":

        def _warm_listing_indexes() -> None:
            for target, name in (
                (store, "_ensure_index"),
                (run_store, "_ensure_index"),
                (workflow_store, "_ensure_manifest"),
                (artifact_store, "_ensure_index"),
                (artifact_store, "_ensure_global_index"),
            ):
                fn = getattr(target, name, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
            # Then pre-populate the read-through listing caches so the
            # first user request isn't the cold fanout path. The
            # ``list_page`` limit matches the Console's first-page size
            # in ``web/views/artifacts.js``.
            for fn in (
                getattr(store, "list_all", None),
                getattr(workflow_store, "all_bindings", None),
                getattr(workspace_store, "list_records", None),
            ):
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
            list_page = getattr(artifact_store, "list_page", None)
            if callable(list_page):
                try:
                    list_page(limit=100)
                except Exception:
                    pass

        threading.Thread(target=_warm_listing_indexes, daemon=True).start()

    # SOR-220: the hosted broker proves a deployment controls its exact
    # public origin by fetching this well-known challenge during the
    # registration handshake. Unauthenticated by design — it only ever
    # returns the currently-pending, already-random challenge token (or
    # 404 when no handshake is in flight).
    @app.get("/.well-known/sbx-broker-challenge", include_in_schema=False)
    def broker_challenge(request: Request) -> Response:
        from starlette.responses import PlainTextResponse

        from control.api_v1.deps import get_github_app

        service = get_github_app(request)
        challenge = getattr(service, "pending_broker_challenge", lambda: None)()
        if not challenge:
            return PlainTextResponse("no pending challenge", status_code=404)
        return PlainTextResponse(str(challenge))

    # SOR-211 + SOR-266: the control plane serves the V2 Session Console
    # (the ``console/dist`` React build) at "/" on the same origin as
    # ``/v1`` — the deployed Modal URL opens the UI directly, and
    # ``sbx open`` hands the browser a one-time grant into it. The legacy
    # ``web/`` static UI is no longer the default product UI: it is only
    # reachable at ``/legacy`` (or at "/" when ``SBX_WEB_DIR`` is set
    # explicitly, the legacy-test lane) and never ships in production.
    _mount_frontends(app)

    return app


def _default_console_dir() -> Path:
    """The repo's ``console/dist`` build output — ``control/app.py`` → repo root."""
    return Path(__file__).resolve().parents[1] / "console" / "dist"


def _default_web_dir() -> Path:
    """The repo's ``web/`` directory — ``control/app.py`` → repo root."""
    return Path(__file__).resolve().parents[1] / "web"


def _mount_frontends(app: FastAPI) -> None:
    """Mount the console/legacy frontends (SOR-266 cutover).

    - ``SBX_CONSOLE_DIR`` (or the default ``console/dist``) serves the V2
      React Console at "/" with SPA history fallback. An explicitly set
      ``SBX_CONSOLE_DIR`` that does not exist fails loudly — a production
      deploy must never silently fall back to ``web/`` at root.
    - When no console build exists, ``web/`` stays reachable only as an
      explicitly-marked legacy surface: ``/legacy``, plus "/" when
      ``SBX_WEB_DIR`` is set explicitly (the e2e/dev legacy-test lane;
      production never sets it).
    """
    console_env = os.environ.get("SBX_CONSOLE_DIR")
    console_dir = Path(console_env) if console_env else _default_console_dir()
    if console_dir.is_dir():
        app.mount("/", SPAStaticFiles(directory=str(console_dir), html=True), name="console")
        return
    if console_env:
        raise RuntimeError(
            f"SBX_CONSOLE_DIR={console_env} does not contain the console build — "
            "run `npm --prefix console ci && npm run build` (or set SBX_CONSOLE_DIST "
            "at deploy time) so the production image ships console/dist"
        )
    web_env = os.environ.get("SBX_WEB_DIR")
    web_dir = Path(web_env) if web_env else _default_web_dir()
    if not web_dir.is_dir():
        return
    legacy = StaticFiles(directory=str(web_dir), html=True)
    app.mount("/legacy", legacy, name="legacy-console")
    if web_env:
        # Explicit legacy opt-in: dev/e2e harnesses only. The URL is the
        # marking — /legacy mirrors the same files.
        app.mount("/", legacy, name="console")


# Local ``uvicorn control.app:app``. Tests should call ``create_app(...)``.
app = create_app()


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="sbx-control local server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
