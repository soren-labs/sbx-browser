"""In-memory port implementations + per-app v1 state (SOR-64).

P2-C (SOR-63) will wire ``modal.Dict``-backed ``AccountRegistry`` /
``ApiKeyStore`` / ``Scheduler`` onto ``app.state``. Until then the v1 router
falls back to the in-memory implementations here so local mode works and tests
can substitute ``tests.fakes.fake_ports`` fakes via ``app.state``.

``V1State`` carries what ``SessionRecord`` does not yet persist (P2-C adds
``provider`` / ``account_id`` to the record): per-agent provider / account /
name, plus runs cancelled through ``/v1`` (P1 ``stop`` leaves no per-turn
marker).
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from control.api_v1.lifecycle import IdempotencyStore, InMemoryRunStates, RunStateStore
from control.config import TERMINAL_STATUSES
from control.ports import Account, ApiKey, ScheduleDecision
from control.workflow_store import InMemoryWorkflowStore, WorkflowStore

PROVIDERS: tuple[str, ...] = ("codex", "antigravity", "grok", "opencode", "devin")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


class InMemoryAccountRegistry:
    """Dict-backed ``ports.AccountRegistry`` fallback (mirrors the WP0 fake).

    ``bind_running`` lets an installed ``AccountScheduler`` report live
    lease counts through ``running_count`` (SOR-63); without a bound source
    the local ``set_running`` counter answers.
    """

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._blobs: dict[str, dict[str, Any]] = {}
        self._lifecycle: dict[str, dict[str, Any]] = {}
        self._running: dict[str, int] = {}
        self._running_src: Any = None
        self._lock = threading.Lock()

    def bind_running(self, source: Any) -> None:
        """Use ``source`` (``account_id -> live count``) for ``running_count``."""
        self._running_src = source

    def list(self, provider: str | None = None) -> list[Account]:
        with self._lock:
            out = list(self._accounts.values())
        if provider is not None:
            out = [a for a in out if a.provider == provider]
        return sorted(out, key=lambda a: a.id)

    def get(self, account_id: str) -> Account | None:
        with self._lock:
            return self._accounts.get(account_id)

    def put(self, account: Account) -> None:
        with self._lock:
            self._accounts[account.id] = account

    def mark_status(
        self,
        account_id: str,
        status: str,
        *,
        cooldown_until: str | None = None,
        last_error: str | None = None,
    ) -> Account:
        with self._lock:
            account = self._accounts.get(account_id)
            if account is None:
                raise KeyError(account_id)
            updated = replace(
                account,
                status=status,
                cooldown_until=cooldown_until,
                last_error=last_error,
            )
            self._accounts[account_id] = updated
            return updated

    def touch(self, account_id: str, used_at: str) -> None:
        with self._lock:
            account = self._accounts.get(account_id)
            if account is None:
                raise KeyError(account_id)
            self._accounts[account_id] = replace(account, last_used_at=used_at)

    def remove(self, account_id: str) -> None:
        with self._lock:
            self._accounts.pop(account_id, None)
            self._blobs.pop(account_id, None)
            self._lifecycle.pop(account_id, None)
            self._running.pop(account_id, None)

    def running_count(self, account_id: str) -> int:
        if self._running_src is not None:
            return self._running_src(account_id)
        with self._lock:
            return self._running.get(account_id, 0)

    def set_running(self, account_id: str, count: int) -> None:
        """Test helper mirroring the WP0 fake's ``set_running``."""
        with self._lock:
            self._running[account_id] = count

    def get_credential_blob(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            blob = self._blobs.get(account_id)
            return dict(blob) if blob is not None else None

    def put_credential_blob(self, account_id: str, blob: dict[str, Any]) -> None:
        with self._lock:
            self._blobs[account_id] = dict(blob)

    def get_credential_lifecycle(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._lifecycle.get(account_id)
            return dict(rec) if isinstance(rec, dict) else None

    def put_credential_lifecycle(self, account_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._lifecycle[account_id] = dict(record)


class InMemoryScheduler:
    """LRU ``auto`` pick over an ``AccountRegistry`` (design v2 §3.3)."""

    def __init__(self, registry: InMemoryAccountRegistry) -> None:
        self._registry = registry

    def decide(self, *, provider: str, account: str | None = "auto") -> ScheduleDecision:
        if provider not in PROVIDERS:
            return ScheduleDecision(error="invalid_provider")
        if account in (None, "auto"):
            candidates = [
                a
                for a in self._registry.list(provider)
                if a.status == "active" and self._registry.running_count(a.id) < a.max_concurrent
            ]
            if not candidates:
                return ScheduleDecision(error="provider_exhausted", retry_after=60.0)
            chosen = min(candidates, key=lambda a: a.last_used_at or "")
            return ScheduleDecision(account=chosen)
        named = self._registry.get(account)
        if named is None or named.provider != provider or named.status != "active":
            return ScheduleDecision(error="account_unavailable")
        if self._registry.running_count(named.id) >= named.max_concurrent:
            return ScheduleDecision(error="account_busy")
        return ScheduleDecision(account=named)


class InMemoryApiKeyStore:
    """Explicit test/demo helper; production defaults live in control.auth_store.

    Generates ``sbx_<hex>`` tokens; stores sha256 hashes only in process memory.
    """

    def __init__(self) -> None:
        self._keys: dict[str, ApiKey] = {}
        self._by_hash: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, *, label: str = "", scopes: Iterable[str] = ("agents",)) -> tuple[ApiKey, str]:
        token = f"sbx_{secrets.token_hex(20)}"
        record = ApiKey(
            id=f"key_{uuid.uuid4().hex[:12]}",
            key_hash=self._hash(token),
            label=label,
            scopes=tuple(scopes),
            created_at=_iso_now(),
        )
        with self._lock:
            self._keys[record.id] = record
            self._by_hash[record.key_hash] = record.id
        return record, token

    def seed(
        self,
        token: str,
        *,
        label: str = "bootstrap",
        scopes: Iterable[str] = ("agents", "admin"),
    ) -> ApiKey:
        """Install a pre-generated bearer token without ever storing plaintext.

        Used only by the Modal control-plane bootstrap path: the plaintext
        token lives in a Modal Secret, while this store keeps its sha256 just
        like keys minted through ``create``. Re-seeding is idempotent.
        """
        if not token.startswith("sbx_"):
            raise ValueError("bootstrap API key must use the sbx_ prefix")
        digest = self._hash(token)
        record = ApiKey(
            id=f"key_bootstrap_{digest[:12]}",
            key_hash=digest,
            label=label,
            scopes=tuple(scopes),
            created_at=_iso_now(),
        )
        with self._lock:
            existing_id = self._by_hash.get(digest)
            if existing_id is not None and existing_id in self._keys:
                return self._keys[existing_id]
            self._keys[record.id] = record
            self._by_hash[digest] = record.id
        return record

    def list(self) -> list[ApiKey]:
        with self._lock:
            return sorted(self._keys.values(), key=lambda k: k.id)

    def lookup(self, token: str) -> ApiKey | None:
        with self._lock:
            key_id = self._by_hash.get(self._hash(token))
            if key_id is None:
                return None
            record = self._keys.get(key_id)
        if record is None or record.revoked_at is not None:
            return None
        return record

    def revoke(self, key_id: str) -> bool:
        with self._lock:
            record = self._keys.get(key_id)
            if record is None or record.revoked_at is not None:
                return False
            self._keys[key_id] = replace(record, revoked_at=_iso_now())
            return True


CONSOLE_GRANT_TTL_S = 120


class ConsoleGrantStore:
    """One-time browser-admin handoff tickets (SOR-211).

    ``sbx open`` mints a ticket via ``POST /v1/console/grant`` (admin scope)
    and hands it to the browser inside the URL fragment — which is never
    sent to the server, so the ticket never appears in access logs. The
    Console redeems it once via ``POST /v1/console/exchange`` for a fresh
    ``sbx_`` API key. Tickets are stored hashed (like API keys), expire in
    ~2 minutes, and are single-use: ``consume`` pops unconditionally, so a
    replayed or guessed ticket never yields a second key. The long-lived
    bootstrap key is never placed in a URL.
    """

    def __init__(self) -> None:
        self._grants: dict[str, float] = {}  # sha256(ticket) → epoch expiry
        self._lock = threading.Lock()

    @staticmethod
    def _hash(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        for digest, expiry in list(self._grants.items()):
            if expiry <= now:
                del self._grants[digest]

    def create(self, ttl_s: float = CONSOLE_GRANT_TTL_S) -> tuple[str, float]:
        """Mint a ticket; return ``(ticket, expires_at_epoch)``."""
        ticket = f"sbxg_{secrets.token_urlsafe(24)}"
        now = time.time()
        expires_at = now + max(float(ttl_s), 1.0)
        with self._lock:
            self._prune(now)
            self._grants[self._hash(ticket)] = expires_at
        return ticket, expires_at

    def consume(self, ticket: str, *, now: float | None = None) -> bool:
        """Redeem a ticket exactly once; True only when unexpired."""
        if not ticket:
            return False
        now = time.time() if now is None else now
        with self._lock:
            self._prune(now)
            expiry = self._grants.pop(self._hash(ticket), None)
        return expiry is not None and now <= expiry


@dataclass
class AgentMeta:
    """Fields ``SessionRecord`` does not yet persist (added by P2-C)."""

    provider: str = "codex"
    account_id: str = "auto"
    name: str | None = None
    idle_timeout_s: int | None = None
    # SOR-129: declared session-resource refs (``{"secrets": [...],
    # "mcp": [...]}``, names only) echoed on the agent view.
    resources: dict[str, Any] | None = None
    # SOR-179: canonical reasoning effort declared at create.
    reasoning_effort: str | None = None


@dataclass
class V1State:
    """Per-app v1 bookkeeping: agent metadata and v1-cancelled runs."""

    agents: dict[str, AgentMeta] = field(default_factory=dict)
    cancelled_runs: dict[str, set[int]] = field(default_factory=dict)
    leases: dict[str, Any] = field(default_factory=dict)
    run_states: RunStateStore = field(default_factory=InMemoryRunStates)
    idempotency: IdempotencyStore = field(default_factory=IdempotencyStore)
    # SOR-84 C1 fallback workflow index; ``app.state.workflow_store`` wins
    # when a durable store is installed (same seam shape as run_states).
    workflows: WorkflowStore = field(default_factory=InMemoryWorkflowStore)
    # SOR-211: one-time Console handoff tickets — inherently ephemeral, so
    # they live on V1State even when durable stores replace the rest.
    console_grants: ConsoleGrantStore = field(default_factory=ConsoleGrantStore)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Monotonic timestamp of the last ``reconcile_leases`` pass — throttle
    # state, not durable data (V1State is process-local by design).
    leases_reconciled_at: float = 0.0
    recovering_leases: set[str] = field(default_factory=set)
    lease_generations: dict[str, int] = field(default_factory=dict)

    def set_meta(self, session_id: str, meta: AgentMeta) -> None:
        with self.lock:
            self.agents[session_id] = meta

    def get_meta(self, session_id: str) -> AgentMeta | None:
        with self.lock:
            return self.agents.get(session_id)

    def set_lease(self, session_id: str, lease: Any) -> None:
        with self.lock:
            self.lease_generations[session_id] = self.lease_generations.get(session_id, 0) + 1
            self.leases[session_id] = lease

    def pop_lease(self, session_id: str) -> Any | None:
        with self.lock:
            return self.leases.pop(session_id, None)

    def reconcile_leases(self, store: Any, *, interval_s: float = 10.0) -> int:
        """Decay leases whose session no longer owns live work.

        ``AccountLease`` objects are in-process — the reaper cron runs in
        a different container and can only *ask* for a release it cannot
        execute here, so dead leases are also decayed lazily on the
        request path: a session that went terminal, ``lost``/
        ``timed_out``, or ``suspended`` (no live sandbox — recovery uses
        the durable checkpoint, not a held slot) must not pin a
        scheduler slot forever (SOR-271 round-5: every finished/cancelled
        session wedged capacity at the global cap). Rate-limited by
        ``interval_s`` so the bind path does not pay a store read per
        lease on every acquire.
        """
        if store is None:
            return 0
        from control.ownership import ScopedSessionStore

        # This is trusted scheduler housekeeping across all users, not a
        # resource read. A scoped miss must not release another user's lease.
        if isinstance(store, ScopedSessionStore):
            store = store.source
        now = time.monotonic()
        if now - self.leases_reconciled_at < interval_s:
            return 0
        self.leases_reconciled_at = now
        with self.lock:
            pending = [
                (sid, lease, self.lease_generations.get(sid, 0))
                for sid, lease in self.leases.items()
            ]
        released = 0
        for session_id, observed_lease, generation in pending:
            try:
                rec = store.get(session_id)
            except Exception:
                # Unreadable durable state — keep the lease this pass and
                # retry rather than free slots on a store blip.
                continue
            if (
                rec is not None
                and rec.status not in TERMINAL_STATUSES
                and (rec.status != "suspended")
            ):
                continue
            with self.lock:
                # Recovery may have reused this lease after our status read.
                # Its creating reservation has not necessarily landed yet.
                if (
                    session_id in self.recovering_leases
                    or self.lease_generations.get(session_id, 0) != generation
                    or self.leases.get(session_id) is not observed_lease
                ):
                    continue
                popped = self.leases.pop(session_id, None)
            if popped is None:
                continue
            release = getattr(popped, "release", None)
            if callable(release):
                release()
                released += 1
        return released

    def mark_cancelled(self, session_id: str, turn_n: int) -> None:
        with self.lock:
            self.cancelled_runs.setdefault(session_id, set()).add(turn_n)

    def cancelled(self, session_id: str) -> set[int]:
        with self.lock:
            return set(self.cancelled_runs.get(session_id, set()))
