"""Single configuration source for the bootstrap CLI (SOR-98).

One TOML file (``$SBX_CONFIG``, else ``$XDG_CONFIG_HOME/sbx/config.toml``,
else ``~/.config/sbx/config.toml``) holds every deployment knob: Modal
profile/app name, durable state names, Secret names, provider image pins,
and the public API base URL. Environment variables override file values;
defaults come from ``control.config`` so the names can never drift from the
control plane's contract constants.

The file never stores secrets — the ``sbx_`` bootstrap key and generated
Basic credentials live in the state dir (``sbx.keys`` / ``sbx.deploy``),
mode 0600.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, get_args

from control.config import (
    ACCOUNT_SECRET_PREFIX,
    ACCOUNTS_DICT_NAME,
    ANTIGRAVITY_IMAGE_NAME,
    ARTIFACTS_DICT_NAME,
    BASIC_SECRET_NAME,
    CODEX_SECRET_NAME,
    DEVIN_IMAGE_NAME,
    GITHUB_APP_DICT_NAME,
    GROK_IMAGE_NAME,
    MODAL_APP_NAME,
    OPENCODE_IMAGE_NAME,
    RUNS_DICT_NAME,
    RUNTIME_DICT_NAME,
    RUNTIME_IMAGE_NAME,
    SESSIONS_DICT_NAME,
    V1_BOOTSTRAP_SECRET_NAME,
    WORKFLOWS_DICT_NAME,
    WORKSPACES_DICT_NAME,
    validate_auth_database_secret,
)
from control.ports import ProviderId

from sbx.errors import BootstrapError

CONFIG_ENV = "SBX_CONFIG"
STATE_DIR_ENV = "SBX_STATE_DIR"
API_KEY_ENV = "SBX_API_KEY"

_APP_DIR = "sbx"

# Contract provider set (``ProviderId`` in control/ports.py — the api-v1
# enum). ``deploy.providers`` must stay inside it.
KNOWN_PROVIDERS: tuple[str, ...] = get_args(ProviderId)

# field -> ((toml section, toml key), env override names in priority order)
_FIELD_MAP: dict[str, tuple[tuple[str, str], tuple[str, ...]]] = {
    "modal_profile": (("modal", "profile"), ("SBX_MODAL_PROFILE", "MODAL_PROFILE")),
    "modal_app_name": (("modal", "app_name"), ("SBX_MODAL_APP_NAME",)),
    "api_base_url": (("api", "base_url"), ("SBX_BASE_URL",)),
    "sessions_dict": (("state", "sessions_dict"), ("SBX_SESSIONS_DICT",)),
    "runs_dict": (("state", "runs_dict"), ("SBX_RUNS_DICT",)),
    "accounts_dict": (("state", "accounts_dict"), ("SBX_ACCOUNTS_DICT",)),
    "workflows_dict": (("state", "workflows_dict"), ("SBX_WORKFLOWS_DICT",)),
    "artifacts_dict": (("state", "artifacts_dict"), ("SBX_ARTIFACTS_DICT",)),
    "workspaces_dict": (("state", "workspaces_dict"), ("SBX_WORKSPACES_DICT",)),
    # SOR-212/SOR-215: deploy-written provider runtime evidence. Not in
    # ``dict_names()`` — records are deploy-derived (recreated each deploy)
    # rather than operator data an upgrade must preserve, and the Dict only
    # exists after the first post-SOR-212 deploy.
    "runtime_dict": (("state", "runtime_dict"), ("SBX_RUNTIME_DICT",)),
    "account_secret_prefix": (
        ("state", "account_secret_prefix"),
        ("SBX_ACCOUNT_SECRET_PREFIX",),
    ),
    "codex_secret": (("secrets", "codex"), ("SBX_CODEX_SECRET_NAME",)),
    "basic_secret": (("secrets", "basic"), ("SBX_BASIC_SECRET_NAME",)),
    "bootstrap_secret": (("secrets", "bootstrap"), ("SBX_V1_BOOTSTRAP_SECRET_NAME",)),
    "auth_database_secret": (("secrets", "auth_database"), ("SBX_AUTH_DATABASE_SECRET_NAME",)),
    "github_ephemeral": (("github", "ephemeral"), ("SBX_GITHUB_EPHEMERAL",)),
    "github_secret_name": (("github", "secret_name"), ("SBX_GITHUB_SECRET_NAME",)),
    # SOR-177 GitHub App one-click auth: app identity + the *name* of the
    # operator-managed Secret holding the App private key — never the key
    # itself — plus the durable Dict for installation metadata.
    "github_app_id": (("github_app", "app_id"), ("SBX_GITHUB_APP_ID",)),
    "github_app_slug": (("github_app", "slug"), ("SBX_GITHUB_APP_SLUG",)),
    "github_app_secret_name": (
        ("github_app", "secret_name"),
        ("SBX_GITHUB_APP_SECRET_NAME",),
    ),
    "github_app_dict": (("github_app", "dict"), ("SBX_GITHUB_APP_DICT",)),
    # SOR-220 default Connect GitHub: the hosted Sorenforge integration
    # broker URL. Empty = the public default; "off"/"disabled" turns the
    # brokered lane off (manifest/env-App/PAT paths still work). The App
    # private key never lives here — only the broker reference.
    "github_broker_url": (("github", "broker_url"), ("SBX_GITHUB_BROKER_URL",)),
    "image_codex": (("images", "codex"), ("SBX_IMAGE_CODEX",)),
    "image_devin": (("images", "devin"), ("SBX_IMAGE_DEVIN",)),
    "image_antigravity": (("images", "antigravity"), ("SBX_IMAGE_ANTIGRAVITY",)),
    "image_grok": (("images", "grok"), ("SBX_IMAGE_GROK",)),
    "image_opencode": (("images", "opencode"), ("SBX_IMAGE_OPENCODE",)),
    "providers": (("deploy", "providers"), ("SBX_PROVIDERS",)),
    "max_concurrent": (("deploy", "max_concurrent"), ("SBX_MAX_CONCURRENT",)),
    # SOR-132/SOR-134: the lifecycle chain — the values a deployment
    # resolves here are replayed into the remote functions' env
    # (``deploy_env`` → ``remote_env_overlay``), so the reaper, the
    # runner's ``--max-seconds``, and ``Sandbox.create``'s native timers
    # all agree instead of drifting back to contract defaults.
    # SOR-135: ``idle_timeout_s`` is the post-session idle retention only;
    # ``sandbox_idle_timeout_s`` is the native ``Sandbox.create`` bound —
    # two deliberately separate knobs, never one value feeding both.
    "idle_timeout_s": (("deploy", "idle_timeout_s"), ("SBX_IDLE_TIMEOUT_S",)),
    "sandbox_idle_timeout_s": (
        ("deploy", "sandbox_idle_timeout_s"),
        ("SBX_SANDBOX_IDLE_TIMEOUT_S",),
    ),
    "turn_max_seconds": (("deploy", "turn_max_seconds"), ("SBX_TURN_MAX_SECONDS",)),
    "sandbox_timeout_s": (("deploy", "sandbox_timeout_s"), ("SBX_SANDBOX_TIMEOUT_S",)),
    "create_grace_s": (("deploy", "create_grace_s"), ("SBX_CREATE_GRACE_S",)),
    "run_grace_s": (("deploy", "run_grace_s"), ("SBX_RUN_GRACE_S",)),
    # SOR-203: control-plane web-container warmth. These are deploy-time
    # autoscaler knobs only — ``control.modal_app`` resolves them while
    # ``modal deploy`` builds the function, so ``deploy_env`` replays them
    # into the subprocess but they never enter the remote env or the Agent
    # Sandbox lifecycle.
    "control_scaledown_window_s": (
        ("deploy", "control_scaledown_window_s"),
        ("SBX_CONTROL_SCALEDOWN_WINDOW_S",),
    ),
    "control_min_containers": (
        ("deploy", "control_min_containers"),
        ("SBX_CONTROL_MIN_CONTAINERS",),
    ),
    "control_buffer_containers": (
        ("deploy", "control_buffer_containers"),
        ("SBX_CONTROL_BUFFER_CONTAINERS",),
    ),
}

# Optional positive-int knobs; ``None`` means "not configured" — never
# written to config.toml and never replayed into the deploy env, so the
# remote contract defaults win over an absent local value.
_POSITIVE_INT_FIELDS = frozenset(
    {
        "max_concurrent",
        "idle_timeout_s",
        "sandbox_idle_timeout_s",
        "turn_max_seconds",
        "sandbox_timeout_s",
        "create_grace_s",
        "run_grace_s",
    }
)

# Optional non-negative-int knobs — same absent-is-None semantics, but 0 is
# a legal explicit value (e.g. ``control_min_containers = 0``).
_NONNEG_INT_FIELDS = frozenset(
    {
        "control_scaledown_window_s",
        "control_min_containers",
        "control_buffer_containers",
    }
)


@dataclass(frozen=True)
class BootstrapConfig:
    """Resolved deployment config; ``providers`` selects images to build."""

    modal_profile: str = ""
    modal_app_name: str = MODAL_APP_NAME
    api_base_url: str = ""
    sessions_dict: str = SESSIONS_DICT_NAME
    runs_dict: str = RUNS_DICT_NAME
    accounts_dict: str = ACCOUNTS_DICT_NAME
    workflows_dict: str = WORKFLOWS_DICT_NAME
    artifacts_dict: str = ARTIFACTS_DICT_NAME
    workspaces_dict: str = WORKSPACES_DICT_NAME
    runtime_dict: str = RUNTIME_DICT_NAME
    account_secret_prefix: str = ACCOUNT_SECRET_PREFIX
    codex_secret: str = CODEX_SECRET_NAME
    basic_secret: str = BASIC_SECRET_NAME
    bootstrap_secret: str = V1_BOOTSTRAP_SECRET_NAME
    # Only the name is persisted. DATABASE_URL lives in this operator-managed
    # Secret. It must never share the bootstrap Secret, which rotation replaces.
    auth_database_secret: str = ""
    image_codex: str = RUNTIME_IMAGE_NAME
    image_devin: str = DEVIN_IMAGE_NAME
    image_antigravity: str = ANTIGRAVITY_IMAGE_NAME
    image_grok: str = GROK_IMAGE_NAME
    image_opencode: str = OPENCODE_IMAGE_NAME
    # Optional GitHub auth bridge (SOR-117/SOR-133): the gate flag and the
    # *name* of the operator-managed Modal Secret holding GH_TOKEN — the
    # token value itself is never persisted.
    github_ephemeral: bool = False
    github_secret_name: str = ""
    # GitHub App one-click authorization (SOR-177): app id/slug identify
    # the GitHub App; ``github_app_secret_name`` names the Modal Secret
    # holding its private key (the key material is never persisted);
    # ``github_app_dict`` names the durable installation-metadata store.
    github_app_id: str = ""
    github_app_slug: str = ""
    github_app_secret_name: str = ""
    github_app_dict: str = GITHUB_APP_DICT_NAME
    # SOR-220 broker lane override (see _FIELD_MAP note). ``""`` = hosted
    # default; only a non-empty value is replayed into the deploy env.
    github_broker_url: str = ""
    # Zero-provider is the fresh-clone default (SOR-210): `./sbx deploy`
    # brings up the core platform (control plane + Console + durable state)
    # with no provider credential or image build; providers opt in via
    # `deploy.providers` / SBX_PROVIDERS / `sbx init --providers`.
    providers: tuple[str, ...] = ()
    # Live-agent/sandbox cap forwarded to the deployed app as
    # ``SBX_MAX_CONCURRENT`` (per-key cap + scheduler global cap). ``None``
    # means "not configured" — the remote defaults apply — so it is never
    # written to config.toml or pushed into the deploy env.
    max_concurrent: int | None = None
    # SOR-132/SOR-134/SOR-135 lifecycle chain, forwarded as
    # SBX_IDLE_TIMEOUT_S / SBX_SANDBOX_IDLE_TIMEOUT_S / SBX_TURN_MAX_SECONDS
    # / SBX_SANDBOX_TIMEOUT_S / SBX_CREATE_GRACE_S / SBX_RUN_GRACE_S. Same
    # ``None``-means-absent semantics as ``max_concurrent``.
    idle_timeout_s: int | None = None
    sandbox_idle_timeout_s: int | None = None
    turn_max_seconds: int | None = None
    sandbox_timeout_s: int | None = None
    create_grace_s: int | None = None
    run_grace_s: int | None = None
    # SOR-203: control-plane web-function autoscaler warmth. Same
    # ``None``-means-absent semantics — the Modal defaults and
    # ``control.config.CONTROL_SCALEDOWN_WINDOW_S`` apply when unset.
    control_scaledown_window_s: int | None = None
    control_min_containers: int | None = None
    control_buffer_containers: int | None = None

    def __post_init__(self) -> None:
        try:
            validate_auth_database_secret(self.auth_database_secret, self.bootstrap_secret)
        except ValueError as exc:
            raise BootstrapError(
                str(exc),
                hint="store DATABASE_URL in a separate Secret and set secrets.auth_database",
                code="auth_database_secret_conflict",
            ) from None

    def image_name(self, provider: str) -> str:
        """Published Modal image name for ``provider``."""
        return str(getattr(self, f"image_{provider}"))

    def secret_names(self) -> tuple[str, ...]:
        """Managed Modal Secrets the control app requires.

        Provider-aware (SOR-116): the shared Codex credential Secret is only
        required when ``codex`` is an enabled provider — other providers
        carry per-account ``<account_secret_prefix><id>`` Secrets instead.
        """
        names = [self.basic_secret, self.bootstrap_secret]
        if self.auth_database_secret:
            names.append(self.auth_database_secret)
        if "codex" in self.providers:
            names.insert(0, self.codex_secret)
        return tuple(names)

    def dict_names(self) -> tuple[str, ...]:
        """Durable stores an upgrade must preserve."""
        names = [
            self.sessions_dict,
            self.runs_dict,
            self.accounts_dict,
            self.workflows_dict,
            self.artifacts_dict,
            self.workspaces_dict,
        ]
        # The installation-metadata Dict only exists once the App is
        # configured — unconfigured deploys never create it, so upgrades
        # must not expect it either.
        if self.github_app_id:
            names.append(self.github_app_dict)
        return tuple(names)

    def deploy_env(self) -> dict[str, str]:
        """``SBX_*`` env the ``modal deploy`` subprocess needs.

        The deployed app resolves its resource names from process env at
        deploy time (``control/modal_app.py`` + ``remote_env_overlay``), so
        the resolved config — whether the values came from the file or env —
        is replayed as env vars. Without this a file-configured parallel
        deploy would create the renamed resources yet keep the app on the
        production contract names.
        """
        fields = (
            "modal_app_name",
            "sessions_dict",
            "runs_dict",
            "accounts_dict",
            "workflows_dict",
            "artifacts_dict",
            "workspaces_dict",
            "runtime_dict",
            "account_secret_prefix",
            "codex_secret",
            "basic_secret",
            "bootstrap_secret",
            "image_codex",
            "image_devin",
            "image_antigravity",
            "image_grok",
            "image_opencode",
            "max_concurrent",
            "idle_timeout_s",
            "sandbox_idle_timeout_s",
            "turn_max_seconds",
            "sandbox_timeout_s",
            "create_grace_s",
            "run_grace_s",
            "control_scaledown_window_s",
            "control_min_containers",
            "control_buffer_containers",
        )
        # ``None`` (e.g. an unset max_concurrent) is never replayed — the
        # remote defaults must win over an absent local value.
        out = {
            _FIELD_MAP[name][1][0]: str(getattr(self, name))
            for name in fields
            if getattr(self, name) is not None
        }
        # ``control.modal_app`` reads this at deploy time to skip mounting
        # the shared Codex Secret and seeding accounts for providers the
        # deployment does not serve (SOR-115/SOR-116). An empty set stays
        # unset — ``remote_env_overlay`` drops falsy values and the remote
        # ``selected_providers`` default must see "no providers" (SOR-210).
        if self.providers:
            out["SBX_PROVIDERS"] = ",".join(self.providers)
        # SOR-133: replay the resolved GitHub bridge so a file-configured
        # deploy arms the remote control plane identically to env-armed
        # ones. The Secret *name* only — token material stays inside the
        # named Modal Secret.
        if self.github_ephemeral:
            out["SBX_GITHUB_EPHEMERAL"] = "1"
        if self.github_secret_name:
            out["SBX_GITHUB_SECRET_NAME"] = self.github_secret_name
        # SOR-177: replay the GitHub App identity so a file-configured
        # deploy enables one-click auth remotely. The Secret *name* only —
        # the App private key stays inside the named Modal Secret and is
        # never in deploy env.
        if self.github_app_id:
            out["SBX_GITHUB_APP_ID"] = self.github_app_id
            out["SBX_GITHUB_APP_DICT"] = self.github_app_dict
        if self.github_app_slug:
            out["SBX_GITHUB_APP_SLUG"] = self.github_app_slug
        if self.github_app_secret_name:
            out["SBX_GITHUB_APP_SECRET_NAME"] = self.github_app_secret_name
        if self.auth_database_secret:
            out["SBX_AUTH_DATABASE_SECRET_NAME"] = self.auth_database_secret
        # SOR-220: replay only an explicit override — an absent value lets
        # the remote default broker URL apply, and "off" disables the lane.
        if self.github_broker_url:
            out["SBX_GITHUB_BROKER_URL"] = self.github_broker_url
        return out


@dataclass(frozen=True)
class ResolvedConfig:
    """A config plus where each value came from: file / env / default."""

    config: BootstrapConfig
    path: Path
    sources: dict[str, str]
    file_exists: bool


def _xdg(env: Mapping[str, str], key: str, fallback: str) -> Path:
    raw = env.get(key)
    if raw:
        return Path(raw)
    return Path(env.get("HOME", str(Path.home()))) / fallback


def config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    raw = env.get(CONFIG_ENV)
    if raw:
        return Path(raw)
    return _xdg(env, "XDG_CONFIG_HOME", ".config") / _APP_DIR / "config.toml"


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    raw = env.get(STATE_DIR_ENV)
    if raw:
        return Path(raw)
    return _xdg(env, "XDG_STATE_HOME", ".local/state") / _APP_DIR


def key_path(env: Mapping[str, str] | None = None) -> Path:
    return state_dir(env) / "bootstrap.key"


def basic_auth_path(env: Mapping[str, str] | None = None) -> Path:
    return state_dir(env) / "basic-auth.json"


def deploy_state_path(env: Mapping[str, str] | None = None) -> Path:
    return state_dir(env) / "deploy.json"


def _field_names() -> tuple[str, ...]:
    return tuple(f.name for f in fields(BootstrapConfig))


def _toml_escape(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _serialize(config: BootstrapConfig) -> str:
    sections: dict[str, list[str]] = {}
    for name in _field_names():
        (section, key), _envs = _FIELD_MAP[name]
        value = getattr(config, name)
        if value is None:
            continue  # unset optional knobs stay absent, not "None"
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, tuple):
            rendered = "[" + ", ".join(_toml_escape(v) for v in value) + "]"
        elif isinstance(value, int) and not isinstance(value, bool):
            rendered = str(value)
        else:
            rendered = _toml_escape(str(value))
        sections.setdefault(section, []).append(f"{key} = {rendered}")
    out = ["# sbx deployment config — env vars override file values (see AGENTS/docs)."]
    for section, lines in sections.items():
        out.append(f"\n[{section}]")
        out.extend(lines)
    return "\n".join(out) + "\n"


def save(
    config: BootstrapConfig, path: Path | None = None, *, env: Mapping[str, str] | None = None
) -> Path:
    path = path or config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(_serialize(config), encoding="utf-8")
    tmp.replace(path)
    return path


def _coerce(name: str, value: Any) -> Any:
    if name == "providers":
        if isinstance(value, str):
            return tuple(p.strip() for p in value.split(",") if p.strip())
        if isinstance(value, (list, tuple)):
            return tuple(str(p).strip() for p in value if str(p).strip())
        raise ValueError("providers must be a list or comma-separated string")
    if name == "github_ephemeral":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        raise ValueError("github_ephemeral must be a boolean")
    if name in _POSITIVE_INT_FIELDS:
        if value in (None, ""):
            return None
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a positive integer") from None
        if n < 1:
            raise ValueError(f"{name} must be a positive integer")
        return n
    if name in _NONNEG_INT_FIELDS:
        if value in (None, ""):
            return None
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a non-negative integer") from None
        if n < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return n
    return str(value)


def load_file_values(path: Path) -> BootstrapConfig:
    """File values only (no env) — used by ``sbx init`` to rewrite config."""
    if not path.is_file():
        return BootstrapConfig()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    overrides: dict[str, Any] = {}
    for name in _field_names():
        (section, key), _envs = _FIELD_MAP[name]
        raw = data.get(section, {})
        if isinstance(raw, dict) and key in raw:
            overrides[name] = _coerce(name, raw[key])
    return replace(BootstrapConfig(), **overrides) if overrides else BootstrapConfig()


def load(
    path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> ResolvedConfig:
    """Resolve file → env → default into a single config view."""
    env = os.environ if env is None else env
    path = path or config_path(env)
    data: dict[str, Any] = {}
    file_exists = path.is_file()
    if file_exists:
        data = tomllib.loads(path.read_text(encoding="utf-8"))

    config = BootstrapConfig()
    sources = {name: "default" for name in _field_names()}
    overrides: dict[str, Any] = {}
    for name in _field_names():
        (section, key), env_names = _FIELD_MAP[name]
        raw = data.get(section, {})
        if isinstance(raw, dict) and key in raw:
            overrides[name] = _coerce(name, raw[key])
            sources[name] = "file"
        for env_name in env_names:
            value = env.get(env_name)
            if value:
                overrides[name] = _coerce(name, value)
                sources[name] = "env"
                break
    if overrides:
        config = replace(config, **overrides)
    return ResolvedConfig(config=config, path=path, sources=sources, file_exists=file_exists)


def validate_providers(providers: tuple[str, ...]) -> None:
    """Deploy precondition: every named provider must be a contract provider.

    An empty set is valid (SOR-210): it deploys the core platform only —
    no provider images, no provider credential gates.
    """
    unknown = [p for p in providers if p not in KNOWN_PROVIDERS]
    if unknown:
        raise BootstrapError(
            f"unknown provider(s): {', '.join(unknown)}",
            hint=f"valid providers: {', '.join(KNOWN_PROVIDERS)}",
            code="invalid_providers",
        )
