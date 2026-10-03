"""Control-plane constants. Values come from contracts + P0 (SOR-28)."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "gpt-5.6-luna"
MAX_CONCURRENT = 2
# SOR-135: post-session idle retention — how long the control plane keeps
# an ``idle`` session's dev-cloud sandbox warm for a follow-up before the
# reaper reclaims it (``timed_out``). Cost knob only; it is NOT the native
# sandbox idle bound below.
IDLE_TIMEOUT_S = 300
# SOR-134/SOR-135: the Modal-native ``Sandbox.create(idle_timeout=...)``
# bound — the sandbox's own inactivity kill while a session is live
# (including mid-``running`` turn). Deliberately a separate resolved knob
# from the post-session retention so dropping the retention to 5 min can
# never shrink the bound a long turn depends on.
SANDBOX_IDLE_TIMEOUT_S = 1800
# SOR-80: a ``creating`` record without ``sandbox_id`` is an in-flight create;
# the reaper leaves it alone for this long before declaring it ``lost``.
CREATE_GRACE_S = 300
SANDBOX_TIMEOUT_S = 14400  # 4h hard cap
SSE_KEEPALIVE_S = 15.0
TURN_MAX_SECONDS = 900  # default; override with SBX_TURN_MAX_SECONDS
# SOR-80: a ``running`` record is finalized by the in-process watcher. When a
# control-plane cutover kills that watcher mid-turn, nothing ever closes the
# record — the reaper treats a turn stale beyond the runner's own
# --max-seconds bound plus this margin as stranded.
RUN_GRACE_S = 300
CPU = (1, 2)
MEMORY_MIB = (1024, 4096)
WORK_DIR = "/work"
CODEX_HOME = "/work/.codex"
MODAL_APP_NAME = "sbx-control"
SESSIONS_DICT_NAME = "sbx-sessions"
RUNS_DICT_NAME = "sbx-runs"
# Compacted per-run activity transcripts, replayed after sandbox teardown.
RUN_ACTIVITY_DICT_NAME = "sbx-run-activity"
ACCOUNTS_DICT_NAME = "sbx-accounts"
WORKFLOWS_DICT_NAME = "sbx-workflows"
CODEX_SECRET_NAME = "sbx-codex-auth"
BASIC_SECRET_NAME = "sbx-basic-auth"
V1_BOOTSTRAP_SECRET_NAME = "sbx-v1-bootstrap"
# Durable stores without a P0-era home: artifacts (SOR-83) and workspaces
# (SOR-83) live here so ``sbx.config`` can default to the same contract names
# without importing the store modules.
ARTIFACTS_DICT_NAME = "sbx-artifacts"
WORKSPACES_DICT_NAME = "sbx-workspaces"
# SOR-127: environment build/snapshot cache records (last-known-good builds).
ENVIRONMENTS_DICT_NAME = "sbx-environments"
# SOR-177: GitHub App installation metadata + pending authorize states.
GITHUB_APP_DICT_NAME = "sbx-github-app"
# SOR-180: per-agent recovery checkpoint records (suspend/restore state).
CHECKPOINTS_DICT_NAME = "sbx-checkpoints"
# SOR-212/SOR-215: deploy-written provider runtime readiness records
# (``runtime/<provider>`` entries — ready/degraded evidence).
RUNTIME_DICT_NAME = "sbx-runtime"
# Modal filesystem-snapshot defaults for environment builds.
ENV_SNAPSHOT_TTL_S = 30 * 24 * 3600  # Modal default retention for filesystem snapshots
ENV_SNAPSHOT_TIMEOUT_S = 300
# Naming convention for per-account credential Secrets: ``sbx-acct-<id>``
# (control/api_v1/bootstrap.py, control/onboarding.py). Operators point it at
# a deployment-scoped prefix (``SBX_ACCOUNT_SECRET_PREFIX``) so a parallel
# deploy's teardown never sweeps another deployment's account Secrets.
ACCOUNT_SECRET_PREFIX = "sbx-acct-"
RUNTIME_IMAGE_NAME = "sbx-runtime"
# SOR-74: provider=devin sandboxes use this named image (sbx-runtime + pinned
# standalone Devin CLI, HOME=$SBX_WORK/home). Keep in sync with
# runtime.image.DEVIN_IMAGE_NAME.
DEVIN_IMAGE_NAME = "sbx-runtime-devin"
# SOR-62/SOR-80: provider=antigravity / grok sandboxes use these named images
# (sbx-runtime + the provider CLI at /usr/local/bin). Keep in sync with
# runtime.image.AGY_IMAGE_NAME / GROK_IMAGE_NAME.
ANTIGRAVITY_IMAGE_NAME = "sbx-runtime-antigravity"
GROK_IMAGE_NAME = "sbx-runtime-grok"
OPENCODE_IMAGE_NAME = "sbx-runtime-opencode"

# Modal Starter sandbox list price (P0): billed at the request floor.
CPU_USD_PER_CORE_S = 0.00003942
MEM_USD_PER_GIB_S = 0.00000667
REQUEST_CPU_CORES = 1.0
REQUEST_MEM_GIB = MEMORY_MIB[0] / 1024.0
SANDBOX_USD_PER_S = CPU_USD_PER_CORE_S * REQUEST_CPU_CORES + MEM_USD_PER_GIB_S * REQUEST_MEM_GIB

TERMINAL_STATUSES = frozenset({"closed", "timed_out", "lost"})
ACTIVE_STATUSES = frozenset({"creating", "idle", "running"})

# SOR-203: control-plane web-container warmth (Modal autoscaler). Modal's
# default idle window is 60s, so nearly every interactive request bursts a
# ~7s cold start; a longer ``scaledown_window`` keeps the last container
# warm after traffic and is billed only for the idle tail — the smallest
# cost-rational strategy for a request-driven control plane (``min_containers``
# is always-on cost; offered as an opt-in override, not the default).
CONTROL_SCALEDOWN_WINDOW_S = 300
# Modal autoscaler bounds for ``scaledown_window``: 2s .. 20min.
CONTROL_SCALEDOWN_WINDOW_MIN_S = 2
CONTROL_SCALEDOWN_WINDOW_MAX_S = 1200


def env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


# SOR-132/SOR-134: the lifecycle chain. Turn bound, the sandbox's native
# timers, the reaper's grace windows, and the resolved deploy config must
# all agree — one resolver so no consumer can quietly fall back to a
# different value than the operator configured.
@dataclass(frozen=True)
class LifecycleConfig:
    """Resolved lifecycle tunables (env-overridable contract defaults).

    ``turn_max_seconds`` bounds one provider turn (runner ``--max-seconds``);
    ``run_stale_s`` (``turn_max_seconds + run_grace_s``) is the reaper's
    stranded-``running`` bound; ``idle_timeout_s`` is the post-session idle
    retention the reaper sweep enforces (SOR-135);
    ``sandbox_idle_timeout_s`` is the native ``Sandbox.create(idle_timeout=)``
    bound and resolves to at least ``run_stale_s`` so it can never reclaim a
    sandbox mid-turn (SOR-134); ``sandbox_timeout_s`` is the Modal hard cap;
    ``create_grace_s`` is the reaper's in-flight create window.
    """

    idle_timeout_s: int
    sandbox_idle_timeout_s: int
    turn_max_seconds: int
    sandbox_timeout_s: int
    create_grace_s: int
    run_grace_s: int

    @property
    def run_stale_s(self) -> int:
        """A ``running`` record is stranded past ``turn_max + run_grace``."""
        return self.turn_max_seconds + self.run_grace_s


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def lifecycle_config(env: Mapping[str, str] | None = None) -> LifecycleConfig:
    """Resolve the lifecycle chain once; consumers share the result.

    The native sandbox idle bound is floored at the stranded-``running``
    bound (``turn_max_seconds + run_grace_s``): anything lower lets Modal
    reclaim a sandbox out from under an in-bounds turn (SOR-134), so the
    configured value clamps up rather than disagreeing with the reaper.
    """
    env = os.environ if env is None else env
    turn_max_seconds = _env_int(env, "SBX_TURN_MAX_SECONDS", TURN_MAX_SECONDS)
    run_grace_s = _env_int(env, "SBX_RUN_GRACE_S", RUN_GRACE_S)
    sandbox_idle = _env_int(env, "SBX_SANDBOX_IDLE_TIMEOUT_S", SANDBOX_IDLE_TIMEOUT_S)
    return LifecycleConfig(
        idle_timeout_s=_env_int(env, "SBX_IDLE_TIMEOUT_S", IDLE_TIMEOUT_S),
        sandbox_idle_timeout_s=max(sandbox_idle, turn_max_seconds + run_grace_s),
        turn_max_seconds=turn_max_seconds,
        sandbox_timeout_s=_env_int(env, "SBX_SANDBOX_TIMEOUT_S", SANDBOX_TIMEOUT_S),
        create_grace_s=_env_int(env, "SBX_CREATE_GRACE_S", CREATE_GRACE_S),
        run_grace_s=run_grace_s,
    )


@dataclass(frozen=True)
class ControlWarmthConfig:
    """Resolved Modal autoscaler warmth for the control-plane web function.

    ``scaledown_window_s`` is the post-traffic idle window before the last
    container scales to zero; ``min_containers`` / ``buffer_containers``
    are opt-in always-warm / headroom knobs (0 = unset, Modal's scale-to-
    zero default). These are *control-plane* tunables only — they resolve
    at deploy time into the ASGI function's autoscaler config and never
    touch the Agent ``Sandbox.create`` lifecycle chain.
    """

    scaledown_window_s: int
    min_containers: int
    buffer_containers: int


def control_warmth_config(env: Mapping[str, str] | None = None) -> ControlWarmthConfig:
    """Resolve the web function's autoscaler warmth once per deploy.

    The scaledown window clamps into Modal's accepted range (2s..1200s)
    rather than failing a deploy on an out-of-range override.
    """
    env = os.environ if env is None else env
    scaledown = _env_int(env, "SBX_CONTROL_SCALEDOWN_WINDOW_S", CONTROL_SCALEDOWN_WINDOW_S)
    scaledown = min(max(scaledown, CONTROL_SCALEDOWN_WINDOW_MIN_S), CONTROL_SCALEDOWN_WINDOW_MAX_S)
    return ControlWarmthConfig(
        scaledown_window_s=scaledown,
        min_containers=max(0, _env_int(env, "SBX_CONTROL_MIN_CONTAINERS", 0)),
        buffer_containers=max(0, _env_int(env, "SBX_CONTROL_BUFFER_CONTAINERS", 0)),
    )


def account_secret_prefix() -> str:
    """Prefix for per-account credential Secret names (``<prefix><id>``)."""
    return env_str("SBX_ACCOUNT_SECRET_PREFIX", ACCOUNT_SECRET_PREFIX)


def selected_providers(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Deploy-selected providers (``SBX_PROVIDERS``, comma-separated).

    Defaults to none (SOR-210): a control plane with no ``SBX_PROVIDERS``
    is a platform-only deployment — no provider Secret mounts or account
    seeding, matching the ``deploy.providers = []`` default.
    """
    env = os.environ if env is None else env
    raw = env.get("SBX_PROVIDERS", "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def degraded_providers(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Providers the deploy marked degraded (``SBX_DEGRADED_PROVIDERS``).

    SOR-217: provider provisioning problems degrade provider *runtime*
    health instead of failing the Platform deploy. Deploy records the set
    here so the app's Secret mounts can skip a credential Secret it knows
    is absent — a degraded provider's missing Secret must not fail
    ``modal deploy``.
    """
    env = os.environ if env is None else env
    raw = env.get("SBX_DEGRADED_PROVIDERS", "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def validate_auth_database_secret(auth_secret: str, bootstrap_secret: str) -> None:
    """Bootstrap rotation replaces its Secret, so it cannot also hold the DB URL."""
    if auth_secret and auth_secret.strip() == bootstrap_secret.strip():
        raise ValueError("auth database Secret must differ from the bootstrap Secret")


def app_secret_names(env: Mapping[str, str] | None = None) -> list[str]:
    """Secrets the control app mounts at deploy time (``control/modal_app.py``).

    The shared Codex credential Secret is only required when ``codex`` is a
    selected provider (``SBX_PROVIDERS``) — an unselected provider's
    credential must never block a deploy (SOR-115) — and it is not mounted
    when deploy marked codex degraded (SOR-217), since its credential is
    the reason for the degrade.
    """
    env = os.environ if env is None else env
    validate_auth_database_secret(
        env.get("SBX_AUTH_DATABASE_SECRET_NAME") or "",
        env.get("SBX_V1_BOOTSTRAP_SECRET_NAME") or V1_BOOTSTRAP_SECRET_NAME,
    )
    names = [
        env.get("SBX_BASIC_SECRET_NAME") or BASIC_SECRET_NAME,
        env.get("SBX_V1_BOOTSTRAP_SECRET_NAME") or V1_BOOTSTRAP_SECRET_NAME,
    ]
    if "codex" in selected_providers(env) and "codex" not in degraded_providers(env):
        names.insert(0, env.get("SBX_CODEX_SECRET_NAME") or CODEX_SECRET_NAME)
    # SOR-117: the GitHub bridge is opt-in end to end. A named Modal Secret
    # holding GH_TOKEN/GITHUB_TOKEN mounts on the control app only when the
    # operator armed the gate AND named the Secret — the remote function
    # then sees the token in its env and forwards it into sandboxes via
    # ``control.github.exec_env`` (local-gate control planes read the token
    # straight from their own env and need no Secret).
    github_secret = env.get("SBX_GITHUB_SECRET_NAME")
    if env.get("SBX_GITHUB_EPHEMERAL") == "1" and github_secret:
        names.append(github_secret)
    # SOR-177: the GitHub App's private key travels the same way — mounted
    # as a named Secret only when the operator named one. The remote app
    # reads ``SBX_GITHUB_APP_PRIVATE_KEY`` out of that Secret; nothing else
    # forwards the key material (it is deliberately absent from
    # ``REMOTE_ENV_KEYS``).
    github_app_secret = env.get("SBX_GITHUB_APP_SECRET_NAME")
    if github_app_secret:
        names.append(github_app_secret)
    # DATABASE_URL is secret material. Mount it by name rather than baking a
    # database password into deployment env/image metadata.
    auth_database_secret = env.get("SBX_AUTH_DATABASE_SECRET_NAME")
    if auth_database_secret:
        names.append(auth_database_secret)
    return names


def basic_credentials() -> tuple[str, str]:
    user = os.environ.get("SBX_BASIC_USER") or os.environ.get("SBX_API_USER", "sbx")
    # SBX_BASIC_PASSWORD: pre-0.1 docs used this name; keep accepting it so a
    # Secret written that way still reaches the app instead of silently
    # falling back to the default password.
    password = (
        os.environ.get("SBX_BASIC_PASS")
        or os.environ.get("SBX_BASIC_PASSWORD")
        or os.environ.get("SBX_API_PASSWORD", "sbx")
    )
    return user, password


# Env vars a deploy may forward into the remote control functions. This is
# an allowlist, not a prefix rule: credential material (``SBX_API_KEY``,
# ``SBX_V1_BOOTSTRAP_KEY``, ``SBX_BASIC_*``, ``SBX_ACCOUNT_CREDENTIAL*``,
# ``SBX_LINEAR_API_KEY``, ``CODEX_AUTH_JSON``) travels exclusively through
# ``modal.Secret`` mounts and must never be baked into a function env. The
# keys below are names/tunables only — safe to record in the deployment.
_PROVIDER_SEED_PROVIDERS = ("CODEX", "DEVIN", "ANTIGRAVITY", "GROK", "OPENCODE")
_PROVIDER_SEED_SUFFIXES = ("ACCOUNT_ID", "SECRET_NAME", "SLOTS", "MODELS", "ACCOUNTS")

REMOTE_ENV_KEYS: tuple[str, ...] = (
    "SBX_MODAL_APP_NAME",
    # The provider selection is runtime policy as well as deploy-time image /
    # Secret configuration. The remote API must seed and schedule only the
    # providers whose images and credentials were deployed.
    "SBX_PROVIDERS",
    "SBX_SESSIONS_DICT",
    "SBX_RUNS_DICT",
    "SBX_RUN_ACTIVITY_DICT",
    "SBX_TASKS_DICT",
    "SBX_REVISIONS_DICT",
    "SBX_ACCOUNTS_DICT",
    "SBX_WORKFLOWS_DICT",
    "SBX_ARTIFACTS_DICT",
    "SBX_WORKSPACES_DICT",
    "SBX_IMAGE_CODEX",
    "SBX_IMAGE_DEVIN",
    "SBX_IMAGE_ANTIGRAVITY",
    "SBX_IMAGE_GROK",
    "SBX_IMAGE_OPENCODE",
    "SBX_CODEX_SECRET_NAME",
    "SBX_BASIC_SECRET_NAME",
    "SBX_V1_BOOTSTRAP_SECRET_NAME",
    "SBX_AUTH_DATABASE_SECRET_NAME",
    "SBX_ACCOUNT_SECRET_PREFIX",
    "SBX_PROVIDERS",
    "SBX_MAX_CONCURRENT",
    "SBX_IDLE_TIMEOUT_S",
    "SBX_SANDBOX_IDLE_TIMEOUT_S",
    "SBX_TURN_MAX_SECONDS",
    "SBX_SANDBOX_TIMEOUT_S",
    "SBX_CREATE_GRACE_S",
    "SBX_RUN_GRACE_S",
    # SOR-199: staleness bound for the Dict listing caches (seconds).
    "SBX_LIST_CACHE_TTL_S",
    "SBX_DEFAULT_MODEL",
    "SBX_SSE_KEEPALIVE_SECONDS",
    "SBX_DEVIN_BURST_SLOTS",
    "SBX_RUNNER_CMD",
    "SBX_DEVIN_TRANSPORT",
    "SBX_CODEX_TRANSPORT",
    "SBX_GITHUB_EPHEMERAL",
    "SBX_GITHUB_SECRET_NAME",
    # SOR-177 GitHub App authorization: app identity, store naming and API
    # base are deploy tunables — the private key itself is Secret material
    # and is intentionally not in this allowlist.
    "SBX_GITHUB_APP_ID",
    "SBX_GITHUB_APP_SLUG",
    "SBX_GITHUB_APP_SECRET_NAME",
    "SBX_GITHUB_APP_DICT",
    "SBX_GITHUB_APP_API_URL",
    # SOR-220: broker base URL for the default Connect path — a deploy
    # tunable, never credential material. SBX_PUBLIC_ORIGIN pins the
    # deployment's trusted https origin behind TLS terminators.
    "SBX_GITHUB_BROKER_URL",
    "SBX_PUBLIC_ORIGIN",
    "SBX_LINEAR_MCP_EPHEMERAL",
    # SOR-147: credential write-back kill-switch (tunable, not a secret).
    "SBX_CRED_WRITEBACK",
    # SOR-129 session-resource registry: Secret-name allowlist and MCP
    # server templates (config refs only — never secret values).
    "SBX_RESOURCE_SECRETS",
    "SBX_MCP_REGISTRY",
    # SOR-127 environment build/snapshot cache: opt-in gate, durable record
    # Dict name, deployment-wide setup command and Modal snapshot tunables.
    # Names/tunables only — build sandboxes never carry Secrets.
    "SBX_ENV_CACHE",
    "SBX_ENVIRONMENTS_DICT",
    "SBX_ENV_SETUP",
    "SBX_ENV_SNAPSHOT_TTL_S",
    "SBX_ENV_SNAPSHOT_TIMEOUT_S",
    # SOR-180 same-agent checkpoint/recovery: durable record Dict name.
    "SBX_CHECKPOINTS_DICT",
    # SOR-212/SOR-215: deploy-written runtime evidence Dict name.
    "SBX_RUNTIME_DICT",
    # SOR-217: providers the deploy degraded — lets the app's Secret
    # mounts skip a credential Secret that is knowingly absent.
    "SBX_DEGRADED_PROVIDERS",
    *(
        f"SBX_{provider}_{suffix}"
        for provider in _PROVIDER_SEED_PROVIDERS
        for suffix in _PROVIDER_SEED_SUFFIXES
    ),
)


def remote_env_overlay(
    env: Mapping[str, str] | None = None, *, app_name: str | None = None
) -> dict[str, str]:
    """Deploy-time env the remote control functions must see.

    ``control/modal_app.py`` bakes this into ``@app.function(env=...)`` so a
    deploy configured with non-default Dict/Secret/image names (a parallel RC
    deployment) actually uses them remotely instead of silently falling back
    to the production contract names. ``SBX_MODAL_APP_NAME`` is always set —
    ``ModalBackend`` scopes ``Sandbox.create`` to it, so an unset value would
    attach the deployment's sandboxes to the production ``sbx-control`` app.
    """
    env = os.environ if env is None else env
    out = {key: env[key] for key in REMOTE_ENV_KEYS if env.get(key)}
    out["SBX_MODAL_APP_NAME"] = app_name or env.get("SBX_MODAL_APP_NAME") or MODAL_APP_NAME
    return out


def default_runner_cmd(*, backend_kind: str) -> list[str]:
    override = os.environ.get("SBX_RUNNER_CMD")
    if override:
        import shlex

        return shlex.split(override)
    if backend_kind != "modal":
        try:
            import runtime.runner  # noqa: F401
        except ImportError:
            stub = Path(__file__).resolve().parents[1] / "tests" / "fakes" / "stub_runner.py"
            if stub.is_file():
                return [sys.executable, str(stub)]
        return [sys.executable, "-m", "runtime.runner"]
    return ["python", "-m", "runtime.runner"]
