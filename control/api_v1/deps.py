"""FastAPI dependencies for the ``/v1`` router.

Everything binds to ``request.app.state`` so tests (and P2-C, later) can
inject real implementations; absent attributes get in-memory defaults from
:mod:`control.api_v1.state`.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from control.api_v1.errors import V1ApiError
from control.api_v1.lifecycle import RUN_TERMINAL, RunStateStore
from control.api_v1.state import (
    InMemoryAccountRegistry,
    InMemoryApiKeyStore,
    V1State,
)
from control.api_v1.workflows import WorkflowService
from control.artifact_ops import HandoffStoreView
from control.artifacts import InMemoryArtifactStore
from control.auth_bearer import bearer_scheme, bearer_token, has_scope, lookup_key
from control.credsync import credential_rotated
from control.handoff import HandoffService
from control.ports import AccountRegistry, ApiKey, ApiKeyStore, Scheduler
from control.resources import ResourceRegistry
from control.scheduler import AccountScheduler, session_running_source
from control.workflow_store import WorkflowStore
from control.workspace import InMemoryWorkspaceStore, WorkspaceService


def get_plane(request: Request) -> Any:
    """The shared SessionService (P1 ``ControlPlane``), same as ``/api/*``."""
    return request.app.state.plane


def get_v1_state(request: Request) -> V1State:
    state = getattr(request.app.state, "v1_state", None)
    if state is None:
        state = V1State()
        request.app.state.v1_state = state
    return state


def get_run_states(request: Request) -> RunStateStore:
    """Run-state seam (SOR-82 A2): default in-memory; A1 (SOR-88) may install a
    durable ``RunStateStore`` on ``app.state.run_states`` without route changes."""
    store = getattr(request.app.state, "run_states", None)
    if store is None:
        store = get_v1_state(request).run_states
    return store


def get_registry(request: Request) -> AccountRegistry:
    registry = getattr(request.app.state, "account_registry", None)
    if registry is None:
        registry = InMemoryAccountRegistry()
        request.app.state.account_registry = registry
    return registry


def get_scheduler(request: Request) -> Scheduler:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        # SOR-63/D1 is the default scheduler even without bootstrap: atomic
        # acquire + cooldown/failover over whatever registry is installed.
        # Running counts derive from the sessions store (design v2 §3.3) so
        # slots stay truthful across control-plane restarts.
        session_store = getattr(getattr(request.app.state, "plane", None), "store", None)
        scheduler = AccountScheduler(
            get_registry(request),
            external_running=(
                session_running_source(session_store) if session_store is not None else None
            ),
        )
        request.app.state.scheduler = scheduler
    return scheduler


def get_key_store(request: Request) -> ApiKeyStore:
    store = getattr(request.app.state, "api_key_store", None)
    if store is None:
        store = InMemoryApiKeyStore()
        request.app.state.api_key_store = store
    return store


# Structured run-error codes (SOR-82 taxonomy) that mark an account for
# cooldown/failover — reported verbatim to the scheduler's
# ``report_failure`` kind. ``auth_invalid`` is permanent (credential
# re-import needed); the others cool the account for ``retry_after`` / the
# scheduler's cooldown window. Control-side codes (``cancelled``,
# ``timeout``, ``runtime_error``, ``event_parse_error``,
# ``model_unavailable``) are not account health.
_ACCOUNT_HEALTH_CODES = frozenset(
    {
        "auth_invalid",
        "rate_limited",
        "quota_exhausted",
        "provider_unavailable",
        "model_capacity",
    }
)


class RunFailureReporter:
    """Feeds terminal provider run-errors back into the scheduler.

    The /v1 read path is where a finished turn's structured error first
    surfaces to the control plane; reportable provider failures
    (``rate_limited`` → cooling, ``auth_invalid`` → invalid, …) mark the
    run's account for cooldown/failover exactly once per run. No-ops when
    the scheduler lacks ``report_failure`` — the frozen ``Scheduler``
    protocol only requires ``decide``.
    """

    def __init__(self) -> None:
        self._reported: set[tuple[str, int]] = set()
        self._lock = threading.Lock()

    def report(
        self,
        *,
        scheduler: Any,
        agent_id: str,
        n: int,
        account_id: str | None,
        status: str | None,
        error: Any,
        credential_fp: str | None = None,
    ) -> None:
        if status not in RUN_TERMINAL or not isinstance(error, dict):
            return
        kind = error.get("code")
        if kind not in _ACCOUNT_HEALTH_CODES or not account_id or account_id == "auto":
            return
        key = (agent_id, n)
        with self._lock:
            if key in self._reported:
                return
            self._reported.add(key)
        if kind == "auth_invalid" and credential_fp:
            # SOR-147 self-heal: when the stored credential blob's fingerprint
            # already differs from what this run was seeded with, a write-back
            # (or manual refresh) rotated the credential — the verdict was
            # computed against stale material, so it must not re-mark the
            # healed account ``invalid``.
            registry = getattr(scheduler, "registry", None) or getattr(scheduler, "_registry", None)
            if credential_rotated(registry, account_id, credential_fp):
                return
        report = getattr(scheduler, "report_failure", None)
        if not callable(report):
            return
        retry_after = error.get("retry_after")
        try:
            # AccountScheduler (SOR-63/D1): report_failure(account_id, kind, ...).
            report(account_id, kind, retry_after=retry_after)
        except TypeError:
            # Single-account pools take report_failure without account_id.
            try:
                report(kind, retry_after=retry_after)
            except Exception:
                pass
        except Exception:
            pass  # feedback is best-effort; never mask the API response


def get_run_reporter(request: Request) -> RunFailureReporter:
    reporter = getattr(request.app.state, "run_failure_reporter", None)
    if reporter is None:
        reporter = RunFailureReporter()
        request.app.state.run_failure_reporter = reporter
    return reporter


def get_artifact_store(request: Request) -> Any:
    """Durable artifact store (SOR-83/B1); in-memory default for tests."""
    store = getattr(request.app.state, "artifact_store", None)
    if store is None:
        store = InMemoryArtifactStore()
        request.app.state.artifact_store = store
    return store


def get_workspace_store(request: Request) -> Any:
    """Workspace record store (SOR-83/B2); in-memory default for tests."""
    store = getattr(request.app.state, "workspace_store", None)
    if store is None:
        store = InMemoryWorkspaceStore()
        request.app.state.workspace_store = store
    return store


def get_workspaces(request: Request) -> WorkspaceService:
    """Workspace prepare/record service bound to the plane's backend."""
    service = getattr(request.app.state, "workspaces", None)
    if service is None:
        plane = get_plane(request)
        service = WorkspaceService(plane.backend, get_workspace_store(request))
        request.app.state.workspaces = service
    return service


def get_handoffs(request: Request) -> HandoffService:
    """Handoff service; consumes the durable artifact store via the B1→B2 view."""
    service = getattr(request.app.state, "handoffs", None)
    if service is None:
        service = HandoffService(
            get_workspaces(request), HandoffStoreView(get_artifact_store(request))
        )
        request.app.state.handoffs = service
    return service


def get_workflow_store(request: Request) -> WorkflowStore:
    """Workflow metadata index (SOR-84 C1): ``app.state.workflow_store`` when
    a durable store is installed, else the ``V1State`` fallback — same seam
    shape as :func:`get_run_states`."""
    store = getattr(request.app.state, "workflow_store", None)
    if store is None:
        store = get_v1_state(request).workflows
    return store


def get_github_app(request: Request) -> Any:
    """GitHub App authorization service (SOR-177): ``app.state.github_app``
    when a test/deploy injects one, else the env-configured default —
    shared with the sandbox injection seam so token/record caches are one.
    """
    service = getattr(request.app.state, "github_app", None)
    if service is None:
        from control import github_app

        service = github_app.default_service()
        request.app.state.github_app = service
    return service


def get_capabilities(request: Request) -> Any:
    """Capability service (SOR-204): discovered model/effort catalog.

    ``app.state.capabilities`` when a test/deploy injects one, else a
    ``CapabilityService`` over the bound registry with a sandbox probe
    bound to the plane's backend + runner command and the deployment's
    capability store.
    """
    service = getattr(request.app.state, "capabilities", None)
    if service is None:
        from control.capabilities import (
            CapabilityService,
            SandboxCapabilityProbe,
            select_capability_store,
        )

        plane = getattr(request.app.state, "plane", None)
        backend = getattr(plane, "backend", None)
        runner_cmd = getattr(plane, "runner_cmd", None)
        probe = (
            SandboxCapabilityProbe(backend, runner_cmd, bin_env=os.environ)
            if backend is not None and runner_cmd
            else None
        )
        service = CapabilityService(
            get_registry(request),
            store=select_capability_store(),
            probe=probe,
            env=os.environ,
        )
        request.app.state.capabilities = service
    return service


def get_resources(request: Request) -> ResourceRegistry:
    """Session-resource registry (SOR-129): ``app.state.resource_registry``
    when a test/deploy injects one, else the env-configured allowlist.
    Built per request — env is process-level and the parse is cheap, so an
    injected registry stays swappable at any point."""
    registry = getattr(request.app.state, "resource_registry", None)
    if registry is None:
        registry = ResourceRegistry.from_env()
    return registry


def get_workflow_service(
    request: Request,
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
) -> WorkflowService:
    """Low-cost workflow seam: index-backed lookup + scoped cleanup.

    Built per request — it is a thin binder over shared stores, and not
    caching it on ``app.state`` keeps ``app.state.workflow_store``
    swappable at any point (tests, P2-C durable wiring).
    """
    return WorkflowService(get_workflow_store(request), plane, v1=v1, run_states=run_states)


def api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    store: ApiKeyStore = Depends(get_key_store),
) -> ApiKey:
    """Any valid (unrevoked) ``sbx_`` key; 401 otherwise."""
    key = lookup_key(store, bearer_token(credentials))
    if key is None:
        raise V1ApiError(401, "unauthorized", "missing or invalid bearer token")
    return key


def agents_key(key: ApiKey = Depends(api_key)) -> ApiKey:
    """Key with the ``agents`` scope (agent / run / meta endpoints)."""
    if not has_scope(key, "agents"):
        raise V1ApiError(403, "forbidden", "api key lacks required scope 'agents'")
    return key


def admin_key(key: ApiKey = Depends(api_key)) -> ApiKey:
    """Key with the ``admin`` scope (accounts / api-keys / verify)."""
    if not has_scope(key, "admin"):
        raise V1ApiError(403, "forbidden", "api key lacks required scope 'admin'")
    return key
