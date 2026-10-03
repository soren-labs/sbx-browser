"""Read/write files inside a sandbox using only SandboxBackend.exec (or a local root)."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from control import github
from control.backend import Process, SandboxBackend, SandboxHandle


def drain(proc: Process) -> int:
    for _ in proc.stdout:
        pass
    return proc.wait()


def is_local_root(handle: SandboxHandle) -> bool:
    try:
        return handle.root.is_dir()
    except OSError:
        return False


# Control-plane env vars explicitly forwarded to sandbox children. Required
# since LocalProcessBackend.exec no longer inherits os.environ (SOR-56) — and
# only applied to local roots: on Modal the sandbox image pins its own
# ``PYTHONPATH=/opt/sbx``, and forwarding the control function's value would
# clobber it (``python -m runtime.runner`` then fails to resolve).
# SOR-80: forwarding is scoped by the sandbox's provider/account tags so
# control-only credential variables never reach a foreign provider's exec.
_SHARED_ENV_KEYS = ("PYTHONPATH",)
_SANDBOX_PYTHONPATH = "/opt/sbx"  # runtime.image.PYTHONPATH_REMOTE

_PROVIDER_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "codex": (
        "CODEX_BIN",
        "SBX_CODEX_TRANSPORT",
        "SBX_PROVIDER_API_KEY",
        "SBX_PROVIDER_BASE_URL",
        "FAKE_CODEX_SCENARIO",
        "FAKE_CODEX_THREAD_ID",
        "FAKE_CODEX_SLOW_SECONDS",
        "FAKE_CODEX_TURN_SECONDS",
        "FAKE_CODEX_MODELS",
    ),
    "antigravity": ("FAKE_AGY_SCENARIO", "FAKE_AGY_SLOW_SECONDS", "FAKE_AGY_MODELS"),
    "grok": ("FAKE_GROK_SCENARIO", "FAKE_GROK_SLOW_SECONDS", "FAKE_GROK_MODELS"),
    "opencode": (
        "OPENCODE_BIN",
        "FAKE_OPENCODE_SCENARIO",
        "FAKE_OPENCODE_SLOW_SECONDS",
        "FAKE_OPENCODE_MODELS",
    ),
    "devin": (
        "DEVIN_BIN",
        "SBX_DEVIN_TRANSPORT",
        "FAKE_DEVIN_SCENARIO",
        "FAKE_DEVIN_SLOW_SECONDS",
        "FAKE_DEVIN_MODELS",
    ),
}

_ACCOUNT_ID_ENV = "SBX_ACCOUNT_ID"
_ACCOUNT_CREDENTIAL_ENV = "SBX_ACCOUNT_CREDENTIAL"


def handle_provider(handle: SandboxHandle) -> str:
    """Provider tag on a sandbox handle (default ``codex``, as in the spec)."""
    return (handle.tags or {}).get("provider", "codex")


def parse_credential_blob(raw: str | None) -> dict[str, Any] | None:
    """Parse a ``SBX_ACCOUNT_CREDENTIAL`` blob; ``None`` when malformed."""
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return blob if isinstance(blob, dict) else None


def _account_scoped(account_id: str | None) -> bool:
    """Whether the ambient account blob may be forwarded to this sandbox.

    The ambient ``SBX_ACCOUNT_ID`` identifies which account the control-plane
    credential blob belongs to: a mismatch (or a credential bound to a named
    account on an untagged sandbox) must not shadow the sandbox's own account
    credentials. An unset ``SBX_ACCOUNT_ID`` is the unscoped single-account
    local-gate fallback and forwards on provider match alone.
    """
    ambient = os.environ.get(_ACCOUNT_ID_ENV)
    if ambient is None:
        return True
    return account_id is not None and ambient == account_id


def _credential_for(provider: str, account_id: str | None) -> str | None:
    """Ambient ``SBX_ACCOUNT_CREDENTIAL`` scoped to this provider/account."""
    blob = parse_credential_blob(os.environ.get(_ACCOUNT_CREDENTIAL_ENV))
    if blob is None or blob.get("provider") != provider:
        return None
    if not _account_scoped(account_id):
        return None
    return os.environ.get(_ACCOUNT_CREDENTIAL_ENV)


def sandbox_env(
    handle: SandboxHandle,
    extra: Mapping[str, str] | None = None,
    *,
    github_repo: str | None = None,
) -> dict[str, str]:
    provider = handle_provider(handle)
    account_id = (handle.tags or {}).get("account_id")
    env = {
        "SBX_WORK": str(handle.root),
        "CODEX_HOME": str(handle.root / ".codex"),
        "PYTHONUNBUFFERED": "1",
        # Sandbox execs have no stdin — an auth prompt must fail fast, not hang.
        "GIT_TERMINAL_PROMPT": "0",
    }
    # Modal ``exec(..., env=)`` replaces the process env and can hide a named
    # Secret. Forward the control-plane copy when present so Codex still
    # auth'd — Codex sandboxes only (SOR-80).
    if provider == "codex":
        auth_json = os.environ.get("CODEX_AUTH_JSON")
        if auth_json:
            env["CODEX_AUTH_JSON"] = auth_json
    if is_local_root(handle):
        for key in _SHARED_ENV_KEYS:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
    else:
        env["PYTHONPATH"] = _SANDBOX_PYTHONPATH
    for key in _PROVIDER_ENV_KEYS.get(provider, ()):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    ambient_account = os.environ.get(_ACCOUNT_ID_ENV)
    if ambient_account is not None and ambient_account == account_id:
        env[_ACCOUNT_ID_ENV] = ambient_account
    credential = _credential_for(provider, account_id)
    if credential is not None:
        env[_ACCOUNT_CREDENTIAL_ENV] = credential
    # SOR-117: the opt-in GitHub bridge is provider-agnostic. Modal
    # ``exec(env=)`` replaces the process env (hiding Secret-mounted vars), so
    # the token + credential-helper wiring is forwarded here for every exec —
    # runner (the agent sees it) and control-plane git ops alike. SOR-177:
    # ``github_repo`` scopes GitHub App mints to the authorizing repo.
    if handle.tags.get("hosted") == "1":
        if github_repo:
            env["SBX_GITHUB_REPO"] = github_repo
    else:
        env.update(github.exec_env(repo=github_repo))
    if extra:
        safe_extra = dict(extra)
        # Never let callers re-introduce credentials that violate the
        # sandbox provider/account boundary after the scoped env above.
        if provider != "codex":
            safe_extra.pop("CODEX_AUTH_JSON", None)
            safe_extra.pop("SBX_PROVIDER_API_KEY", None)
            safe_extra.pop("SBX_PROVIDER_BASE_URL", None)
        extra_account = safe_extra.get(_ACCOUNT_ID_ENV)
        if extra_account is not None and extra_account != account_id:
            safe_extra.pop(_ACCOUNT_ID_ENV, None)
            safe_extra.pop(_ACCOUNT_CREDENTIAL_ENV, None)
        extra_credential = safe_extra.get(_ACCOUNT_CREDENTIAL_ENV)
        if extra_credential is not None:
            blob = parse_credential_blob(extra_credential)
            if blob is None or blob.get("provider") != provider:
                safe_extra.pop(_ACCOUNT_CREDENTIAL_ENV, None)
        # The GitHub token/helper env enters only through the opt-in seam —
        # a caller's ``extra`` must not smuggle it in or clobber the wiring.
        for key in list(safe_extra):
            if github.owns_env_key(key):
                safe_extra.pop(key, None)
        env.update(safe_extra)
    return env


def write_file(backend: SandboxBackend, handle: SandboxHandle, relative: str, content: str) -> None:
    path = handle.root / relative
    if is_local_root(handle):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    dest = str(path)
    script = (
        "import base64, pathlib;"
        f"p=pathlib.Path({dest!r});"
        "p.parent.mkdir(parents=True, exist_ok=True);"
        f"p.write_bytes(base64.b64decode({encoded!r}))"
    )
    proc = backend.exec(handle, ["python3", "-c", script], env=sandbox_env(handle))
    code = drain(proc)
    if code != 0:
        raise RuntimeError(f"failed to write {relative} in sandbox {handle.id}")


def read_text(backend: SandboxBackend, handle: SandboxHandle, relative: str) -> str | None:
    path = handle.root / relative
    if is_local_root(handle):
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    dest = str(path)
    script = (
        "import pathlib, sys;"
        f"p=pathlib.Path({dest!r});"
        "sys.exit(2) if not p.is_file() else sys.stdout.write(p.read_text())"
    )
    proc = backend.exec(handle, ["python3", "-c", script], env=sandbox_env(handle))
    chunks = list(proc.stdout)
    code = proc.wait()
    if code != 0:
        return None
    return "\n".join(chunks)


def read_json(
    backend: SandboxBackend, handle: SandboxHandle, relative: str
) -> dict[str, Any] | None:
    raw = read_text(backend, handle, relative)
    if raw is None or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        # RecursionError: a sandbox-written file with pathological nesting
        # is unreadable evidence, not a crash — same as unparseable JSON.
        return None
    return data if isinstance(data, dict) else None


def events_path(handle: SandboxHandle) -> Path:
    return handle.root / "events.jsonl"
