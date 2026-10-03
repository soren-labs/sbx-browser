"""Modal-only bootstrap wiring for the P2 real /v1 gate.

Nothing is enabled by default. When ``SBX_V1_BOOTSTRAP_KEY`` is injected via
a Modal Secret, seed a hash-only API key plus accounts for the deploy-selected
P2 Core providers (``SBX_PROVIDERS``) so ``/v1`` never schedules an image or
credential mount that was intentionally omitted. A selected provider seeds
one account by default; ``SBX_<PROVIDER>_ACCOUNTS`` (a JSON list of ``{"id",
"label"?, "secret_name"?, "slots"?, "models"?}``) seeds a real multi-account
fleet — the Antigravity-4 / Grok-2 gate shape.

Scheduling runs on the SOR-63/D1 implementation: accounts persist in a
``PersistentAccountRegistry`` (``select_store()`` — ``modal.Dict
sbx-accounts`` on Modal, the local file store elsewhere) and
``AccountScheduler`` provides the atomic ``decide``/``acquire``/
``report_failure`` surface the /v1 routes consume behind
``app.state.scheduler``. Per-account slots come from ``Account.max_concurrent``
and the global cap from ``SBX_MAX_CONCURRENT``.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from control.accounts import PersistentAccountRegistry, select_store
from control.auth_store import BootstrapApiKeyStore, configure_auth
from control.config import account_secret_prefix, env_int, selected_providers
from control.ports import Account
from control.scheduler import AccountScheduler, session_running_source

# Flat per-account slot cap for non-Devin providers; Devin's seeded account
# takes ``SBX_DEVIN_BURST_SLOTS`` (the old tiered pool's ceiling). Overridable
# per provider via ``SBX_<PROVIDER>_SLOTS``.
_DEFAULT_PROVIDER_SLOTS = 4

# Default advertised models per provider; ``SBX_<PROVIDER>_MODELS`` overrides
# (comma-separated). Informational only — scheduling does not gate on models.
# ``/v1`` also uses the first entry as the default model when the resolved
# account advertises none and ``AgentSpec.model`` is omitted.
PROVIDER_DEFAULT_MODELS = {
    "codex": ("gpt-5.6-luna",),
    "devin": ("swe-2-high", "swe-2-medium"),
    "antigravity": ("gemini-3.8-flash-low",),
    "grok": ("grok-4.6",),
    # SOR-96: OpenCode models travel as ``provider/model`` argv (``-m``).
    # The default pair covers the two auth channels the gate account uses
    # (OpenAI OAuth subscription + the OpenCode Zen API key) with ids that
    # resolve on those channels — ``anthropic/claude-sonnet-4.5`` and
    # ``openai/gpt-5.3-codex`` do not exist there (real ids use ``-4-5`` /
    # the ``opencode`` zen prefix). A deploy overrides via
    # ``SBX_OPENCODE_MODELS``.
    "opencode": ("openai/gpt-5.6-luna", "opencode/claude-sonnet-4-5"),
}


def _models(provider: str) -> tuple[str, ...]:
    raw = os.environ.get(f"SBX_{provider.upper()}_MODELS")
    if raw is None:
        return PROVIDER_DEFAULT_MODELS[provider]
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _upsert_seeded(registry: PersistentAccountRegistry, account: Account) -> Account:
    """Insert or refresh a seeded account without clobbering runtime state.

    Bootstrap runs on every control-plane boot and the registry is durable
    (file store / ``modal.Dict``), so an existing record's health and LRU
    fields must survive re-seeding — otherwise each restart silently
    re-enables ``disabled``/``invalid`` accounts and cancels in-flight
    cooldowns. Config fields (label/secret_name/slots/models) refresh from
    env. A record re-seeded under a different provider is a different
    logical account and is replaced wholesale; a corrupt record (decoded
    as provider-less ``disabled``) is healed by the fresh seed.
    """
    existing = registry.get(account.id)
    if existing is not None and existing.provider == account.provider:
        account = replace(
            account,
            status=existing.status,
            cooldown_until=existing.cooldown_until,
            last_error=existing.last_error,
            last_used_at=existing.last_used_at,
            created_at=existing.created_at or account.created_at,
        )
    registry.put(account)
    return account


def _seed_account(
    registry: PersistentAccountRegistry,
    provider: str,
    *,
    default_secret_name: str | None,
    default_slots: int,
    label: str,
    created_at: str,
) -> str:
    """Seed one ``unverified`` account for ``provider``; return the id.

    SOR-216 verified-only lifecycle: seeding no longer fabricates a
    scheduler-eligible account — the record starts ``unverified`` and only
    a passing cloud verify (``/v1/accounts/{id}/verify`` or
    ``OnboardingService.verify`` with an authoritative probe) promotes it
    to ``active``.

    ``SBX_<PROVIDER>_ACCOUNT_ID`` / ``SBX_<PROVIDER>_SECRET_NAME`` /
    ``SBX_<PROVIDER>_SLOTS`` override the defaults. ``default_secret_name`` of
    ``None`` resolves to the ``<prefix><account_id>`` convention
    (``SBX_ACCOUNT_SECRET_PREFIX``); ``""`` attaches no per-account Secret
    (the provider's default credential path).
    """
    prefix = f"SBX_{provider.upper()}"
    account_id = (os.environ.get(f"{prefix}_ACCOUNT_ID") or f"{provider}-1").strip()
    raw_secret = os.environ.get(f"{prefix}_SECRET_NAME")
    if raw_secret is not None:
        secret_name = raw_secret.strip()
    elif default_secret_name is None:
        secret_name = f"{account_secret_prefix()}{account_id}"
    else:
        secret_name = default_secret_name
    slots = env_int(f"{prefix}_SLOTS", default_slots)
    _upsert_seeded(
        registry,
        Account(
            id=account_id,
            provider=provider,
            label=label,
            status="unverified",
            max_concurrent=slots,
            secret_name=secret_name,
            models=_models(provider),
            created_at=created_at,
        ),
    )
    return account_id


def _seed_accounts(
    registry: PersistentAccountRegistry,
    provider: str,
    *,
    default_secret_name: str | None,
    default_slots: int,
    label: str,
    created_at: str,
) -> list[Account]:
    """Seed ``provider``'s accounts; return them in seed order.

    ``SBX_<PROVIDER>_ACCOUNTS`` — a JSON list of ``{"id", "label"?,
    "secret_name"?, "slots"?, "models"?}`` — seeds a multi-account pool
    (the Antigravity-4 / Grok-2 gate shape). Per entry, ``secret_name``
    defaults to the ``<prefix><id>`` convention (or ``default_secret_name``
    when the provider overrides it), ``slots`` to ``default_slots``,
    ``models`` to the provider defaults. Without the JSON var, falls back to
    the single-account ``SBX_<PROVIDER>_ACCOUNT_ID`` path.
    """
    prefix = f"SBX_{provider.upper()}"
    raw = os.environ.get(f"{prefix}_ACCOUNTS")
    if raw is None:
        account_id = _seed_account(
            registry,
            provider,
            default_secret_name=default_secret_name,
            default_slots=default_slots,
            label=label,
            created_at=created_at,
        )
        account = registry.get(account_id)
        assert account is not None
        return [account]
    try:
        specs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{prefix}_ACCOUNTS is not valid JSON: {exc}") from exc
    if not isinstance(specs, list) or not specs:
        raise ValueError(f"{prefix}_ACCOUNTS must be a non-empty JSON list")
    seeded: list[Account] = []
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict) or not str(spec.get("id") or "").strip():
            raise ValueError(f"{prefix}_ACCOUNTS[{index - 1}] needs a non-empty 'id'")
        account_id = str(spec["id"]).strip()
        raw_secret = spec.get("secret_name")
        if raw_secret is not None:
            secret_name = str(raw_secret).strip()
        elif default_secret_name is None:
            secret_name = f"{account_secret_prefix()}{account_id}"
        else:
            secret_name = default_secret_name
        raw_models = spec.get("models")
        models = (
            tuple(str(m).strip() for m in raw_models if str(m).strip())
            if isinstance(raw_models, list)
            else _models(provider)
        )
        slots = spec.get("slots")
        account = Account(
            id=account_id,
            provider=provider,
            label=str(spec.get("label") or f"{label} {index}"),
            status="unverified",
            max_concurrent=int(slots) if slots is not None else default_slots,
            secret_name=secret_name,
            models=models,
            created_at=created_at,
        )
        seeded.append(_upsert_seeded(registry, account))
    return seeded


def _migrate_verified_gate(registry: PersistentAccountRegistry) -> list[str]:
    """Demote ``active`` accounts that carry no cloud-verify evidence.

    SOR-216 migration: accounts seeded before the verified-only lifecycle
    shipped were persisted as ``active`` without ever passing a verify
    probe. On boot they must leave the scheduler pool — a nonconforming
    id is left untouched (the store lanes already refuse it). Returns the
    demoted ids; never touches cooling/invalid/disabled/unverified.
    """
    from control.credlifecycle import CredentialLifecycleService

    lifecycle = CredentialLifecycleService(registry)
    demoted: list[str] = []
    for account in registry.list():
        if account.status != "active":
            continue
        try:
            if lifecycle.verified(account.id):
                continue
        except Exception:
            pass  # no lifecycle lane → no evidence → demote
        try:
            registry.mark_status(
                account.id,
                "unverified",
                cooldown_until=account.cooldown_until,
                last_error=account.last_error,
            )
            demoted.append(account.id)
        except (KeyError, ValueError):
            pass
    return demoted


def configure_v1_bootstrap(app: Any) -> bool:
    """Seed the P2 production gate from env; return whether it was enabled.

    The bearer plaintext is never attached to app state. Only its hash enters
    an operator-only overlay over the durable product key store. No User is
    created and bootstrap rotation cannot leave old product keys behind.
    Account credentials stay in the per-account
    Modal Secrets referenced by name (``sbx-acct-<account_id>``) or in the
    registry's credential-blob store; the seeded codex account carries no
    Secret name so sandboxes keep the default ``CODEX_AUTH_JSON`` credential
    path.
    """
    token = (os.environ.get("SBX_V1_BOOTSTRAP_KEY") or "").strip()
    if not token:
        return False

    configure_auth(app)
    key_store = app.state.api_key_store
    if isinstance(key_store, BootstrapApiKeyStore):
        key_store = key_store.store
    app.state.api_key_store = BootstrapApiKeyStore(key_store, token)

    registry = PersistentAccountRegistry(select_store())
    created_at = datetime.now(UTC).isoformat()

    # Devin's seeded slots take the old burst ceiling (SBX_DEVIN_BURST_SLOTS);
    # codex / antigravity / grok take SBX_<PROVIDER>_SLOTS or the flat
    # per-account default. The codex account defaults to an empty Secret
    # name — its sandboxes keep the ``sbx-codex-auth`` / ``CODEX_AUTH_JSON``
    # credential path (``SBX_CODEX_SECRET_NAME`` can name a per-account
    # Secret instead). ``SBX_<PROVIDER>_ACCOUNTS`` JSON switches any provider
    # to a multi-account fleet. Only selected providers seed — a disabled
    # provider's accounts would reference Secrets the deployment never
    # required (SOR-116).
    provider_specs = (
        ("devin", None, env_int("SBX_DEVIN_BURST_SLOTS", 8)),
        ("codex", "", _DEFAULT_PROVIDER_SLOTS),
        ("antigravity", None, _DEFAULT_PROVIDER_SLOTS),
        ("grok", None, _DEFAULT_PROVIDER_SLOTS),
        ("opencode", None, _DEFAULT_PROVIDER_SLOTS),
    )
    enabled = selected_providers()
    for provider, default_secret_name, default_slots in provider_specs:
        if provider not in enabled:
            continue
        _seed_accounts(
            registry,
            provider,
            default_secret_name=default_secret_name,
            default_slots=default_slots,
            label=f"P2 {provider}",
            created_at=created_at,
        )

    _migrate_verified_gate(registry)
    app.state.account_registry = registry
    # Derive per-account running counts from the sessions store so slots stay
    # truthful across control-plane restarts (design v2 §3.3): in-process
    # leases alone forget sessions whose sandboxes are still live.
    session_store = getattr(getattr(app.state, "plane", None), "store", None)
    app.state.scheduler = AccountScheduler(
        registry,
        providers=enabled,
        external_running=(
            session_running_source(session_store) if session_store is not None else None
        ),
    )
    return True
