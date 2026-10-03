"""Session state machine and sandbox orchestration."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from control.backend import Process, SandboxBackend, SandboxHandle, SandboxSpec
from control.compute import ComputeSpec, compute_for_record, usd_per_second
from control.config import (
    DEFAULT_MODEL,
    IDLE_TIMEOUT_S,
    MAX_CONCURRENT,
    SANDBOX_USD_PER_S,
    TERMINAL_STATUSES,
    TURN_MAX_SECONDS,
    env_float,
    env_int,
    lifecycle_config,
)
from control.credsync import TAG_CRED_BASE_FP
from control.run_activity import RunActivityStore, compact_run_events
from control.run_errors import run_error_for_run
from control.run_store import (
    RunLedger,
    apply_output_contract,
    outcome_from_turn_payload,
    run_error,
)
from control.sandbox_io import drain, read_json, read_text, sandbox_env, write_file
from control.store import SessionRecord, SessionStore, merge_usage

Clock = Callable[[], datetime]

# SOR-268: bound on concurrent remote teardown jobs (kill / stop-hook /
# cancels / terminate) — a bulk cancel/close fans out to this many remote
# calls at once and queues the rest, instead of serializing them under
# the plane lock on the request path.
_TEARDOWN_WORKERS = env_int("SBX_TEARDOWN_WORKERS", 4)

# How long stop()/close() wait for the teardown worker before returning.
# The durable state is already committed, so this only orders fast
# teardowns (the observable contract: a close that completes teardown
# inside the window returns with side-effects applied); slow provider
# work converges on the pool after the ACK.
_TEARDOWN_ACK_S = env_float("SBX_TEARDOWN_ACK_S", 0.5)

# SOR-268: minimum seconds between remote turn-settle probes triggered by
# the read path (``maybe_reconcile_turn``). Mutation paths always run the
# full reconcile — this only caps settle-on-read frequency.
_RECONCILE_COOLDOWN_S = env_float("SBX_RECONCILE_COOLDOWN_S", 2.0)
_READ_RECONCILE_WAIT_S = 0.05
_READ_RECONCILE_WORKERS = 4


def _utcnow() -> datetime:
    return datetime.now(UTC)


TITLE_MAX_CHARS = 60


def title_from_prompt(prompt: str | None) -> str | None:
    """A short display title from the first non-empty line of a prompt."""
    if not prompt:
        return None
    for line in prompt.splitlines():
        words = line.strip().lstrip("#>*- ").split()
        if not words:
            continue
        title = " ".join(words)
        if len(title) > TITLE_MAX_CHARS:
            title = title[: TITLE_MAX_CHARS - 1].rstrip() + "…"
        return title
    return None


def iso(ts: datetime) -> str:
    return ts.isoformat()


def cost_estimate_usd(sandbox_seconds: float, compute: ComputeSpec | None = None) -> float:
    """USD estimate for ``sandbox_seconds`` billed at the request floor.

    SOR-181: a session with a resolved compute spec bills at its declared
    ``cpu[0]``/``memory_mib[0]`` floor; ``None`` keeps the P0 deployment
    default (``SANDBOX_USD_PER_S``).
    """
    if compute is None:
        return round(max(0.0, sandbox_seconds) * SANDBOX_USD_PER_S, 6)
    return round(max(0.0, sandbox_seconds) * usd_per_second(compute), 6)


@dataclass
class LiveTurn:
    turn_id: str
    n: int
    proc: Process


class SessionConflict(Exception):
    def __init__(self, error: str, code: int = 409) -> None:
        super().__init__(error)
        self.error = error
        self.code = code


class ConcurrencyLimit(Exception):
    def __init__(self) -> None:
        super().__init__("concurrency_limit")
        self.error = "concurrency_limit"
        self.code = 429


def release_lease(v1_state: Any, session_id: str | None) -> None:
    """Idempotently release a ``/v1`` scheduler lease held for ``session_id``.

    The lease surface lives on ``app.state.v1_state`` (owned by api_v1); the
    control plane coordinates through ``pop_lease`` + ``release()`` — the same
    calls ``/v1`` uses — without importing the routes module.
    """
    if v1_state is None or not session_id:
        return
    pop = getattr(v1_state, "pop_lease", None)
    if not callable(pop):
        return
    lease = pop(session_id)
    release = getattr(lease, "release", None)
    if callable(release):
        release()


def release_lease_for_action(v1_state: Any, action: Any) -> None:
    """Release the ``/v1`` lease for a reaper action's session, if any."""
    release_lease(v1_state, getattr(action, "session_id", None))


class ControlPlane:
    def __init__(
        self,
        backend: SandboxBackend,
        store: SessionStore,
        runner_cmd: list[str],
        *,
        clock: Clock | None = None,
        max_concurrent: int = MAX_CONCURRENT,
        default_model: str = DEFAULT_MODEL,
        idle_timeout_s: int = IDLE_TIMEOUT_S,
        turn_max_seconds: int = TURN_MAX_SECONDS,
        run_ledger: RunLedger | None = None,
        workspaces: Any = None,
        handoffs: Any = None,
    ) -> None:
        self.backend = backend
        self.store = store
        self.runner_cmd = list(runner_cmd)
        self.clock = clock or _utcnow
        self.max_concurrent = max_concurrent
        self.default_model = default_model
        self.idle_timeout_s = idle_timeout_s
        self.turn_max_seconds = turn_max_seconds
        self.run_ledger = run_ledger
        # SOR-83: optional WorkspaceService / HandoffService wired by the app
        # layer; ``None`` means workspace declarations are not configured.
        self.workspaces = workspaces
        self.handoffs = handoffs
        # SOR-83: fired inside close() after runs are finalized and before the
        # sandbox is terminated, so workspace artifacts land in the durable
        # store while the sandbox is still readable. Best-effort: failures are
        # swallowed — a broken snapshot must never wedge teardown.
        self.snapshot_hook: Callable[[SessionRecord, SandboxHandle], None] | None = None
        # SOR-225: the durable-revision service + the materialization hook
        # fired on every FINISHED turn. ``revisions`` is set by the app
        # layer; ``revision_hook`` wraps it with registry/store context the
        # plane itself does not own. Best-effort like the snapshot hook: a
        # failed materialization never rewrites the run verdict.
        self.revisions: Any = None
        self.revision_hook: Callable[[SessionRecord, SandboxHandle, int], None] | None = None
        # SOR-127 environment build/snapshot cache — optional, wired by the
        # app layer. ``snapshot_provider`` restores a sandbox from a build
        # record's snapshot ref; ``environments`` is the build-record
        # service the /v1 worker resolves/fills through. Both None means
        # the cache is disabled and provisioning is unchanged.
        self.snapshot_provider: Any = None
        self.environments: Any = None
        # SOR-147: optional CredentialSync wired by the app layer; None means
        # no credential write-back (no registry installed, or disabled).
        self.credential_sync: Any = None
        # SOR-180: optional CheckpointService wired by the app layer; None
        # means suspend/recovery is unavailable — suspended agents cannot
        # recover and an idle expiry falls back to ``timed_out``.
        self.checkpoints: Any = None
        # App-layer account reservation shared by every recovery entrypoint.
        # The returned callback commits on success or releases on failure.
        self.recovery_reserve: Callable[[SessionRecord], Callable[[bool], None]] | None = None
        # Durable per-run activity transcripts, captured at turn end so the
        # run stream stays replayable after sandbox teardown. None disables.
        self.run_activity: RunActivityStore | None = None
        self._lock = threading.RLock()
        self._live: dict[str, LiveTurn] = {}
        # Per-session provider/account/model context for run records. Lost on
        # restart; persisted RunRecords carry their own copies, and sandbox
        # tags keep provider/account for sessions that predate the restart.
        self._run_meta: dict[str, dict[str, str | None]] = {}
        # Sessions whose first turn was queued by ``open_session`` but not yet
        # dispatched: post_message must not allocate that turn id to a
        # follow-up run in the gap between provision and dispatch (SOR-82 A2).
        self._first_turn_pending: set[str] = set()
        # SOR-268: per-session timestamp of the last remote reconcile probe
        # — ``maybe_reconcile_turn`` throttles the read-path settle so a
        # busy/SSE-heavy plane doesn't pay poll+read_json on every read.
        self._reconcile_at: dict[str, float] = {}
        self._reconcile_lock = threading.Lock()
        self._reconciling: set[str] = set()
        self._read_reconcile_pending: set[str] = set()
        self._read_reconcile_pool: ThreadPoolExecutor | None = None
        # SOR-268: bounded executor for the remote teardown tail of
        # stop()/close() (proc kill, runner stop hook, open-run cancels,
        # sandbox terminate). The durable intent lands synchronously under
        # ``_lock``; provider calls converge on these workers so a bulk
        # cancel/close cannot serialize the whole control plane through
        # remote Modal calls held under one lock or exhaust the anyio
        # request pool. Lazily created — threads spawn on first submit.
        self._remote_ops: ThreadPoolExecutor | None = None
        self._remote_ops_lock = threading.Lock()

    def runner(self, *args: str) -> list[str]:
        return [*self.runner_cmd, *args]

    def _store_get(self, session_id: str) -> SessionRecord | None:
        """Authoritative point read for the plane's mutation paths.

        A store that caches ``get`` (``ModalDictStore``) exposes
        ``get_fresh``; every other store serves the live row anyway.
        Read-modify-write sequences under ``_lock`` must not run on a
        cached row — a stale base would clobber a concurrent writer's
        ``put`` on the way back out.
        """
        fresh = getattr(self.store, "get_fresh", None)
        if callable(fresh):
            return fresh(session_id)
        return self.store.get(session_id)

    def _submit_teardown(self, fn: Callable[[], None]) -> threading.Event:
        """Run the remote teardown tail on the bounded worker pool.

        Returns a ``done`` event so the caller may bound its wait
        (``_TEARDOWN_ACK_S``): teardown that completes inside the window
        keeps the legacy synchronous ordering; slower provider work
        converges asynchronously. A pool failure degrades to inline
        execution — teardown is never dropped.
        """
        done = threading.Event()

        def _go() -> None:
            try:
                fn()
            finally:
                done.set()

        try:
            with self._remote_ops_lock:
                if self._remote_ops is None:
                    self._remote_ops = ThreadPoolExecutor(
                        max_workers=_TEARDOWN_WORKERS,
                        thread_name_prefix="sbx-teardown",
                    )
                self._remote_ops.submit(_go)
        except Exception:
            try:
                _go()
            except Exception:
                pass
        return done

    def public(self, rec: SessionRecord) -> dict[str, Any]:
        now = self.clock()
        if rec.status in TERMINAL_STATUSES:
            end = rec.ended_at or rec.updated_at or now
        else:
            end = now
        sandbox_seconds = max(0.0, (end - rec.created_at).total_seconds())
        usage = dict(rec.usage or {})
        usage.setdefault("input_tokens", 0)
        usage.setdefault("cached_input_tokens", 0)
        usage.setdefault("output_tokens", 0)
        return {
            "id": rec.id,
            "title": rec.title,
            # SOR-180: ``suspended`` is an internal, recoverable state —
            # the frozen AgentStatus enum cannot grow, so the wire view is
            # the closest public equivalent: a live agent awaiting a run.
            "status": "idle" if rec.status == "suspended" else rec.status,
            "created_at": iso(rec.created_at),
            "updated_at": iso(rec.updated_at),
            "model": rec.model,
            "reasoning_effort": rec.reasoning_effort,
            "turns": rec.turns,
            "usage": usage,
            "cost_estimate_usd": cost_estimate_usd(
                sandbox_seconds,
                compute_for_record(rec.compute, rec.sandbox_tags),
            ),
            "sandbox_seconds": sandbox_seconds,
            "messages": list(rec.messages),
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self.public(rec) for rec in self.store.list_all()]

    def get(self, session_id: str) -> SessionRecord | None:
        return self.store.get(session_id)

    def find_by_idempotency(self, owner: str, key: str) -> SessionRecord | None:
        """Durable ``Idempotency-Key`` lookup across restarts (SOR-82).

        The in-memory ``IdempotencyStore`` only covers one process; the
        session record pins ``(owner, key)`` durably so a retry landing after
        a control-plane restart still resolves to the original agent.
        ``lost`` records are failed creates — the key is free for retry.
        """
        with self._lock:
            for rec in self.store.list_all():
                if rec.owner == owner and rec.idempotency_key == key and rec.status != "lost":
                    return rec
        return None

    def create_session(
        self,
        *,
        owner: str,
        title: str | None,
        model: str | None,
        provider: str = "codex",
        account_id: str = "auto",
        secret_name: str | None = None,
    ) -> str:
        session_id = self.open_session(
            owner=owner,
            title=title,
            model=model,
            provider=provider,
            account_id=account_id,
        )
        self.provision_session(
            session_id,
            provider=provider,
            account_id=account_id,
            secret_name=secret_name,
        )
        return session_id

    def _ensure_capacity(self, owner: str) -> None:
        """Check creates and checkpoint restores under the same plane lock."""
        live = self.backend.list(tags={"owner": owner})
        records = [rec for rec in self.store.list_all() if rec.owner == owner]
        by_id = {rec.id: rec for rec in records}
        by_sandbox = {rec.sandbox_id: rec for rec in records if rec.sandbox_id}
        live_bound_ids: set[str] = set()
        for handle in live:
            rec = by_id.get((handle.tags or {}).get("session_id")) or by_sandbox.get(handle.id)
            if (
                rec is not None
                and rec.status not in TERMINAL_STATUSES
                and rec.status != "suspended"
            ):
                live_bound_ids.add(rec.id)
        now = self.clock()
        creating = sum(
            1
            for rec in records
            if rec.status == "creating"
            and rec.id not in live_bound_ids
            and (
                now
                - (
                    rec.updated_at
                    if rec.sandbox_tags.get("recovering") == "true"
                    else rec.created_at
                )
            ).total_seconds()
            < lifecycle_config().create_grace_s
        )
        if len(live_bound_ids) + creating >= self.max_concurrent:
            raise ConcurrencyLimit()

    def open_session(
        self,
        *,
        owner: str,
        title: str | None,
        model: str | None,
        provider: str = "codex",
        account_id: str = "auto",
        first_prompt: str | None = None,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
        output_contract: dict[str, Any] | None = None,
        resource_refs: dict[str, Any] | None = None,
        compute: dict[str, Any] | ComputeSpec | None = None,
        reasoning_effort: str | None = None,
        effort_surface: Sequence[str] | None = None,
    ) -> str:
        """Publish a ``creating`` record without provisioning the sandbox.

        SOR-82 A2: ``/v1`` callers return immediately after this call and run
        ``provision_session`` on a background thread; ``/api`` keeps calling
        ``create_session``, which is ``open_session`` + ``provision_session``
        inline and unchanged.

        ``first_prompt`` queues the first run: its user message is appended as
        ``turn-1``, the turn id is reserved in ``_first_turn_pending`` so a
        follow-up ``post_message`` cannot steal it before the worker
        dispatches, and a ``CREATING`` run-1 record is persisted in the run
        ledger before the session record becomes visible.

        ``idempotency_key``/``idempotency_fingerprint`` pin a client-supplied
        ``Idempotency-Key`` to the durable record so a retry that lands after
        a control-plane restart still dedups (``find_by_idempotency``).
        """
        with self._lock:
            self._ensure_capacity(owner)
            now = self.clock()
            session_id = uuid.uuid4().hex
            tags = {"session_id": session_id, "owner": owner}
            # Preserve the exact P1 sandbox shape for legacy /api callers.
            # P2.1 provider sessions carry enough metadata for Modal to select
            # the correct image and reattach the per-account Secret on exec.
            if provider != "codex" or account_id != "auto":
                tags.update({"provider": provider, "account_id": account_id})
            if resource_refs:
                # SOR-129: declared resource *refs* (names only — never
                # values) ride the durable sandbox tags so the agent view
                # stays truthful across control-plane restarts.
                tags["resources"] = json.dumps(resource_refs)
            if compute is not None:
                # SOR-181: the resolved compute spec is durable — the
                # record field feeds the status echo and cost estimate,
                # the tag keeps the sizing recoverable from the sandbox
                # itself after a control-plane restart.
                compute_public = (
                    compute.public() if isinstance(compute, ComputeSpec) else dict(compute)
                )
                tags["compute"] = json.dumps(compute_public)
            if reasoning_effort:
                # SOR-179: the declared canonical effort rides the durable
                # tags so the agent view stays truthful across restarts.
                tags["reasoning_effort"] = reasoning_effort
            if effort_surface:
                # SOR-204: the account's discovered effort surface rides
                # with the record so the in-sandbox backstop validates the
                # declaration against live capabilities, not the floor.
                tags["effort_surface"] = ",".join(effort_surface)
            now = self.clock()
            messages: list[dict[str, Any]] = []
            if first_prompt is not None:
                messages.append(
                    {"role": "user", "text": first_prompt, "turn_id": "turn-1", "ts": iso(now)}
                )
                self._first_turn_pending.add(session_id)
            rec = SessionRecord(
                id=session_id,
                title=title or title_from_prompt(first_prompt) or "untitled",
                status="creating",
                created_at=now,
                updated_at=now,
                model=model or self.default_model,
                turns=0,
                usage=None,
                messages=messages,
                owner=owner,
                sandbox_tags=tags,
                last_activity_at=now,
                idempotency_key=idempotency_key,
                idempotency_fingerprint=idempotency_fingerprint,
                compute=compute_public if compute is not None else None,
                reasoning_effort=reasoning_effort,
            )
            self._run_meta[session_id] = {
                "provider": provider,
                "account_id": account_id,
                "model": rec.model,
                "reasoning_effort": reasoning_effort,
            }
            if first_prompt is not None and self.run_ledger is not None:
                # The durable ledger is the source of truth: run-1 exists as
                # CREATING before the session record is visible, so no reader
                # can observe a queued run with no persisted state.
                self.run_ledger.begin(
                    agent_id=session_id,
                    n=1,
                    provider=provider,
                    account_id=account_id,
                    model=rec.model,
                    reasoning_effort=rec.reasoning_effort,
                    status="CREATING",
                    output_contract=output_contract,
                )
            # Publish the record before the sandbox exists: the reaper
            # distinguishes an in-flight create from an orphan sandbox via
            # this record (SOR-80), so ``backend.create`` cannot race it.
            self.store.put(rec)
            return session_id

    def provision_session(
        self,
        session_id: str,
        *,
        provider: str = "codex",
        account_id: str = "auto",
        secret_name: str | None = None,
        resource_secrets: list[str] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        env_snapshot: str | None = None,
    ) -> None:
        """Create the sandbox and run ``runner init`` for an open session.

        Blocking — intended for a worker thread under ``/v1`` (SOR-82 A2).
        On failure the record is terminal ``lost`` with sandbox metadata kept
        for the reaper; ``SessionConflict`` means the session was closed while
        provisioning ran.

        SOR-129 session resources: ``resource_secrets`` are Modal Secret
        names attached to this sandbox only (validated upstream — never
        account credential Secrets); ``mcp_servers`` are the resolved MCP
        config templates forwarded to ``runner init`` via
        ``SBX_MCP_SERVERS`` (env indirection, never values).

        SOR-127: ``env_snapshot`` is a prepared-environment snapshot ref —
        the sandbox is restored from it through ``snapshot_provider``
        instead of a cold ``backend.create``. Restoring still mounts this
        session's own Secrets (account credential channels are per-sandbox
        env, never filesystem state), and the restored base still has to
        pass the workspace ``base_sha`` gate at prepare time.
        """
        with self._lock:
            rec = self._store_get(session_id)
            if rec is None:
                raise KeyError(session_id)
            if rec.status != "creating":
                raise SessionConflict("session_not_runnable")
            tags = dict(rec.sandbox_tags)
        secrets = [secret_name] if secret_name else []
        # SOR-181: the compute spec is read back from the durable record —
        # the same resolution a post-restart reprovision would take — so
        # create and recovery always agree on the sizing.
        compute = compute_for_record(rec.compute, rec.sandbox_tags)
        spec = SandboxSpec(
            tags=tags,
            secrets=secrets,
            resource_secrets=list(resource_secrets or ()),
            cpu=compute.cpu if compute is not None else None,
            memory_mib=compute.memory_mib if compute is not None else None,
        )
        try:
            if env_snapshot is not None:
                if self.snapshot_provider is None:
                    raise RuntimeError(
                        "env_snapshot restore requested but no snapshot provider is configured"
                    )
                handle = self.snapshot_provider.restore(env_snapshot, spec)
            else:
                handle = self.backend.create(spec)
        except Exception:
            self._mark_create_failed(rec)
            raise

        with self._lock:
            stored = self._store_get(session_id)
            if stored is None or stored.status in TERMINAL_STATUSES:
                # Closed while the sandbox was being created — do not bind it.
                try:
                    self.backend.terminate(handle)
                except Exception:
                    pass
                raise SessionConflict("session_not_runnable")
            stored.sandbox_id = handle.id
            stored.sandbox_root = str(handle.root)
            if handle.tags.get("hosted") == "1":
                stored.sandbox_tags = dict(handle.tags)
            # SOR-180: persist the Secret *refs* this sandbox was created
            # with — a checkpoint restore re-declares them so the fresh
            # sandbox mounts the same credential/resource channels.
            stored.spec_secrets = {
                "secrets": list(secrets),
                "resource_secrets": list(resource_secrets or ()),
            }
            stored.updated_at = self.clock()
            self.store.put(stored)
            rec = stored

        sync = self.credential_sync
        seed_fp = None
        seed_blob: dict[str, Any] | None = None
        if sync is not None and account_id != "auto":
            try:
                seed_fp = sync.seed_fingerprint(account_id)
            except Exception:
                seed_fp = None
            try:
                seed_blob = sync.seed_blob(account_id)
            except Exception:
                seed_blob = None

        try:
            init_args = ["init", "--auth", "auth_json", "--model", rec.model]
            if rec.reasoning_effort:
                # SOR-179: the agent's declared canonical effort is bound
                # into session.json; every turn (incl. resume) inherits it.
                init_args += ["--reasoning-effort", rec.reasoning_effort]
            init_env: dict[str, str] = {}
            if provider != "codex" or account_id != "auto":
                init_args += ["--provider", provider]
                if account_id != "auto":
                    init_args += ["--account-id", account_id]
                    init_env["SBX_ACCOUNT_ID"] = account_id
            if seed_blob:
                # The stored blob is the authoritative credential: inject it
                # at init so a blob-carrying account restores its auth files
                # even when no managed Secret mounts (``secret_name`` unset
                # or never materialized, or a backend without Secrets).
                init_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(seed_blob, ensure_ascii=False)
            if rec.sandbox_tags.get("effort_surface"):
                init_env["SBX_EFFORT_SURFACE"] = rec.sandbox_tags["effort_surface"]
            if mcp_servers:
                # SOR-129: resolved MCP config templates (${env:VAR}
                # indirection only — no secret values) for ``runner init``.
                init_env["SBX_MCP_SERVERS"] = json.dumps(list(mcp_servers))
            init = self.backend.exec(
                handle,
                self.runner(*init_args),
                env=sandbox_env(handle, init_env),
            )
            code = drain(init)
            if code != 0:
                tail = ""
                stderr_text = getattr(init, "stderr_text", None)
                if callable(stderr_text):
                    try:
                        tail = stderr_text().strip()
                    except Exception:
                        tail = ""
                detail = f": {tail[-300:]}" if tail else ""
                raise RuntimeError(f"runner init exited {code}{detail}")
        except Exception:
            # Mark the record lost *before* terminating: even if terminate
            # fails, the record stays terminal with sandbox_id bound so the
            # reaper can retry cleanup (SOR-80).
            self._mark_create_failed(rec)
            try:
                self.backend.terminate(handle)
            except Exception:
                pass
            raise

        with self._lock:
            stored = self._store_get(session_id)
            if stored is None or stored.status in TERMINAL_STATUSES:
                # Closed concurrently while init ran — do not resurrect it.
                try:
                    self.backend.terminate(handle)
                except Exception:
                    pass
                raise SessionConflict("session_not_runnable")
            stored.status = "idle"
            now = self.clock()
            stored.updated_at = now
            stored.last_activity_at = now
            # SOR-147: anchor the credential CAS — record the fingerprint of
            # the blob this sandbox was seeded with so later write-backs can
            # prove the stored credential hasn't moved.
            if seed_fp:
                stored.sandbox_tags[TAG_CRED_BASE_FP] = seed_fp
            self.store.put(stored)

    def _mark_create_failed(self, rec: SessionRecord) -> None:
        """Terminal ``lost`` transition for a failed create; keeps sandbox_id."""
        with self._lock:
            self._first_turn_pending.discard(rec.id)
            stored = self._store_get(rec.id) or rec
            if stored.status in TERMINAL_STATUSES:
                return
            stored.status = "lost"
            stored.ended_at = self.clock()
            stored.updated_at = stored.ended_at
            self.store.put(stored)

    def discard_queued_first_turn(self, session_id: str) -> None:
        """Drop a queued-but-undispatched first turn reservation (cancel path).

        Only the reservation marker is dropped — the session status is left
        alone. While provisioning is still in flight the record stays
        ``creating`` (the truth); the worker settles it to ``idle`` or
        ``lost`` when ``provision_session`` resolves. Reporting ``idle``
        early would let a follow-up run dispatch into a half-provisioned
        sandbox whose ``runner init`` is still blocked (SOR-82 review).
        """
        with self._lock:
            self._first_turn_pending.discard(session_id)

    def recover_session(self, session_id: str) -> None:
        """SOR-180: restore a ``suspended`` agent from its checkpoint.

        A suspended agent owns no live sandbox; this creates a fresh one
        from the durable checkpoint — same filesystem, same native
        provider session (``session.json`` survives the snapshot), Secret
        refs re-declared on the spec and credentials re-attached
        in-sandbox — and returns the record to ``idle``. Terminal
        ``lost`` is reserved for a missing or proven-invalid checkpoint
        (the explicit diagnosis for checkpoint loss); a retryable
        restore failure leaves the record ``suspended`` so the next
        follow-up re-attempts. ``SessionConflict`` mirrors the normal
        not-runnable refusal either way.
        """
        checkpoints = self.checkpoints

        def finish_reservation(_success: bool) -> None:
            pass

        with self._lock:
            rec = self._store_get(session_id)
            if rec is None or rec.status != "suspended":
                return
            try:
                self._ensure_capacity(rec.owner)
            except ConcurrencyLimit as exc:
                raise SessionConflict(exc.error, exc.code) from None
            if self.recovery_reserve is not None:
                finish_reservation = self.recovery_reserve(rec)
            # Publish the reservation before the slow restore. New creates
            # count it, and concurrent follow-ups cannot restore twice.
            reserved = replace(
                rec,
                status="creating",
                sandbox_id=None,
                sandbox_root=None,
                updated_at=self.clock(),
                sandbox_tags={**rec.sandbox_tags, "recovering": "true"},
            )
            try:
                self.store.put(reserved)
            except Exception:
                finish_reservation(False)
                raise

        def rollback() -> None:
            try:
                with self._lock:
                    stored = self._store_get(session_id)
                    if (
                        stored is not None
                        and stored.status == "creating"
                        and stored.sandbox_tags.get("recovering") == "true"
                    ):
                        # Revert only the fields this reservation owns —
                        # restoring the stale ``rec`` wholesale would
                        # clobber writes that landed on the creating
                        # record meanwhile (e.g. a queued follow-up whose
                        # QUEUED ledger row already exists).
                        stored.status = "suspended"
                        stored.sandbox_id = rec.sandbox_id
                        stored.sandbox_root = rec.sandbox_root
                        stored.sandbox_tags = dict(stored.sandbox_tags)
                        stored.sandbox_tags.pop("recovering", None)
                        stored.updated_at = self.clock()
                        self.store.put(stored)
            finally:
                finish_reservation(False)

        if checkpoints is None:
            rollback()
            self._mark_unrecoverable(session_id)
            raise SessionConflict("session_not_runnable")
        try:
            handle = checkpoints.restore(rec)
        except Exception as exc:
            rollback()
            if getattr(exc, "retryable", False):
                # Transient restore failure: the checkpoint stays usable
                # and the record ``suspended`` — the next follow-up
                # re-attempts the restore.
                raise SessionConflict("session_not_runnable") from None
            self._mark_unrecoverable(session_id)
            raise SessionConflict("session_not_runnable") from None
        with self._lock:
            try:
                stored = self._store_get(session_id)
            except Exception:
                try:
                    self.backend.terminate(handle)
                finally:
                    finish_reservation(False)
                raise
            if (
                stored is None
                or stored.status != "creating"
                or stored.sandbox_tags.get("recovering") != "true"
            ):
                # Closed/terminated while the sandbox was being restored —
                # do not bind it.
                try:
                    self.backend.terminate(handle)
                except Exception:
                    pass
                finish_reservation(False)
                raise SessionConflict("session_not_runnable")
            stored.sandbox_id = handle.id
            stored.sandbox_root = str(handle.root)
            if handle.tags:
                stored.sandbox_tags = dict(handle.tags)
            stored.sandbox_tags.pop("recovering", None)
            stored.status = "idle"
            now = self.clock()
            stored.updated_at = now
            stored.last_activity_at = now
            try:
                self.store.put(stored)
            except Exception:
                try:
                    self.backend.terminate(handle)
                finally:
                    rollback()
                raise
            finish_reservation(True)

    def _mark_unrecoverable(self, session_id: str) -> None:
        """Terminal ``lost`` for a suspended agent that cannot be restored."""
        with self._lock:
            stored = self._store_get(session_id)
            if stored is None or stored.status != "suspended":
                return
            stored.status = "lost"
            now = self.clock()
            stored.ended_at = now
            stored.updated_at = now
            stored.current_turn_id = None
            stored.current_turn_n = None
            self.store.put(stored)

    def _next_turn_n(self, rec: SessionRecord) -> int:
        """Next never-reused turn number for ``rec``.

        ``turns`` counts finished turns only; queued-but-undispatched run
        messages (SOR-82 A2 ``first_prompt``) already occupy their turn id, so
        allocation is ``max(turns, message turn ids, current_turn_n) + 1``.
        """
        known = {rec.turns, rec.current_turn_n or 0}
        for message in rec.messages:
            turn_id = message.get("turn_id")
            if isinstance(turn_id, str) and turn_id.startswith("turn-"):
                try:
                    known.add(int(turn_id[5:]))
                except ValueError:
                    pass
        return max(known) + 1

    def post_message(
        self,
        session_id: str,
        text: str,
        *,
        output_contract: dict[str, Any] | None = None,
        queue: bool = False,
        idempotency: dict[str, Any] | None = None,
    ) -> str:
        """Post a user message; ``queue=True`` parks it durably when busy.

        SOR-224: a follow-up landing while a turn is in progress (or while
        earlier queued turns are still pending) becomes a durable ``QUEUED``
        run — message + ledger record persisted — instead of the 409
        ``turn_in_progress`` the default ``queue=False`` path keeps. The
        queue drains FIFO via ``drain_queued`` as turns finish; without a
        run ledger there is nowhere durable to park the turn, so the busy
        refusal stays.
        """
        # A ``running`` record with no in-process watcher is a stranded turn
        # (control-plane cutover); reconcile it from evidence first so a
        # finished provider run frees the agent instead of 409ing forever.
        self.reconcile_turn(session_id)
        # SOR-180: a suspended agent owns no live sandbox; restore it from
        # its checkpoint before the runnable checks so a follow-up lands on
        # the same Agent id / filesystem / native provider session.
        self.recover_session(session_id)
        enqueued = False
        with self._lock:
            rec = self._store_get(session_id)
            if rec is None:
                raise KeyError(session_id)
            if rec.status in TERMINAL_STATUSES:
                raise SessionConflict("session_not_runnable")
            busy = (
                session_id in self._first_turn_pending
                or rec.status == "running"
                or rec.current_turn_id is not None
            )
            if busy and not queue:
                raise SessionConflict("turn_in_progress")
            queued_backlog = bool(self._queued_ns(session_id)) if queue else False
            if (busy or queued_backlog) and queue:
                if self.run_ledger is None:
                    # A queued follow-up is only durable when the ledger is
                    # attached; without it the old refusal is the honest answer.
                    raise SessionConflict("turn_in_progress")
                # Non-idle states (``creating`` pre-dispatch, suspended-then-
                # recovered) cannot dispatch now — the turn simply parks in
                # the queue until ``drain_queued`` finds the agent idle.
                n = self._next_turn_n(rec)
                turn_id = f"turn-{n}"
                now = self.clock()
                rec.messages.append(
                    {"role": "user", "text": text, "turn_id": turn_id, "ts": iso(now)}
                )
                rec.updated_at = now
                self.store.put(rec)
                meta = self._run_meta.get(session_id, {})
                self.run_ledger.begin(
                    agent_id=session_id,
                    n=n,
                    provider=meta.get("provider") or rec.sandbox_tags.get("provider") or "codex",
                    account_id=meta.get("account_id")
                    or rec.sandbox_tags.get("account_id")
                    or "auto",
                    model=meta.get("model") or rec.model,
                    reasoning_effort=meta.get("reasoning_effort") or rec.reasoning_effort,
                    status="QUEUED",
                    output_contract=output_contract,
                    idempotency=idempotency,
                )
                enqueued = True
            else:
                if rec.status != "idle":
                    # ``creating`` (or anything else non-idle) is not runnable.
                    raise SessionConflict("session_not_runnable")
                handle = rec.handle()
                if handle is None:
                    raise SessionConflict("session_not_runnable")
                n = self._next_turn_n(rec)
                turn_id = f"turn-{n}"
                now = self.clock()
                rec.status = "running"
                rec.current_turn_id = turn_id
                rec.current_turn_n = n
                rec.updated_at = now
                rec.messages.append(
                    {"role": "user", "text": text, "turn_id": turn_id, "ts": iso(now)}
                )
                if self.credential_sync is not None:
                    # SOR-147: bind the run to the credential fingerprint it is
                    # dispatched with — the /v1 failure reporter compares it to
                    # the stored blob so an auth_invalid verdict computed against
                    # a since-rotated credential is recognized as stale.
                    self.credential_sync.mark_run_credential(rec.sandbox_tags)
                self.store.put(rec)
                if self.run_ledger is not None:
                    meta = self._run_meta.get(session_id, {})
                    self.run_ledger.begin(
                        agent_id=session_id,
                        n=n,
                        provider=meta.get("provider")
                        or rec.sandbox_tags.get("provider")
                        or "codex",
                        account_id=meta.get("account_id")
                        or rec.sandbox_tags.get("account_id")
                        or "auto",
                        model=meta.get("model") or rec.model,
                        reasoning_effort=meta.get("reasoning_effort") or rec.reasoning_effort,
                        output_contract=output_contract,
                        idempotency=idempotency,
                    )
        if enqueued:
            # Idle agents with a backlog start the head of the queue now;
            # a busy agent drains when its turn finishes.
            self.drain_queued(session_id)
            return turn_id
        try:
            return self._dispatch_turn(session_id, turn_id, n, handle, text, drop_message=True)
        except Exception:
            # Rollback already finalized the record: a terminal (lost)
            # session means the sandbox died between the liveness check and
            # dispatch, so the refusal is the canonical session_not_runnable
            # rather than an unhandled 500.
            rec = self._store_get(session_id)
            if rec is not None and rec.status in TERMINAL_STATUSES:
                raise SessionConflict("session_not_runnable") from None
            raise

    def _queued_ns(self, session_id: str) -> list[int]:
        """Persisted QUEUED run numbers for the session, FIFO order."""
        ledger = self.run_ledger
        if ledger is None:
            return []
        try:
            return ledger.queued_ns(session_id)
        except Exception:
            return []

    def drain_queued(self, session_id: str) -> str | None:
        """Dispatch the head of the durable queue when the agent is idle.

        Called wherever the agent may have just become runnable: turn
        finish, stop/cancel, follow-up enqueue, and the reconcile sweep
        (which is how queued turns recover after a control-plane restart —
        the QUEUED records and their messages are durable). Loops past
        per-turn dispatch failures: a turn that cannot even start is
        persisted ``ERROR`` and the next queued turn gets its chance —
        one bad turn must not wedge the queue.

        Returns the dispatched turn id, or None when nothing could run.
        """
        if self.run_ledger is None:
            return None
        while True:
            ns = self._queued_ns(session_id)
            if not ns:
                return None
            # A suspended agent is the public equivalent of idle — restore
            # it from its checkpoint so the queued turn can dispatch.
            # ``recover_session`` no-ops for non-suspended records.
            try:
                self.recover_session(session_id)
            except Exception:
                return None
            n = ns[0]
            turn_id = f"turn-{n}"
            with self._lock:
                rec = self._store_get(session_id)
                if (
                    rec is None
                    or rec.status != "idle"
                    or rec.current_turn_id is not None
                    or session_id in self._first_turn_pending
                ):
                    return None
                handle = rec.handle()
                if handle is None:
                    # Suspended/dead-but-unreaped agent: the queue stays
                    # parked until a follow-up restores it.
                    return None
                text = next(
                    (
                        str(m.get("text") or "")
                        for m in rec.messages
                        if m.get("turn_id") == turn_id and m.get("role") == "user"
                    ),
                    None,
                )
                if text is None:
                    # A queued run whose message is gone can never execute —
                    # close it out as an explicit error rather than letting a
                    # phantom QUEUED record park the queue forever.
                    self.run_ledger.finish(
                        session_id,
                        n,
                        status="ERROR",
                        error=run_error(
                            "runtime_error",
                            "queued run lost its prompt message",
                            source="control",
                            retryable=True,
                        ),
                    )
                    continue
                claimed = self.run_ledger.mark_running(session_id, n)
                if claimed is None or claimed.status != "RUNNING":
                    # A cancel/close landed between the queue snapshot and
                    # the claim: the record is already terminal (the claim
                    # is atomic under the ledger lock, so the durable
                    # verdict wins) — the turn must never dispatch. Skip to
                    # the next queued entry.
                    continue
                now = self.clock()
                rec.status = "running"
                rec.current_turn_id = turn_id
                rec.current_turn_n = n
                rec.updated_at = now
                if self.credential_sync is not None:
                    self.credential_sync.mark_run_credential(rec.sandbox_tags)
                self.store.put(rec)
            try:
                return self._dispatch_turn(session_id, turn_id, n, handle, text, drop_message=False)
            except Exception as exc:
                # The rollback inside _dispatch_turn already reset the
                # session; persist the transport failure on the run record —
                # a QUEUED→ERROR transition, never a silent drop.
                self.run_ledger.finish(
                    session_id,
                    n,
                    status="ERROR",
                    error=run_error(
                        "runtime_error",
                        f"queued turn failed to dispatch: {type(exc).__name__}",
                        source="control",
                        retryable=True,
                    ),
                )
                continue

    def post_queued_first_turn(self, session_id: str) -> str:
        """Dispatch the run-1 queued by ``open_session(first_prompt=...)``.

        The user message was appended at open time; here we only flip the
        record to ``running`` and exec the turn. Raises SessionConflict when
        the queue marker is gone (already dispatched/dropped) or the session
        is not runnable.
        """
        # SOR-180: a suspended agent owns no live sandbox — recover the
        # checkpoint first so the queued turn lands on the same agent.
        self.recover_session(session_id)
        with self._lock:
            rec = self._store_get(session_id)
            if rec is None:
                self._first_turn_pending.discard(session_id)
                raise KeyError(session_id)
            if session_id not in self._first_turn_pending:
                raise SessionConflict("turn_in_progress")
            if rec.status in TERMINAL_STATUSES:
                self._first_turn_pending.discard(session_id)
                raise SessionConflict("session_not_runnable")
            if rec.status != "idle":
                raise SessionConflict("session_not_runnable")
            handle = rec.handle()
            if handle is None:
                raise SessionConflict("session_not_runnable")
            self._first_turn_pending.discard(session_id)
            n = 1
            turn_id = f"turn-{n}"
            rec.status = "running"
            rec.current_turn_id = turn_id
            rec.current_turn_n = n
            rec.updated_at = self.clock()
            if self.credential_sync is not None:
                self.credential_sync.mark_run_credential(rec.sandbox_tags)
            self.store.put(rec)
            if self.run_ledger is not None:
                # The run-1 record already exists (CREATING, written by
                # open_session / the route seam); dispatch is the RUNNING
                # transition. begin() backstops records created before the
                # ledger existed; mark_running is terminal-safe.
                meta = self._run_meta.get(session_id, {})
                self.run_ledger.begin(
                    agent_id=session_id,
                    n=n,
                    provider=meta.get("provider") or rec.sandbox_tags.get("provider") or "codex",
                    account_id=meta.get("account_id")
                    or rec.sandbox_tags.get("account_id")
                    or "auto",
                    model=meta.get("model") or rec.model,
                    reasoning_effort=meta.get("reasoning_effort") or rec.reasoning_effort,
                )
                self.run_ledger.mark_running(session_id, n)
        text = next(
            (
                str(m.get("text") or "")
                for m in rec.messages
                if m.get("turn_id") == turn_id and m.get("role") == "user"
            ),
            "",
        )
        # On dispatch failure the queued user message stays: turn-1 remains
        # allocated to run-1 (which the caller marks ERROR) and the next
        # post_message allocates turn-2 — run ids never collide.
        return self._dispatch_turn(session_id, turn_id, n, handle, text, drop_message=False)

    def _dispatch_turn(
        self,
        session_id: str,
        turn_id: str,
        n: int,
        handle: Any,
        text: str,
        *,
        drop_message: bool,
    ) -> str:
        rel = f"_prompt_{n}.md"
        # SOR-130: the run's normalized output contract (persisted on the
        # ledger record at begin) rides into the sandbox as _contract_<n>.json
        # so the runner can steer + evaluate the provider's final message.
        contract = None
        if self.run_ledger is not None:
            record = self.run_ledger.get(session_id, n)
            contract = record.output_contract if record is not None else None
        turn_args = [
            "turn",
            "--n",
            str(n),
            "--message-file",
            str(handle.root / rel),
            "--max-seconds",
            str(self.turn_max_seconds),
        ]
        try:
            write_file(self.backend, handle, rel, text)
            if contract is not None:
                contract_rel = f"_contract_{n}.json"
                write_file(
                    self.backend,
                    handle,
                    contract_rel,
                    json.dumps(
                        {
                            "schema": contract.get("schema"),
                            "enforcement": contract.get("enforcement", "strict"),
                        }
                    ),
                )
                turn_args += ["--output-contract", str(handle.root / contract_rel)]
            proc = self.backend.exec(
                handle,
                self.runner(*turn_args),
                env=sandbox_env(handle, self._workdir_env(session_id)),
            )
        except Exception:
            self._rollback_turn(session_id, turn_id, handle, drop_message=drop_message)
            raise
        dead_on_arrival = False
        with self._lock:
            self._live[session_id] = LiveTurn(turn_id=turn_id, n=n, proc=proc)
            record = self.run_ledger.get(session_id, n) if self.run_ledger is not None else None
            if record is not None and record.terminal:
                # A cancel won the claim→dispatch gap: the durable verdict
                # already holds — the just-spawned proc is killed instead
                # of running billed work under a terminal record.
                self._live.pop(session_id, None)
                dead_on_arrival = True
        if dead_on_arrival:
            try:
                proc.kill()
            except Exception:
                pass
            return turn_id
        thread = threading.Thread(
            target=self._watch_turn,
            args=(session_id, turn_id, n, proc),
            daemon=True,
            name=f"sbx-turn-{session_id}-{n}",
        )
        thread.start()
        return turn_id

    def _workdir_env(self, session_id: str) -> dict[str, str] | None:
        """``SBX_WORKDIR`` for ``runner turn`` when a workspace was prepared
        (SOR-174): provider CLIs run inside the declared workdir while
        ``$SBX_WORK`` stays the runner state root. Unprepared/missing
        records dispatch without it, keeping the pre-workspace layout.
        """
        if self.workspaces is None:
            return None
        record = self.workspaces.get(session_id)
        if record is None or not record.prepared:
            return None
        return {"SBX_WORKDIR": record.workdir}

    def _rollback_turn(
        self, session_id: str, turn_id: str, handle: Any, *, drop_message: bool = True
    ) -> None:
        """Undo a queued turn whose write/exec never started (SOR-80).

        Deterministic end state: ``idle`` when the sandbox is still alive
        (runnable again) or ``lost`` when it is gone (terminal). The pending
        user message is rolled back too, except for queued first turns
        (``drop_message=False``) whose turn id must stay allocated.
        """
        with self._lock:
            rec = self._store_get(session_id)
            if rec is None or rec.current_turn_id != turn_id:
                return
            try:
                alive = bool(self.backend.poll(handle).alive)
            except Exception:
                alive = False
            now = self.clock()
            if drop_message:
                rec.messages = [m for m in rec.messages if m.get("turn_id") != turn_id]
            rec.current_turn_id = None
            rec.current_turn_n = None
            rec.status = "idle" if alive else "lost"
            if not alive:
                rec.ended_at = now
            rec.updated_at = now
            rec.last_activity_at = now
            self.store.put(rec)
            if self.run_ledger is not None and drop_message:
                # The turn never started; its open run record is rolled back
                # with the pending user message. Queued first turns keep
                # theirs (drop_message=False): run-1 stays allocated so the
                # caller can persist the terminal startup failure on it.
                try:
                    n = int(turn_id.rsplit("-", 1)[-1])
                except ValueError:
                    n = 0
                if n:
                    self.run_ledger.discard(session_id, n)

    def _watch_turn(self, session_id: str, turn_id: str, n: int, proc: Process) -> None:
        try:
            drain(proc)
        except Exception:
            pass
        self._finish_turn(session_id, turn_id, n)

    def _capture_activity(self, handle: SandboxHandle | None, n: int) -> list[dict[str, Any]]:
        if self.run_activity is None or handle is None:
            return []
        try:
            return compact_run_events(read_text(self.backend, handle, "events.jsonl"), n)
        except Exception:
            return []

    def _persist_activity(self, session_id: str, n: int, transcript: list[dict[str, Any]]) -> None:
        if self.run_activity is None:
            return
        try:
            self.run_activity.put(session_id, n, transcript)
        except Exception:
            pass

    def _finish_turn(self, session_id: str, turn_id: str, n: int) -> None:
        # Phase 1 (locked): snapshot the live handle only. Everything after
        # this — the backend evidence read and the contract verdict — runs
        # unlocked, so untrusted work can strand this watcher thread but
        # never the whole control plane (SOR-130 review).
        with self._lock:
            rec = self._store_get(session_id)
            handle = rec.handle() if rec is not None else None
        payload = None
        if handle is not None:
            try:
                payload = read_json(self.backend, handle, f"turns/{n}.json")
            except Exception:
                # Sandbox reclaimed mid-turn: the turn outcome is
                # unreadable — the ledger persist below records it as
                # ERROR, never success.
                payload = None
        transcript = self._capture_activity(handle, n)
        # Phase 2 (unlocked): judge the evidence. apply_output_contract is
        # pure and budget-bounded; the backstop keeps even an unforeseen
        # failure diagnosable instead of wedging the run open.
        contract = None
        try:
            status, error, result_text, usage = outcome_from_turn_payload(payload)
            # SOR-130: enforce the run's output contract on the recorded
            # message — a strict violation becomes ERROR +
            # contract_violation, never a silent FINISHED.
            if self.run_ledger is not None:
                record = self.run_ledger.get(session_id, n)
                contract = record.output_contract if record is not None else None
            status, error, structured_output, contract_result = apply_output_contract(
                status, error, result_text, contract
            )
        except Exception:
            # The enforcement seam judges sandbox-written evidence, so it is
            # designed total — this guard is the last resort: a failure to
            # evaluate fails closed, the run still terminates diagnosably
            # instead of wedging open.
            status, result_text, usage, structured_output = (
                "ERROR",
                None,
                None,
                None,
            )
            error = run_error(
                "runtime_error",
                "turn outcome could not be evaluated",
                source="control",
                retryable=True,
            )
            contract_result = (
                {
                    "enforcement": str(contract.get("enforcement") or "strict"),
                    "schema_digest": contract.get("schema_digest"),
                    "status": "invalid",
                    "extraction": None,
                    "violations": [
                        {
                            "path": "$",
                            "code": "evaluation_error",
                            "message": "turn outcome could not be evaluated",
                        }
                    ],
                }
                if isinstance(contract, dict)
                else None
            )
        # Phase 2.5 (unlocked): a FINISHED verdict materializes its durable
        # Revision BEFORE the terminal ledger record is published — a
        # caller observing FINISHED must already resolve revisions/latest
        # (the materialize→finish gap was user-visible as a transient 404).
        # materialize() dedups by run_id so a reconcile/settle re-entry
        # replays rather than double-materializes. A run whose ledger
        # record already went terminal — a cancel landed first — does not
        # materialize: cancelled work must never reach a revision.
        if status == "FINISHED" and self.revision_hook is not None:
            ledger_open = True
            if self.run_ledger is not None:
                try:
                    prior = self.run_ledger.get(session_id, n)
                    ledger_open = prior is None or not prior.terminal
                except Exception:
                    ledger_open = True
            if ledger_open:
                self._materialize_revision(session_id, handle, n)

        # Phase 3 (locked): fold the evidence into the session record and
        # persist the terminal outcome. finish() is monotonic, so a cancel
        # recorded by a concurrent stop()/close() still wins over this late
        # success — the verdict computed unlocked cannot resurrect a run.
        eager = False
        with self._lock:
            rec = self._store_get(session_id)
            active = rec is not None and rec.status not in TERMINAL_STATUSES
            now = self.clock()
            if active and payload is not None:
                # The turn payload is sandbox-written evidence: corrupt
                # fields degrade individually, they never wedge the finish.
                try:
                    turn_usage = payload.get("usage")
                    rec.usage = merge_usage(
                        rec.usage, turn_usage if isinstance(turn_usage, dict) else None
                    )
                except (TypeError, ValueError, AttributeError):
                    pass
                try:
                    rec.turns = max(rec.turns, int(payload.get("n") or n))
                except (TypeError, ValueError):
                    rec.turns = max(rec.turns, n)
                message = payload.get("message") or ""
                if message and rec.current_turn_id == turn_id:
                    rec.messages.append(
                        {
                            "role": "assistant",
                            "text": str(message),
                            "turn_id": turn_id,
                            "ts": iso(now),
                        }
                    )
            if active:
                # With checkpoints wired, the record stays ``running``
                # (marker kept) through the eager checkpoint below: a
                # follow-up landing inside the scrub→snapshot→reattach
                # window must see the agent busy, and the public status is
                # honest — the turn is still settling. A crashed watcher
                # self-heals via reconcile_turn: the written turn payload
                # re-runs this finish (hooks are idempotent) and settles
                # the record.
                eager = (
                    rec.status == "running" and self.checkpoints is not None and handle is not None
                )
                if not eager:
                    if rec.current_turn_id == turn_id:
                        rec.current_turn_id = None
                        rec.current_turn_n = None
                    if rec.status == "running":
                        rec.status = "idle"
                rec.updated_at = now
                rec.last_activity_at = now
                self.store.put(rec)
            ledger_record = None
            if self.run_ledger is not None:
                # Persist the terminal outcome now, while turns/<n>.json may
                # still be readable; after teardown this record is the only
                # evidence.
                ledger_record = self.run_ledger.finish(
                    session_id,
                    n,
                    status=status,
                    result_text=result_text,
                    error=error,
                    usage=usage,
                    structured_output=structured_output,
                    contract_result=contract_result,
                )
            if transcript:
                self._persist_activity(session_id, n, transcript)
            if not eager:
                # The in-process watcher releases its reconcile gate with
                # the record settled; the eager path releases it after the
                # checkpoint settle below so reconcile_turn cannot re-enter
                # a finish this watcher is still completing.
                self._live.pop(session_id, None)

        # SOR-178: a git policy with ``auto_publish`` declares that a
        # successfully finished run publishes itself — the same publish path
        # as POST /git/publish (push + create-or-update PR). Best-effort
        # like the credential write-back below: publish() persists any
        # failure as ``publish_error`` on the workspace record, so the
        # FINISHED verdict is never rewritten and never silent either.
        # Auto-delivery only when the durable verdict is FINISHED too:
        # finish() is monotonic, so a cancel that won the finish race keeps
        # the record terminal — cancelled work must never reach a
        # revision/branch/PR. The revision itself already materialized in
        # phase 2.5 (before the verdict published); publish stays gated on
        # the durable FINISHED.
        if status == "FINISHED":
            if ledger_record is None or ledger_record.status == "FINISHED":
                self._auto_publish_git(session_id, handle)
            elif self.revisions is not None:
                # A cancel won the finish race after the revision already
                # materialized — the run's work product must not stay
                # deliverable.
                try:
                    self.revisions.void_for_run(
                        session_id,
                        f"run-{n}",
                        code="run_cancelled",
                        message="run verdict settled non-FINISHED after materialization",
                    )
                except Exception:
                    pass

        # SOR-147: harvest refreshed credential files after every turn — a
        # provider CLI that rotated its OAuth token mid-turn (incl. an
        # auth_invalid failure) writes the new blob back to the account
        # store, CAS-guarded by this session's base fingerprint.
        self._writeback_credentials(rec, handle)

        # SOR-180: take the durable checkpoint eagerly at turn-end, while
        # the sandbox is provably alive — an idle agent with no checkpoint
        # is one platform loss away from terminal ``lost``. The record
        # stays ``running`` until the checkpoint settles so a follow-up
        # cannot dispatch onto the credential-scrubbed window, and a
        # crashed watcher re-enters this finish from the turn payload.
        if eager:
            checkpoints = self.checkpoints
            outcome = "none"
            try:
                outcome = checkpoints.checkpoint_live(rec, handle)
            except Exception:
                outcome = "none"
            suspend = outcome == "suspend"
            if suspend:
                # Durable checkpoint but the live sandbox lost its
                # credentials irrecoverably — release it; the next turn
                # restores from the checkpoint instead.
                try:
                    self.backend.terminate(handle)
                except Exception:
                    pass
            with self._lock:
                stored = self._store_get(session_id)
                if stored is not None and stored.current_turn_id == turn_id:
                    stored.current_turn_id = None
                    stored.current_turn_n = None
                    if stored.status == "running":
                        stored.status = "suspended" if suspend else "idle"
                    stored.updated_at = self.clock()
                    self.store.put(stored)
                self._live.pop(session_id, None)

        # SOR-224: the turn ended — the agent is idle again, so the head of
        # the durable queue (if any) dispatches next. Best-effort: a queue
        # failure must not wedge the watcher that already persisted the
        # terminal verdict.
        try:
            self.drain_queued(session_id)
        except Exception:
            pass

    def _auto_publish_git(self, session_id: str, handle: Any) -> None:
        """Best-effort automatic publish on run success; swallows failure.

        The durable policy is the trigger — only ``git.auto_publish``
        declared at agent create fires. ``publish()`` already persists
        ``publish_error`` on the workspace record for real failures, so
        this hook only guards against unforeseen ones.
        """
        if self.workspaces is None or handle is None:
            return
        record = self.workspaces.get(session_id)
        git = (record.git or {}) if record is not None else {}
        # SOR-224: ``auto_publish`` is the only automatic trigger —
        # ``auto_create_pr``/``merge`` declare *steps* a publish performs,
        # and stay explicit-only (POST /git/publish or the task delivery
        # endpoint), per the SOR-128 policy contract.
        if record is None or not git.get("auto_publish"):
            return
        try:
            self.workspaces.publish(handle, session_id)
        except Exception:
            pass
        # SOR-225: mirror the publish outcome onto the durable revision —
        # first-class delivery state, not only ``workspace.publish_error``.
        if self.revisions is not None:
            try:
                revision = self.revisions.latest(session_id)
                record = self.workspaces.get(session_id)
                if revision is not None and record is not None:
                    self.revisions.sync_delivery(revision, record)
            except Exception:
                pass

    def _materialize_revision(self, session_id: str, handle: Any, n: int) -> None:
        """Materialize the durable revision for a finished turn (SOR-225).

        Best-effort like the snapshot hook: the hook itself records snapshot
        failures as ``materialization_failed`` revisions; anything it cannot
        record is swallowed rather than rewriting the run verdict.
        """
        hook = self.revision_hook
        if hook is None or handle is None:
            return
        try:
            with self._lock:
                rec = self._store_get(session_id)
            if rec is None:
                return
            hook(rec, handle, n)
        except Exception:
            pass

    def _writeback_credentials(self, rec: SessionRecord | None, handle: Any) -> None:
        """Best-effort credential write-back; swallows every failure.

        The store commit and managed-Secret refresh happen inside
        ``CredentialSync``; here we only fold a committed fingerprint into
        ``cred_base_fp`` so the same session's next write-back compares
        against the credential it just committed.
        """
        sync = self.credential_sync
        if sync is None or rec is None or handle is None:
            return
        try:
            outcome = sync.writeback(
                backend=self.backend,
                handle=handle,
                runner_cmd=self.runner_cmd,
                tags=rec.sandbox_tags,
            )
        except Exception:
            return
        if outcome is None or outcome.code != "committed" or not outcome.fingerprint:
            return
        with self._lock:
            stored = self._store_get(rec.id)
            if stored is not None and stored.status not in TERMINAL_STATUSES:
                stored.sandbox_tags[TAG_CRED_BASE_FP] = outcome.fingerprint
                self.store.put(stored)

    def reconcile_turn(self, session_id: str) -> bool:
        # A read, cron, and mutation can discover the same orphaned turn.
        # Only one may finish it: duplicate eager checkpoints would scrub
        # and reattach credentials concurrently on the same sandbox.
        with self._reconcile_lock:
            if session_id in self._reconciling:
                return False
            self._reconciling.add(session_id)
        try:
            return self._reconcile_turn(session_id)
        finally:
            with self._reconcile_lock:
                self._reconciling.discard(session_id)

    def _reconcile_turn(self, session_id: str) -> bool:
        """Settle a ``running`` record whose in-process watcher is gone (SOR-139).

        A control-plane restart or deploy cutover drains the container and its
        ``_watch_turn`` threads; the session record then stays ``running``
        forever — refusing follow-ups and publish — even when the provider
        already wrote ``turns/<n>.json``. The settle is evidence-gated: only a
        readable turn payload proves completion, so a turn still executing on
        the live sandbox (its watcher lives on the drained container, or it
        is genuinely wedged — the reaper's ``run_grace_s`` bound owns that
        case) is left alone. The fold is ``_finish_turn`` itself, so the
        reconciler persists identical session/ledger truth to the watcher's
        own persist and stays monotonic — a late watcher cannot rewrite a
        reconciled terminal.

        Returns ``True`` when the turn was settled from evidence.
        """
        with self._lock:
            if session_id in self._live:
                return False
            rec = self._store_get(session_id)
            if (
                rec is None
                or rec.status != "running"
                or rec.current_turn_n is None
                or rec.current_turn_id is None
            ):
                return False
            n = int(rec.current_turn_n)
            turn_id = rec.current_turn_id
            handle = rec.handle()
        if handle is None:
            return False
        # Stamp before the remote probe — ``maybe_reconcile_turn`` reads
        # this as "a probe ran recently"; a failed probe counts the same
        # so a wedged sandbox can't turn the read path into a retry loop.
        self._reconcile_at[session_id] = time.monotonic()
        if len(self._reconcile_at) > 4096:
            cutoff = time.monotonic() - max(_RECONCILE_COOLDOWN_S * 10, 60.0)
            self._reconcile_at = {key: at for key, at in self._reconcile_at.items() if at > cutoff}
        try:
            if not self.backend.poll(handle).alive:
                # Dead sandbox: the reaper's lost/timed_out transition owns it.
                return False
            payload = read_json(self.backend, handle, f"turns/{n}.json")
        except Exception:
            return False
        if payload is None:
            return False
        self._finish_turn(session_id, turn_id, n)
        return True

    def maybe_reconcile_turn(self, session_id: str) -> bool:
        """Bounded read-path settle; slow sandbox work continues off-request.

        Cooldown alone does not bound a poll, evidence read, or eager
        checkpoint. Keep at most four probes in flight, with no unbounded
        queue, and let reads return the durable status after a short wait.
        Fast local probes retain immediate convergence.
        """
        done = threading.Event()
        result = False
        with self._reconcile_lock:
            if (
                session_id in self._live
                or session_id in self._reconciling
                or session_id in self._read_reconcile_pending
                or len(self._read_reconcile_pending) >= _READ_RECONCILE_WORKERS
                or time.monotonic() - self._reconcile_at.get(session_id, 0.0)
                < _RECONCILE_COOLDOWN_S
            ):
                return False
            self._read_reconcile_pending.add(session_id)
            self._reconcile_at[session_id] = time.monotonic()
            if self._read_reconcile_pool is None:
                self._read_reconcile_pool = ThreadPoolExecutor(
                    max_workers=_READ_RECONCILE_WORKERS,
                    thread_name_prefix="sbx-reconcile",
                )

        def probe() -> None:
            nonlocal result
            try:
                result = self.reconcile_turn(session_id)
            except Exception:
                pass
            finally:
                with self._reconcile_lock:
                    self._read_reconcile_pending.discard(session_id)
                done.set()

        try:
            self._read_reconcile_pool.submit(probe)
        except Exception:
            with self._reconcile_lock:
                self._read_reconcile_pending.discard(session_id)
            return False
        return result if done.wait(_READ_RECONCILE_WAIT_S) else False

    def reconcile_turns(self) -> list[str]:
        """Settle every watcher-less ``running`` session from turn evidence.

        A cron/reaper plane owns no watchers (``_live`` is per-process), so
        every ``running`` record is a candidate; only positive
        ``turns/<n>.json`` evidence finalizes. Run before the reaper so a
        provider success lands FINISHED + idle instead of ``lost`` when the
        container died mid-watch. Every stage is failure-isolated: a
        throwing remote read yields what it could settle, never an
        exception — the cron caller must reach the reaper every tick
        (SOR-271 round-3 zombie finding).
        """
        settled: list[str] = []
        try:
            records = self.store.list_all()
        except Exception:
            return settled
        for rec in records:
            try:
                reconciled = rec.status == "running" and self.reconcile_turn(rec.id)
            except Exception:
                # One unreadable record must not starve the rest of the
                # sweep (or the reaper downstream of this call).
                continue
            if reconciled:
                settled.append(rec.id)
            elif rec.status == "idle":
                # SOR-224: queued turns outlive a control-plane restart
                # (ledger + session messages are durable); an idle agent
                # with parked work dispatches its queue head here.
                try:
                    self.drain_queued(rec.id)
                except Exception:
                    pass
        return settled

    def settle_orphaned_runs(self, session_id: str, *, session_status: str) -> list[int]:
        """Persist a terminal verdict for a terminal session's open runs.

        The reaper closes watcher-less sessions (``lost`` / ``timed_out``)
        whose runs never produced readable evidence; without this their
        ledger records stay open forever — read back as UNKNOWN, a perpetual
        non-answer. The session's terminal state is the durable truth: each
        still-open run is persisted to the matching terminal outcome — never
        FINISHED without evidence.
        """
        if self.run_ledger is None:
            return []
        if session_status in ("timed_out", "lost"):
            status = "EXPIRED"
            verdict = run_error_for_run("EXPIRED", agent_status=session_status)
        else:
            status = "ERROR"
            verdict = run_error_for_run("ERROR")
        error = verdict.public() if verdict is not None else None
        settled: list[int] = []
        for record in self.run_ledger.list(session_id):
            if record.terminal:
                continue
            self.run_ledger.finish(session_id, record.n, status=status, error=error)
            settled.append(record.n)
        return settled

    def stop(self, session_id: str) -> str:
        # Settle evidence first: a turn the provider already finished must not
        # be rewritten to CANCELLED by a stop landing after its watcher died.
        self.reconcile_turn(session_id)
        with self._lock:
            rec = self._store_get(session_id)
            if rec is None:
                raise KeyError(session_id)
            self._first_turn_pending.discard(session_id)
            handle = rec.handle()
            live = self._live.pop(session_id, None)
            if rec.status == "running":
                if self.run_ledger is not None and rec.current_turn_n is not None:
                    # Persist CANCELLED before the session goes idle: a late
                    # _finish_turn can then never flip the run to FINISHED.
                    self.run_ledger.cancel(session_id, int(rec.current_turn_n))
                rec.status = "idle"
                rec.current_turn_id = None
                rec.current_turn_n = None
                rec.updated_at = self.clock()
                rec.last_activity_at = rec.updated_at
                self.store.put(rec)
            rec = self._store_get(session_id) or rec
            if rec.status == "suspended":
                # Already released for checkpoint recovery — the public
                # equivalent of an idle, runnable agent.
                final = "idle"
            else:
                final = rec.status or "closed"
        # SOR-268: the remote tail (turn proc kill + runner stop hook +
        # queued drain) converges on the bounded teardown pool — the
        # durable cancel is already persisted, so the caller ACKs without
        # waiting on seconds of provider calls, and a bulk cancel cannot
        # serialize the plane through remote work held under ``_lock``.
        done = self._submit_teardown(
            lambda: self._converge_stop(session_id, live=live, handle=handle, final=final)
        )
        done.wait(_TEARDOWN_ACK_S)
        return final

    def _converge_stop(
        self,
        session_id: str,
        *,
        live: LiveTurn | None,
        handle: SandboxHandle | None,
        final: str,
    ) -> None:
        """Remote tail of ``stop`` on a teardown worker; all best-effort
        because the ledger cancel + record update are already durable."""
        try:
            if live is not None:
                try:
                    live.proc.kill()
                except Exception:
                    pass
            if handle is not None:
                try:
                    stop = self.backend.exec(handle, self.runner("stop"), env=sandbox_env(handle))
                    drain(stop)
                except Exception:
                    # Best-effort hook only: the ledger cancel + proc kill
                    # above are the real stop; a dead sandbox has nothing
                    # left to run it on and must not mask the cancel.
                    pass
            # SOR-224: a cancelled run frees the agent — the next queued
            # turn (if any) dispatches now. Durable queue entries already
            # cancelled are skipped by the drain.
            if final == "idle":
                try:
                    self.drain_queued(session_id)
                except Exception:
                    pass
        except Exception:
            pass

    def close(self, session_id: str) -> SessionRecord:
        # Reconcile before cancelling open runs: a provider success must land
        # FINISHED, not CANCELLED, when the watcher died ahead of the close.
        self.reconcile_turn(session_id)
        with self._lock:
            rec = self._store_get(session_id)
            if rec is None:
                raise KeyError(session_id)
            self._first_turn_pending.discard(session_id)
            live = self._live.pop(session_id, None)
            handle = rec.handle()
            self._run_meta.pop(session_id, None)
            now = self.clock()
            rec.status = "closed"
            rec.ended_at = now
            rec.updated_at = now
            rec.current_turn_id = None
            rec.current_turn_n = None
            self.store.put(rec)
        # SOR-268: the remote tail (open-run cancels, turn proc kill,
        # workspace snapshot, checkpoint discard, credential write-back,
        # sandbox terminate) converges on the bounded teardown pool. The
        # run cancels re-list *after* the closed mark landed: a begin
        # racing ``close`` either lost the lock (refused by the terminal
        # check) or committed first and is seen by the re-list. This is
        # what keeps a bulk close from wedging the control plane for
        # minutes — the wedge was remote Modal calls held under ``_lock``.
        done = self._submit_teardown(
            lambda: self._converge_close(session_id, rec=rec, live=live, handle=handle)
        )
        done.wait(_TEARDOWN_ACK_S)
        return rec

    def _converge_close(
        self,
        session_id: str,
        *,
        rec: SessionRecord,
        live: LiveTurn | None,
        handle: SandboxHandle | None,
    ) -> None:
        """Remote tail of ``close`` on a teardown worker; all steps
        best-effort — the record is already terminal and the reaper
        retries a failed terminate (SOR-80)."""
        try:
            if self.run_ledger is not None:
                try:
                    # Runs still open can never complete once the sandbox
                    # is terminated; finalize them as CANCELLED so they
                    # stay truthful after teardown. Fresh list: an open
                    # run that committed just before the close mark must
                    # be seen here, never left open.
                    for record in self.run_ledger.list_fresh(session_id):
                        if not record.terminal:
                            self.run_ledger.cancel(session_id, record.n, message="agent closed")
                except Exception:
                    pass
            if live is not None:
                try:
                    live.proc.kill()
                except Exception:
                    pass
            if handle is not None and self.snapshot_hook is not None:
                try:
                    self.snapshot_hook(rec, handle)
                except Exception:
                    pass
            # SOR-180: the agent is gone for good — drop its checkpoint
            # record so it can never be restored past close.
            checkpoints = self.checkpoints
            if checkpoints is not None:
                try:
                    checkpoints.discard(session_id)
                except Exception:
                    pass
            # SOR-147: last write-back before teardown — a CLI-rotated
            # credential must not die with the sandbox.
            self._writeback_credentials(rec, handle)
            if handle is not None:
                try:
                    self.backend.terminate(handle)
                except Exception:
                    # The record is already terminal and keeps
                    # sandbox_id/root/tags, so the reaper retries the
                    # terminate (SOR-80) while the caller can still
                    # release capacity (slots, leases).
                    pass
        except Exception:
            pass


def format_sse(event_id: int, payload: dict[str, Any]) -> str:
    return (
        f"id: {event_id}\n"
        f"event: {payload.get('type', 'message')}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n"
        "\n"
    )
