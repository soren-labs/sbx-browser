"""SOR-204: dynamic provider model + reasoning capability discovery.

Capability truth comes from the *authenticated provider CLI*, not the
static ``default_models`` declared at onboarding/bootstrap. A discovery
refresh provisions a throwaway sandbox, restores the account's credential
blob exactly like a real run (``runner init``), then execs the provider's
own model-listing command (``<cli> models`` by default —
``PROVIDER_DISCOVERY_COMMANDS``, overridable per provider via
``SBX_<PROVIDER>_DISCOVERY_ARGV``). The answer is normalized into an
``AccountCapabilities`` report:

    provider / account / model / display / family / aliases /
    reasoning_efforts / default_effort / availability /
    source / refreshed_at / stale

Reports persist in a ``CapabilityStore`` (in-memory, file, or the
``sbx-capabilities`` Modal Dict) with a TTL (``SBX_CAPABILITY_TTL_S``,
default 15 min). Staleness is computed at read time: a report is stale
once its TTL expires, when the stored credential blob's fingerprint no
longer matches (any credential write path — import, refresh, sync
write-back — invalidates it), or when the deployment's pinned CLI version
for the provider changed (``SBX_CLI_VERSIONS`` / ``SBX_<PROVIDER>_VERSION``).
Reads always serve the last-good report flagged ``stale`` rather than
dropping to nothing; a failed refresh keeps the stored report and records
the error on it.

Validation rule for ``POST /v1/agents``: a non-empty catalog is proof —
an explicit ``AgentSpec.model`` must resolve to an entry (or alias) and a
declared ``reasoning_effort`` must be one the resolved model exposes. An
empty catalog is not proof of anything, so explicit models pass through as
before.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from runtime.runner.effort import CANONICAL_EFFORTS, effort_error, supported_efforts

from control.accounts import validate_account_id
from control.onboarding import _AUTH_FAIL_MARKERS, _PROVIDER_BINS
from control.ports import Account

DEFAULT_TTL_S = 900
CAPABILITIES_DICT_NAME = "sbx-capabilities"
_STORE_DIR_ENV = "SBX_CAPABILITY_STORE_DIR"
_DICT_ENV = "SBX_CAPABILITIES_DICT"
_TTL_ENV = "SBX_CAPABILITY_TTL_S"

# Env contract the sandbox runner consumes for this account (runner-cli.md)
# — mirrored from control.onboarding so the probe can scrub it before
# exec'ing the provider CLI.
_CREDENTIAL_ENV = "SBX_ACCOUNT_CREDENTIAL"
_ACCOUNT_ID_ENV = "SBX_ACCOUNT_ID"

REPORT_SOURCES = ("cli", "declared")
AVAILABILITY = ("available", "unavailable")

# The per-provider CLI subcommand that enumerates the account's models —
# one tuple per deployable provider, overridable via
# ``SBX_<PROVIDER>_DISCOVERY_ARGV`` (shlex-split) for CLIs whose listing
# command differs (e.g. ``models list --json``).
PROVIDER_DISCOVERY_COMMANDS: dict[str, tuple[str, ...]] = {
    "codex": ("models",),
    "devin": ("models",),
    "antigravity": ("models",),
    "grok": ("models",),
    "opencode": ("models",),
}

# Trailing model-id tokens that denote a tier rather than the model
# family itself (``swe-2-high`` → family ``swe-2``).
_TIER_SUFFIXES = frozenset(
    {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "mini",
        "flash",
        "pro",
        "free",
        "turbo",
        "preview",
        "experimental",
    }
)

_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][\w./-]*")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


# ------------------------------------------------------------------ argv


def provider_discovery_argv(
    provider: str, env: Mapping[str, str] | None = None
) -> list[str] | None:
    """Argv for the provider's capability-discovery command; ``None`` when
    the provider has no discoverable surface.

    ``*_BIN`` overrides mirror the runner adapters / auth checks; a single
    ``.py`` token is re-executed with the current interpreter.
    """
    env = os.environ if env is None else env
    raw = env.get(f"SBX_{provider.upper()}_DISCOVERY_ARGV")
    if raw is not None:
        tail = tuple(shlex.split(raw))
    else:
        tail = PROVIDER_DISCOVERY_COMMANDS.get(provider)
    if not tail:
        return None
    bin_env, default_bin = _PROVIDER_BINS.get(provider, ("", provider))
    tokens = shlex.split(env.get(bin_env) or default_bin)
    if not tokens:
        return None
    if len(tokens) == 1 and tokens[0].endswith(".py"):
        return [sys.executable, tokens[0], *tail]
    return [*tokens, *tail]


# ------------------------------------------------------------ normalization


def infer_family(model: str) -> str:
    """Best-effort family for a model id the CLI didn't tag.

    ``provider/name`` ids group under the provider namespace; bare ids
    drop a single trailing tier token (``swe-2-high`` → ``swe-2``,
    ``muse-spark-1.3-contributor-free`` → ``muse-spark-1.3-contributor``).
    """
    if "/" in model:
        return model.split("/", 1)[0]
    head, sep, tail = model.rpartition("-")
    if sep and tail.lower() in _TIER_SUFFIXES:
        return head
    return model


def _canonical_efforts(raw: Any, provider: str) -> tuple[str, ...]:
    """Normalize a reported effort list onto the canonical ladder.

    An absent key falls back to the provider's native effort surface; an
    explicit list (possibly empty) is intersected with the canonical set
    in canonical order — non-canonical levels the CLI invents are dropped.
    """
    if raw is None:
        return supported_efforts(provider)
    if not isinstance(raw, (list, tuple)):
        return ()
    reported = {str(level) for level in raw}
    return tuple(level for level in CANONICAL_EFFORTS if level in reported)


@dataclass(frozen=True)
class ModelCapability:
    """One normalized model entry a provider account exposes."""

    model: str
    display: str
    family: str
    aliases: tuple[str, ...] = ()
    reasoning_efforts: tuple[str, ...] = ()
    default_effort: str | None = None
    availability: str = "available"  # ``AVAILABILITY``
    free: bool = False  # provider-reported free/community tier (e.g. Zen)

    def matches(self, model: str) -> bool:
        return model == self.model or model in self.aliases

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "display": self.display,
            "family": self.family,
            "aliases": list(self.aliases),
            "reasoning_efforts": list(self.reasoning_efforts),
            "default_effort": self.default_effort,
            "availability": self.availability,
            "free": self.free,
        }

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> ModelCapability:
        return ModelCapability(
            model=str(raw.get("model") or ""),
            display=str(raw.get("display") or raw.get("model") or ""),
            family=str(raw.get("family") or raw.get("model") or ""),
            aliases=tuple(str(a) for a in raw.get("aliases") or ()),
            reasoning_efforts=tuple(str(e) for e in raw.get("reasoning_efforts") or ()),
            default_effort=(str(raw["default_effort"]) if raw.get("default_effort") else None),
            availability=str(raw.get("availability") or "available"),
            free=bool(raw.get("free", False)),
        )


def normalize_model(provider: str, raw: Any) -> ModelCapability | None:
    """Normalize one CLI-reported model entry; ``None`` when it has no id."""
    if isinstance(raw, str):
        raw = {"id": raw}
    if not isinstance(raw, Mapping):
        return None
    model_id = str(raw.get("id") or raw.get("model") or raw.get("name") or "").strip()
    if not model_id:
        return None
    efforts = _canonical_efforts(raw.get("reasoning_efforts", raw.get("efforts")), provider)
    default_effort = str(raw.get("default_effort") or "").strip() or None
    if default_effort not in efforts:
        default_effort = None
    availability = (
        str(
            raw.get("availability")
            or ("available" if raw.get("available", True) else "unavailable")
        )
        .strip()
        .lower()
    )
    if availability not in AVAILABILITY:
        availability = "available"
    return ModelCapability(
        model=model_id,
        display=str(raw.get("display") or raw.get("display_name") or raw.get("label") or model_id),
        family=str(raw.get("family") or "").strip() or infer_family(model_id),
        aliases=tuple(str(a).strip() for a in raw.get("aliases") or () if str(a).strip()),
        reasoning_efforts=efforts,
        default_effort=default_effort,
        availability=availability,
        free=bool(raw.get("free", False)),
    )


def _json_doc(text: str) -> Any:
    """Parse a JSON document out of CLI stdout; ``None`` when absent.

    Whole-output first (``<cli> models --json``), then the longest
    single-line JSON object — CLIs may print banner/status lines around
    the payload.
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    for line in sorted(stripped.splitlines(), key=len, reverse=True):
        line = line.strip()
        if not line.startswith(("{", "[")):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def parse_models_payload(provider: str, text: str) -> tuple[list[Any], dict[str, Any]]:
    """Parse discovery stdout → ``(raw model entries, report meta)``.

    Report meta carries account-level fields the CLI volunteered:
    ``plan`` (subscription descriptor) and ``families`` (model-family
    names the subscription exposes). When the output is not JSON, each
    non-empty line's first token is treated as a model id.
    """
    meta: dict[str, Any] = {"plan": None, "families": ()}
    doc = _json_doc(text)
    if isinstance(doc, list):
        return doc, meta
    if isinstance(doc, dict):
        models = doc.get("models")
        raw = models if isinstance(models, list) else []
        plan = doc.get("plan") or doc.get("subscription") or doc.get("tier")
        families = doc.get("families")
        meta["plan"] = str(plan) if plan is not None else None
        if isinstance(families, (list, tuple)):
            meta["families"] = tuple(str(f) for f in families)
        elif isinstance(families, Mapping):
            meta["families"] = tuple(str(f) for f in families)
        return raw, meta
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*•> ")
        match = _MODEL_ID_RE.match(line)
        token = match.group(0) if match else ""
        if token and not token.endswith(":"):
            entries.append({"id": token})
    return entries, meta


# ------------------------------------------------------------- report


@dataclass(frozen=True)
class AccountCapabilities:
    """Normalized capability report for one provider account."""

    account_id: str
    provider: str
    source: str  # ``REPORT_SOURCES``
    refreshed_at: str
    stale: bool
    models: tuple[ModelCapability, ...] = ()
    plan: str | None = None
    families: tuple[str, ...] = ()
    cli_version: str | None = None
    credential_fingerprint: str | None = None
    error: str | None = None

    def find(self, model: str) -> ModelCapability | None:
        for entry in self.models:
            if entry.matches(model):
                return entry
        return None

    def default_model(self) -> str | None:
        for entry in self.models:
            if entry.availability == "available":
                return entry.model
        return self.models[0].model if self.models else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "provider": self.provider,
            "source": self.source,
            "refreshed_at": self.refreshed_at,
            "stale": self.stale,
            "plan": self.plan,
            "families": list(self.families),
            "cli_version": self.cli_version,
            "credential_fingerprint": self.credential_fingerprint,
            "error": self.error,
            "models": [m.to_dict() for m in self.models],
        }

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> AccountCapabilities:
        models = raw.get("models")
        return AccountCapabilities(
            account_id=str(raw.get("account_id") or ""),
            provider=str(raw.get("provider") or ""),
            source=str(raw.get("source") or "declared"),
            refreshed_at=str(raw.get("refreshed_at") or ""),
            stale=bool(raw.get("stale", False)),
            plan=(str(raw["plan"]) if raw.get("plan") is not None else None),
            families=tuple(str(f) for f in raw.get("families") or ()),
            cli_version=(str(raw["cli_version"]) if raw.get("cli_version") is not None else None),
            credential_fingerprint=(
                str(raw["credential_fingerprint"])
                if raw.get("credential_fingerprint") is not None
                else None
            ),
            error=(str(raw["error"]) if raw.get("error") is not None else None),
            models=tuple(
                entry
                for entry in (
                    ModelCapability.from_dict(e)
                    if isinstance(e, Mapping)
                    else normalize_model(str(raw.get("provider") or ""), e)
                    for e in models or ()
                )
                if entry is not None
            ),
        )


# ------------------------------------------------------------- store


class CapabilityStore(Protocol):
    """Persistence seam for per-account capability reports."""

    def get_report(self, account_id: str) -> dict[str, Any] | None: ...

    def put_report(self, account_id: str, report: dict[str, Any]) -> None: ...

    def delete_report(self, account_id: str) -> None: ...

    def iter_reports(self) -> Iterable[tuple[str, dict[str, Any]]]: ...


class InMemoryCapabilityStore:
    def __init__(self) -> None:
        self._reports: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get_report(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            raw = self._reports.get(account_id)
            return dict(raw) if raw is not None else None

    def put_report(self, account_id: str, report: dict[str, Any]) -> None:
        with self._lock:
            self._reports[account_id] = dict(report)

    def delete_report(self, account_id: str) -> None:
        with self._lock:
            self._reports.pop(account_id, None)

    def iter_reports(self) -> Iterable[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [(k, dict(v)) for k, v in sorted(self._reports.items())]


class FileCapabilityStore:
    """Local durable store: ``root/<account_id>.json``, atomic writes."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def _path(self, account_id: str) -> Path:
        validate_account_id(account_id)
        return self._root / f"{account_id}.json"

    def get_report(self, account_id: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(self._path(account_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def put_report(self, account_id: str, report: dict[str, Any]) -> None:
        path = self._path(account_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(path)

    def delete_report(self, account_id: str) -> None:
        with self._lock:
            self._path(account_id).unlink(missing_ok=True)

    def iter_reports(self) -> Iterable[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        try:
            entries = sorted(self._root.glob("*.json"))
        except OSError:
            return out
        for path in entries:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            out.append((path.stem, raw if isinstance(raw, dict) else {}))
        return out


class ModalDictCapabilityStore:
    """Production store backed by ``modal.Dict sbx-capabilities``."""

    def __init__(self, name: str = CAPABILITIES_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def get_report(self, account_id: str) -> dict[str, Any] | None:
        validate_account_id(account_id)
        raw = self._d().get(account_id)
        return raw if isinstance(raw, dict) else None

    def put_report(self, account_id: str, report: dict[str, Any]) -> None:
        validate_account_id(account_id)
        self._d().put(account_id, dict(report))

    def delete_report(self, account_id: str) -> None:
        validate_account_id(account_id)
        try:
            self._d().pop(account_id)
        except KeyError:
            return

    def iter_reports(self) -> Iterable[tuple[str, dict[str, Any]]]:
        return [(k, v) for k, v in self._d().items() if isinstance(k, str) and isinstance(v, dict)]


def select_capability_store(
    *,
    store_dir: Path | str | None = None,
    backend: str | None = None,
) -> CapabilityStore:
    """Pick the capability store the same way ``select_store`` picks the
    account store: Modal Dict on ``SBX_BACKEND=modal``, else a file store
    under ``SBX_CAPABILITY_STORE_DIR`` / ``$XDG_STATE_HOME/sbx-browser/
    capabilities``."""
    kind = backend if backend is not None else os.environ.get("SBX_BACKEND", "local")
    if kind == "modal":
        return ModalDictCapabilityStore(os.environ.get(_DICT_ENV) or CAPABILITIES_DICT_NAME)
    root = store_dir or os.environ.get(_STORE_DIR_ENV)
    if not root:
        xdg = os.environ.get("XDG_STATE_HOME")
        root = Path(xdg) if xdg else Path.home() / ".local" / "state"
        root = Path(root) / "sbx-browser" / "capabilities"
    return FileCapabilityStore(root)


# ------------------------------------------------------------- probe


@dataclass(frozen=True)
class DiscoveryOutcome:
    """Result of one in-sandbox capability-discovery exec."""

    ok: bool
    error: str | None = None  # machine code when not ok
    detail: str | None = None
    plan: str | None = None
    families: tuple[str, ...] = ()
    models: tuple[ModelCapability, ...] = ()


class SandboxCapabilityProbe:
    """Authoritative probe: ``runner init`` + the provider's own model list.

    Mirrors ``SandboxAuthVerifyProbe``: the credential restores inside a
    throwaway sandbox, then the discovery argv execs with ``HOME`` pinned
    at the restored home and credential env scrubbed — the provider's own
    answer is the capability truth.
    """

    def __init__(
        self,
        backend: Any,
        runner_cmd: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        bin_env: Mapping[str, str] | None = None,
    ) -> None:
        self._backend = backend
        self._runner_cmd = list(runner_cmd)
        self._env = dict(env or {})
        self._bin_env = bin_env

    def _exec_discovery(
        self, handle: Any, account: Account, env: Mapping[str, str]
    ) -> DiscoveryOutcome:
        argv = provider_discovery_argv(account.provider, env=self._bin_env)
        if argv is None:
            return DiscoveryOutcome(
                ok=False,
                error="probe_unavailable",
                detail=f"no discovery command for provider {account.provider!r}",
            )
        home = handle.root / "home"
        check_env = {
            k: v
            for k, v in env.items()
            if k
            not in (
                _CREDENTIAL_ENV,
                _ACCOUNT_ID_ENV,
                "CODEX_AUTH_JSON",
                "SBX_PROVIDER_API_KEY",
                "SBX_PROVIDER_BASE_URL",
            )
        }
        check_env["HOME"] = str(home)
        check_env.setdefault("PATH", os.environ.get("PATH", os.defpath))
        if account.provider in ("devin", "opencode"):
            check_env.update(
                {
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_CACHE_HOME": str(home / ".cache"),
                    "XDG_DATA_HOME": str(home / ".local" / "share"),
                    "XDG_STATE_HOME": str(home / ".local" / "state"),
                }
            )
        try:
            proc = self._backend.exec(handle, argv, env=check_env)
            output = "\n".join(proc.stdout)
            code = proc.wait()
        except Exception:
            return DiscoveryOutcome(ok=False, error="probe_unavailable", detail="exec_failed")
        if code != 0:
            # A nonzero exit carrying an auth marker is a credential
            # problem, not a catalog problem — surface it as such.
            error = (
                "auth_invalid"
                if any(marker in output.lower() for marker in _AUTH_FAIL_MARKERS)
                else "discovery_failed"
            )
            return DiscoveryOutcome(ok=False, error=error, detail=f"exit {code}")
        raw_models, meta = parse_models_payload(account.provider, output)
        models = tuple(
            m
            for m in (normalize_model(account.provider, raw) for raw in raw_models)
            if m is not None
        )
        return DiscoveryOutcome(
            ok=True,
            plan=meta.get("plan"),
            families=tuple(meta.get("families") or ()),
            models=models,
        )

    def discover(self, account: Account, blob: dict[str, Any] | None) -> DiscoveryOutcome:
        from control.backend import SandboxSpec
        from control.sandbox_io import sandbox_env

        if blob is None and not account.secret_name:
            return DiscoveryOutcome(
                ok=False,
                error="no_credential",
                detail="no stored credential blob and no secret_name",
            )
        handle = None
        try:
            handle = self._backend.create(
                SandboxSpec(
                    tags={
                        "purpose": "capability-discovery",
                        "provider": account.provider,
                        "account_id": account.id,
                    },
                    secrets=[account.secret_name] if account.secret_name else [],
                    env=self._env,
                )
            )
            extra = {_ACCOUNT_ID_ENV: account.id}
            if blob:
                extra[_CREDENTIAL_ENV] = json.dumps(blob, ensure_ascii=False)
            env = sandbox_env(handle, extra)
            model = account.models[0] if account.models else "gpt-5.6-luna"
            argv = [
                *self._runner_cmd,
                "init",
                "--auth",
                "auth_json",
                "--model",
                model,
                "--provider",
                account.provider,
                "--account-id",
                account.id,
            ]
            proc = self._backend.exec(handle, argv, env=env)
            for _ in proc.stdout:
                pass
            code = proc.wait()
            if code == 0:
                return self._exec_discovery(handle, account, env)
            if code == 5:
                return DiscoveryOutcome(ok=False, error="auth_invalid")
            return DiscoveryOutcome(
                ok=False, error="init_failed", detail=f"runner init exited {code}"
            )
        except NotImplementedError:
            return DiscoveryOutcome(
                ok=False, error="probe_unavailable", detail="sandbox backend not implemented"
            )
        except Exception:
            return DiscoveryOutcome(ok=False, error="provider_unavailable", detail="exec_failed")
        finally:
            if handle is not None:
                try:
                    self._backend.terminate(handle)
                except Exception:
                    pass


# ------------------------------------------------------------- service


def credential_fingerprint(blob: dict[str, Any] | None) -> str | None:
    """Stable sha256-16 fingerprint of a credential blob.

    Content never leaves the hash — the fingerprint only detects *changes*
    so any credential write path invalidates the stored report.
    """
    if not blob:
        return None
    payload = json.dumps(blob, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def cli_versions_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """provider → pinned CLI version for staleness checks.

    ``SBX_CLI_VERSIONS`` (JSON object) wins; ``SBX_<PROVIDER>_VERSION``
    fills gaps. Absent everywhere → no CLI-version staleness axis.
    """
    env = os.environ if env is None else env
    versions: dict[str, str] = {}
    raw = env.get("SBX_CLI_VERSIONS")
    if raw:
        try:
            doc = json.loads(raw)
            if isinstance(doc, dict):
                versions.update({str(k): str(v) for k, v in doc.items() if v})
        except json.JSONDecodeError:
            pass
    for provider in PROVIDER_DISCOVERY_COMMANDS:
        value = env.get(f"SBX_{provider.upper()}_VERSION")
        if value and provider not in versions:
            versions[provider] = value
    return versions


class CapabilityService:
    """Discovery, normalization and caching over an ``AccountRegistry``."""

    def __init__(
        self,
        registry: Any,
        *,
        store: CapabilityStore | None = None,
        probe: Any = None,
        ttl_s: int | None = None,
        bin_env: Mapping[str, str] | None = None,
        cli_versions: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._store = store if store is not None else InMemoryCapabilityStore()
        self._probe = probe
        self._bin_env = bin_env
        self._env = os.environ if env is None else env
        if ttl_s is None:
            try:
                ttl_s = int(self._env.get(_TTL_ENV) or DEFAULT_TTL_S)
            except ValueError:
                ttl_s = DEFAULT_TTL_S
        self._ttl_s = max(0, ttl_s)
        self._cli_versions = (
            cli_versions_from_env(self._env) if cli_versions is None else dict(cli_versions)
        )
        self._lock = threading.Lock()

    # -- freshness ------------------------------------------------------

    def _fingerprint(self, account_id: str) -> str | None:
        getter = getattr(self._registry, "get_credential_blob", None)
        if not callable(getter):
            return None
        try:
            return credential_fingerprint(getter(account_id))
        except Exception:
            return None

    def _is_stale(self, report: AccountCapabilities, now_ts: float) -> bool:
        if report.stale:
            return True
        if now_ts - _epoch(report.refreshed_at) > self._ttl_s:
            return True
        if report.credential_fingerprint != self._fingerprint(report.account_id):
            return True
        version = self._cli_versions.get(report.provider)
        if version is not None and report.cli_version != version:
            return True
        return False

    # -- reports ---------------------------------------------------------

    def _declared(self, account: Account, *, error: str | None = None) -> AccountCapabilities:
        """Last-resort truth: the account's own model declaration."""
        models = tuple(
            m
            for m in (normalize_model(account.provider, {"id": mid}) for mid in account.models)
            if m is not None
        )
        return AccountCapabilities(
            account_id=account.id,
            provider=account.provider,
            source="declared",
            refreshed_at=account.created_at or _iso_now(),
            stale=False,
            models=models,
            error=error,
        )

    def report(
        self, account: Account | str, *, now_ts: float | None = None
    ) -> AccountCapabilities | None:
        """Last-good report for an account (stored, else declared fallback)."""
        if isinstance(account, str):
            resolved = self._registry.get(account)
            if resolved is None:
                return None
            account = resolved
        now_ts = now_ts if now_ts is not None else datetime.now(UTC).timestamp()
        raw = self._store.get_report(account.id)
        if raw is not None:
            report = AccountCapabilities.from_dict(raw)
            return AccountCapabilities(
                **{
                    **report.__dict__,
                    "stale": self._is_stale(report, now_ts),
                }
            )
        return self._declared(account)

    def catalog(self, providers: Iterable[str] | None = None) -> dict[str, AccountCapabilities]:
        """account_id → report, optionally restricted to enabled providers."""
        allowed = set(providers) if providers is not None else None
        out: dict[str, AccountCapabilities] = {}
        for account in self._registry.list():
            if allowed is not None and account.provider not in allowed:
                continue
            report = self.report(account)
            if report is not None:
                out[account.id] = report
        return out

    # -- refresh / invalidation ------------------------------------------

    def refresh(self, account: Account | str) -> AccountCapabilities | None:
        """Re-run authenticated discovery for one account.

        On success the fresh report is persisted as the new last-good. On
        failure the stored report is kept and the error recorded on it —
        never a fabricated model list.
        """
        if isinstance(account, str):
            resolved = self._registry.get(account)
            if resolved is None:
                return None
            account = resolved
        if self._probe is None:
            return self._declared(account, error="probe_unavailable")
        with self._lock:
            blob_getter = getattr(self._registry, "get_credential_blob", None)
            blob = blob_getter(account.id) if callable(blob_getter) else None
            outcome = self._probe.discover(account, blob)
            if outcome.ok:
                report = AccountCapabilities(
                    account_id=account.id,
                    provider=account.provider,
                    source="cli",
                    refreshed_at=_iso_now(),
                    stale=False,
                    models=outcome.models,
                    plan=outcome.plan,
                    families=outcome.families,
                    cli_version=self._cli_versions.get(account.provider),
                    credential_fingerprint=credential_fingerprint(blob),
                    error=None,
                )
                self._store.put_report(account.id, report.to_dict())
                return report
            # Last-good fallback: keep the stored report, annotate the failure.
            prior = self.report(account)
            assert prior is not None  # account exists
            failed = AccountCapabilities(
                **{
                    **prior.__dict__,
                    "stale": True,
                    "error": outcome.error or "discovery_failed",
                }
            )
            if prior.source == "cli":
                # Persist the error on the stored report so it survives restarts.
                self._store.put_report(account.id, failed.to_dict())
            return failed

    def refresh_all(self, providers: Iterable[str] | None = None) -> dict[str, AccountCapabilities]:
        out: dict[str, AccountCapabilities] = {}
        allowed = set(providers) if providers is not None else None
        for account in self._registry.list():
            if allowed is not None and account.provider not in allowed:
                continue
            report = self.refresh(account)
            if report is not None:
                out[account.id] = report
        return out

    def invalidate(self, account_id: str) -> None:
        """Drop the stored report (account removal / explicit reset)."""
        self._store.delete_report(account_id)

    # -- create-time resolution -------------------------------------------

    def resolve_model(
        self, account: Account | None, requested: str | None
    ) -> tuple[str | None, str | None]:
        """``(canonical model, refusal)`` for an ``AgentSpec.model``.

        A non-empty catalog is proof: an explicit model must match an entry
        or alias, and is normalized to the canonical id. An empty catalog
        proves nothing — explicit ids pass through and the caller falls
        back to the account/provider default.
        """
        if account is None:
            return requested, None
        report = self.report(account)
        if report is None or not report.models:
            return requested, None
        if requested is None:
            return report.default_model() or requested, None
        entry = report.find(requested)
        if entry is None:
            return (
                None,
                f"model {requested!r} is not available on account {account.id!r} "
                f"(available: {[m.model for m in report.models if m.availability == 'available']})",
            )
        if entry.availability != "available":
            return (
                None,
                f"model {entry.model!r} is {entry.availability} on account {account.id!r}",
            )
        return entry.model, None

    def effort_refusal(
        self,
        provider: str,
        account: Account | None,
        effort: str | None,
        model: str | None,
    ) -> str | None:
        """Model-aware effort refusal; falls back to the provider surface."""
        if effort is None:
            return None
        if account is None:
            return effort_error(provider, effort)
        report = self.report(account)
        entry = report.find(model) if report is not None and model else None
        if entry is None:
            return effort_error(account.provider, effort)
        if not entry.reasoning_efforts:
            return (
                f"model {entry.model!r} does not support reasoning_effort on account {account.id!r}"
            )
        if effort not in entry.reasoning_efforts:
            return (
                f"model {entry.model!r} does not support reasoning_effort {effort!r} "
                f"on account {account.id!r} (supported: {list(entry.reasoning_efforts)})"
            )
        return None
