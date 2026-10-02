"""``sbx deploy`` / ``sbx upgrade`` orchestration (SOR-98).

Deploy is a fixed pipeline of idempotent steps — every step is check-then-
act, so reruns converge instead of duplicating resources, and a failure
reports the completed steps, the failed step, and the remediation hint.
Nothing writes to Modal until the Modal auth check passes, so a failed run
never leaves half-initialized resources behind.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from control.accounts import is_valid_account_id

import sbx
from sbx.config import (
    BootstrapConfig,
    ResolvedConfig,
    basic_auth_path,
    deploy_state_path,
    key_path,
    load_file_values,
    save,
    state_dir,
    validate_providers,
)
from sbx.credentials import scan_credentials
from sbx.errors import BootstrapError
from sbx.httpapi import ApiError, V1Client
from sbx.keys import fingerprint, load_or_create_key
from sbx.plane import Plane
from sbx.prereqs import check_modal_package, check_python, require


@dataclass(frozen=True)
class StepResult:
    name: str
    changed: bool
    detail: str


@dataclass(frozen=True)
class DeployReport:
    steps: tuple[StepResult, ...]
    base_url: str
    version: str
    key_created: bool
    key_rotated: bool = False
    cli_versions: dict[str, str] | None = None
    # SOR-217: provider → why its runtime is degraded. The Platform deploy
    # succeeded; these are provider-health gaps, never deploy failures.
    degraded_providers: dict[str, str] | None = None


def app_version() -> str:
    try:
        return importlib.metadata.version("sbx-browser")
    except importlib.metadata.PackageNotFoundError:
        return sbx.__version__


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _write_state(env: Mapping[str, str], payload: dict[str, Any]) -> Path:
    path = deploy_state_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def read_deploy_state(env: Mapping[str, str]) -> dict[str, Any]:
    try:
        data = json.loads(deploy_state_path(env).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_basic_auth(env: Mapping[str, str], user: str, password: str) -> Path:
    path = basic_auth_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # force the mode even when the file already existed
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"user": user, "password": password}) + "\n")
    return path


def _read_basic_auth(env: Mapping[str, str]) -> dict[str, str] | None:
    try:
        data = json.loads(basic_auth_path(env).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("user") and data.get("password"):
        return {"user": str(data["user"]), "password": str(data["password"])}
    return None


def _require_modal_auth(plane: Plane, *, login: Callable[[], str | None] | None = None) -> str:
    """Prove Modal auth; when unauthenticated and ``login`` is provided, run
    it once (interactive ``modal token new`` on the TTY) and re-probe so a
    deploy resumes inside the same invocation (SOR-209). Non-interactive
    callers (CI, MODAL_TOKEN_* env) never get a ``login`` callable."""
    workspace = plane.workspace()
    if workspace is None and login is not None:
        workspace = login()
    if workspace is None:
        raise BootstrapError(
            "not authenticated with Modal",
            hint="run `modal token new` (or `modal setup`), or export "
            "MODAL_TOKEN_ID/MODAL_TOKEN_SECRET, then rerun `sbx deploy`",
            code="modal_auth_missing",
        )
    return workspace


def _ensure_bootstrap_secret(
    cfg: BootstrapConfig, plane: Plane, env: Mapping[str, str]
) -> tuple[StepResult, bool, bool]:
    """Ensure ``sbx-v1-bootstrap`` holds the local key.

    Control only ever sees the token inside the Secret env and stores its
    sha256; when the local key file was just minted while a stale Secret
    exists, the remote value is unrecoverable — rotate it so local and
    remote agree again.
    """
    token, created = load_or_create_key(key_path(env))
    rotated = False
    changed = False
    if created and cfg.bootstrap_secret in plane.list_secret_names():
        plane.delete_secret(cfg.bootstrap_secret)
        rotated = True
    if plane.ensure_secret(cfg.bootstrap_secret, {"SBX_V1_BOOTSTRAP_KEY": token}):
        changed = True
    detail = f"{cfg.bootstrap_secret} seeded ({fingerprint(token)})"
    if rotated:
        detail = f"{cfg.bootstrap_secret} rotated ({fingerprint(token)})"
    return StepResult("secret:bootstrap", changed or rotated, detail), created, rotated


def _ensure_basic_secret(cfg: BootstrapConfig, plane: Plane, env: Mapping[str, str]) -> StepResult:
    """Ensure the internal ``/api/*`` Basic-auth Secret exists.

    Generated credentials are stored locally (0600) for the web board; the
    Secret itself is never echoed.
    """
    if cfg.basic_secret in plane.list_secret_names():
        detail = f"{cfg.basic_secret} present"
        if _read_basic_auth(env) is None:
            detail += " (credentials managed remotely; local copy absent)"
        return StepResult("secret:basic", False, detail)
    creds = _read_basic_auth(env)
    if creds is None:
        creds = {"user": "sbx", "password": secrets.token_urlsafe(24)}
        _write_basic_auth(env, creds["user"], creds["password"])
    plane.ensure_secret(
        cfg.basic_secret,
        {"SBX_BASIC_USER": creds["user"], "SBX_BASIC_PASS": creds["password"]},
    )
    return StepResult("secret:basic", True, f"{cfg.basic_secret} created (local copy saved)")


def _codex_secret_preflight(
    cfg: BootstrapConfig, plane: Plane, env: Mapping[str, str]
) -> tuple[StepResult, str | None]:
    """Report the shared Codex credential Secret's presence.

    SOR-217: a missing provider credential degrades codex runtime health —
    it is never a Platform deploy failure. Returns ``(step, reason)``;
    ``reason`` is the degrade detail when the Secret is absent. The step
    detail carries the remediation, which reflects the local scan: no
    ``~/.codex/auth.json`` → ``codex login`` first; unusable file → its
    remediation; clean file → the create command. Never exposes credential
    contents (SOR-115).
    """
    if cfg.codex_secret in plane.list_secret_names():
        return StepResult("secret:codex", False, f"{cfg.codex_secret} present"), None
    scan = scan_credentials(("codex",), env=env)[0]
    create = f'`modal secret create {cfg.codex_secret} CODEX_AUTH_JSON="$(cat ~/.codex/auth.json)"`'
    if scan.ok:
        hint = (
            f"create it with {create}, or set "
            "SBX_CODEX_SECRET_NAME to an existing Secret, then rerun `sbx deploy`"
        )
    else:
        hint = (
            f"{scan.hint}; then create the Secret with {create}, or set "
            "SBX_CODEX_SECRET_NAME to an existing Secret"
        )
    return (
        StepResult("secret:codex", False, f"{cfg.codex_secret} missing — {hint}"),
        f"credential Secret {cfg.codex_secret!r} missing ({scan.detail})",
    )


def _require_github_secret(cfg: BootstrapConfig, existing: set[str]) -> StepResult | None:
    """Fail-before-write check for the optional GitHub bridge Secret (SOR-117).

    Only applies when the resolved config armed the bridge
    (``github.ephemeral`` / ``SBX_GITHUB_EPHEMERAL``) AND named a Secret
    (``github.secret_name`` / ``SBX_GITHUB_SECRET_NAME``) — the named Secret
    holds GH_TOKEN/GITHUB_TOKEN so a *remote* control app sees the token in
    its env and forwards it into sandboxes. A missing named Secret is an
    explicit deploy failure rather than a silently inert bridge.
    """
    name = cfg.github_secret_name
    if not cfg.github_ephemeral or not name:
        return None
    if name in existing:
        return StepResult("secret:github", False, f"{name} present")
    raise BootstrapError(
        f"GitHub bridge Secret {name!r} is missing",
        hint=f"create it with `modal secret create {name} GH_TOKEN=...` "
        "(least privilege: a fine-grained PAT scoped to the agent repos), or "
        "clear github.secret_name / SBX_GITHUB_SECRET_NAME, then rerun `sbx deploy`",
        code="secret_missing",
    )


def _require_github_app_secret(cfg: BootstrapConfig, existing: set[str]) -> StepResult | None:
    """Fail-before-write check for the GitHub App Secret (SOR-177).

    Only applies when the resolved config identifies an App
    (``github_app.app_id`` / ``SBX_GITHUB_APP_ID``) AND named a Secret
    (``github_app.secret_name`` / ``SBX_GITHUB_APP_SECRET_NAME``) — the
    named Secret holds ``SBX_GITHUB_APP_PRIVATE_KEY`` so the remote
    control app can mint installation tokens. A missing named Secret is
    an explicit deploy failure rather than a silently dead authorize
    flow.
    """
    name = cfg.github_app_secret_name
    if not cfg.github_app_id or not name:
        return None
    if name in existing:
        return StepResult("secret:github-app", False, f"{name} present")
    raise BootstrapError(
        f"GitHub App Secret {name!r} is missing",
        hint=f"create it with `modal secret create {name} "
        'SBX_GITHUB_APP_PRIVATE_KEY="$(cat private-key.pem)"` (the App\'s '
        "generated private key), or clear github_app.secret_name / "
        "SBX_GITHUB_APP_SECRET_NAME, then rerun `sbx deploy`",
        code="secret_missing",
    )


def _check_account_secrets(
    cfg: BootstrapConfig, plane: Plane, existing: set[str]
) -> tuple[StepResult, dict[str, str]]:
    """Degrade-check enabled providers' account Secrets (SOR-217).

    Every enabled-provider account record that names a Secret needs it at
    sandbox create. Deployment-managed names (``<account_secret_prefix><id>``)
    are satisfied by a stored ``credential/<id>`` blob — the materialize step
    below recreates them — while anything else must already exist. Accounts
    of providers that are not enabled carry no prerequisite.

    Missing Secrets degrade the affected provider — they never abort the
    Platform deploy. Returns ``(step, {provider: reason})``.
    """
    if not plane.has_dict(cfg.accounts_dict):
        return StepResult("credentials:preflight", False, "no account registry yet"), {}
    try:
        items = plane.dict_items(cfg.accounts_dict)
    except Exception as exc:
        raise BootstrapError(
            f"cannot read account credential store {cfg.accounts_dict!r}: {exc}",
            hint="check Modal Dict access, then rerun `sbx deploy`",
            code="account_credentials_unreadable",
        ) from exc

    blobs = {
        key[len("credential/") :]
        for key, _ in items
        if isinstance(key, str) and key.startswith("credential/")
    }
    missing_by_provider: dict[str, list[str]] = {}
    referenced = 0
    for key, value in items:
        if not (isinstance(key, str) and key.startswith("account/") and isinstance(value, dict)):
            continue
        provider = str(value.get("provider") or "")
        if provider not in cfg.providers:
            continue  # provider not enabled — its credentials are not a prerequisite
        if str(value.get("status") or "active") == "disabled":
            continue  # never scheduled, so its Secret is never mounted
        name = str(value.get("secret_name") or "").strip()
        if not name:
            continue
        referenced += 1
        if name in existing:
            continue
        account_id = key[len("account/") :]
        if name == f"{cfg.account_secret_prefix}{account_id}" and account_id in blobs:
            continue  # materialized from the stored blob below
        missing_by_provider.setdefault(provider, []).append(f"{name} (account {account_id})")
    if missing_by_provider:
        missing = [
            f"{name} [{provider}]"
            for provider, names in missing_by_provider.items()
            for name in names
        ]
        preview = ", ".join(sorted(missing)[:5])
        if len(missing) > 5:
            preview += f", +{len(missing) - 5} more"
        reasons = {
            provider: f"account credential Secret(s) missing: {', '.join(sorted(names))}"
            for provider, names in missing_by_provider.items()
        }
        return (
            StepResult(
                "credentials:preflight",
                False,
                f"missing account credential Secret(s): {preview} — "
                "import with `python -m control.onboarding --modal import "
                "--provider <provider> --from <file>`, or create the Secret "
                "with `modal secret create`, then rerun `sbx deploy`",
            ),
            reasons,
        )
    detail = (
        f"{referenced} referenced account Secret(s) satisfied"
        if referenced
        else "no account Secrets required"
    )
    return StepResult("credentials:preflight", False, detail), {}


def _resolve_cli_versions(
    config: BootstrapConfig,
    env: Mapping[str, str],
    *,
    versions_lock: str | None,
    fetch: Any = None,
    host_probe: Any = None,
    degraded: dict[str, list[str]] | None = None,
    unbuildable: set[str] | None = None,
) -> tuple[StepResult, Any, Path]:
    """Resolve + freeze provider CLI versions for this deployment (SOR-175).

    Runs once per deploy, before any image build: ``latest`` requests in
    ``runtime/packages.txt`` (or ``SBX_*_VERSION`` overrides) resolve
    upstream on the build host, pins pass through, and a ``--versions-lock``
    / ``SBX_VERSIONS_LOCK`` file replays a previous deployment's frozen set
    verbatim — the rollback lane. The outcome freezes to
    ``<state>/cli-versions.json`` as the deployment's version evidence and
    is passed to every image build so they all carry identical versions.

    SOR-217: a provider that cannot resolve degrades rather than fails the
    deploy — ``blocked`` entries are grafted into the frozen lock as
    ``unresolved`` evidence, and the provider is recorded in ``degraded``
    (+ ``unbuildable`` when given) so the caller skips its image build.
    """
    from runtime.image import load_packages
    from runtime.provider_runtime import spec_or_none
    from runtime.versions import (
        VERSION_ENVS,
        VersionEntry,
        VersionResolutionError,
        lock_out_path_for,
        resolve_versions,
        write_lock,
    )

    degraded = degraded if degraded is not None else {}
    unbuildable = unbuildable if unbuildable is not None else set()
    providers = set(config.providers) | {"codex"}
    # SOR-212/SOR-215: the local-assisted lane (host-binary providers, today
    # agy/grok) may not block a Platform deploy. A provider whose build-host
    # CLI is absent is dropped from resolution — its entry is grafted below
    # as ``unresolved`` so the frozen lock still carries the evidence — and
    # the image step degrades it instead of failing.
    blocked: dict[str, str] = {}
    for provider in sorted(providers):
        rspec = spec_or_none(provider)
        if rspec is not None and rspec.local_assisted and rspec.host_bin(env) is None:
            providers.discard(provider)
            reason = (
                f"{rspec.cli} CLI not found on the build host "
                f"(set {rspec.host_bin_env} or install it)"
            )
            blocked[provider] = reason
            degraded.setdefault(provider, []).append(reason)
            unbuildable.add(provider)
    while True:
        try:
            resolved = resolve_versions(
                env=env,
                # ``codex`` always resolves: its CLI rides in the base recipe of
                # every provider image, enabled or not.
                providers=providers,
                fetch=fetch,
                host_probe=host_probe,
                lock=Path(versions_lock) if versions_lock else None,
            )
            break
        except VersionResolutionError as exc:
            # SOR-217: provider version resolution is provider health, not
            # Platform health — any provider that cannot resolve degrades
            # (the local-assisted lane degraded this way since SOR-215).
            if exc.provider not in providers:
                raise BootstrapError(
                    f"cannot resolve provider CLI versions: {exc}",
                    hint=exc.hint
                    or "check runtime/packages.txt and SBX_*_VERSION "
                    "overrides, or replay a frozen set via --versions-lock / "
                    "SBX_VERSIONS_LOCK",
                    code="version_resolution_failed",
                ) from exc
            providers.discard(exc.provider)
            blocked[exc.provider] = str(exc)
            degraded.setdefault(exc.provider, []).append(str(exc))
            unbuildable.add(exc.provider)
    if blocked:
        raw_spec = load_packages()
        entries = dict(resolved.entries)
        for provider, reason in blocked.items():
            rspec = spec_or_none(provider)
            requested = env.get(VERSION_ENVS.get(provider, "")) or str(
                getattr(raw_spec, rspec.spec_field)
            )
            entries[provider] = VersionEntry(
                provider=provider,
                requested=requested,
                version=None,
                source="unresolved",
                evidence={"detail": reason},
            )
        resolved = replace(resolved, entries=entries)
    lock_path = write_lock(
        resolved,
        lock_out_path_for(env)
        if env.get("SBX_VERSIONS_LOCK_OUT")
        else state_dir(env) / "cli-versions.json",
    )
    detail = (
        ", ".join(f"{p} {v}" for p, v in sorted(resolved.cli_versions().items()))
        or "nothing to resolve"
    )
    return (
        StepResult("versions", True, f"{detail} (frozen to {lock_path.name})"),
        resolved,
        lock_path,
    )


def _materialize_account_secrets(cfg: BootstrapConfig, plane: Plane) -> StepResult:
    """Materialize deployment-scoped account blobs as Modal Secrets.

    ``control.onboarding --modal import`` intentionally stores credential blobs
    in the durable accounts Dict.  Runtime sandboxes mount named Secrets, so
    deploy is the bridge: for accounts using this deployment's managed
    ``account_secret_prefix``, refresh the Secret from the stored blob.

    Accounts with an empty or custom Secret name are externally managed and
    are never overwritten here.  Credential values are never included in the
    report.
    """
    try:
        items = plane.dict_items(cfg.accounts_dict)
    except Exception as exc:
        raise BootstrapError(
            f"cannot read account credential store {cfg.accounts_dict!r}: {exc}",
            hint="check Modal Dict access, then rerun `sbx deploy`",
            code="account_credentials_unreadable",
        ) from exc

    records: dict[str, dict[str, Any]] = {}
    blobs: dict[str, dict[str, Any]] = {}
    for key, value in items:
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        if key.startswith("account/"):
            records[key[len("account/") :]] = value
        elif key.startswith("credential/"):
            blobs[key[len("credential/") :]] = value

    existing = plane.list_secret_names()
    materialized = 0
    refreshed = 0
    for account_id, blob in sorted(blobs.items()):
        if not is_valid_account_id(account_id):
            continue
        record = records.get(account_id)
        if record is None:
            continue
        provider = str(record.get("provider") or blob.get("provider") or "")
        if provider not in cfg.providers:
            continue  # provider not enabled — nothing to materialize for it
        secret_name = str(record.get("secret_name") or "").strip()
        expected = f"{cfg.account_secret_prefix}{account_id}"
        if secret_name != expected:
            # Empty/custom names are explicitly external-management lanes.
            continue
        if secret_name in existing:
            plane.delete_secret(secret_name)
            refreshed += 1
        payload = json.dumps(blob, ensure_ascii=False, separators=(",", ":"))
        plane.ensure_secret(secret_name, {"SBX_ACCOUNT_CREDENTIAL": payload})
        existing.add(secret_name)
        materialized += 1

    if materialized == 0:
        detail = "no deployment-managed account credentials to materialize"
    else:
        detail = f"{materialized} account credential Secret(s) ready"
        if refreshed:
            detail += f" ({refreshed} refreshed)"
    return StepResult("credentials:accounts", bool(materialized), detail)


def _repo_root() -> Path:
    """Repo checkout root — ``src/sbx/deploy.py`` → repo root."""
    return Path(__file__).resolve().parents[2]


def _git_sha(repo_root: Path, env: Mapping[str, str]) -> str | None:
    """The checkout's HEAD SHA — ``SBX_GIT_SHA`` wins when set explicitly."""
    override = env.get("SBX_GIT_SHA")
    if override:
        return override
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _console_manifest(dist: Path, *, git_sha: str | None) -> dict[str, Any]:
    """Load (or synthesize) the console build manifest under ``dist``.

    ``npm run build`` writes ``build-manifest.json`` itself; a prebuilt
    ``SBX_CONSOLE_DIST`` without one is hashed here so release evidence
    stays complete either way.
    """
    manifest_path = dist / "build-manifest.json"
    manifest: dict[str, Any] = {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            manifest = data
    except (OSError, json.JSONDecodeError):
        pass
    if not isinstance(manifest.get("files"), list) or not manifest["files"]:
        files = []
        for path in sorted(dist.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(dist).as_posix()
            if rel == "build-manifest.json":
                continue
            files.append(
                {
                    "path": rel,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                }
            )
        manifest["files"] = files
    if not manifest.get("primary_asset"):
        manifest["primary_asset"] = next(
            (
                f
                for f in manifest["files"]
                if f.get("path", "").startswith("assets/") and str(f.get("path")).endswith(".js")
            ),
            None,
        )
    manifest.setdefault("git_sha", git_sha)
    manifest.setdefault("frontend_source", "console/")
    return manifest


def _prepare_console(env: Mapping[str, str]) -> tuple[StepResult, dict[str, Any]]:
    """Produce the SHA-bound ``console/dist`` the control image ships (SOR-266).

    Default lane: ``npm ci`` + ``npm run build`` in ``console/`` with
    ``VITE_API_MODE=http`` (live API, same-origin base) and
    ``VITE_BUILD_SHA=<git HEAD>`` — the build stamps index.html's
    ``sbx-build-sha`` meta and writes ``dist/build-manifest.json``.
    ``SBX_CONSOLE_DIST`` points at a prebuilt artifact instead (the
    operator asserts it matches the deployed source); its manifest is
    still read/recorded.
    """
    repo_root = _repo_root()
    git_sha = _git_sha(repo_root, env)
    override = env.get("SBX_CONSOLE_DIST")
    dist = Path(override).expanduser().resolve() if override else repo_root / "console" / "dist"
    if override:
        if not dist.is_dir() or not (dist / "index.html").is_file():
            raise BootstrapError(
                f"SBX_CONSOLE_DIST={override} does not contain a built console",
                hint="point it at a console/dist build output (with index.html)",
                code="console_dist_missing",
            )
        manifest = _console_manifest(dist, git_sha=git_sha)
        return (
            StepResult("console", False, f"prebuilt {dist} @ {git_sha or 'unknown-sha'}"),
            _frontend_record(dist, manifest, git_sha),
        )

    console_src = repo_root / "console"
    if not (console_src / "package.json").is_file():
        raise BootstrapError(
            "console/ source is missing from this checkout",
            hint="deploy from a full sbx-browser checkout, or set SBX_CONSOLE_DIST "
            "to a prebuilt console/dist",
            code="console_source_missing",
        )
    npm = shutil.which("npm")
    if npm is None:
        raise BootstrapError(
            "npm not found — cannot build the Session Console",
            hint="install Node.js 20+ (npm) on the deploy host, or build "
            "`npm --prefix console ci && npm run build` elsewhere and set "
            "SBX_CONSOLE_DIST to that output",
            code="console_build_unavailable",
        )
    build_env = dict(os.environ)
    build_env["VITE_API_MODE"] = env.get("VITE_API_MODE") or "http"
    build_env["VITE_API_BASE"] = env.get("VITE_API_BASE", "")
    if git_sha:
        build_env["VITE_BUILD_SHA"] = git_sha
    for argv in ([npm, "ci"], [npm, "run", "build"]):
        proc = subprocess.run(
            argv,
            cwd=console_src,
            env=build_env,
            capture_output=True,
            text=True,
            timeout=900,
        )
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr).strip()[-800:]
            raise BootstrapError(
                f"console build failed at `{' '.join(argv[1:])}`: {tail}",
                hint="fix the console build locally (`npm --prefix console ci && "
                "npm --prefix console run build`), then rerun `sbx deploy`",
                code="console_build_failed",
            )
    if not dist.is_dir() or not (dist / "index.html").is_file():
        raise BootstrapError(
            "console build produced no dist/index.html",
            hint="inspect `npm --prefix console run build` output",
            code="console_build_failed",
        )
    manifest = _console_manifest(dist, git_sha=git_sha)
    primary = manifest.get("primary_asset") or {}
    return (
        StepResult(
            "console",
            True,
            f"console/dist @ {git_sha or 'unknown-sha'} "
            f"({len(manifest['files'])} assets, primary {primary.get('path', 'none')})",
        ),
        _frontend_record(dist, manifest, git_sha),
    )


def _frontend_record(dist: Path, manifest: dict[str, Any], git_sha: str | None) -> dict[str, Any]:
    primary = manifest.get("primary_asset") or {}
    return {
        "source": "console/dist",
        "dir": str(dist),
        "git_sha": manifest.get("git_sha") or git_sha,
        "primary_asset": {
            "path": primary.get("path"),
            "sha256": primary.get("sha256"),
        }
        if primary
        else None,
        "asset_count": len(manifest.get("files") or []),
        "manifest": "build-manifest.json",
    }


def _console_probe_detail(frontend: dict[str, Any]) -> str:
    primary = frontend.get("primary_asset") or {}
    sha = (frontend.get("git_sha") or "unknown-sha")[:12]
    return f"/ serves console @ {sha} (primary {primary.get('path', 'none')} hash-verified)"


def _probe_console(
    base_url: str,
    frontend: dict[str, Any],
    *,
    transport: httpx.BaseTransport | None,
) -> None:
    """Fetch the deployed root + build manifest; fail loudly on the legacy UI."""
    url = base_url.rstrip("/")
    try:
        with httpx.Client(transport=transport, timeout=10.0) as client:
            root = client.get(url + "/")
            manifest_resp = client.get(url + "/build-manifest.json")
            primary = frontend.get("primary_asset") or {}
            asset_resp = (
                client.get(url + "/" + str(primary["path"])) if primary.get("path") else None
            )
    except httpx.HTTPError as exc:
        raise BootstrapError(
            f"deployed app did not serve the console at {url}: {exc}",
            hint="cold start can take a minute — rerun `sbx deploy` or check `sbx doctor`",
            code="deploy_verify_failed",
        ) from exc
    html = root.text if root.status_code == 200 else ""
    if (
        root.status_code != 200
        or 'id="root"' not in html
        or "text/html" not in (root.headers.get("content-type") or "")
    ):
        raise BootstrapError(
            f"deployed root at {url} is not the V2 Session Console (status {root.status_code})",
            hint="the deployment is serving the legacy web/ UI — verify the image "
            "ships console/dist and SBX_CONSOLE_DIR is set (SOR-266)",
            code="deploy_verify_failed",
        )
    if manifest_resp.status_code == 200:
        served = manifest_resp.json()
        expected_sha = frontend.get("git_sha")
        served_sha = served.get("git_sha") if isinstance(served, dict) else None
        if expected_sha and served_sha and expected_sha != served_sha:
            raise BootstrapError(
                f"deployed console manifest git_sha {served_sha[:12]} != "
                f"deployed source {str(expected_sha)[:12]}",
                hint="the image baked a stale console/dist — rebuild and rerun `sbx deploy`",
                code="deploy_verify_failed",
            )
    if asset_resp is not None:
        if asset_resp.status_code != 200:
            raise BootstrapError(
                f"deployed console primary asset {primary['path']} answered "
                f"{asset_resp.status_code}",
                hint="the image shipped an incomplete console/dist — rerun `sbx deploy`",
                code="deploy_verify_failed",
            )
        digest = hashlib.sha256(asset_resp.content).hexdigest()
        if primary.get("sha256") and digest != primary["sha256"]:
            raise BootstrapError(
                f"deployed console primary asset hash mismatch for {primary['path']}",
                hint="the served bundle does not match the recorded build — "
                "rebuild and rerun `sbx deploy`",
                code="deploy_verify_failed",
            )


def _probe_v1(
    base_url: str,
    token: str,
    *,
    transport: httpx.BaseTransport | None,
    attempts: int,
    sleep: Callable[[float], None],
) -> None:
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            with V1Client(base_url, token, transport=transport, timeout=10.0) as client:
                client.me()
            return
        except (ApiError, httpx.HTTPError) as exc:
            last = exc
            sleep(1.0)
    raise BootstrapError(
        f"deployed app did not answer /v1/me at {base_url}: {last}",
        hint="run `sbx doctor` for a full check; cold start can take a minute",
        code="deploy_verify_failed",
    )


def deploy(
    cfg: ResolvedConfig,
    plane: Plane,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    probe_attempts: int = 5,
    version: str | None = None,
    versions_lock: str | None = None,
    fetch: Any = None,
    host_probe: Any = None,
    modal_login: Callable[[], str | None] | None = None,
) -> DeployReport:
    """Run the idempotent deploy pipeline; return a per-step report.

    ``modal_login`` is the interactive-auth seam (SOR-209): when the plane
    reports no workspace, it is invoked once (``modal token new`` on the
    TTY) and the workspace is re-probed, so a fresh-clone deploy completes
    in one invocation. ``None`` keeps the non-interactive behavior.
    """
    env = os.environ if env is None else env
    config = cfg.config
    steps: list[StepResult] = []

    # SOR-209: `sbx deploy` on a fresh clone writes config.toml implicitly —
    # the same file `sbx init` with no flags would produce.
    if not cfg.file_exists:
        save(load_file_values(cfg.path), cfg.path, env=env)
        steps.append(StepResult("config", True, f"initialized {cfg.path.name}"))

    require([check_python(), check_modal_package()])
    validate_providers(config.providers)
    workspace = _require_modal_auth(plane, login=modal_login)
    steps.append(StepResult("modal-auth", False, f"workspace {workspace}"))

    # Preflight (SOR-217): Platform prerequisites — Modal auth above and
    # the opt-in GitHub bridge gates below — still fail before the first
    # write. Provider credentials are provider health, not Platform
    # health: a missing provider Secret degrades the provider (recorded in
    # ``degraded`` → its ``runtime/<provider>`` record + the app-level
    # Secret mount skipped via ``SBX_DEGRADED_PROVIDERS``) instead of
    # aborting the deploy.
    degraded: dict[str, list[str]] = {}
    unbuildable: set[str] = set()  # providers whose image cannot be built
    existing_secrets = plane.list_secret_names()
    if "codex" in config.providers:
        step, reason = _codex_secret_preflight(config, plane, env)
        steps.append(step)
        if reason:
            degraded.setdefault("codex", []).append(reason)
    step, account_reasons = _check_account_secrets(config, plane, existing_secrets)
    steps.append(step)
    for provider, reason in account_reasons.items():
        degraded.setdefault(provider, []).append(reason)
    github_step = _require_github_secret(config, existing_secrets)
    if github_step is not None:
        steps.append(github_step)
    github_app_step = _require_github_app_secret(config, existing_secrets)
    if github_app_step is not None:
        steps.append(github_app_step)
    if config.auth_database_secret and config.auth_database_secret not in existing_secrets:
        raise BootstrapError(
            "configured auth database Secret is missing; create a Modal Secret holding "
            "DATABASE_URL before deploying",
            code="auth_database_secret_missing",
        )

    # SOR-175: resolve + freeze provider CLI versions once, before any
    # write. SOR-210: a platform-only deploy (no providers, no explicit
    # lock replay) skips resolution entirely — provider CLI versions are
    # not a gate. SOR-217: a provider that cannot resolve degrades — the
    # deploy continues with the frozen evidence.
    resolved_versions: Any = None
    versions_lock_path: Path | None = None
    if config.providers or versions_lock or env.get("SBX_VERSIONS_LOCK"):
        versions_step, resolved_versions, versions_lock_path = _resolve_cli_versions(
            config,
            env,
            versions_lock=versions_lock,
            fetch=fetch,
            host_probe=host_probe,
            degraded=degraded,
            unbuildable=unbuildable,
        )
        steps.append(versions_step)
    else:
        steps.append(StepResult("versions", False, "skipped — platform-only deploy (no providers)"))

    step, key_created, key_rotated = _ensure_bootstrap_secret(config, plane, env)
    steps.append(step)
    steps.append(_ensure_basic_secret(config, plane, env))

    created_dicts = [name for name in config.dict_names() if plane.ensure_dict(name)]
    steps.append(
        StepResult(
            "state",
            bool(created_dicts),
            "durable dicts ready"
            + (f" (created: {', '.join(created_dicts)})" if created_dicts else ""),
        )
    )
    steps.append(_materialize_account_secrets(config, plane))

    # SOR-212/SOR-215: the runtime evidence Dict — deploy writes one
    # ``runtime/<provider>`` record per enabled provider (``ready`` /
    # ``degraded``) for ``/v1/providers`` to expose as runtime readiness.
    # Best-effort: evidence writes never block the deploy.
    from control.runtime_state import RUNTIME_KEY_PREFIX, ProviderRuntimeRecord
    from runtime.provider_runtime import spec_or_none as _provider_spec

    try:
        plane.ensure_dict(config.runtime_dict)
        runtime_dict_ok = True
    except BootstrapError:
        runtime_dict_ok = False

    runtime_records: dict[str, dict[str, Any]] = {}
    for provider in config.providers:
        image_name = config.image_name(provider)
        rspec = _provider_spec(provider)
        problem = (
            rspec.host_assist_problem(resolved_versions.spec, env=env)
            if rspec is not None and rspec.local_assisted
            else None
        )
        if problem is not None:
            degraded.setdefault(provider, []).append(problem)
            unbuildable.add(provider)
        if provider in unbuildable:
            steps.append(
                StepResult(
                    f"image:{provider}",
                    False,
                    f"{image_name} not built — {'; '.join(degraded[provider])}",
                )
            )
        else:
            try:
                plane.ensure_image(provider, image_name, spec=resolved_versions.spec)
                steps.append(StepResult(f"image:{provider}", True, image_name))
            except BootstrapError as exc:
                # SOR-217: a provider image build failure degrades the
                # provider — the Platform deploy continues.
                degraded.setdefault(provider, []).append(f"image build failed: {exc}")
                steps.append(
                    StepResult(f"image:{provider}", False, f"{image_name} not built — {exc}")
                )
        status = "degraded" if provider in degraded else "ready"
        detail = "; ".join(degraded.get(provider, []))
        record = ProviderRuntimeRecord(
            provider=provider,
            status=status,
            image=image_name,
            version=resolved_versions.cli_versions().get(provider),
            detail=detail,
            updated_at=_iso_now(),
        ).to_dict()
        runtime_records[provider] = record
        if runtime_dict_ok:
            try:
                plane.dict_put(config.runtime_dict, f"{RUNTIME_KEY_PREFIX}{provider}", record)
            except BootstrapError:
                pass  # evidence writes never block the deploy

    # SOR-266: build the React Console (``console/dist``, live HTTP API
    # mode, bound to the checkout's git SHA) before ``modal deploy`` copies
    # it into the control image. A build failure aborts the deploy — a
    # fresh deployment must never silently fall back to the legacy web/ UI.
    console_step, frontend = _prepare_console(env)
    steps.append(console_step)

    # SOR-217: degraded providers skip their app-level credential Secret
    # mount (``control.config.app_secret_names``) — ``modal deploy`` must
    # not fail on a Secret the deploy already knows is absent.
    deploy_env = config.deploy_env()
    if degraded:
        deploy_env["SBX_DEGRADED_PROVIDERS"] = ",".join(sorted(degraded))
    base_url = plane.deploy_app(config.modal_app_name, env=deploy_env)
    steps.append(
        StepResult(
            "app",
            True,
            f"{config.modal_app_name} → {base_url}"
            + (f" ({len(degraded)} provider(s) degraded)" if degraded else ""),
        )
    )

    token, _ = load_or_create_key(key_path(env))
    _probe_v1(base_url, token, transport=transport, attempts=probe_attempts, sleep=sleep)
    steps.append(StepResult("verify", False, "/v1/me answered 200"))
    # SOR-266: prove the deployment actually serves the same-SHA console
    # build at "/" — the primary asset's sha256 must match the local
    # build manifest recorded in the deploy state below.
    _probe_console(base_url, frontend, transport=transport)
    steps.append(StepResult("verify:console", False, _console_probe_detail(frontend)))

    if cfg.sources.get("api_base_url") != "env" and config.api_base_url != base_url:
        save(_replace_base_url(config, base_url), cfg.path, env=env)
    version = version or app_version()
    state: dict[str, Any] = {
        "version": version,
        "deployed_at": _iso_now(),
        "app": config.modal_app_name,
        "app_url": base_url,
        "key_fingerprint": fingerprint(token),
        # SOR-266 release evidence: which frontend the deployment serves,
        # bound to the checkout SHA and the primary asset's content hash.
        "frontend": {**frontend, "served_url": base_url, "deployed_at": _iso_now()},
        # SOR-212/SOR-215: per-provider runtime evidence written to the
        # ``sbx-runtime`` Dict and consumed by ``/v1/providers``.
        "runtime": runtime_records,
    }
    if resolved_versions is not None:
        # SOR-175 version evidence: the frozen CLI set this deployment
        # built, plus where its lock file lives for replay/rollback.
        state["cli_versions"] = resolved_versions.lock_payload()
        state["versions_lock"] = str(versions_lock_path)
    _write_state(env, state)
    return DeployReport(
        steps=tuple(steps),
        base_url=base_url,
        version=version,
        key_created=key_created,
        key_rotated=key_rotated,
        cli_versions=resolved_versions.cli_versions() if resolved_versions else None,
        degraded_providers=(
            {p: "; ".join(reasons) for p, reasons in sorted(degraded.items())} if degraded else None
        ),
    )


def _replace_base_url(config: BootstrapConfig, base_url: str) -> BootstrapConfig:
    return replace(config, api_base_url=base_url)


def snapshot_durable(cfg: BootstrapConfig, plane: Plane) -> dict[str, int]:
    """Readable key counts of every durable store (upgrade invariant)."""
    snapshot: dict[str, int] = {}
    for name in cfg.dict_names():
        try:
            snapshot[name] = plane.dict_len(name)
        except Exception as exc:
            raise BootstrapError(
                f"durable store {name!r} is unreadable: {exc}",
                hint="upgrade aborted before touching anything; fix Modal access and retry",
                code="durable_unreadable",
            ) from exc
    return snapshot


@dataclass(frozen=True)
class UpgradeReport:
    deploy: DeployReport
    from_version: str
    to_version: str
    durable: dict[str, int]


def upgrade(
    cfg: ResolvedConfig,
    plane: Plane,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    probe_attempts: int = 5,
    version: str | None = None,
    versions_lock: str | None = None,
    fetch: Any = None,
    host_probe: Any = None,
    modal_login: Callable[[], str | None] | None = None,
) -> UpgradeReport:
    """Redeploy while proving durable stores stay readable end to end.

    Snapshots every durable Dict before and after the deploy; a store that
    stops answering aborts the upgrade as a failure, never silently.
    """
    env = os.environ if env is None else env
    prior = read_deploy_state(env)
    from_version = str(prior.get("version") or "unknown")
    before = snapshot_durable(cfg.config, plane)

    report = deploy(
        cfg,
        plane,
        env=env,
        transport=transport,
        sleep=sleep,
        probe_attempts=probe_attempts,
        version=version,
        versions_lock=versions_lock,
        fetch=fetch,
        host_probe=host_probe,
        modal_login=modal_login,
    )

    after = snapshot_durable(cfg.config, plane)
    lost = [name for name, count in before.items() if after.get(name, -1) < count]
    if lost:
        raise BootstrapError(
            f"durable stores lost keys across upgrade: {', '.join(lost)}",
            hint="inspect the Modal Dicts before retrying; do not rerun `sbx deploy` blindly",
            code="durable_lost",
        )
    return UpgradeReport(
        deploy=report,
        from_version=from_version,
        to_version=report.version,
        durable=after,
    )
