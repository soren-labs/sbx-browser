"""Public ``/v1`` endpoints (Cursor Cloud Agents shape, ``api-v1.yaml``).

``agent ≙ session``, ``run ≙ turn``: ``POST /v1/agents`` creates a session and
immediately queues its first run; follow-ups are new runs on the same agent.
All endpoints consume ``ports.*`` Protocols plus the shared SessionService
(``app.state.plane``); provider / account metadata is tracked in ``V1State``
until P2-C persists it on the session record.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import queue
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Header, Request
from fastapi.responses import RedirectResponse, Response
from runtime.runner.contract import STATUS_SKIPPED, ContractError, normalize_contract
from runtime.runner.effort import effort_error, normalize_effort

from control.api_v1 import router
from control.api_v1.bootstrap import PROVIDER_DEFAULT_MODELS
from control.api_v1.deps import (
    RunFailureReporter,
    admin_key,
    agents_key,
    api_key,
    get_artifact_store,
    get_capabilities,
    get_github_app,
    get_handoffs,
    get_key_store,
    get_plane,
    get_provider_connect,
    get_registry,
    get_resources,
    get_revisions,
    get_run_reporter,
    get_run_states,
    get_runtime_store,
    get_scheduler,
    get_task_store,
    get_v1_state,
    get_workflow_service,
    get_workspaces,
    owner_visible,
    require_artifact_owner,
)
from control.api_v1.errors import V1ApiError, not_found
from control.api_v1.lifecycle import (
    RUN_TERMINAL,
    RunStateStore,
    launch_first_run,
    request_fingerprint,
)
from control.api_v1.schemas import (
    VALID_SCOPES,
    AuthConnectRequest,
    ConsoleGrantExchangeRequest,
    CreateAccountRequest,
    CreateAgentRequest,
    CreateApiKeyRequest,
    CreateArtifactRequest,
    CreateRunRequest,
    GitHubAppAuthorizeCallbackRequest,
    GitHubAppManifestCompleteRequest,
    GitHubAppManifestRequest,
    HandoffRef,
    OutputContract,
    PairCompleteRequest,
    ProviderId,
    ReviewWorkspaceRequest,
    account_public,
    agent_public,
    api_key_public,
    usage_public,
)
from control.api_v1.state import CONSOLE_GRANT_TTL_S, AgentMeta, V1State
from control.api_v1.workflows import WorkflowService
from control.artifact_ops import credential_forbidden_values, snapshot_workspace_artifact
from control.artifacts import (
    ArtifactCorruptError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactSecretError,
    manifest_to_dict,
    page_manifests,
)
from control.auth_bearer import has_scope
from control.capabilities import (
    CapabilitySnapshot,
    capability_from_model_id,
    declared_snapshot,
)
from control.compute import ComputeError, ComputeSpec, compute_for_record, resolve_compute
from control.config import (
    TERMINAL_STATUSES,
    account_secret_prefix,
    env_int,
    env_str,
    selected_providers,
)
from control.connect import (
    CONNECT_STATES,
    ConnectError,
    plane_verify,
    probe_account_credential,
)
from control.credlifecycle import CredentialLifecycleService, CredentialRefresher
from control.credsync import TAG_CRED_RUN_FP, CredentialSync
from control.devin_pool import ScheduleRefused
from control.github_app import GitHubAppError
from control.latency import observe
from control.onboarding import OnboardingError
from control.ports import Account, AccountRegistry, ApiKey, ApiKeyStore, Scheduler
from control.provider_auth import AUTH_SESSION_STATES
from control.resources import ResourceError, resolve_resources, resource_refs
from control.revisions import RevisionError
from control.run_errors import run_error_for_run
from control.run_store import (
    UNKNOWN_RUN_STATUS,
    RunRecord,
    apply_output_contract,
    contract_view,
    default_artifact_refs,
    outcome_from_turn_payload,
)
from control.sandbox_io import read_json, read_text, sandbox_env
from control.service import ConcurrencyLimit, SessionConflict, format_sse
from control.workspace import (
    ARTIFACT_NOT_FOUND,
    WORKSPACE_INVALID,
    WORKSPACE_NOT_FOUND,
    WorkspaceError,
    WorkspaceSpec,
    is_safe_ref,
    validate_git_policy,
)
from control.workspace import record_to_dict as workspace_record_to_dict

AGENTS_PAGE_SIZE = 100
ARTIFACTS_PAGE_MAX = 500  # SOR-201: cap per-page manifest fetches
_TURN_ID_RE = re.compile(r"^turn-(\d+)$")
_RUN_ID_RE = re.compile(r"^run-(\d+)$")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _registry_account(registry: AccountRegistry, account_id: str) -> Account:
    """``registry.get`` with 404 mapping. A non-conformant id is refused by
    the registry before any store access (SOR-105) — no such account can
    exist, so it surfaces as ``not_found`` rather than a 500."""
    try:
        account = registry.get(account_id)
    except ValueError:
        account = None
    if account is None:
        raise not_found("account not found")
    return account


def _running_or_zero(registry: AccountRegistry, account_id: str) -> int:
    """``running_count`` that treats a non-conformant stored id as 0 — such a
    record can never hold a session and must not 500 a listing (SOR-105)."""
    try:
        return registry.running_count(account_id)
    except ValueError:
        return 0


def _turn_n(turn_id: str | None) -> int | None:
    match = _TURN_ID_RE.match(turn_id or "")
    return int(match.group(1)) if match else None


def _account_view(registry: AccountRegistry, account: Account) -> dict[str, Any]:
    """``account_public`` + the canonical ``auth_state`` (SOR-213).

    Derived from non-secret lanes only: the credential-lifecycle record
    and blob presence — never credential material. Stores without the
    optional lanes degrade to ``None``.
    """
    blob = None
    rec = None
    get_blob = getattr(registry, "get_credential_blob", None)
    get_lifecycle = getattr(registry, "get_credential_lifecycle", None)
    try:
        if callable(get_blob):
            blob = get_blob(account.id)
        if callable(get_lifecycle):
            rec = get_lifecycle(account.id)
    except Exception:
        blob = None
        rec = None
    from control.provider_auth import auth_state_for

    auth_state = auth_state_for(account, rec, blob is not None)
    return account_public(account, _running_or_zero(registry, account.id), auth_state)


def _run_n(run_id: str) -> int | None:
    match = _RUN_ID_RE.match(run_id or "")
    return int(match.group(1)) if match else None


def _ledger(plane: Any) -> Any:
    """The durable run ledger attached to the plane (None when absent)."""
    return getattr(plane, "run_ledger", None)


def _meta_for(v1: V1State, rec: Any) -> AgentMeta:
    """Agent metadata with a restart-stable fallback to sandbox tags.

    ``V1State`` is per-process; after a control-plane restart the durable
    session record's sandbox tags still carry provider/account, so identity
    does not drift back to defaults (SOR-82).
    """
    meta = v1.get_meta(rec.id)
    if meta is not None:
        return meta
    tags = getattr(rec, "sandbox_tags", None) or {}
    resources: dict[str, Any] | None = None
    raw_resources = tags.get("resources")
    if raw_resources:
        try:
            parsed = json.loads(raw_resources)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            resources = parsed
    return AgentMeta(
        provider=tags.get("provider") or "codex",
        account_id=tags.get("account_id") or "auto",
        resources=resources,
        reasoning_effort=tags.get("reasoning_effort") or getattr(rec, "reasoning_effort", None),
    )


def _known_run_ns(
    rec: Any,
    ledger: Any = None,
    run_states: Any = None,
    *,
    records: dict[int, Any] | None = None,
    states: dict[int, Any] | None = None,
) -> set[int]:
    ns = set(range(1, int(rec.turns) + 1))
    for message in rec.messages:
        n = _turn_n(message.get("turn_id"))
        if n is not None:
            ns.add(n)
    if rec.current_turn_n is not None:
        ns.add(int(rec.current_turn_n))
    if ledger is not None:
        # The ledger is authoritative: a run persisted there is known even
        # when the session record lost the matching messages/turn count.
        try:
            if records is None:
                ns.update(record.n for record in ledger.list(rec.id))
            else:
                ns.update(records)
        except Exception:
            pass
    rs_ledger = getattr(run_states, "_ledger", None)
    if run_states is not None and (rs_ledger is None or rs_ledger is not ledger):
        # A separate run-state seam (ledger-less deployments, injected test
        # stores) can know runs the ledger does not — e.g. a queued CREATING
        # run-1 before the ledger saw it. ``LedgerRunStates`` over the same
        # ledger is skipped: its list is the identical store pass the
        # ledger branch just ran, and on a ``modal.Dict`` backend that
        # second serialized enumeration costs seconds.
        try:
            if states is None:
                ns.update(s.n for s in run_states.list(rec.id))
            else:
                ns.update(states)
        except Exception:
            pass
    return ns


def _turn_payload(plane: Any, rec: Any, n: int) -> dict[str, Any] | None:
    """Read ``turns/<n>.json`` when the sandbox is still reachable."""
    backend = getattr(plane, "backend", None)
    handle = rec.handle()
    if backend is None or handle is None:
        return None
    try:
        poll = backend.poll(handle)
    except Exception:
        return None
    if not poll.alive:
        return None
    try:
        payload = read_json(backend, handle, f"turns/{n}.json")
    except Exception:
        return None
    return payload


def _record_public(
    record: RunRecord,
    pub: dict[str, Any],
    meta: Any = None,
    *,
    status: str | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persisted ``RunRecord`` → Cursor-shaped Run (authoritative)."""
    run: dict[str, Any] = {
        "id": record.id,
        "agent_id": record.agent_id,
        "status": status or record.status,
        "created_at": record.created_at or pub["created_at"],
        "updated_at": record.updated_at or pub["updated_at"],
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "result": {"text": record.result_text} if record.result_text else None,
        "error": record.error if error is None else error,
        "provider": record.provider or (meta.provider if meta else None),
        "account_id": record.account_id or (meta.account_id if meta else None),
        "model": record.model or pub.get("model"),
        # SOR-179: the effective effort this run executed under.
        "reasoning_effort": record.reasoning_effort
        or (meta.reasoning_effort if meta else None)
        or pub.get("reasoning_effort"),
        "artifact_refs": list(record.artifact_refs),
        # SOR-130: extracted JSON + the contract verdict metadata (null when
        # the run carried no output contract).
        "structured_output": record.structured_output,
        "output_contract": contract_view(record),
    }
    if record.usage is not None:
        run["usage"] = usage_public(record.usage)
    return run


def _fallback_status(rec: Any) -> str:
    """Honest status for a run whose evidence is gone, keyed on the session.

    ``closed`` means the agent was deleted (run cancelled); ``timed_out``
    means the sandbox expired mid-flight; anything else — including
    ``lost`` and sessions that are still live but no longer own the turn —
    is explicit ``UNKNOWN``, never inferred success.
    """
    if rec.status == "closed":
        return "CANCELLED"
    if rec.status == "timed_out":
        return "EXPIRED"
    return "UNKNOWN"


def _with_skipped_contract(record: RunRecord) -> RunRecord:
    """Render-side copy of an open record whose run is reported terminal.

    The ledger record stays open (the verdict was never evaluated), so the
    contract must not read ``pending`` on a terminal run — report the same
    ``skipped`` a non-FINISHED persist would have written.
    """
    if record.output_contract is None or record.contract_result is not None:
        return record
    verdict = {
        "enforcement": record.output_contract.get("enforcement", "strict"),
        "schema_digest": record.output_contract.get("schema_digest"),
        "status": STATUS_SKIPPED,
        "extraction": None,
        "violations": [],
    }
    return replace(record, contract_result=verdict)


def _run_error_public(
    status: str,
    *,
    payload: Any = None,
    cancelled: bool = False,
    agent_status: str | None = None,
) -> dict[str, Any] | None:
    """Canonical ``run.error`` payload for a derived (non-ledger) status."""
    err = run_error_for_run(
        status,
        payload=payload,
        cancelled=cancelled,
        agent_status=agent_status,
    )
    return err.public() if err is not None else None


# Sentinel: the list route's single store pass supplies per-run ledger
# records / run-state rows explicitly (``None`` = known absent); the
# sentinel means "not prefetched — resolve serially" so single-run callers
# keep their shape (SOR-199: the serial per-run ``ledger.get`` made
# ``GET /v1/agents/{id}/runs`` an N+1 on Dict backends).
_RUN_FETCH_UNRESOLVED = object()


def _run_public(
    plane: Any,
    pub: dict[str, Any],
    rec: Any,
    n: int,
    cancelled: set[int],
    meta: Any = None,
    run_states: RunStateStore | None = None,
    *,
    scheduler: Any = None,
    reporter: Any = None,
    record: Any = _RUN_FETCH_UNRESOLVED,
    state: Any = _RUN_FETCH_UNRESOLVED,
) -> dict[str, Any]:
    """Render the run, then feed terminal provider errors to the scheduler.

    Reporting is the /v1 cooldown/failover seam (SOR-63/D2): a rendered
    terminal provider error (``rate_limited``, ``auth_invalid``, …) marks
    the run's account via ``RunFailureReporter``, deduped per run. Both
    knobs default off so every existing call site keeps its shape.
    """
    run = _render_run(plane, pub, rec, n, cancelled, meta, run_states, record=record, state=state)
    prompt = _run_prompt(rec, n)
    run["prompt"] = {"text": prompt} if prompt else None
    if reporter is not None and scheduler is not None:
        reporter.report(
            scheduler=scheduler,
            agent_id=rec.id,
            n=n,
            account_id=run.get("account_id"),
            status=run.get("status"),
            error=run.get("error"),
            credential_fp=(getattr(rec, "sandbox_tags", None) or {}).get(TAG_CRED_RUN_FP),
        )
    return run


def _run_prompt(rec: Any, n: int) -> str | None:
    """The user message that opened run ``n``, from the durable session record."""
    turn_id = f"turn-{n}"
    for message in getattr(rec, "messages", None) or ():
        if message.get("turn_id") == turn_id and message.get("role") == "user":
            text = message.get("text")
            return str(text) if text else None
    return None


def _render_run(
    plane: Any,
    pub: dict[str, Any],
    rec: Any,
    n: int,
    cancelled: set[int],
    meta: Any = None,
    run_states: RunStateStore | None = None,
    *,
    record: Any = _RUN_FETCH_UNRESOLVED,
    state: Any = _RUN_FETCH_UNRESOLVED,
) -> dict[str, Any]:
    """Cursor-shaped Run; the durable ledger is authoritative once written.

    Open records and pre-ledger sessions fall back to evidence-checked
    derivation: a readable ``turns/<n>.json`` decides the terminal status
    (persisted into the ledger when one is attached); without evidence the
    status is explicit ``UNKNOWN`` or the session-derived fallback — never
    inferred ``FINISHED``. While the session is still live, a persisted open
    record's own status (``CREATING`` pre-dispatch, ``RUNNING`` after) is
    authoritative — the derived view cannot see the queued-run window
    (SOR-82 A2). A separate ``RunStateStore`` (when the ledger is absent or a
    test injects one) overlays the derived view the same way.

    ``record``/``state`` are the prefetched row for ``n`` (``None`` =
    known absent) when the list route already paid the store pass;
    the sentinel falls back to a serial point read.
    """
    ledger = _ledger(plane)
    if record is _RUN_FETCH_UNRESOLVED:
        record = ledger.get(rec.id, n) if ledger is not None else None
    live = rec.current_turn_n == n and rec.status not in TERMINAL_STATUSES
    if record is not None:
        if record.terminal or live:
            return _record_public(record, pub, meta)
        if n in cancelled:
            return _record_public(
                _with_skipped_contract(record),
                pub,
                meta,
                status="CANCELLED",
                error=_run_error_public("CANCELLED", cancelled=True, agent_status=rec.status),
            )
        # These runs have not dispatched. A sandbox evidence read cannot
        # settle them and can block every detail read during provisioning
        # (or a queued follow-up behind a long-running provider turn).
        if record.status in ("CREATING", "QUEUED") and rec.status not in TERMINAL_STATUSES:
            return _record_public(record, pub, meta)
        payload = _turn_payload(plane, rec, n)
        if payload is not None:
            status, error, result_text, usage = outcome_from_turn_payload(payload)
            # SOR-130: the read-path backfill must apply the same contract
            # enforcement as _finish_turn — a strict violation persists as
            # ERROR + contract_violation, never a silent FINISHED.
            status, error, structured_output, contract_result = apply_output_contract(
                status, error, result_text, record.output_contract
            )
            record = ledger.finish(
                rec.id,
                n,
                status=status,
                result_text=result_text,
                error=error,
                usage=usage,
                provider=meta.provider if meta else None,
                account_id=meta.account_id if meta else None,
                model=pub.get("model"),
                reasoning_effort=pub.get("reasoning_effort"),
                structured_output=structured_output,
                contract_result=contract_result,
            )
            return _record_public(record, pub, meta)
        if record.status == UNKNOWN_RUN_STATUS:
            # Corrupt stored payload: report it as-is (UNKNOWN) rather than
            # guessing a terminal state from the session.
            return _record_public(record, pub, meta)
        if rec.status in TERMINAL_STATUSES:
            # The session died with the run still open and no evidence left:
            # fall back to the session-derived terminal status.
            status = _fallback_status(rec)
            return _record_public(
                _with_skipped_contract(record),
                pub,
                meta,
                status=status,
                error=record.error or _run_error_public(status, agent_status=rec.status),
            )
        # Live session, open record, no readable evidence yet — the record's
        # persisted open status (CREATING/RUNNING) is the truth.
        return _record_public(record, pub, meta)

    # No ledger record (pre-ledger session or lost store): derive honestly.
    turn_id = f"turn-{n}"
    created_at: str = pub["created_at"]
    updated_at: str = pub["updated_at"]
    result_text: str | None = None
    for message in rec.messages:
        if message.get("turn_id") != turn_id:
            continue
        if message.get("role") == "user":
            created_at = str(message.get("ts") or created_at)
        elif message.get("role") == "assistant":
            result_text = str(message.get("text") or "") or None
            updated_at = str(message.get("ts") or updated_at)

    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    if live:
        status = "RUNNING"
    elif n in cancelled:
        status = "CANCELLED"
    elif n <= int(rec.turns):
        payload = _turn_payload(plane, rec, n)
        if payload is not None:
            status, error, payload_text, usage = outcome_from_turn_payload(payload)
            if payload_text is not None:
                result_text = payload_text
            if ledger is not None:
                # Backfill: persist the outcome so later reads stay truthful
                # after the sandbox is reclaimed.
                record = ledger.finish(
                    rec.id,
                    n,
                    status=status,
                    result_text=result_text,
                    error=error,
                    usage=usage,
                    provider=meta.provider if meta else None,
                    account_id=meta.account_id if meta else None,
                    model=pub.get("model"),
                    created_at=created_at,
                )
                return _record_public(record, pub, meta)
        else:
            # Turn counted but outcome unreadable — explicit unknown.
            status = "UNKNOWN"
    elif rec.status in ("timed_out", "lost"):
        status = "EXPIRED"
    elif rec.status == "closed":
        status = "CANCELLED"
    else:
        # The turn ended without a turns/<n>.json record (runner-internal
        # failure) — report a diagnosable ERROR, never a silent success.
        status = "ERROR"

    if run_states is not None:
        if state is _RUN_FETCH_UNRESOLVED:
            state = run_states.get(rec.id, n)
        if state is not None:
            if state.status in RUN_TERMINAL:
                status = state.status
                updated_at = state.updated_at
            else:
                # The derived view cannot see a queued/pre-dispatch run; its
                # catch-all "CANCELLED"/"ERROR" is only real when the cancel
                # was recorded or the session went terminal.
                catch_all_terminal = (
                    status in RUN_TERMINAL
                    and n not in cancelled
                    and rec.status not in ("closed", "timed_out", "lost")
                )
                if status in RUN_TERMINAL and not catch_all_terminal:
                    # Derived terminal truth (turn finished, session died):
                    # fold it into a RUNNING record so the store converges.
                    # A CREATING record belongs to the worker, which persists
                    # ERROR/CANCELLED itself.
                    if state.status == "RUNNING":
                        run_states.transition(rec.id, n, status)
                elif status != "RUNNING":
                    status = state.status
            run_state = state
        else:
            run_state = None
    else:
        run_state = None

    error = _run_error_public(
        status,
        payload=payload,
        cancelled=n in cancelled,
        agent_status=rec.status,
    )
    run: dict[str, Any] = {
        "id": f"run-{n}",
        "agent_id": rec.id,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "started_at": created_at if status == "RUNNING" else None,
        "finished_at": updated_at if status != "RUNNING" else None,
        "result": {"text": result_text} if result_text else None,
        "error": error,
        "provider": meta.provider if meta else None,
        "account_id": meta.account_id if meta else None,
        "model": pub.get("model"),
        "artifact_refs": default_artifact_refs(n),
        # SOR-130: no ledger record means no contract could be attached.
        "structured_output": None,
        "output_contract": None,
    }
    if run_state is not None and run_state.error is not None:
        run["error"] = run_state.error
    if usage is not None:
        run["usage"] = usage_public(usage)
    return run


def _ledger_record_map(ledger: Any, agent_id: str) -> dict[int, Any] | None:
    """Every run record in one store pass; ``None`` → caller reads serially.

    ``ModalDictRunStore.list`` fetches the per-agent index doc then the
    records through a bounded pool, so the map costs ~2 round-trips total
    instead of one ``ledger.get`` per run (SOR-199 N+1).
    """
    if ledger is None:
        return None
    try:
        return {r.n: r for r in ledger.list(agent_id)}
    except Exception:
        return None


def _run_state_map(
    run_states: Any,
    ledger: Any,
    agent_id: str,
    records: dict[int, Any] | None,
) -> dict[int, Any] | None:
    """Run-state rows for the listing's single store pass.

    ``LedgerRunStates`` over the same ledger shares the record pass — its
    ``get``/``list`` are the identical reads and a second one is pure N+1.
    A separate ``RunStateStore`` lists its own rows once. ``None`` → the
    renderer resolves serially.
    """
    if run_states is None:
        return None
    rs_ledger = getattr(run_states, "_ledger", None)
    if rs_ledger is not None and rs_ledger is ledger:
        return records
    try:
        return {s.n: s for s in run_states.list(agent_id)}
    except Exception:
        return None


def _runs(
    plane: Any,
    rec: Any,
    v1: V1State,
    run_states: RunStateStore | None = None,
    *,
    scheduler: Any = None,
    reporter: Any = None,
) -> list[dict[str, Any]]:
    pub = plane.public(rec)
    cancelled = v1.cancelled(rec.id)
    meta = _meta_for(v1, rec)
    ledger = _ledger(plane)
    records = _ledger_record_map(ledger, rec.id)
    states = _run_state_map(run_states, ledger, rec.id, records)
    ns = sorted(_known_run_ns(rec, ledger, run_states, records=records, states=states))
    return [
        _run_public(
            plane,
            pub,
            rec,
            n,
            cancelled,
            meta,
            run_states,
            scheduler=scheduler,
            reporter=reporter,
            record=records.get(n) if records is not None else _RUN_FETCH_UNRESOLVED,
            state=states.get(n) if states is not None else _RUN_FETCH_UNRESOLVED,
        )
        for n in ns
    ]


def _require_agent(plane: Any, agent_id: str) -> Any:
    rec = plane.get(agent_id)
    if rec is None:
        raise not_found("agent not found")
    if rec.status == "running":
        # A ``running`` record with no in-process watcher is stranded by a
        # control-plane cutover — the provider may already have written the
        # turn outcome. Settle it from evidence before answering so reads
        # report the truth and publish/handoff see an idle agent (SOR-139).
        reconcile = getattr(plane, "reconcile_turn", None)
        if callable(reconcile) and reconcile(agent_id):
            rec = plane.get(agent_id) or rec
    return rec


# Sentinel: ``task`` not supplied → resolve the binding serially (single
# record reads); ``None`` → pre-resolved as "no binding" by a batch pass.
_TASK_UNRESOLVED = object()


def _agent_payload(
    plane: Any,
    v1: V1State,
    workflows: WorkflowService,
    rec: Any,
    *,
    task: Any = _TASK_UNRESOLVED,
) -> dict[str, Any]:
    """Agent view: contract fields + workflow binding + honest usage.

    ``usage`` comes from the session record — ``None`` (never measured)
    serializes as ``null``, never fabricated zeros (SOR-84). ``metadata``
    echoes the durable workflow/task binding when one is attached. List
    paths pass a batch-resolved ``task`` so the per-agent Dict read is
    amortized into one store pass (SOR-200).
    """
    if task is _TASK_UNRESOLVED:
        try:
            task = workflows.for_agent(rec.id)
        except Exception:
            task = None
    metadata = (
        {
            "workflow_id": task.workflow_id,
            "task_id": task.task_id,
            "role": task.role,
            "parent_task_id": task.parent_task_id,
        }
        if task is not None
        else None
    )
    # SOR-181: the echo resolves through the same durable-state lookup
    # as provisioning/cost so tag-only records report their sizing too.
    _compute = compute_for_record(getattr(rec, "compute", None), getattr(rec, "sandbox_tags", None))
    return agent_public(
        plane.public(rec),
        _meta_for(v1, rec),
        usage=rec.usage,
        metadata=metadata,
        compute=_compute.public() if _compute is not None else None,
    )


def _require_run(
    plane: Any,
    rec: Any,
    run_id: str,
    v1: V1State,
    run_states: RunStateStore | None = None,
    *,
    scheduler: Any = None,
    reporter: Any = None,
) -> dict[str, Any]:
    n = _run_n(run_id)
    known = _known_run_ns(rec, _ledger(plane), run_states)
    if n is None or n not in known:
        raise not_found("run not found")
    pub = plane.public(rec)
    return _run_public(
        plane,
        pub,
        rec,
        n,
        v1.cancelled(rec.id),
        _meta_for(v1, rec),
        run_states,
        scheduler=scheduler,
        reporter=reporter,
    )


# ---------------------------------------------------------------- agents


def _raise_schedule_error(
    error: str | None,
    *,
    retry_after: float | None,
    provider: str,
    requested: str,
) -> None:
    if not error:
        return
    if error == "invalid_provider":
        raise V1ApiError(400, "invalid_provider", f"unknown provider {provider!r}")
    if error in ("account_busy", "account_unavailable"):
        raise V1ApiError(409, error, f"account {requested!r} cannot take the run")
    if error == "concurrency_limit":
        # Global cap (SBX_MAX_CONCURRENT), not a provider-pool refusal.
        raise V1ApiError(
            429,
            "concurrency_limit",
            "global live-agent cap (SBX_MAX_CONCURRENT) reached — "
            "idle agents hold slots until closed",
            retry_after=retry_after,
        )
    raise V1ApiError(
        429,
        "provider_exhausted",
        f"no account available for provider {provider!r}",
        retry_after=retry_after,
    )


def _release_lease(lease: Any) -> None:
    """Release one scheduler lease; idempotent and never raises."""
    if lease is not None:
        try:
            lease.release()
        except Exception:
            pass


def _release_agent_lease(v1: V1State, agent_id: str) -> None:
    """Pop and release the lease stored for ``agent_id`` (no-op when absent)."""
    _release_lease(v1.pop_lease(agent_id))


def _discard_agent(plane: Any, v1: V1State, agent_id: str, lease: Any = None) -> None:
    """Best-effort teardown of a half-created agent.

    Closes the session and frees the scheduler lease; secondary failures are
    swallowed so the original route error is never masked. Reaper / internal
    ``/api`` cleanup belongs to the control plane (P2-C), not this route.
    """
    try:
        plane.close(agent_id)
    except Exception:
        pass
    stored = v1.pop_lease(agent_id)
    _release_lease(stored)
    if lease is not None and lease is not stored:
        _release_lease(lease)


def _snapshot_for(
    capabilities: Any, account: Account | None, *, ensure: bool = True
) -> CapabilitySnapshot | None:
    """Catalog snapshot for the resolved account.

    ``None`` only when there is no account. A missing or non-catalog
    ``capabilities`` (e.g. a direct unit call) degrades to declared rows —
    the endpoint always advertises the models a seeded account carries.
    ``ensure=False`` serves the current cache only — used on the agent
    create path so a request can never spawn a probe sandbox as a
    side-effect of validation.
    """
    if account is None:
        return None
    if capabilities is None or not callable(getattr(capabilities, "get", None)):
        return declared_snapshot(account)
    try:
        return capabilities.get(account, ensure=ensure)
    except TypeError:
        return capabilities.get(account)
    except Exception:
        return declared_snapshot(account)


def _model_row(snapshot: CapabilitySnapshot, model: str | None) -> Any:
    """The capability row for ``model`` (id or alias); ``None`` if absent."""
    if model is None:
        return None
    for m in snapshot.models:
        if m.model == model or model in m.aliases:
            return m
    return None


def _effort_row(provider: str, model: str | None, snapshot: CapabilitySnapshot | None) -> Any:
    """Catalog row for ``model``, derived from the id when uncatalogued.

    Non-discovered snapshots don't gate model ids, but the effort surface
    stays model-scoped — a tier-less ``agy`` id must not inherit the
    provider floor (real ``agy --effort`` on it fails ``model_unavailable``
    mid-run). Deriving the row keeps the create gate and the init
    ``effort_surface`` tag on the same data.
    """
    row = _model_row(snapshot, model) if snapshot is not None else None
    if row is None and model is not None and (snapshot is None or snapshot.source != "discovered"):
        row = capability_from_model_id(provider, model)
    return row


def _default_model(provider: str, account: Account | None, capabilities: Any = None) -> str | None:
    """Omitted ``AgentSpec.model`` → a valid provider/account default.

    SOR-204: the capability catalog's default wins — a discovered snapshot
    reflects what the account actually serves. Then the account's declared
    models, then the provider's seeded default. ``None`` defers to the
    plane's configured default (``gpt-5.6-luna``, codex compatibility).
    """
    snapshot = _snapshot_for(capabilities, account, ensure=False)
    if snapshot is not None and snapshot.default_model:
        return snapshot.default_model
    if account is not None and account.models:
        return account.models[0]
    defaults = PROVIDER_DEFAULT_MODELS.get(provider) or ()
    return defaults[0] if defaults else None


def _workspace_error(exc: WorkspaceError) -> V1ApiError:
    """SOR-83 domain error → canonical v1 error (machine code preserved)."""
    if exc.code in (WORKSPACE_NOT_FOUND, ARTIFACT_NOT_FOUND):
        return V1ApiError(404, exc.code, exc.message)
    if exc.code == WORKSPACE_INVALID:
        return V1ApiError(400, exc.code, exc.message)
    return V1ApiError(409, exc.code, exc.message)


def _validate_workspace_decl(
    body: CreateAgentRequest,
    artifacts: Any,
    *,
    key: ApiKey | None = None,
    plane: Any = None,
    task_store: Any = None,
    revisions: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Validate the SOR-83 ``workspace``/``handoff`` and SOR-128 ``git``
    declarations.

    Handoff without a workspace is meaningless at create time (there is no
    prior record to apply onto), a referenced artifact must exist in the
    durable store, and a git policy only makes sense on a declared repo —
    all fail fast with explicit errors before any claim or sandbox work
    begins.
    """
    workspace = body.workspace.model_dump() if body.workspace is not None else None
    handoff = body.handoff.model_dump() if body.handoff is not None else None
    git = body.git.model_dump(exclude_none=True) if body.git is not None else None
    if workspace is not None:
        try:
            WorkspaceSpec(
                repo=workspace["repo"],
                base_ref=workspace["base_ref"],
                base_sha=workspace["base_sha"],
            )
        except WorkspaceError as exc:
            raise _workspace_error(exc) from exc
    if git is not None:
        if workspace is None:
            raise V1ApiError(400, WORKSPACE_INVALID, "git policy requires a workspace declaration")
        try:
            validate_git_policy(git)
        except WorkspaceError as exc:
            raise _workspace_error(exc) from exc
    if handoff is not None:
        handoff.pop("workspace", None)  # only meaningful on the handoff route
        # SOR-225: resolve the caller-friendly refs server-side so the
        # downstream lifecycle sees the canonical artifact_id / pull_request
        # forms — no caller-supplied ref/SHA needed.
        if handoff.get("task_id") is not None:
            if handoff.get("revision") is not None and not isinstance(handoff["revision"], str):
                raise V1ApiError(400, WORKSPACE_INVALID, "handoff 'revision' must be a string ref")
            task = task_store.get(handoff["task_id"]) if task_store else None
            if task is None or (key is not None and task.owner != key.id):
                raise not_found("task not found")
            if not task.agent_id:
                raise V1ApiError(
                    409,
                    "revision_not_found",
                    "task has no agent yet — no revisions exist",
                )
            try:
                rev = revisions.resolve(task.agent_id, handoff.get("revision"))
            except RevisionError as exc:
                raise V1ApiError(exc.status_code, exc.code, exc.message) from exc
            if not rev.artifact_id:
                raise V1ApiError(
                    409,
                    "revision_not_ready",
                    f"revision {rev.revision_id} has no artifact payload (status={rev.status!r})",
                )
            handoff["artifact_id"] = rev.artifact_id
        if handoff.get("pr_url") is not None:
            try:
                ref, pr_sha = revisions.resolve_pr_url(handoff["pr_url"])
            except RevisionError as exc:
                raise V1ApiError(exc.status_code, exc.code, exc.message) from exc
            handoff["pull_request"] = {"ref": ref, "head_sha": pr_sha}
        handoff.pop("task_id", None)
        handoff.pop("revision", None)
        handoff.pop("pr_url", None)
        has_artifact = bool(handoff.get("artifact_id"))
        has_head = bool(handoff.get("head_sha"))
        has_pr = bool(handoff.get("pull_request"))
        if sum((has_artifact, has_head, has_pr)) != 1:
            raise V1ApiError(
                400,
                WORKSPACE_INVALID,
                "handoff needs exactly one of artifact_id, head_sha, "
                "pull_request, task_id or pr_url",
            )
        if workspace is None:
            raise V1ApiError(400, WORKSPACE_INVALID, "handoff requires a workspace declaration")
        if has_artifact:
            if key is not None and key.id.startswith("usr_"):
                require_artifact_owner(plane, artifacts, key, handoff["artifact_id"])
            try:
                artifacts.manifest(handoff["artifact_id"])
            except ArtifactNotFoundError as exc:
                raise V1ApiError(
                    404, ARTIFACT_NOT_FOUND, f"unknown artifact {handoff['artifact_id']!r}"
                ) from exc
            except ArtifactError as exc:
                raise V1ApiError(409, "artifact_invalid", str(exc)) from exc
        if has_pr:
            pr = handoff["pull_request"]
            if not is_safe_ref(pr.get("ref")):
                raise V1ApiError(
                    400, WORKSPACE_INVALID, f"unsafe pull_request ref: {pr.get('ref')!r}"
                )
    return workspace, handoff, git


def _normalize_contract(contract: OutputContract | None) -> dict[str, Any] | None:
    """Validate + normalize an ``output_contract`` declaration (SOR-130).

    A malformed body — bad enforcement, non-object schema, or a schema using
    keywords outside the deterministic validator subset — is refused as
    ``400 invalid_output_contract`` before any run is allocated, so an
    unenforceable contract can never reach a sandbox.
    """
    if contract is None:
        return None
    try:
        return normalize_contract(contract.model_dump(by_alias=True))
    except ContractError as exc:
        raise V1ApiError(400, "invalid_output_contract", str(exc)) from exc
    except Exception as exc:
        # normalize_contract only raises ContractError by design; anything
        # else (e.g. a schema too deep to serialize) is still a refused
        # contract — 400, never an uncaught 500.
        raise V1ApiError(
            400, "invalid_output_contract", f"unusable output contract: {type(exc).__name__}"
        ) from exc


def _validate_compute(body: CreateAgentRequest) -> ComputeSpec:
    """Validate + resolve the SOR-181 ``compute`` declaration.

    Always returns a concrete spec — an omitted declaration resolves to
    the canonical defaults so the durable record carries the sandbox's
    real sizing. Malformed/inverted/out-of-bounds values fail as
    ``400 invalid_compute`` before any claim or sandbox work begins.
    """
    try:
        return resolve_compute(body.compute)
    except ComputeError as exc:
        raise V1ApiError(400, exc.code, exc.message) from exc


def _validate_reasoning_effort(body: CreateAgentRequest) -> str | None:
    """Validate a declared canonical ``reasoning_effort`` (SOR-179/204).

    Only the canonical shape is checked here — whether the *account* can
    honor the level depends on discovered capabilities, which are known
    only after the scheduler resolves the account; that check lives in
    ``_create_agent_once`` (``_check_effort_capability``).
    """
    try:
        return normalize_effort(body.agent.reasoning_effort)
    except ValueError as exc:
        raise V1ApiError(400, "unsupported", str(exc)) from exc


def _check_effort_capability(
    provider: str, model: str | None, effort: str, snapshot: CapabilitySnapshot | None
) -> None:
    """Refuse an effort the resolved account/model cannot honor (SOR-204).

    A discovered snapshot is authoritative — the row's ``reasoning_efforts``
    decide. Declared/env/static rows carry the provider's verified floor
    only where effort is an orthogonal CLI flag (codex, grok); an empty
    list there means the provider has no native surface at all. Uncatalogued
    ids on non-discovered snapshots get the same id-derived surface
    (``_effort_row``); model-less checks fall back to the static floor.
    """
    refusal: str | None = None
    row = _effort_row(provider, model, snapshot)
    if row is not None:
        if not row.reasoning_efforts:
            refusal = f"model {model!r} on {provider!r} has no native effort surface"
        elif effort not in row.reasoning_efforts:
            refusal = (
                f"model {model!r} does not support reasoning_effort {effort!r} "
                f"(supported: {list(row.reasoning_efforts)})"
            )
    elif snapshot is not None and snapshot.source == "discovered":
        refusal = f"model {model!r} is not advertised by this account"
    else:
        refusal = effort_error(provider, effort)
    if refusal is not None:
        raise V1ApiError(400, "unsupported", refusal)


def _validate_resources(body: CreateAgentRequest, registry: Any) -> dict[str, Any] | None:
    """Validate + resolve SOR-129 ``resources`` refs against the registry.

    Unknown/disallowed refs fail as ``400 invalid_resource``; MCP refs on a
    provider with no MCP channel fail as ``400 unsupported`` — both before
    any claim or sandbox work begins, never silently ignored.
    """
    try:
        return resolve_resources(body.resources, provider=body.agent.provider, registry=registry)
    except ResourceError as exc:
        raise V1ApiError(400, exc.code, exc.message) from exc


@router.post("/agents", status_code=201)
def create_agent(
    body: CreateAgentRequest,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
    scheduler: Scheduler = Depends(get_scheduler),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    reporter: RunFailureReporter = Depends(get_run_reporter),
    artifacts: Any = Depends(get_artifact_store),
    workflows: WorkflowService = Depends(get_workflow_service),
    resources_registry: Any = Depends(get_resources),
    capabilities: Any = Depends(get_capabilities),
    task_store: Any = Depends(get_task_store),
    revisions: Any = Depends(get_revisions),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Create an agent and queue its first run.

    Returns as soon as the session record + a ``CREATING`` run-1 exist;
    sandbox cold start / ``runner init`` / first-turn dispatch run on a
    background worker and surface through ``GET`` polling. A retried
    ``Idempotency-Key`` with the same body replays the original response;
    a different body under a used key is a 409 ``idempotency_conflict``.

    ``workspace`` declares the checkout the run must start on;
    ``handoff`` makes run-1 start from a referenced artifact or commit.
    """
    workspace, handoff, git = _validate_workspace_decl(
        body, artifacts, key=key, plane=plane, task_store=task_store, revisions=revisions
    )
    contract = _normalize_contract(body.output_contract)
    compute = _validate_compute(body)
    effort = _validate_reasoning_effort(body)
    resources = _validate_resources(body, resources_registry)
    if contract is not None and _ledger(plane) is None:
        # Contracted runs need the durable ledger for both dispatch and the
        # persisted verdict — refuse rather than run uncontracted.
        raise V1ApiError(409, "session_not_runnable", "output contracts require the run ledger")
    owned = None
    fingerprint = request_fingerprint(body)
    if idempotency_key:
        outcome, entry = v1.idempotency.claim(key.id, idempotency_key, fingerprint)
        if outcome == "hit":
            return entry.body
        if outcome == "conflict":
            raise V1ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different request body",
            )
        if outcome == "timeout":
            raise V1ApiError(
                409,
                "idempotency_in_progress",
                "a create with this Idempotency-Key is still in progress",
            )
        # Owned the claim: now the durable bound. The session record pins
        # (api key, key), so a retry landing after a control-plane restart —
        # where the in-memory IdempotencyStore is empty — still resolves to
        # the original agent instead of provisioning a second worker.
        prior = plane.find_by_idempotency(key.id, idempotency_key)
        if prior is not None:
            if prior.idempotency_fingerprint not in (None, fingerprint):
                v1.idempotency.abandon(key.id, idempotency_key, entry)
                raise V1ApiError(
                    409,
                    "idempotency_conflict",
                    "Idempotency-Key was already used with a different request body",
                )
            if body.metadata is not None:
                # Idempotent upsert: covers the rare case where the first
                # attempt died between open_session and attach.
                workflows.attach(owner=key.id, agent_id=prior.id, metadata=body.metadata)
            pub = plane.public(prior)
            meta = _meta_for(v1, prior)
            result = {
                "agent": _agent_payload(plane, v1, workflows, prior),
                "run": _run_public(
                    plane,
                    pub,
                    prior,
                    1,
                    v1.cancelled(prior.id),
                    meta,
                    run_states,
                    scheduler=scheduler,
                    reporter=reporter,
                ),
            }
            v1.idempotency.complete(key.id, idempotency_key, entry, agent_id=prior.id, body=result)
            v1.idempotency.settle(key.id, idempotency_key, entry)
            return result
        owned = entry

    on_provisioned = None
    if owned is not None:
        # The claim resolves only once the worker's sandbox allocation has —
        # a duplicate that lands mid-provision waits instead of racing a
        # second backend.create.
        on_provisioned = lambda: v1.idempotency.settle(  # noqa: E731
            key.id, idempotency_key, owned
        )
    try:
        result = _create_agent_once(
            body,
            key,
            plane,
            registry,
            scheduler,
            v1,
            run_states,
            workflows,
            capabilities=capabilities,
            reporter=reporter,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=fingerprint,
            on_provisioned=on_provisioned,
            workspace=workspace,
            handoff=handoff,
            git=git,
            output_contract=contract,
            resources=resources,
            compute=compute,
            reasoning_effort=effort,
        )
    except Exception:
        if owned is not None:
            # Failed creates don't pin the key — a retry may proceed.
            v1.idempotency.abandon(key.id, idempotency_key, owned)
        raise
    if owned is not None:
        v1.idempotency.complete(
            key.id,
            idempotency_key,
            owned,
            agent_id=result["agent"]["id"],
            body=result,
        )
    return result


def _create_agent_once(
    body: CreateAgentRequest,
    key: ApiKey,
    plane: Any,
    registry: AccountRegistry,
    scheduler: Scheduler,
    v1: V1State,
    run_states: RunStateStore,
    workflows: WorkflowService,
    *,
    capabilities: Any = None,
    reporter: RunFailureReporter | None = None,
    idempotency_key: str | None = None,
    idempotency_fingerprint: str | None = None,
    on_provisioned: Any = None,
    workspace: dict[str, Any] | None = None,
    handoff: dict[str, Any] | None = None,
    git: dict[str, Any] | None = None,
    output_contract: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
    compute: ComputeSpec | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    provider = body.agent.provider
    requested = body.agent.account_id or "auto"

    # In-process leases can only decay here — the reaper cron runs in a
    # separate container and its lease release is a no-op on this process.
    # Sessions that went terminal/lost/timed_out/suspended between binds
    # would otherwise pin scheduler slots until manual close (SOR-271 r5).
    reconcile_leases = getattr(v1, "reconcile_leases", None)
    if callable(reconcile_leases):
        reconcile_leases(getattr(plane, "store", None))

    # P2.1's Devin pool exposes an atomic acquire() in addition to the frozen
    # consultative Scheduler.decide() port.  Use it when available so two
    # concurrent POSTs cannot both observe the same free slot.
    lease = None
    acquire = getattr(scheduler, "acquire", None)
    if callable(acquire):
        try:
            lease = acquire(provider=provider, account=requested)
        except ScheduleRefused as exc:
            _raise_schedule_error(
                exc.error,
                retry_after=exc.retry_after,
                provider=provider,
                requested=requested,
            )
            raise AssertionError("unreachable")
        account = lease.account
    else:
        decision = scheduler.decide(provider=provider, account=requested)
        _raise_schedule_error(
            decision.error,
            retry_after=decision.retry_after,
            provider=provider,
            requested=requested,
        )
        account = decision.account

    resolved = account.id if account is not None else requested
    secret_name = None
    if account is not None:
        secret_name = account.secret_name or None
    snapshot = _snapshot_for(capabilities, account, ensure=False)
    model = body.agent.model or _default_model(provider, account, capabilities)

    # SOR-204: reject combinations the account cannot serve. A discovered
    # snapshot is authoritative — an explicit model it does not advertise
    # fails fast instead of surfacing as a provider error mid-run.
    if (
        body.agent.model
        and snapshot is not None
        and snapshot.source == "discovered"
        and _model_row(snapshot, body.agent.model) is None
    ):
        _release_lease(lease)
        advertised = [m.model for m in snapshot.models]
        raise V1ApiError(
            400,
            "unsupported",
            f"model {body.agent.model!r} is not advertised by account "
            f"{account.id!r} (available: {advertised})",
        )
    if reasoning_effort is not None:
        try:
            _check_effort_capability(provider, model, reasoning_effort, snapshot)
        except V1ApiError:
            _release_lease(lease)
            raise

    # SOR-204: carry the resolved row's effort surface to init so the
    # in-sandbox backstop validates the declaration against the same
    # (possibly discovered-widened) surface the API just checked.
    surface_row = _effort_row(provider, model, snapshot)
    effort_surface = list(surface_row.reasoning_efforts) if surface_row is not None else None

    try:
        session_id = plane.open_session(
            owner=key.id,
            title=body.name,
            model=model,
            provider=provider,
            account_id=resolved,
            first_prompt=body.prompt.text,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=idempotency_fingerprint,
            output_contract=output_contract,
            resource_refs=resource_refs(resources),
            compute=compute,
            reasoning_effort=reasoning_effort,
            effort_surface=effort_surface,
        )
    except ConcurrencyLimit as exc:
        _release_lease(lease)
        raise V1ApiError(
            429,
            "concurrency_limit",
            "per-key live-agent cap (SBX_MAX_CONCURRENT) reached — "
            "idle agents hold slots until closed",
        ) from exc
    except Exception:
        _release_lease(lease)
        raise

    try:
        if lease is not None:
            v1.set_lease(session_id, lease)
        v1.set_meta(
            session_id,
            AgentMeta(
                provider=provider,
                account_id=resolved,
                name=body.name,
                idle_timeout_s=body.idle_timeout_s,
                resources=resource_refs(resources),
                reasoning_effort=reasoning_effort,
            ),
        )
        if account is not None:
            try:
                registry.touch(account.id, _iso_now())
            except KeyError:
                pass
        if body.metadata is not None:
            # SOR-84 C1: persist the caller's workflow/task binding before
            # the worker starts so recovery never sees an untracked agent.
            workflows.attach(owner=key.id, agent_id=session_id, metadata=body.metadata)
        # Backstop for ledger-less run-state seams: with the durable ledger
        # attached, open_session already persisted run-1 as CREATING and this
        # is an idempotent no-op.
        run_states.begin(session_id, 1, prompt=body.prompt.text)

        # The worker provisions the sandbox, runs ``runner init`` and
        # dispatches run-1; failures land as persisted run ERROR and release
        # the lease. If the thread itself cannot start, the discard below
        # frees the lease — otherwise the session would sit ``creating``
        # with a held lease until the reaper's create grace expires.
        launch_first_run(
            plane=plane,
            v1=v1,
            run_states=run_states,
            session_id=session_id,
            provider=provider,
            account_id=resolved,
            secret_name=secret_name,
            on_provisioned=on_provisioned,
            workspace=workspace,
            handoff=handoff,
            git=git,
            resources=resources,
        )
    except Exception:
        _discard_agent(plane, v1, session_id, lease)
        raise

    rec = _require_agent(plane, session_id)
    pub = plane.public(rec)
    agent = _agent_payload(plane, v1, workflows, rec)
    run = _run_public(
        plane,
        pub,
        rec,
        1,
        v1.cancelled(session_id),
        _meta_for(v1, rec),
        run_states,
        scheduler=scheduler,
        reporter=reporter,
    )
    return {"agent": agent, "run": run}


@router.get("/agents")
def list_agents(
    provider: ProviderId | None = None,
    account_id: str | None = None,
    status: str | None = None,
    workflow_id: str | None = None,
    cursor: str | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            raise V1ApiError(400, "invalid_request", "malformed cursor") from None
    # SOR-200: the independent store passes run concurrently — the
    # session listing, the workflow scope index (when filtered) and the
    # binding scan each cost a remote round-trip, so serializing them
    # would multiply the page latency; per-agent reads would be an N+1.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        records_fut = pool.submit(plane.store.list_all)
        scoped_fut = (
            pool.submit(workflows.agent_ids, key.id, workflow_id)
            if workflow_id is not None
            else None
        )
        bindings_fut = pool.submit(workflows.all_bindings)
        records = records_fut.result()
        records = [rec for rec in records if owner_visible(key, rec.owner)]
        if scoped_fut is not None:
            # SOR-84: index-backed scope — only agents whose durable
            # binding matches (caller key id, workflow_id) are listed.
            scoped = scoped_fut.result()
            records = [rec for rec in records if rec.id in scoped]
        try:
            bindings = bindings_fut.result()
        except Exception:
            # Same degradation as a failed serial ``for_agent``: echo
            # no metadata rather than fail the whole listing.
            bindings = {}
    # Filter, sort and page on record-level fields *before* enrichment —
    # provider/account/status derive from the durable record and
    # in-process meta/tags, so off-page and filtered-out rows never pay
    # a payload build.
    rows: list[tuple[Any, AgentMeta]] = []
    for rec in records:
        meta = _meta_for(v1, rec)
        if provider is not None and (meta.provider or "codex") != provider:
            continue
        if account_id is not None and (meta.account_id or "auto") != account_id:
            continue
        public_status = "idle" if rec.status == "suspended" else rec.status
        if status is not None and public_status != status:
            continue
        rows.append((rec, meta))
    rows.sort(key=lambda row: (row[0].created_at.isoformat(), row[0].id))
    page_rows = rows[start : start + AGENTS_PAGE_SIZE]
    next_cursor = str(start + AGENTS_PAGE_SIZE) if start + AGENTS_PAGE_SIZE < len(rows) else None
    agents = [
        _agent_payload(plane, v1, workflows, rec, task=bindings.get(rec.id))
        for rec, _meta in page_rows
    ]
    return {"agents": agents, "next_cursor": next_cursor}


@router.get("/agents/summary")
def agents_summary(
    provider: ProviderId | None = None,
    account_id: str | None = None,
    status: str | None = None,
    workflow_id: str | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """Cheap rollup the Console polls instead of a full ``GET /v1/agents``.

    Applies the same record-level filters as the list route but skips the
    per-agent payload build and the all-bindings scan: one store pass.
    ``version`` pins ``{total}:{max updated_at}`` so clients only refetch
    the expensive page when the set actually changed.
    """
    records = plane.store.list_all()
    records = [rec for rec in records if owner_visible(key, rec.owner)]
    if workflow_id is not None:
        try:
            scoped = workflows.agent_ids(key.id, workflow_id)
        except Exception:
            scoped = set()
        records = [rec for rec in records if rec.id in scoped]
    by_status: dict[str, int] = {}
    latest: datetime | None = None
    live = 0
    for rec in records:
        meta = _meta_for(v1, rec)
        if provider is not None and (meta.provider or "codex") != provider:
            continue
        if account_id is not None and (meta.account_id or "auto") != account_id:
            continue
        public_status = "idle" if rec.status == "suspended" else rec.status
        if status is not None and public_status != status:
            continue
        by_status[public_status] = by_status.get(public_status, 0) + 1
        if public_status in ("creating", "idle", "running"):
            live += 1
        if latest is None or rec.updated_at > latest:
            latest = rec.updated_at
    total = sum(by_status.values())
    return {
        "total": total,
        "live": live,
        "by_status": by_status,
        "version": f"{total}:{latest.isoformat() if latest else ''}",
    }


@router.get("/agents/{agent_id}")
def get_agent(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    return _agent_payload(plane, v1, workflows, rec)


@router.delete("/agents/{agent_id}")
def delete_agent(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    try:
        rec = plane.close(agent_id)
    except KeyError:
        raise not_found("agent not found") from None
    finally:
        # The account slot is freed even when close/terminate fails; deeper
        # sandbox cleanup stays with the control plane / reaper (P2-C).
        _release_agent_lease(v1, agent_id)
    return _agent_payload(plane, v1, workflows, rec)


# -------------------------------------------------------------- workflows


@router.get("/workflows/{workflow_id}")
def get_workflow(
    workflow_id: str,
    key: ApiKey = Depends(agents_key),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """Workflow query / recover read.

    Agents + latest runs + progress for ``(caller key id, workflow_id)``,
    served from persisted records only — cheap enough to poll while a fresh
    client process re-attaches after losing local state.
    """
    view = workflows.lookup(key.id, workflow_id)
    if view is None:
        raise not_found("workflow not found")
    return view


@router.delete("/workflows/{workflow_id}")
def delete_workflow(
    workflow_id: str,
    key: ApiKey = Depends(agents_key),
    workflows: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """Scoped cleanup: close exactly this workflow's agents (idempotent).

    Other workflows — and other principals' same-named workflows — are
    never touched; the per-agent owner is re-checked before close.
    """
    result = workflows.cleanup(key.id, workflow_id)
    if result is None:
        raise not_found("workflow not found")
    return result


# --------------------------------------------- workspaces + artifacts (SOR-83)


def _require_live_idle(plane: Any, agent_id: str) -> Any:
    """The session record for workspace-mutating routes: must exist, sit
    idle on a live sandbox (a running turn would mutate files mid-apply)."""
    rec = _require_agent(plane, agent_id)
    if rec.status == "running":
        raise V1ApiError(409, "turn_in_progress", "a run is in progress")
    if rec.status != "idle":
        raise V1ApiError(409, "session_not_runnable", f"agent status is {rec.status}")
    if rec.handle() is None:
        raise V1ApiError(409, "session_not_runnable", "agent has no live sandbox")
    return rec


@router.get("/agents/{agent_id}/workspace")
def get_workspace(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    workspaces: Any = Depends(get_workspaces),
) -> dict[str, Any]:
    """Durable workspace record: declared base, actual checkout/head, and
    the sha an independent reviewer pinned (``reviewed_head_sha``)."""
    _require_agent(plane, agent_id)
    try:
        with observe("v1.workspace.get", agent_id=agent_id):
            record = workspaces.get(agent_id)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    if record is None:
        raise not_found("workspace not found")
    return {"workspace": workspace_record_to_dict(record)}


@router.post("/agents/{agent_id}/workspace/review")
def review_workspace(
    agent_id: str,
    body: ReviewWorkspaceRequest | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    workspaces: Any = Depends(get_workspaces),
) -> dict[str, Any]:
    """Pin ``reviewed_head_sha`` — the exact commit a reviewer signed off.

    ``head_sha`` defaults to the recorded head; an explicit value that
    disagrees with it is an explicit ``head_sha_mismatch``, never a silent
    mislabel.

    ``comment`` additionally posts a machine-readable comment on
    the workspace's recorded pull request — never a formal review approval
    under the shared GitHub identity. Commenting requires the agent's live
    sandbox.
    """
    comment = body.comment if body else None
    rec = _require_agent(plane, agent_id)
    try:
        with observe("v1.workspace.review", agent_id=agent_id):
            record = workspaces.mark_reviewed(agent_id, body.head_sha if body else None)
            if comment:
                if rec.status == "running":
                    raise V1ApiError(409, "turn_in_progress", "a run is in progress")
                handle = rec.handle()
                if handle is None:
                    raise V1ApiError(409, "session_not_runnable", "comment needs a live sandbox")
                record = workspaces.post_review_comment(handle, agent_id, comment)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    return {"workspace": workspace_record_to_dict(record)}


@router.post("/agents/{agent_id}/git/publish")
def publish_git(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    workspaces: Any = Depends(get_workspaces),
) -> dict[str, Any]:
    """Execute the agent's declared git policy.

    Refreshes the recorded head, pushes the work branch to the workspace
    repo's remote, verifies the remote head (drift fails closed), and —
    when the policy's ``auto_create_pr`` is set — opens the declared pull
    request. ``pushed_head_sha`` / ``pull_request`` land on the durable
    workspace record so a reviewer can pin the exact published head.
    """
    rec = _require_live_idle(plane, agent_id)
    try:
        with observe("v1.git.publish", agent_id=agent_id):
            record = workspaces.publish(rec.handle(), agent_id)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    return {"workspace": workspace_record_to_dict(record)}


@router.post("/agents/{agent_id}/git/merge")
def merge_git(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    workspaces: Any = Depends(get_workspaces),
) -> dict[str, Any]:
    """Merge the recorded pull request — review-gated.

    Requires the policy's ``merge`` flag, a recorded pull request, and an
    independent exact-sha review pin (``reviewed_head_sha`` set via
    ``POST /agents/{id}/workspace/review``). The recorded PR head and the
    remote PR ref must still equal the pin — any drift is
    ``head_sha_mismatch`` and needs a fresh review; an absent pin is
    ``review_required``. Merge metadata lands on ``workspace.merge``.
    """
    rec = _require_live_idle(plane, agent_id)
    try:
        with observe("v1.git.merge", agent_id=agent_id):
            record = workspaces.merge(rec.handle(), agent_id)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    return {"workspace": workspace_record_to_dict(record)}


@router.post("/agents/{agent_id}/handoff")
def apply_handoff(
    agent_id: str,
    body: HandoffRef,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    workspaces: Any = Depends(get_workspaces),
    handoffs: Any = Depends(get_handoffs),
    task_store: Any = Depends(get_task_store),
    revisions: Any = Depends(get_revisions),
    artifacts: Any = Depends(get_artifact_store),
) -> dict[str, Any]:
    """Apply a second-agent handoff into a live agent's workspace.

    ``artifact_id`` applies a durable artifact package; ``head_sha`` checks
    out an exact commit; ``pull_request`` fetches a remote ref pinned to an
    exact head — drift fails closed. Caller-friendly forms: ``task_id`` +
    ``revision`` (default ``"latest"``) hands off the
    task's durable revision artifact, and ``pr_url`` resolves a GitHub pull
    URL to its ref + head server-side. All validate against the workspace's
    recorded head before touching the workdir — a gap is an explicit
    ``base_sha_mismatch`` / ``head_sha_mismatch``.
    """
    rec = _require_live_idle(plane, agent_id)
    handle = rec.handle()
    has_artifact = bool(body.artifact_id)
    if has_artifact and key.id.startswith("usr_"):
        require_artifact_owner(plane, artifacts, key, body.artifact_id)
    has_head = bool(body.head_sha)
    has_pr = body.pull_request is not None
    has_task = bool(body.task_id)
    has_pr_url = bool(body.pr_url)
    if sum((has_artifact, has_head, has_pr, has_task, has_pr_url)) != 1:
        raise V1ApiError(
            400,
            WORKSPACE_INVALID,
            "handoff needs exactly one of artifact_id, head_sha, pull_request, task_id or pr_url",
        )
    if body.revision and not has_task:
        raise V1ApiError(
            400,
            WORKSPACE_INVALID,
            "handoff field 'revision' is only valid together with 'task_id'",
        )
    task_artifact_id = None
    if has_task:
        task = task_store.get(body.task_id)
        if task is None or task.owner != key.id:
            raise not_found("task not found")
        if not task.agent_id:
            raise V1ApiError(
                409, "revision_not_found", "task has no agent yet — no revisions exist"
            )
        try:
            revision = revisions.resolve(task.agent_id, body.revision)
        except RevisionError as exc:
            raise V1ApiError(exc.status_code, exc.code, exc.message) from exc
        if not revision.artifact_id:
            raise V1ApiError(
                409,
                "revision_not_ready",
                f"revision {revision.revision_id} has no artifact payload "
                f"(status={revision.status!r})",
            )
        task_artifact_id = revision.artifact_id
    pr_ref = None
    pr_head_sha = None
    if has_pr_url:
        try:
            pr_ref, pr_head_sha = revisions.resolve_pr_url(body.pr_url)
        except RevisionError as exc:
            raise V1ApiError(exc.status_code, exc.code, exc.message) from exc
    spec = None
    if body.workspace is not None:
        try:
            spec = WorkspaceSpec(
                repo=body.workspace.repo,
                base_ref=body.workspace.base_ref,
                base_sha=body.workspace.base_sha,
            )
        except WorkspaceError as exc:
            raise _workspace_error(exc) from exc
    try:
        with observe("v1.workspace.handoff", agent_id=agent_id):
            if task_artifact_id is not None:
                record = handoffs.prepare_from_artifact(
                    handle, agent_id, task_artifact_id, spec=spec
                )
            elif has_artifact:
                record = handoffs.prepare_from_artifact(
                    handle, agent_id, body.artifact_id, spec=spec
                )
            elif has_pr_url:
                record = handoffs.prepare_from_pull_request(
                    handle, agent_id, pr_ref, pr_head_sha, spec=spec
                )
            elif has_pr:
                record = handoffs.prepare_from_pull_request(
                    handle,
                    agent_id,
                    body.pull_request.ref,
                    body.pull_request.head_sha,
                    spec=spec,
                )
            else:
                record = handoffs.prepare_from_head(handle, agent_id, body.head_sha, spec=spec)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    return {"workspace": workspace_record_to_dict(record)}


def _artifact_public(manifest: Any) -> dict[str, Any]:
    out = manifest_to_dict(manifest)
    out["download_url"] = f"/v1/artifacts/{manifest.artifact_id}/download"
    return out


def _artifact_forbidden(plane: Any, v1: V1State, registry: Any, rec: Any) -> tuple[bytes, ...]:
    """Secrets that must never enter this agent's artifact: the account's
    credential blob contents plus ambient credential env values."""
    account_id = _meta_for(v1, rec).account_id
    blob = None
    get_blob = getattr(registry, "get_credential_blob", None)
    if callable(get_blob) and account_id and account_id != "auto":
        try:
            blob = get_blob(account_id)
        except Exception:
            blob = None
    return credential_forbidden_values(blob)


@router.post("/agents/{agent_id}/artifacts", status_code=201)
def create_artifact(
    agent_id: str,
    body: CreateArtifactRequest | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
    v1: V1State = Depends(get_v1_state),
    workspaces: Any = Depends(get_workspaces),
    artifacts: Any = Depends(get_artifact_store),
) -> dict[str, Any]:
    """Snapshot the agent's declared workspace into a durable artifact.

    Runs while the sandbox is still alive so the package outlives teardown.
    The manifest records base/head shas, per-file checksums, producer
    identity and any test result; ``run_id`` (default: the last run) gets an
    ``artifact://<id>`` ref persisted on its durable run record.
    """
    rec = _require_live_idle(plane, agent_id)
    run_id = body.run_id if body is not None else None
    run_n: int | None = None
    if run_id is not None:
        run_n = _run_n(run_id)
        if run_n is None:
            raise V1ApiError(400, "invalid_request", f"malformed run_id {run_id!r}")
        if run_n not in _known_run_ns(rec, _ledger(plane)):
            raise not_found("run not found")
    elif rec.turns:
        run_n = int(rec.turns)
        run_id = f"run-{run_n}"
    try:
        with observe("v1.artifact.create", agent_id=agent_id, run_id=run_id):
            manifest = snapshot_workspace_artifact(
                backend=plane.backend,
                handle=rec.handle(),
                workspaces=workspaces,
                store=artifacts,
                agent_id=agent_id,
                run_id=run_id,
                test_command=body.test_command if body is not None else None,
                forbidden_values=_artifact_forbidden(plane, v1, registry, rec),
                ledger=_ledger(plane),
                run_n=run_n,
            )
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    except ArtifactSecretError as exc:
        raise V1ApiError(409, "artifact_secret", str(exc)) from exc
    except ArtifactError as exc:
        raise V1ApiError(409, "artifact_invalid", str(exc)) from exc
    return {"artifact": _artifact_public(manifest)}


@router.get("/artifacts")
def list_artifacts(
    agent_id: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    key: ApiKey = Depends(agents_key),
    artifacts: Any = Depends(get_artifact_store),
    plane: Any = Depends(get_plane),
) -> dict[str, Any]:
    """Durable artifact manifests, ``(created_at, artifact_id)`` keyset order.

    ``?agent_id=`` filters by producer through the durable per-agent
    index — the query reads one index document plus the page's manifests,
    never scans the whole store. ``?limit=`` pages the result
    and ``next_cursor`` resumes it; omit both for the full listing.
    """
    if limit is not None and not 1 <= limit <= ARTIFACTS_PAGE_MAX:
        raise V1ApiError(400, "invalid_request", f"limit must be 1..{ARTIFACTS_PAGE_MAX}")
    list_page = getattr(artifacts, "list_page", None)
    try:
        with observe("v1.artifact.list", agent_id=agent_id):
            if not has_scope(key, "admin"):
                owners = {rec.id: rec.owner for rec in plane.store.list_all()}
                visible = [
                    m
                    for m in artifacts.list(agent_id=agent_id)
                    if (m.producer_agent_id not in owners and not key.id.startswith("usr_"))
                    or (
                        m.producer_agent_id in owners
                        and owner_visible(key, owners[m.producer_agent_id])
                    )
                ]
                page = page_manifests(visible, cursor=cursor, limit=limit)
            elif callable(list_page):
                page = list_page(agent_id=agent_id, cursor=cursor, limit=limit)
            else:
                # Stores without list_page (custom injects): page in memory
                # over the full listing so the route contract still holds.
                page = page_manifests(artifacts.list(agent_id=agent_id), cursor=cursor, limit=limit)
    except ArtifactError as exc:
        raise V1ApiError(400, "invalid_request", str(exc)) from exc
    return {
        "artifacts": [_artifact_public(m) for m in page.artifacts],
        "next_cursor": page.next_cursor,
    }


@router.get("/artifacts/{artifact_id}")
def get_artifact(
    artifact_id: str,
    key: ApiKey = Depends(agents_key),
    artifacts: Any = Depends(get_artifact_store),
) -> dict[str, Any]:
    """Artifact manifest: file checksums, base/head shas, producer identity."""
    try:
        with observe("v1.artifact.get", artifact_id=artifact_id):
            manifest = artifacts.manifest(artifact_id)
    except ArtifactNotFoundError as exc:
        raise not_found("artifact not found") from exc
    except ArtifactCorruptError as exc:
        raise V1ApiError(409, "artifact_invalid", str(exc)) from exc
    return _artifact_public(manifest)


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(
    artifact_id: str,
    member: str = "patch.diff",
    key: ApiKey = Depends(agents_key),
    artifacts: Any = Depends(get_artifact_store),
) -> Response:
    """Download one member's bytes (``manifest.json``, ``patch.diff``,
    ``repo.bundle``, or ``files/<path>``). Checksum-verified on read; works
    after the producing sandbox is gone."""
    try:
        with observe("v1.artifact.download", artifact_id=artifact_id, member=member):
            data = artifacts.read(artifact_id, member)
    except ArtifactNotFoundError as exc:
        raise not_found("artifact or member not found") from exc
    except ArtifactCorruptError as exc:
        raise V1ApiError(409, "artifact_invalid", str(exc)) from exc
    except ArtifactError as exc:
        raise V1ApiError(400, "invalid_request", str(exc)) from exc
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"X-SBX-Artifact-Id": artifact_id},
    )


# ------------------------------------------------------------------- runs


@router.post("/agents/{agent_id}/runs", status_code=201)
def create_run(
    agent_id: str,
    body: CreateRunRequest,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: RunFailureReporter = Depends(get_run_reporter),
    workflows: WorkflowService = Depends(get_workflow_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    _require_agent(plane, agent_id)
    contract = _normalize_contract(body.output_contract)
    if contract is not None and _ledger(plane) is None:
        # Contracted runs need the durable ledger for both dispatch and the
        # persisted verdict — refuse rather than run uncontracted.
        raise V1ApiError(409, "session_not_runnable", "output contracts require the run ledger")
    # SOR-224: run create is a side-effect mutation — it dedups on
    # ``Idempotency-Key`` exactly like agent/task create: same key + same
    # body replays the original run; same key + different body conflicts.
    # The durable pin lives on the run's own ledger record so a replay
    # after a control-plane restart still resolves.
    pin_key = f"run:{agent_id}:{idempotency_key}" if idempotency_key else None
    fingerprint = request_fingerprint(body)
    owned = None
    pin = None
    if idempotency_key:
        outcome, entry = v1.idempotency.claim(key.id, pin_key or "", fingerprint)
        if outcome == "hit":
            return entry.body
        if outcome == "conflict":
            raise V1ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different request body",
            )
        if outcome == "timeout":
            raise V1ApiError(
                409,
                "idempotency_in_progress",
                "a create with this Idempotency-Key is still in progress",
            )
        ledger = _ledger(plane)
        if ledger is not None:
            prior = ledger.find_by_idempotency(agent_id, key.id, pin_key or "")
            if prior is not None:
                prior_fp = (prior.idempotency or {}).get("fingerprint")
                if prior_fp not in (None, fingerprint):
                    v1.idempotency.abandon(key.id, pin_key or "", entry)
                    raise V1ApiError(
                        409,
                        "idempotency_conflict",
                        "Idempotency-Key was already used with a different request body",
                    )
                rec = _require_agent(plane, agent_id)
                replay = _run_public(
                    plane,
                    plane.public(rec),
                    rec,
                    prior.n,
                    v1.cancelled(agent_id),
                    _meta_for(v1, rec),
                    run_states,
                    scheduler=scheduler,
                    reporter=reporter,
                )
                v1.idempotency.complete(
                    key.id, pin_key or "", entry, agent_id=agent_id, body=replay
                )
                v1.idempotency.settle(key.id, pin_key or "", entry)
                return replay
        owned = entry
        pin = {"key_id": key.id, "key": pin_key, "fingerprint": fingerprint}
    try:
        turn_id = plane.post_message(
            agent_id,
            body.prompt.text,
            output_contract=contract,
            queue=body.on_busy == "queue",
            idempotency=pin,
        )
    except KeyError:
        if owned is not None:
            v1.idempotency.abandon(key.id, pin_key or "", owned)
        raise not_found("agent not found") from None
    except SessionConflict as exc:
        if owned is not None:
            v1.idempotency.abandon(key.id, pin_key or "", owned)
        raise V1ApiError(
            exc.code,
            exc.error,
            exc.error,
            # ``turn_in_progress`` under ``on_busy=reject`` carries the turn
            # bound as a retry hint — the running turn ends within it.
            retry_after=(plane.turn_max_seconds if exc.error == "turn_in_progress" else None),
        ) from exc
    if body.metadata is not None:
        # SOR-84: a follow-up may re-bind the agent's workflow task; the
        # run is already queued, so a refused message never re-binds.
        workflows.attach(owner=key.id, agent_id=agent_id, metadata=body.metadata)
    n = _turn_n(turn_id) or 0
    # The ledger already holds the record (RUNNING when dispatched at once,
    # QUEUED when the agent is busy); begin() mirrors its status so a
    # ledger-less run-state seam reports the same truth.
    ledger = _ledger(plane)
    born = ledger.get(agent_id, n) if ledger is not None else None
    run_states.begin(
        agent_id,
        n,
        prompt=body.prompt.text,
        status=born.status if born is not None and born.status != "UNKNOWN" else "RUNNING",
    )
    rec = _require_agent(plane, agent_id)
    pub = plane.public(rec)
    result = _run_public(
        plane,
        pub,
        rec,
        n,
        v1.cancelled(agent_id),
        _meta_for(v1, rec),
        run_states,
        scheduler=scheduler,
        reporter=reporter,
    )
    if owned is not None:
        v1.idempotency.complete(key.id, pin_key or "", owned, agent_id=agent_id, body=result)
        v1.idempotency.settle(key.id, pin_key or "", owned)
    return result


@router.get("/agents/{agent_id}/runs")
def list_runs(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: RunFailureReporter = Depends(get_run_reporter),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    return {"runs": _runs(plane, rec, v1, run_states, scheduler=scheduler, reporter=reporter)}


@router.get("/agents/{agent_id}/runs/{run_id}")
def get_run(
    agent_id: str,
    run_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: RunFailureReporter = Depends(get_run_reporter),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    return _require_run(plane, rec, run_id, v1, run_states, scheduler=scheduler, reporter=reporter)


@router.post("/agents/{agent_id}/runs/{run_id}/cancel")
def cancel_run(
    agent_id: str,
    run_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
    scheduler: Scheduler = Depends(get_scheduler),
    reporter: RunFailureReporter = Depends(get_run_reporter),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    n = _run_n(run_id)
    if n is None or n not in _known_run_ns(rec, _ledger(plane), run_states):
        raise not_found("run not found")
    state = run_states.get(agent_id, n)
    if state is not None and state.status == "QUEUED":
        # SOR-224: a queued run was never dispatched — cancelling it just
        # parks the terminal verdict; the session record keeps the message
        # as audit and the drain skips terminal records.
        run_states.transition(agent_id, n, "CANCELLED")
        v1.mark_cancelled(agent_id, n)
        rec = _require_agent(plane, agent_id)
        if rec.current_turn_n == n and rec.status == "running":
            # The drain claimed the run between our status read and the
            # transition — stop the turn so a cancelled run runs no
            # billed work.
            try:
                plane.stop(agent_id)
            except Exception:
                pass
            rec = _require_agent(plane, agent_id)
    elif state is not None and state.status == "CREATING":
        # Pre-dispatch run-1 (SOR-82 A2): drop the queued turn so the worker
        # skips it, and persist CANCELLED — terminal, never resurrected.
        plane.discard_queued_first_turn(agent_id)
        run_states.transition(agent_id, n, "CANCELLED")
        v1.mark_cancelled(agent_id, n)
        rec = _require_agent(plane, agent_id)
        if rec.current_turn_n == n and rec.status == "running":
            # The worker dispatched between our read and the transition:
            # the turn just started — stop it so a cancelled run does not
            # keep executing billed work.
            try:
                plane.stop(agent_id)
            except Exception:
                pass
            rec = _require_agent(plane, agent_id)
    elif rec.current_turn_n == n and rec.status == "running":
        try:
            plane.stop(agent_id)
        except KeyError:
            raise not_found("agent not found") from None
        v1.mark_cancelled(agent_id, n)
        run_states.transition(agent_id, n, "CANCELLED")
        rec = _require_agent(plane, agent_id)
    try:
        # SOR-224: freeing the slot (or cancelling the queue head) may let
        # the next durable QUEUED run dispatch.
        plane.drain_queued(agent_id)
    except Exception:
        pass
    return _require_run(plane, rec, run_id, v1, run_states, scheduler=scheduler, reporter=reporter)


# ------------------------------------------------------------------- SSE


def _belongs_to_run(obj: dict[str, Any], run_n: int, current_turn: int) -> tuple[bool, int]:
    """Track turn boundaries via ``sbx.turn_started``; report membership.

    Lines preceding the first ``sbx.turn_started`` (e.g. ``sbx.session_meta``)
    are attributed to run 1. ``id`` keeps the absolute events.jsonl line number
    so ``Last-Event-ID`` resume stays consistent with ``/api/*`` semantics.
    """
    if obj.get("type") == "sbx.turn_started":
        try:
            current_turn = int(obj.get("n") or 0)
        except (TypeError, ValueError):
            pass
    return (current_turn == run_n or (run_n == 1 and current_turn == 0)), current_turn


def _parse_event_line(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
        return {"type": "error", "message": raw}
    except json.JSONDecodeError:
        return {"type": "error", "message": "bad json in event stream"}


@router.get("/agents/{agent_id}/runs/{run_id}/stream")
async def stream_run(
    request: Request,
    agent_id: str,
    run_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
    run_states: RunStateStore = Depends(get_run_states),
) -> Any:
    from control.app import DisconnectAwareStreamingResponse

    # SOR-271 round 2: every remote call in this handler and in the
    # generator below runs via ``asyncio.to_thread`` — a synchronous Dict
    # get/poll on the ASGI event loop stalls every other in-flight
    # request while it blocks (read starvation under SSE fanout).
    rec = await asyncio.to_thread(_require_agent, plane, agent_id)
    n = _run_n(run_id)
    ledger = _ledger(plane)
    if n is None or n not in await asyncio.to_thread(_known_run_ns, rec, ledger, run_states):
        raise not_found("run not found")
    try:
        last_id = int(last_event_id) if last_event_id else 0
    except ValueError:
        last_id = 0
    start_line = max(1, last_id + 1)

    backend = getattr(plane, "backend", None)
    keepalive_s: float = getattr(request.app.state, "keepalive_s", 15.0)

    def _live_handle() -> tuple[Any, Any]:
        """Current sandbox handle + poll, re-fetched so a CREATING run can
        attach once the background provisioner binds the sandbox (SOR-82 A2)."""
        if backend is None:
            return None, None
        rec_now = plane.get(agent_id)
        if rec_now is None:
            return None, None
        h = rec_now.handle()
        if h is None:
            return None, None
        try:
            p = backend.poll(h)
        except Exception:
            p = None
        return h, p

    async def gen() -> AsyncIterator[str]:
        proc: Any = None
        current_turn = 0
        try:
            yield ": keepalive\n\n"
            handle, poll = await asyncio.to_thread(_live_handle)
            next_ka = time.monotonic() + keepalive_s
            # Wait out the CREATING window: the sandbox appears once the
            # background worker binds it; a terminal run/session exits to the
            # replay path below.
            while handle is None or poll is None or not poll.alive:
                state = await asyncio.to_thread(run_states.get, agent_id, n)
                if state is not None and state.status in RUN_TERMINAL:
                    break
                rec_now = await asyncio.to_thread(plane.get, agent_id)
                if rec_now is None or rec_now.status in ("closed", "timed_out", "lost"):
                    break
                now = time.monotonic()
                if now >= next_ka:
                    yield ": keepalive\n\n"
                    next_ka = now + keepalive_s
                await asyncio.sleep(0.05)
                handle, poll = await asyncio.to_thread(_live_handle)
            if backend is not None and handle is not None and poll is not None and poll.alive:
                proc = await asyncio.to_thread(
                    backend.exec,
                    handle,
                    ["tail", "-n", "+1", "-F", str(handle.root / "events.jsonl")],
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

                threading.Thread(target=_reader, daemon=True, name="sbx-v1-sse-tail").start()

                lineno = 0
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
                    # Contract: id is the events.jsonl 1-based line number —
                    # a blank/torn line still consumes one (``/api/*`` parity).
                    lineno += 1
                    if not raw.strip():
                        continue
                    obj = _parse_event_line(raw)
                    emit, current_turn = _belongs_to_run(obj, n, current_turn)
                    if emit and lineno >= start_line:
                        yield format_sse(lineno, obj)
                    now = time.monotonic()
                    if now >= next_ka:
                        yield ": keepalive\n\n"
                        next_ka = now + keepalive_s
                return

            # Sandbox unreachable: replay the run's slice if the file is
            # locally readable, then keep the stream open like /api/* does.
            lines: list[str] = []
            if backend is not None and handle is not None:
                try:
                    text = await asyncio.to_thread(read_text, backend, handle, "events.jsonl")
                except Exception:
                    text = None
                if text:
                    lines = text.splitlines()
            lineno = 0
            for raw in lines:
                lineno += 1
                if not raw.strip():
                    continue
                obj = _parse_event_line(raw)
                emit, current_turn = _belongs_to_run(obj, n, current_turn)
                if emit and lineno >= start_line:
                    yield format_sse(lineno, obj)
            if not lines:
                # Sandbox gone: fall back to the transcript captured at turn end.
                activity = getattr(request.app.state, "run_activity", None)
                try:
                    entries = (
                        await asyncio.to_thread(activity.get, agent_id, n)
                        if activity is not None
                        else None
                    )
                except Exception:
                    entries = None
                for entry in entries or ():
                    if entry["id"] >= start_line:
                        yield format_sse(entry["id"], entry["event"])
            while True:
                await asyncio.sleep(keepalive_s)
                yield ": keepalive\n\n"
        finally:
            if proc is not None:
                try:
                    await asyncio.to_thread(proc.kill)
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


# ------------------------------------------------------------------ misc


@router.get("/agents/{agent_id}/usage")
def get_agent_usage(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    pub = plane.public(rec)
    return {
        # None (never measured) serializes as null — unavailable, not
        # fabricated zeros (SOR-84).
        "usage": usage_public(rec.usage),
        "cost_estimate_usd": pub.get("cost_estimate_usd", 0.0),
        "sandbox_seconds": pub.get("sandbox_seconds", 0.0),
    }


def _capability_rows(registry: AccountRegistry, capabilities: Any) -> list[dict[str, Any]]:
    """One row per (account, model) from the capability catalog.

    SOR-204: each row carries the full capability surface — display name,
    family, aliases, canonical efforts with the provider-native map,
    availability, discovery provenance (``source``/``refreshed_at``/
    ``stale``) — plus ``accounts_available`` for compatibility.
    """
    enabled = frozenset(selected_providers())
    rows: list[dict[str, Any]] = []
    for account in registry.list():
        # Durable registries can retain accounts from an earlier deployment
        # with a wider provider set. Never advertise a provider whose image
        # and credential mounts are intentionally absent from this deploy.
        if account.provider not in enabled:
            continue
        snapshot = _snapshot_for(capabilities, account)
        if snapshot is None:
            continue
        active = account.status == "active"
        free = active and _running_or_zero(registry, account.id) < account.max_concurrent
        availability = "available" if free else ("busy" if active else "unavailable")
        for m in snapshot.models:
            rows.append(
                {
                    "provider": account.provider,
                    "account": account.id,
                    "model": m.model,
                    "display_name": m.display_name,
                    "family": m.family,
                    "aliases": list(m.aliases),
                    "reasoning_efforts": list(m.reasoning_efforts),
                    "effort_native": dict(m.effort_native),
                    "default_effort": m.default_effort,
                    "availability": availability,
                    "accounts_available": 0,
                    "source": snapshot.source,
                    "refreshed_at": snapshot.refreshed_at,
                    "stale": snapshot.stale,
                }
            )
    free_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        if row["availability"] == "available":
            key_ = (row["provider"], row["model"])
            free_counts[key_] = free_counts.get(key_, 0) + 1
    for row in rows:
        row["accounts_available"] = free_counts.get((row["provider"], row["model"]), 0)
    rows.sort(key=lambda r: (r["provider"], r["model"], r["account"]))
    return rows


@router.get("/models")
def list_models(
    key: ApiKey = Depends(agents_key),
    registry: AccountRegistry = Depends(get_registry),
    capabilities: Any = Depends(get_capabilities),
) -> dict[str, Any]:
    """Advertised models come from the capability catalog: live
    CLI discovery per account with TTL + stale-last-good, falling back to
    declared/env/static models until the first probe lands."""
    return {"models": _capability_rows(registry, capabilities)}


# ------------------------------------------------------------- providers


def _runtime_record_for(runtime: Any, provider: str) -> dict[str, Any] | None:
    """Deploy-written runtime record for ``provider``; tolerant of stores.

    An absent/unreadable/evolving store must degrade a provider to
    ``unknown`` readiness — never 500 the listing or fabricate ``ready``.
    """
    getter = getattr(runtime, "get", None)
    if not callable(getter):
        return None
    try:
        record = getter(provider)
    except Exception:
        return None
    if record is None:
        return None
    to_dict = getattr(record, "to_dict", None)
    data = to_dict() if callable(to_dict) else record
    return data if isinstance(data, dict) else None


def _image_name_for(provider: str, default: str) -> str:
    """Configured Modal image name for ``provider`` (``SBX_IMAGE_*``)."""
    return env_str(f"SBX_IMAGE_{provider.upper()}", default)


def _connection_detail(accounts: list[Account], schedulable: int, status: str) -> str:
    """Why a provider's connection is not serving work (SOR-258).

    The blocker names *what kind* of problem it is — capacity (busy),
    credential lifecycle (needs login / verify / re-enable), or a
    transient refresh lane (cooling) — so a ``degraded`` connection never
    silently folds all of them together.
    """
    if status == "connected":
        return ""
    if status == "not_connected":
        return "no accounts registered"
    if any(account.status == "active" for account in accounts):
        return "every active account is at capacity"
    if any(account.status == "cooling" for account in accounts):
        return "accounts are cooling (credential refresh pending)"
    if all(account.status == "disabled" for account in accounts):
        return "all accounts disabled"
    return "accounts need login or verification"


def _provider_readiness(
    *,
    enabled: bool,
    runtime_status: str,
    accounts: list[Account],
    schedulable: int,
) -> str:
    """Fold runtime + connection into the normalized readiness vocabulary.

    ``disabled`` is strictly the deployment-time gate (provider not in
    ``SBX_PROVIDERS``) — never inferred from login or capacity state.
    ``unhealthy`` is runtime evidence failing (``degraded``) or accounts
    cooling. ``needs_login`` is a credential/verification problem;
    ``busy`` is verified capacity fully consumed — the two never share a
    state (SOR-258).
    """
    if not enabled:
        return "disabled"
    if runtime_status == "degraded":
        return "unhealthy"
    if schedulable:
        return "ready"
    if not accounts:
        return "needs_login"
    if any(account.status == "active" for account in accounts):
        return "busy"
    if any(account.status == "cooling" for account in accounts):
        return "unhealthy"
    if all(account.status == "disabled" for account in accounts):
        return "disabled"
    return "needs_login"


def _provider_rows(registry: AccountRegistry, runtime: Any) -> list[dict[str, Any]]:
    """The three-way split (SOR-212/SOR-215): catalog / runtime / connection.

    **Catalog** — every contract provider appears, selected or not, with
    its ``ProviderRuntimeSpec`` truth (support tier, distribution lane,
    in-image CLI path, credential relpaths, default models). Nothing here
    infers ``connected`` from deployment config: a zero-account plane still
    reports every supported provider ``available`` + ``not_connected``.

    **Runtime** — deploy-written evidence: ``ready``/``degraded`` records
    from ``sbx deploy``; ``unknown`` when the deployment predates the
    record (or the store is unreadable); ``disabled`` when the provider is
    not selected by this deployment's ``SBX_PROVIDERS``.

    **Connection** — live account state only: ``not_connected`` when no
    account exists, ``connected`` when at least one is schedulable,
    ``degraded`` when accounts exist but none can take work.

    **Readiness** (SOR-258) — the normalized single answer
    (``ready``/``needs_login``/``busy``/``disabled``/``unhealthy``) folded
    from the layers above, with the dominant blocker in
    ``connection.detail`` when the provider cannot serve.
    """
    from runtime.provider_runtime import provider_runtime_specs

    enabled = frozenset(selected_providers())
    accounts_by_provider: dict[str, list[Account]] = {}
    for account in registry.list():
        accounts_by_provider.setdefault(account.provider, []).append(account)
    rows: list[dict[str, Any]] = []
    for rspec in provider_runtime_specs():
        accounts = accounts_by_provider.get(rspec.provider, [])
        schedulable = sum(
            1
            for account in accounts
            if account.status == "active"
            and _running_or_zero(registry, account.id) < account.max_concurrent
        )
        if not accounts:
            connection_status = "not_connected"
        elif schedulable:
            connection_status = "connected"
        else:
            connection_status = "degraded"
        is_enabled = rspec.provider in enabled
        record = _runtime_record_for(runtime, rspec.provider) if is_enabled else None
        if not is_enabled:
            runtime_status = "disabled"
            runtime_detail = "not selected by this deployment (SBX_PROVIDERS)"
            image = _image_name_for(rspec.provider, rspec.image_name)
            version = None
            updated_at = None
        elif record is not None:
            runtime_status = str(record.get("status") or "unknown")
            runtime_detail = str(record.get("detail") or "")
            image = str(record.get("image") or "") or _image_name_for(
                rspec.provider, rspec.image_name
            )
            version = record.get("version")
            updated_at = record.get("updated_at")
        else:
            runtime_status = "unknown"
            runtime_detail = "enabled but no deploy evidence recorded yet"
            image = _image_name_for(rspec.provider, rspec.image_name)
            version = None
            updated_at = None
        rows.append(
            {
                "provider": rspec.provider,
                "support": rspec.support,
                "status": "available",
                "readiness": _provider_readiness(
                    enabled=is_enabled,
                    runtime_status=runtime_status,
                    accounts=accounts,
                    schedulable=schedulable,
                ),
                "distribution": {
                    "kind": rspec.install_kind,
                    "local_assisted": rspec.local_assisted,
                },
                "cli": rspec.cli,
                "cli_path": rspec.cli_path,
                "credential_files": list(rspec.credential_files),
                "optional_credential_files": list(rspec.optional_credential_files),
                "default_models": list(rspec.default_models),
                "runtime": {
                    "enabled": is_enabled,
                    "image": image,
                    "status": runtime_status,
                    "version": version,
                    "detail": runtime_detail,
                    "updated_at": updated_at,
                },
                "connection": {
                    "status": connection_status,
                    "accounts_total": len(accounts),
                    "accounts_available": schedulable,
                    "detail": _connection_detail(accounts, schedulable, connection_status),
                },
                "summary": rspec.summary,
            }
        )
    return rows


@router.get("/providers")
def list_providers(
    key: ApiKey = Depends(agents_key),
    registry: AccountRegistry = Depends(get_registry),
    runtime: Any = Depends(get_runtime_store),
) -> dict[str, Any]:
    """Provider catalog + runtime readiness + account connection.

    Lists every supported provider regardless of deployment selection or
    account presence — ``connected`` is never inferred from config, and
    zero accounts still surfaces the full catalog as ``not_connected``.
    """
    return {"providers": _provider_rows(registry, runtime)}


@router.post("/models/refresh")
def refresh_models(
    provider: str | None = None,
    account_id: str | None = None,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
    capabilities: Any = Depends(get_capabilities),
) -> dict[str, Any]:
    """Synchronous capability refresh.

    Re-probes the matching accounts' provider CLIs and returns the updated
    catalog rows. Failed probes keep serving last-good data marked
    ``stale`` — refresh never empties the catalog.
    """
    if provider is not None and provider not in PROVIDER_DEFAULT_MODELS:
        raise V1ApiError(400, "invalid_provider", f"unknown provider {provider!r}")
    refreshed = 0
    for account in registry.list(provider):
        if account_id is not None and account.id != account_id:
            continue
        capabilities.refresh(account, registry.get_credential_blob(account.id))
        refreshed += 1
    return {"refreshed": refreshed, "models": _capability_rows(registry, capabilities)}


@router.get("/me")
def get_me(key: ApiKey = Depends(api_key)) -> dict[str, Any]:
    return {"key_id": key.id, "label": key.label, "scopes": list(key.scopes)}


# --------------------------------------------------------------- accounts


@router.get("/accounts")
def list_accounts(
    provider: ProviderId | None = None,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return {"accounts": [_account_view(registry, account) for account in registry.list(provider)]}


def _materialize_account_secret(account: Account, blob: dict[str, Any]) -> None:
    """Mirror the stored credential blob into the managed Modal Secret.

    ``<account_secret_prefix><id>`` is the mount lane agent sandboxes read;
    without it a verified account's credential never reaches its turns.
    Best-effort like the onboarding/relink path — the registry blob stays
    authoritative and a missed refresh is retried by write-back.
    """
    if env_str("SBX_BACKEND", "local") != "modal" or not account.secret_name:
        return
    try:
        from control.credsync import CREDENTIAL_ENV, ModalCredentialSecretWriter

        ModalCredentialSecretWriter().refresh(
            account.secret_name,
            {CREDENTIAL_ENV: json.dumps(blob, ensure_ascii=False, separators=(",", ":"))},
        )
    except Exception:
        pass


@router.post("/accounts", status_code=201)
def create_account(
    body: CreateAccountRequest,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    account_id = f"acct-{body.provider}-{uuid.uuid4().hex[:8]}"
    account = Account(
        id=account_id,
        provider=body.provider,
        label=body.label,
        # Verified-only lifecycle (SOR-216): a created account is never
        # scheduler-eligible until the verify probe proves it.
        status="unverified",
        max_concurrent=body.max_concurrent,
        models=tuple(body.models),
        # Managed credential lane (SOR-213): an account carrying a
        # credential claims the conventional ``<prefix><id>`` Secret name
        # up front — that name is what mounts the blob into sandboxes.
        secret_name=(
            f"{account_secret_prefix()}{account_id}" if body.credential is not None else ""
        ),
        created_at=_iso_now(),
    )
    files: Any = None
    if body.credential is not None:
        files = body.credential.get("files")
        if files is not None and (
            not isinstance(files, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in files.items())
        ):
            # Validate before any write: a refused create must not leave a
            # credential-less account dangling.
            raise V1ApiError(400, "invalid_request", "credential.files must be a string map")
    registry.put(account)
    if body.credential is not None:
        blob = {"provider": body.provider, "files": dict(files or {})}
        registry.put_credential_blob(account.id, blob)
        try:
            CredentialLifecycleService(registry).note_credential(account.id, blob)
        except Exception:
            pass
        _materialize_account_secret(account, blob)
    return _account_view(registry, account)


@router.get("/accounts/{account_id}")
def get_account(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    account = _registry_account(registry, account_id)
    return _account_view(registry, account)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> Response:
    _registry_account(registry, account_id)
    registry.remove(account_id)
    return Response(status_code=204)


@router.post("/accounts/{account_id}/verify")
def verify_account(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Probe the stored credential in a throwaway sandbox.

    Runs ``runner init --provider <account.provider>`` with the account's
    credential attached: the named Modal Secret when ``secret_name`` is set,
    else the local registry blob via ``SBX_ACCOUNT_CREDENTIAL`` /
    ``SBX_ACCOUNT_ID`` (restored under ``$SBX_WORK/home``). A non-zero init
    marks the account ``invalid``. When the plane exposes no usable backend
    the probe cannot run — the verified-only lifecycle keeps the
    current status and only records ``probe_unavailable``; the account is
    never promoted without evidence.
    """
    account = _registry_account(registry, account_id)
    model = (
        _default_model(account.provider, account)
        or getattr(plane, "default_model", None)
        or "gpt-5.6-luna"
    )
    updated = probe_account_credential(plane, registry, account, model=model)
    return _account_view(registry, updated)


@router.get("/accounts/{account_id}/lifecycle")
def account_credential_lifecycle(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Non-secret credential lifecycle metadata.

    States: ``healthy`` / ``access_expiring`` / ``refreshing`` /
    ``healthy_refreshed`` / ``reauth_required`` / ``revoked``.
    """
    account = _registry_account(registry, account_id)
    return {
        "account_id": account.id,
        "credential_lifecycle": CredentialLifecycleService(registry).describe(account_id),
    }


@router.post("/accounts/{account_id}/lifecycle/refresh")
def refresh_account_credential(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Run one synchronous credential refresh through the worker path.

    Reuses the app's background refresher when the plane has one; otherwise
    builds an ad-hoc refresher over the plane's backend. Returns the refresh
    outcome plus the resulting lifecycle metadata — never token material.
    """
    _registry_account(registry, account_id)
    refresher = getattr(plane, "credential_refresher", None)
    backend = getattr(plane, "backend", None)
    if refresher is None:
        if backend is None:
            raise V1ApiError(503, "unavailable", "no backend for credential refresh")
        sync = getattr(plane, "credential_sync", None) or CredentialSync(lambda: registry)
        refresher = CredentialRefresher(
            registry_source=lambda: registry,
            backend=backend,
            runner_cmd=list(getattr(plane, "runner_cmd", []) or []),
            sync=sync,
            lifecycle=CredentialLifecycleService(registry),
            default_model=getattr(plane, "default_model", None) or "gpt-5.6-luna",
        )
    try:
        return refresher.refresh_account(account_id)
    except Exception:
        raise V1ApiError(500, "internal", "credential refresh failed") from None


# -------------------------------------------------------------- api keys


@router.get("/api-keys")
def list_api_keys(
    key: ApiKey = Depends(admin_key),
    store: ApiKeyStore = Depends(get_key_store),
) -> dict[str, Any]:
    return {"api_keys": [api_key_public(k) for k in store.list()]}


@router.post("/api-keys", status_code=201)
def create_api_key(
    body: CreateApiKeyRequest | None = None,
    key: ApiKey = Depends(admin_key),
    store: ApiKeyStore = Depends(get_key_store),
) -> dict[str, Any]:
    body = body or CreateApiKeyRequest()
    scopes = body.scopes if body.scopes is not None else ["agents"]
    if any(scope not in VALID_SCOPES for scope in scopes):
        raise V1ApiError(400, "invalid_scope", f"unknown scope; allowed: {list(VALID_SCOPES)}")
    record, token = store.create(label=body.label, scopes=scopes)
    return {**api_key_public(record), "key": token}


@router.delete("/api-keys/{key_id}", status_code=204)
def delete_api_key(
    key_id: str,
    key: ApiKey = Depends(admin_key),
    store: ApiKeyStore = Depends(get_key_store),
) -> Response:
    if not store.revoke(key_id):
        raise not_found("api key not found")
    return Response(status_code=204)


# -------------------------------------------------------------- console grants
# SOR-211: the browser-admin handoff. `sbx open` mints a single-use, short-TTL
# ticket with the caller's admin key; the ticket rides the URL fragment
# (never sent to the server) and the Console redeems it for a fresh `sbx_`
# key. The long-lived bootstrap key never appears in a URL.


@router.post("/console/grant", status_code=201)
def create_console_grant(
    key: ApiKey = Depends(admin_key),
    v1: V1State = Depends(get_v1_state),
) -> dict[str, Any]:
    ttl = env_int("SBX_CONSOLE_GRANT_TTL_S", CONSOLE_GRANT_TTL_S)
    ticket, expires_at = v1.console_grants.create(ttl)
    return {"grant": ticket, "expires_in": ttl, "expires_at": expires_at}


@router.post("/console/exchange", status_code=201)
def exchange_console_grant(
    body: ConsoleGrantExchangeRequest,
    v1: V1State = Depends(get_v1_state),
    store: ApiKeyStore = Depends(get_key_store),
) -> dict[str, Any]:
    """Redeem a one-time grant ticket for a minted admin+agents key.

    Deliberately unauthenticated — the ticket itself is the credential —
    but single-use and short-lived, so an expired or replayed ticket is a
    clean 401, not a leaked key.
    """
    if not v1.console_grants.consume(body.grant):
        raise V1ApiError(401, "grant_invalid", "grant is invalid, expired, or already used")
    record, token = store.create(label="console handoff", scopes=("agents", "admin"))
    return {**api_key_public(record), "key": token}


# ---------------------------------------------------------------------------
# GitHub App authorization (SOR-177)
# ---------------------------------------------------------------------------


@router.get("/github/app")
def github_app_status(
    key: ApiKey = Depends(agents_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """Authorization posture: app configured?, installations, bridge fallback."""
    try:
        return app.status()
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc


@router.post("/github/app/authorize", status_code=201)
def github_app_begin_authorize(
    key: ApiKey = Depends(agents_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """One-click connect, step 1: return the GitHub install URL to open."""
    try:
        return app.begin_authorization()
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc


@router.post("/github/app/authorize/callback")
def github_app_authorize_callback(
    body: GitHubAppAuthorizeCallbackRequest,
    key: ApiKey = Depends(agents_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """Step 2: record an installation selected in the browser.

    ``{"installation_id": <int>, "state": "<from authorize>"}`` — the
    single-use ``state`` from step 1 is the callback's credential: the
    browser redirect itself carries no Authorization header, so it cannot
    prove the caller holds an API key; possession of the state can.
    """
    try:
        record = app.complete_authorization(body.installation_id, body.state)
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc
    return {"installation": record.public()}


@router.post("/github/app/sync")
def github_app_sync(
    key: ApiKey = Depends(admin_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """Refresh installation metadata from GitHub (drops deleted installs)."""
    try:
        return {"installations": [r.public() for r in app.sync()]}
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc


@router.delete("/github/app/installations/{installation_id}")
def github_app_revoke(
    installation_id: int,
    key: ApiKey = Depends(admin_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """Revoke one installation: best-effort delete on GitHub, always forgets
    the local record and any cached tokens. Re-run authorize to reconnect."""
    try:
        return app.revoke(installation_id)
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc


def _github_app_error(exc: GitHubAppError) -> V1ApiError:
    return V1ApiError(exc.status_code, exc.code, exc.message)


# ---------------------------------------------------------------------------
# SOR-220 default connect — brokered public GitHub App installation
# ---------------------------------------------------------------------------


@router.post("/github/install", status_code=201)
def github_install_begin(
    request: Request,
    key: ApiKey = Depends(agents_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """Default Connect GitHub, step 1 — for UI and CLI alike.

    Returns ``{authorize_url, mode, ...}``. Broker mode (the default when no
    deployment-local App exists) resolves to the official
    ``github.com/apps/<public-sbx-app>/installations/new`` page via the
    hosted Sorenforge broker — the signed state binds the flow to this
    deployment's own ``/v1/github/install/callback``. ``app`` mode (local
    App configured) keeps the authorize flow. The first GitHub page
    the user sees is always the App *installation* page — never
    ``settings/apps/new``.
    """
    from control.github_app import public_request_origin

    try:
        # The broker binds the flow to this deployment's *trusted public
        # origin* — SBX_PUBLIC_ORIGIN or proxy forwarded headers — never a
        # raw http request URL behind a TLS terminator.
        origin = public_request_origin(request)
        callback_path = request.url_for("github_install_callback").path
        redirect = (
            f"{origin}{callback_path}"
            if origin
            else str(request.url_for("github_install_callback"))
        )
        return app.begin_install(redirect_uri=redirect)
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc


@router.get("/github/install/callback", name="github_install_callback")
def github_install_callback(
    code: str = "",
    app: Any = Depends(get_github_app),
) -> Response:
    """Broker-mode step 2: the browser redirect target the broker 302s to
    after the GitHub install. Unauthenticated by design — the one-time
    ``code`` is the credential (single-use, short TTL, fail closed). Exits
    303 to the Console GitHub view with ``broker=connected|broker_error``."""
    try:
        app.complete_broker(code)
    except GitHubAppError as exc:
        return RedirectResponse(f"/#/admin/github?broker_error={exc.code}", status_code=303)
    return RedirectResponse("/#/admin/github?broker=connected", status_code=303)


@router.post("/github/app/manifest", status_code=201)
def github_app_begin_manifest(
    request: Request,
    body: GitHubAppManifestRequest,
    key: ApiKey = Depends(admin_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """Zero-config registration, step 1 — the App manifest + the URL to POST it.

    The Console auto-submits ``manifest`` to ``manifest_url`` as a form
    post — GitHub's supported App Manifest registration flow — and GitHub
    redirects the browser back to ``/v1/github/app/manifest/callback``
    with the conversion ``code``. The pending ``state`` is the
    single-use capability binding the two steps.
    """
    try:
        redirect = str(request.url_for("github_app_manifest_callback"))
        return app.begin_manifest(redirect_url=redirect, name=body.name, org=body.org)
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc


@router.get("/github/app/manifest/callback", name="github_app_manifest_callback")
def github_app_manifest_callback(
    code: str = "",
    state: str = "",
    app: Any = Depends(get_github_app),
) -> Response:
    """Zero-config registration, step 2 (browser redirect target): exchange
    ``code`` and register the deployment-scoped App, then bounce the browser back to
    the Console GitHub view — unauthenticated by design (the one-time
    ``state`` issued by step 1 is the credential)."""
    try:
        app.complete_manifest(code, state)
    except GitHubAppError as exc:
        return RedirectResponse(f"/#/admin/github?manifest_error={exc.code}", status_code=303)
    return RedirectResponse("/#/admin/github?manifest=connected", status_code=303)


@router.post("/github/app/manifest/complete")
def github_app_manifest_complete(
    body: GitHubAppManifestCompleteRequest,
    key: ApiKey = Depends(admin_key),
    app: Any = Depends(get_github_app),
) -> dict[str, Any]:
    """Programmatic completion of the manifest flow (``{"code", "state"}``)
    for API clients that collect the redirect themselves."""
    try:
        return app.complete_manifest(body.code, body.state)
    except GitHubAppError as exc:
        raise _github_app_error(exc) from exc


# ---------------------------------------------------------------------------
# Provider Connect (SOR-214) — canonical auth sessions over provider logins


def _connect_error(exc: ConnectError) -> V1ApiError:
    return V1ApiError(exc.status_code, exc.code, exc.message)


def _plane_verify_fn(plane: Any, registry: Any) -> Any:
    """A ``verify(account_id) -> bool`` closure over the /v1 probe — the
    same ``runner init`` sandbox check ``POST /accounts/{id}/verify`` runs."""

    def _model_for(account: Any) -> str | None:
        return _default_model(account.provider, account)

    return plane_verify(plane, registry, model_for=_model_for)


@router.get("/auth")
def auth_sessions(
    key: ApiKey = Depends(admin_key),
    connect: Any = Depends(get_provider_connect),
) -> dict[str, Any]:
    """Connect sessions + the canonical auth-session state vocabulary."""
    return {
        "auth_states": list(AUTH_SESSION_STATES),
        "connect_states": list(CONNECT_STATES),
        "sessions": connect.list(),
    }


@router.post("/auth/connect", status_code=201)
def auth_connect(
    body: AuthConnectRequest,
    key: ApiKey = Depends(admin_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
    connect: Any = Depends(get_provider_connect),
) -> dict[str, Any]:
    """Begin a connect session for ``provider`` — hosted lane when the
    control plane can exec the provider's login itself (the session's
    ``browser_url``/``user_code`` surface device-flow details live), or
    the pair lane (``pair_command`` / ``pair_ticket``) on cloud deploys
    where it cannot. ``account_id`` relinks an existing account."""
    try:
        return connect.begin(
            body.provider,
            label=body.label,
            slots=body.max_concurrent,
            models=body.models,
            account_id=body.account_id,
            verify=_plane_verify_fn(plane, registry),
        )
    except ConnectError as exc:
        raise _connect_error(exc) from exc
    except OnboardingError as exc:
        raise V1ApiError(400, exc.code, str(exc)) from exc


@router.get("/auth/connect/{session_id}")
def auth_connect_session(
    session_id: str,
    key: ApiKey = Depends(admin_key),
    connect: Any = Depends(get_provider_connect),
) -> dict[str, Any]:
    """Connect session detail — state, browser/device URLs, error."""
    try:
        return connect.get(session_id)
    except ConnectError as exc:
        raise _connect_error(exc) from exc


@router.post("/auth/connect/{session_id}/cancel")
def auth_connect_cancel(
    session_id: str,
    key: ApiKey = Depends(admin_key),
    connect: Any = Depends(get_provider_connect),
) -> dict[str, Any]:
    """Cancel an in-flight session (terminates the hosted login, voids the
    pair ticket); idempotent for terminal sessions."""
    try:
        return connect.cancel(session_id)
    except ConnectError as exc:
        raise _connect_error(exc) from exc


@router.post("/auth/connect/{session_id}/retry", status_code=201)
def auth_connect_retry(
    session_id: str,
    key: ApiKey = Depends(admin_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
    connect: Any = Depends(get_provider_connect),
) -> dict[str, Any]:
    """Re-open a terminal session as a fresh one (same provider/label/
    account target); 409 while the session is still running."""
    try:
        return connect.retry(session_id, verify=_plane_verify_fn(plane, registry))
    except ConnectError as exc:
        raise _connect_error(exc) from exc
    except OnboardingError as exc:
        raise V1ApiError(400, exc.code, str(exc)) from exc


@router.get("/auth/pair/{ticket}")
def auth_pair_info(
    ticket: str,
    connect: Any = Depends(get_provider_connect),
) -> dict[str, Any]:
    """Unauthenticated pair-ticket lookup — the local CLI's ``sbx auth
    pair <ticket>`` learns which provider to log in. The ticket itself
    (single-use, short-TTL) is the credential — no Bearer key here by
    design."""
    try:
        return connect.pair_info(ticket)
    except ConnectError as exc:
        raise _connect_error(exc) from exc


@router.post("/auth/pair/complete")
def auth_pair_complete(
    body: PairCompleteRequest,
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
    connect: Any = Depends(get_provider_connect),
) -> dict[str, Any]:
    """Consume the pair ticket and materialize the captured credential
    blob — the local-pair counterpart of the hosted lane's capture. The
    blob is schema-validated before the ticket is consumed."""
    try:
        return connect.complete_pair(
            body.ticket, body.credential, verify=_plane_verify_fn(plane, registry)
        )
    except ConnectError as exc:
        raise _connect_error(exc) from exc
