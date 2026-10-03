"""SOR-222/223: public Task API — resolution, preflight and persistence.

The task layer is the caller-facing resource on top of the existing agent
machinery: a caller declares ``prompt`` + ``source`` + ``execution`` +
``delivery`` and the control plane resolves that declaration into a concrete
``CreateAgentRequest`` — a canonical repo URL, a ``base_ref``/``base_sha``
pin, and a concrete provider/model/effort/account plan. ``requested`` is
persisted verbatim next to ``resolved`` so the gap between intent and the
authoritative plan stays inspectable.

Resolution is split into two deliberately reusable pieces:

- source resolution (``resolve_source``): canonicalize the repo URL, pick
  the default ref when none is declared, resolve the base ref to an exact
  commit sha, and run the GitHub authorization/permission preflight.
- execution resolution (``resolve_execution``): capability-aware account
  scheduling — every registered account is filtered by provider /
  runtime / model / reasoning-effort / auth material / health / capacity
  before the LRU pick, so ``auto`` is a real selection with recorded
  evidence, not a scheduler coin flip.

``POST /v1/tasks/preflight`` runs this code path advisory-only (no slot is
reserved); ``POST /v1/tasks`` re-runs it and then reserves through the
authoritative ``Scheduler.acquire`` in ``_create_agent_once`` — a stale
advisory answer can never create on a dead account.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from control.accounts import cooldown_expired
from control.config import env_float, env_int
from control.ports import Account, AccountRegistry

# Canonical provider set — same tuple the scheduler/adapter registry use.
CANONICAL_PROVIDERS: tuple[str, ...] = ("codex", "antigravity", "grok", "opencode", "devin")

TASKS_DICT_NAME = "sbx-tasks"
TASKS_DICT_ENV = "SBX_TASKS_DICT"
TASK_STORE_DIR_ENV = "SBX_TASK_STORE_DIR"

_LS_REMOTE_TIMEOUT_S = 30.0
_GITHUB_API_TIMEOUT_S = 15.0
_GITHUB_API_URL_ENV = "SBX_GITHUB_API_URL"
_GITHUB_API_DEFAULT = "https://api.github.com"

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Tri-state permission answers: evidence must distinguish "denied" from
# "could not determine" — a caller should see *why* the check is uncertain.
_PERMISSION_TRISTATE = ("yes", "no", "unknown")

# GitHub object permissions that count as push-capable.
_GITHUB_WRITE_PERMISSIONS = ("push", "maintain", "admin")

# Token prefixes that carry *user-context* repository permissions. Anything
# else (``ghs_`` server-to-server / installation tokens most importantly)
# reports an all-false ``permissions`` block on ``GET /repos`` — that block
# is the *user's* repo role, which is empty for app tokens — so the repo
# object can never answer push capability for them.
_GITHUB_USER_TOKEN_PREFIXES = ("ghp_", "github_pat_", "gho_", "ghu_", "ghr_")


class TaskRefusal(Exception):
    """Resolution failure carrying a canonical ``{error:{code}}`` mapping."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
        checks: Sequence[Check] = (),
        candidates: Sequence[AccountCandidate] = (),
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.checks = list(checks)
        self.candidates = list(candidates)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Check:
    """One advisory preflight check line (name + pass/warn/fail + detail)."""

    name: str
    status: str  # "pass" | "warn" | "fail"
    detail: str

    def public(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


# --------------------------------------------------------------------------
# repo canonicalization
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalRepo:
    """Normalized repo address + what it implies for resolution."""

    canonical: str
    kind: str  # "github" | "remote" | "local"
    slug: str | None = None  # owner/repo — github kind only


# ``owner/repo`` or ``github.com/owner/repo`` without a scheme. Only taken as
# GitHub when no such local path exists, so relative local repos still work.
_GITHUB_SHORTHAND_RE = re.compile(
    r"^(?:(?:www\.)?github\.com/)?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


def canonicalize_repo(repo: Any) -> CanonicalRepo:
    """Normalize a caller-supplied repo address for persistence + probing.

    github.com https/ssh forms and the ``owner/repo`` shorthand collapse to
    the canonical https URL;
    userinfo is always stripped — credential material must never persist
    on the task record. Everything else (other hosts, ``file://``, plain
    paths) is kept verbatim so ``git clone`` semantics are unchanged.
    """
    if not isinstance(repo, str) or not repo.strip():
        raise TaskRefusal(400, "workspace_invalid", "source.repo must be a non-empty string")
    raw = repo.strip()
    # Strip any userinfo segment — it is never persisted.
    scheme_at = raw.find("://")
    candidate = raw
    if scheme_at >= 0:
        at = candidate.find("@", scheme_at + 3)
        slash = candidate.find("/", scheme_at + 3)
        if at >= 0 and (slash < 0 or at < slash):
            candidate = f"{candidate[: scheme_at + 3]}{candidate[at + 1 :]}"
    from control import github

    slug = github.repo_slug(raw) or github.repo_slug(candidate)
    if slug is not None:
        # GitHub owner/repo names are case-insensitive — fold to lower.
        slug = slug.lower()
        return CanonicalRepo(canonical=f"https://github.com/{slug}", kind="github", slug=slug)
    shorthand = _GITHUB_SHORTHAND_RE.match(candidate)
    if shorthand is not None and not os.path.exists(candidate):
        slug = f"{shorthand.group(1)}/{shorthand.group(2)}".lower()
        return CanonicalRepo(canonical=f"https://github.com/{slug}", kind="github", slug=slug)
    if candidate.startswith("file://"):
        return CanonicalRepo(canonical=candidate, kind="local")
    if candidate.startswith("/") or candidate.startswith("."):
        return CanonicalRepo(canonical=candidate, kind="local")
    if "://" in candidate or ":" in candidate:
        # Other git remotes (gitlab, bitbucket, git://, ssh git@host): kept
        # verbatim — clone semantics and auth are the operator's concern.
        return CanonicalRepo(canonical=candidate, kind="remote")
    return CanonicalRepo(canonical=candidate, kind="local")


# --------------------------------------------------------------------------
# repo probing (default branch / exact sha / access posture)
# --------------------------------------------------------------------------


@runtime_checkable
class RepoResolver(Protocol):
    """Host-side remote probe: resolves refs and access posture.

    Implementations must never surface credential material — tokens exist
    only inside subprocess env / request headers. ``None`` answers mean
    "could not determine", never "defaulted".
    """

    def default_branch(self, repo: CanonicalRepo) -> str | None:
        """The remote's HEAD ref name, or ``None`` when undetermined."""

    def resolve_ref(self, repo: CanonicalRepo, ref: str) -> str | None:
        """Exact commit sha for ``ref`` (branch/tag/sha/HEAD), or ``None``."""

    def access(self, repo: CanonicalRepo) -> dict[str, str]:
        """``{"read": yes|no|unknown, "push": yes|no|unknown, "source": ...}``."""


def _git_env(env: Mapping[str, str]) -> dict[str, str]:
    """Subprocess env for host-side git: no prompts, no credential bleed."""
    out = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": env.get("HOME") or os.environ.get("HOME") or "",
        "PATH": env.get("PATH") or os.environ.get("PATH") or os.defpath,
    }
    # Forward proxy settings only — the git bridge owns credential envs and
    # they are deliberately not copied into the probe's env.
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
        if env.get(key):
            out[key] = env[key]  # type: ignore[index]
    return out


class GitLsRemoteResolver:
    """``git ls-remote`` probe — the generic path for any reachable remote.

    Works for github.com too (fallback when the API seam is unavailable)
    and for ``file://``/local-path repos, which keeps it usable in unit
    tests with a bare on-disk repo and no network at all.
    """

    def __init__(
        self,
        *,
        git: str = "git",
        timeout_s: float = _LS_REMOTE_TIMEOUT_S,
        env: Mapping[str, str] | None = None,
        runner: Any = subprocess.run,
    ) -> None:
        self._git = git
        self._timeout_s = timeout_s
        self._env = dict(env or os.environ)
        self._runner = runner

    def _ls_remote(self, url: str, *args: str) -> tuple[int, list[str]]:
        try:
            proc = self._runner(
                [self._git, "ls-remote", *args, url],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                env=_git_env(self._env),
            )
        except (OSError, subprocess.TimeoutExpired):
            return -1, []
        # stderr is deliberately never surfaced — it may echo the URL.
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        return proc.returncode, lines

    @staticmethod
    def _ref_table(lines: list[str]) -> dict[str, str]:
        """``sha<TAB>refname`` lines → refname → sha (peeled ``^{}`` skipped)."""
        table: dict[str, str] = {}
        for line in lines:
            sha, _, name = line.partition("\t")
            sha = sha.strip()
            name = name.strip()
            if not sha or not name or name.endswith("^{}"):
                continue
            table[name] = sha
        return table

    def default_branch(self, repo: CanonicalRepo) -> str | None:
        code, lines = self._ls_remote(repo.canonical, "--symref")
        if code != 0:
            return None
        for line in lines:
            if line.startswith("ref:") and "\t" in line:
                ref = line[4:].split("\t", 1)[0].strip()
                if ref.startswith("refs/heads/"):
                    return ref[len("refs/heads/") :]
        return None

    def resolve_ref(self, repo: CanonicalRepo, ref: str) -> str | None:
        if _COMMIT_SHA_RE.fullmatch(ref):
            # A bare sha needs containment proof: scan the remote's refs.
            code, lines = self._ls_remote(repo.canonical)
            if code != 0:
                return None
            shas = {ln.split("\t", 1)[0].strip() for ln in lines}
            return ref if ref in shas else None
        # Named ref: prefer heads, then tags, then the verbatim name, HEAD.
        code, lines = self._ls_remote(repo.canonical)
        if code != 0:
            return None
        table = self._ref_table(lines)
        for candidate in (
            f"refs/heads/{ref}",
            f"refs/tags/{ref}",
            ref,
            ("HEAD" if ref == "HEAD" else f"refs/remotes/{ref}"),
        ):
            sha = table.get(candidate)
            if sha and _COMMIT_SHA_RE.fullmatch(sha):
                return sha
        return None

    def access(self, repo: CanonicalRepo) -> dict[str, str]:
        code, _ = self._ls_remote(repo.canonical)
        # ls-remote can only prove read reachability; push is never probed
        # (a push probe would mutate the remote).
        if code == 0:
            return {"read": "yes", "push": "unknown", "source": "ls_remote"}
        return {"read": "unknown", "push": "unknown", "source": "ls_remote"}


class GitHubApiResolver:
    """github.com REST probe: repo metadata, ref resolution, permissions.

    Auth uses the same sources as the sandbox injection seam — env
    ``GH_TOKEN``/``GITHUB_TOKEN`` first, else a repo-scoped GitHub App
    installation token (minted lazily and only when an installation
    actually authorizes the repo). Unauthenticated calls still resolve
    public repos; a 404 without a token is reported ``unknown``, not
    ``no`` — a private repo reads as absent without credentials.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: float = _GITHUB_API_TIMEOUT_S,
        client: Any = None,
    ) -> None:
        self._env = dict(env or os.environ)
        self._timeout_s = timeout_s
        self._client = client

    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx

        base = (
            self._env.get(_GITHUB_API_URL_ENV)
            or self._env.get("SBX_GITHUB_APP_API_URL")
            or _GITHUB_API_DEFAULT
        )
        return httpx.Client(base_url=base, timeout=self._timeout_s)

    def _token(self, repo: CanonicalRepo) -> tuple[str | None, str | None]:
        """(token, source-name) — value never leaves this module."""
        from control import github

        token = github.resolve_token(self._env)
        if token is not None:
            return token, "env"
        if repo.slug:
            from control import github_app

            try:
                token = github_app.sandbox_token(self._env, repo=repo.slug)
            except Exception:
                token = None
            if token is not None:
                return token, "github_app"
        return None, None

    def _get(self, path: str, token: str | None) -> tuple[int, Any]:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        client = self._http()
        try:
            resp = client.get(path, headers=headers)
            try:
                body = resp.json()
            except Exception:
                body = None
            return resp.status_code, body
        except Exception:
            return -1, None

    def _repo(self, repo: CanonicalRepo) -> tuple[int, dict[str, Any] | None, str | None]:
        token, _source = self._token(repo)
        status, body = self._get(f"/repos/{repo.slug}", token)
        return status, body if isinstance(body, dict) else None, token

    def _push_capability(self, repo: CanonicalRepo, token: str | None) -> str:
        """``yes``/``no``/``unknown`` — can ``token`` git-push to ``repo``?

        Repository-object ``permissions`` blocks are the *user* role
        grant — all-false for installation tokens on ``GET /repos`` and
        inside ``GET /installation/repositories`` alike, so no repo
        listing can answer this for the only credential class sbx mints
        or bridges. ``git push`` itself answers it on its opening
        request: ``GET <repo>.git/info/refs?service=git-receive-pack``
        serves the ref advertisement only when the credential may write
        (401/403 when it may not). The probe is non-mutating — the
        advertisement is read-only, and repo selection coverage is
        inherent: a repo the token cannot see never reaches this call.
        """
        if not token:
            return "unknown"
        client = self._http()
        try:
            # git smart-HTTP accepts only Basic credentialing on
            # github.com — ``Bearer`` is answered 401 even for a
            # push-capable token (verified against live GitHub).
            resp = client.get(
                f"{repo.canonical}.git/info/refs?service=git-receive-pack",
                auth=("x-access-token", token),
                follow_redirects=True,
            )
        except Exception:
            return "unknown"
        if resp.status_code == 200:
            return "yes"
        if resp.status_code in (401, 403):
            return "no"
        return "unknown"

    def default_branch(self, repo: CanonicalRepo) -> str | None:
        if repo.slug is None:
            return None
        status, body, _ = self._repo(repo)
        if status == 200 and body:
            branch = body.get("default_branch")
            return branch if isinstance(branch, str) and branch else None
        return None

    def resolve_ref(self, repo: CanonicalRepo, ref: str) -> str | None:
        if repo.slug is None:
            return None
        token, _ = self._token(repo)
        status, body = self._get(f"/repos/{repo.slug}/commits/{ref}", token)
        if status == 200 and isinstance(body, dict):
            sha = body.get("sha")
            if isinstance(sha, str) and _COMMIT_SHA_RE.fullmatch(sha):
                return sha
        return None

    def access(self, repo: CanonicalRepo) -> dict[str, str]:
        if repo.slug is None:
            return {"read": "unknown", "push": "unknown", "source": "github_api"}
        token, source = self._token(repo)
        status, body = self._get(f"/repos/{repo.slug}", token)
        if status == 200 and isinstance(body, dict):
            installation = source == "github_app" or (
                token is not None and not token.startswith(_GITHUB_USER_TOKEN_PREFIXES)
            )
            if installation:
                # ``GET /repos``'s ``permissions`` is the *user* role block —
                # always all-false for installation tokens, and the repo
                # entries in ``GET /installation/repositories`` carry the
                # same empty block. git's receive-pack advertisement is
                # the only honest answer an installation token can read.
                push = self._push_capability(repo, token)
            else:
                perms = body.get("permissions") if isinstance(body.get("permissions"), dict) else {}
                can_push = any(bool(perms.get(p)) for p in _GITHUB_WRITE_PERMISSIONS)
                push = "yes" if can_push else ("no" if perms else "unknown")
            return {
                "read": "yes",
                "push": push,
                "source": f"github_api:{source or 'anonymous'}",
            }
        if status == 404:
            # Without a token a private repo is indistinguishable from a
            # missing one — report unknown, never a false "no".
            return {
                "read": "unknown" if token is None else "no",
                "push": "unknown",
                "source": f"github_api:{source or 'anonymous'}",
            }
        return {
            "read": "unknown",
            "push": "unknown",
            "source": f"github_api:{source or 'anonymous'}",
        }


class ChainRepoResolver:
    """github.com → GitHub API first (permissions), ls-remote fallback;
    every other remote goes straight to ``git ls-remote``."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        api: GitHubApiResolver | None = None,
        git: GitLsRemoteResolver | None = None,
    ) -> None:
        env = env if env is not None else os.environ
        self._api = api or GitHubApiResolver(env=env)
        self._git = git or GitLsRemoteResolver(env=env)

    def default_branch(self, repo: CanonicalRepo) -> str | None:
        if repo.kind == "github":
            branch = self._api.default_branch(repo)
            if branch is not None:
                return branch
        return self._git.default_branch(repo)

    def resolve_ref(self, repo: CanonicalRepo, ref: str) -> str | None:
        if repo.kind == "github":
            sha = self._api.resolve_ref(repo, ref)
            if sha is not None:
                return sha
        return self._git.resolve_ref(repo, ref)

    def access(self, repo: CanonicalRepo) -> dict[str, str]:
        if repo.kind == "github":
            access = self._api.access(repo)
            if access["read"] != "unknown":
                return access
            git_access = self._git.access(repo)
            git_access["source"] = f"{access['source']}+{git_access['source']}"
            return git_access
        return self._git.access(repo)


def default_repo_resolver(env: Mapping[str, str] | None = None) -> ChainRepoResolver:
    """Production default: GitHub API → ls-remote chain."""
    return ChainRepoResolver(env=env)


# --------------------------------------------------------------------------
# source resolution — canonical repo, default ref, exact sha, permissions
# --------------------------------------------------------------------------


@dataclass
class SourceResolution:
    """Resolved ``source`` declaration — inputs for the workspace spec."""

    repo: str  # canonical, userinfo-stripped
    kind: str
    base_ref: str
    base_sha: str
    slug: str | None
    access: dict[str, str]

    def workspace(self) -> dict[str, str]:
        return {"repo": self.repo, "base_ref": self.base_ref, "base_sha": self.base_sha}

    def evidence(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "kind": self.kind,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "slug": self.slug,
            "access": dict(self.access),
        }


def resolve_source(
    source: Mapping[str, Any],
    *,
    resolver: RepoResolver,
    env: Mapping[str, str] | None = None,
    needs_push: bool = False,
) -> tuple[SourceResolution, list[Check], list[str]]:
    """Resolve a ``source`` block to a pinned ``base_ref``/``base_sha``.

    Raises ``TaskRefusal`` on hard failures (unreachable repo, unresolvable
    ref); permission gaps the caller can still proceed past land as
    ``warn`` checks — a read-accessible repo with unknown push rights is a
    valid task, only ``delivery`` escalation would need push.
    """
    env = os.environ if env is None else env
    checks: list[Check] = []
    warnings: list[str] = []
    repo = canonicalize_repo(source.get("repo"))
    checks.append(
        Check("source.repo", "pass", f"repo canonicalized as {repo.canonical!r} ({repo.kind})")
    )
    ref = source.get("ref")
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        raise TaskRefusal(
            400, "workspace_invalid", "source.ref must be a non-empty string", checks=checks
        )
    ref = ref.strip() if isinstance(ref, str) else None

    from control import github
    from control.workspace import is_commit_sha, is_safe_ref

    # -- base ref -----------------------------------------------------------
    if ref is None or ref in ("", "auto", "HEAD"):
        branch = resolver.default_branch(repo)
        if branch is None:
            checks.append(
                Check("source.ref", "fail", "could not determine the repo's default branch")
            )
            raise TaskRefusal(
                409,
                "repo_unavailable",
                f"cannot resolve the default branch of {repo.canonical!r}",
                checks=checks,
            )
        base_ref = branch
        checks.append(Check("source.ref", "pass", f"default branch resolved to {base_ref!r}"))
    elif is_commit_sha(ref):
        # Exact-sha input: the sha itself is the ref — ``git rev-parse
        # <sha>^{commit}`` in the fresh clone is the authoritative check
        # (``resolve_ref`` may still verify existence when the probe can).
        base_ref = ref
        checks.append(Check("source.ref", "pass", f"exact commit pinned at {ref[:12]}"))
    else:
        if not is_safe_ref(ref):
            raise TaskRefusal(
                400,
                "workspace_invalid",
                f"source.ref is not a safe git ref name: {ref!r}",
                checks=checks,
            )
        base_ref = ref
        checks.append(Check("source.ref", "pass", f"ref {base_ref!r} declared"))

    # -- exact sha -----------------------------------------------------------
    base_sha = resolver.resolve_ref(repo, base_ref)
    if base_sha is None:
        if is_commit_sha(base_ref):
            # Exact-sha input stays advisory-unverifiable when the probe
            # cannot see it (private remote without credentials, probe
            # offline); the worker's rev-parse in the fresh clone is the
            # authoritative gate and fails closed if the sha is absent.
            checks.append(
                Check(
                    "source.sha",
                    "warn",
                    f"commit {base_ref[:12]} not advertised by {repo.canonical!r} "
                    "— verified again at run time",
                )
            )
            base_sha = base_ref
            warnings.append(f"base sha {base_ref[:12]} could not be verified remotely")
        else:
            detail = f"ref {base_ref!r} does not resolve to a commit on {repo.canonical!r}"
            checks.append(Check("source.sha", "fail", detail))
            raise TaskRefusal(409, "repo_unavailable", detail, checks=checks)
    else:
        checks.append(Check("source.sha", "pass", f"{base_ref!r} resolves to {base_sha[:12]}"))

    # -- authorization / permission preflight --------------------------------
    access: dict[str, str]
    if repo.kind == "github":
        access = resolver.access(repo)
        source_name = github.token_source(env, repo=repo.slug)
        if access["read"] == "no":
            checks.append(
                Check(
                    "github.read",
                    "fail",
                    f"no credential authorizes read on {repo.slug!r}",
                )
            )
            raise TaskRefusal(
                409,
                "repo_unavailable",
                f"repo {repo.slug!r} is not readable with the configured GitHub credentials",
                checks=checks,
            )
        if access["read"] == "unknown":
            checks.append(
                Check(
                    "github.read",
                    "warn",
                    f"read access on {repo.slug!r} could not be verified "
                    f"(credential source: {source_name or 'none'})",
                )
            )
            warnings.append(f"read access on {repo.slug!r} unverified")
        else:
            checks.append(
                Check(
                    "github.read",
                    "pass",
                    f"{repo.slug!r} readable via {access['source']}",
                )
            )
        if needs_push:
            if access["push"] == "no":
                checks.append(
                    Check(
                        "github.push",
                        "fail",
                        f"credentials lack push permission on {repo.slug!r}",
                    )
                )
                raise TaskRefusal(
                    409,
                    "repo_unavailable",
                    f"repo {repo.slug!r} is not pushable with the configured GitHub credentials",
                    checks=checks,
                )
            if access["push"] == "unknown":
                checks.append(
                    Check(
                        "github.push",
                        "warn",
                        f"push permission on {repo.slug!r} unverified"
                        + ("" if source_name else " — no GitHub credential is configured"),
                    )
                )
                warnings.append(f"push permission on {repo.slug!r} unverified")
            else:
                checks.append(
                    Check("github.push", "pass", f"{repo.slug!r} pushable via {access['source']}")
                )
        if source_name is None and repo.kind == "github":
            checks.append(
                Check(
                    "github.credential",
                    "warn",
                    "no GitHub credential configured — private repos will fail at clone",
                )
            )
            warnings.append("no GitHub credential configured")
    elif repo.kind == "remote":
        access = resolver.access(repo)
        if access["read"] == "unknown":
            checks.append(
                Check("repo.read", "warn", f"{repo.canonical!r} unreachable via git ls-remote")
            )
            warnings.append(f"{repo.canonical!r} could not be probed")
        else:
            checks.append(Check("repo.read", "pass", f"{repo.canonical!r} reachable"))
        if needs_push:
            checks.append(
                Check("repo.push", "warn", "push permission is not verifiable before run time")
            )
            warnings.append("push permission unverified")
    else:
        access = {"read": "yes", "push": "unknown", "source": "local"}
        checks.append(Check("repo.read", "pass", "local repo path — host permissions apply"))

    return (
        SourceResolution(
            repo=repo.canonical,
            kind=repo.kind,
            base_ref=base_ref,
            base_sha=base_sha,
            slug=repo.slug,
            access=access,
        ),
        checks,
        warnings,
    )


# --------------------------------------------------------------------------
# delivery → git policy
# --------------------------------------------------------------------------


def delivery_to_git(delivery: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Task ``delivery`` block → the SOR-128 ``git`` policy dict.

    ``pull_request`` implies push + auto_create_pr; ``auto_publish``
    implies push. ``branch`` alone just names the work branch — publish
    stays manual. ``target`` defaults to the resolved ``base_ref`` inside
    ``normalize_git_policy``.
    """
    if not delivery:
        return None
    branch = delivery.get("branch")
    pr = delivery.get("pull_request")
    auto_publish = bool(delivery.get("auto_publish"))
    if pr:
        git: dict[str, Any] = {
            "push": True,
            "auto_create_pr": True,
            "auto_publish": auto_publish,
            "draft": bool(pr.get("draft")),
        }
        if pr.get("title"):
            git["title"] = pr["title"]
        if pr.get("body"):
            git["body"] = pr["body"]
        if pr.get("target"):
            git["target"] = pr["target"]
        if branch:
            git["branch"] = branch
        return git
    if auto_publish:
        git = {"push": True, "auto_publish": True}
        if branch:
            git["branch"] = branch
        return git
    if branch:
        return {"branch": branch}
    return None


# --------------------------------------------------------------------------
# execution resolution — capability-aware account scheduling
# --------------------------------------------------------------------------


def _ambient_credential(provider: str, account_id: str, env: Mapping[str, str]) -> bool:
    """Whether ambient control env can seed this account's credential."""
    raw = env.get("SBX_ACCOUNT_CREDENTIAL")
    if raw:
        try:
            blob = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            blob = None
        if isinstance(blob, dict) and blob.get("provider") == provider:
            ambient = env.get("SBX_ACCOUNT_ID")
            if ambient is None or ambient == account_id:
                return True
    if provider == "codex" and env.get("CODEX_AUTH_JSON"):
        return True
    return False


def has_auth_material(
    account: Account, registry: AccountRegistry | None, env: Mapping[str, str]
) -> bool:
    """Account-level auth check: managed Secret, stored blob, or ambient.

    Mirrors the three credential channels ``sandbox_env`` can attach at
    provision time — an account with none of them can only fail
    ``auth_invalid`` mid-run, so it is never an eligible ``auto`` pick.
    """
    if account.secret_name:
        return True
    if registry is not None:
        get_blob = getattr(registry, "get_credential_blob", None)
        if callable(get_blob):
            try:
                if get_blob(account.id):
                    return True
            except Exception:
                pass
    return _ambient_credential(account.provider, account.id, env)


@dataclass
class AccountCandidate:
    """Per-account eligibility verdict + the account's own resolved plan."""

    account_id: str
    provider: str
    status: str
    running: int
    max_concurrent: int
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    model: str | None = None
    model_source: str | None = None  # discovered|declared|default
    effort: str | None = None
    effort_source: str | None = None
    snapshot_source: str | None = None
    snapshot_stale: bool = False
    last_used_at: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "provider": self.provider,
            "status": self.status,
            "running": self.running,
            "max_concurrent": self.max_concurrent,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "model": self.model,
            "reasoning_effort": self.effort,
        }


def _snapshot_for(capabilities: Any, account: Account) -> Any:
    """Catalog snapshot without spawning probes (``ensure=False``)."""
    from control.capabilities import declared_snapshot

    if capabilities is None or not callable(getattr(capabilities, "get", None)):
        return declared_snapshot(account)
    try:
        return capabilities.get(account, ensure=False)
    except TypeError:
        return capabilities.get(account)
    except Exception:
        return declared_snapshot(account)


def _model_row(snapshot: Any, model: str | None) -> Any:
    if snapshot is None or model is None:
        return None
    for row in snapshot.models:
        if row.model == model or model in row.aliases:
            return row
    return None


def _effort_row(provider: str, model: str | None, snapshot: Any) -> Any:
    """Same row semantics as the /v1 create path's ``_effort_row``."""
    from control.capabilities import capability_from_model_id

    row = _model_row(snapshot, model) if snapshot is not None else None
    if row is None and model is not None and (snapshot is None or snapshot.source != "discovered"):
        row = capability_from_model_id(provider, model)
    return row


def _account_default_model(
    provider: str, account: Account, snapshot: Any
) -> tuple[str | None, str]:
    """Model for ``auto``: catalog default → declared → provider floor."""
    from control.api_v1.bootstrap import PROVIDER_DEFAULT_MODELS

    if snapshot is not None and snapshot.default_model:
        return snapshot.default_model, snapshot.source
    if account.models:
        return account.models[0], "declared"
    defaults = PROVIDER_DEFAULT_MODELS.get(provider) or ()
    return (defaults[0], "default") if defaults else (None, "default")


def evaluate_account(
    account: Account,
    *,
    model_req: str | None,
    effort_req: str | None,
    registry: AccountRegistry | None,
    capabilities: Any,
    running_count: Callable[[str], int],
    env: Mapping[str, str],
    enabled_providers: Sequence[str],
    now: datetime | None = None,
) -> AccountCandidate:
    """Filter one account through the eligibility chain.

    Order mirrors the issue's filter: provider → runtime → auth → health →
    capacity → capability (model/effort). ``reasons`` accumulates every
    disqualifier, not just the first — a candidate's evidence should say
    everything that kept it out.
    """
    from control.onboarding import provider_auth_argv, provider_models_argv

    now = now or datetime.now(UTC)
    reasons: list[str] = []
    running = 0
    try:
        running = int(running_count(account.id)) if callable(running_count) else 0
    except Exception:
        running = 0

    if account.provider not in CANONICAL_PROVIDERS:
        reasons.append("provider_unsupported")
    elif account.provider not in enabled_providers:
        reasons.append("provider_disabled")
    if (
        provider_models_argv(account.provider) is None
        and provider_auth_argv(account.provider) is None
    ):
        reasons.append("runtime_unavailable")

    status = account.status
    if status == "cooling" and cooldown_expired(account, now):
        status = "active"
    if status != "active":
        reasons.append(f"status_{status}")

    if not has_auth_material(account, registry, env):
        reasons.append("no_credential")

    cap = account.max_concurrent if account.max_concurrent > 0 else 1
    if running >= cap:
        reasons.append("at_capacity")

    snapshot = _snapshot_for(capabilities, account)
    snapshot_source = snapshot.source if snapshot is not None else None
    snapshot_stale = bool(getattr(snapshot, "stale", False))

    # -- model / effort ------------------------------------------------------
    model: str | None = None
    model_source: str | None = None
    effort: str | None = None
    effort_source: str | None = None
    if model_req is not None:
        model = model_req
        model_source = "requested"
        if (
            snapshot is not None
            and snapshot.source == "discovered"
            and _model_row(snapshot, model_req) is None
        ):
            reasons.append("model_not_advertised")
            model_source = None
    else:
        model, model_source = _account_default_model(account.provider, account, snapshot)
        if model is None:
            reasons.append("model_unresolvable")

    if effort_req is not None:
        effort = effort_req
        effort_source = "requested"
        row = _effort_row(account.provider, model, snapshot)
        if row is not None:
            if not row.reasoning_efforts:
                reasons.append("effort_no_surface")
            elif effort_req not in row.reasoning_efforts:
                reasons.append("effort_unsupported")
        elif snapshot is not None and snapshot.source == "discovered":
            reasons.append("effort_unsupported")
        else:
            from runtime.runner.effort import effort_error

            if effort_error(account.provider, effort_req):
                reasons.append("effort_unsupported")
    else:
        row = _effort_row(account.provider, model, snapshot)
        # Adopt the row's default effort only when the model advertises a
        # real surface for it — a tier baked into the model id
        # (``swe-2-high`` on effort-less devin) leaves ``default_effort``
        # set but must not become a ``reasoning_effort`` the provider's
        # CLI then refuses.
        if row is not None and row.default_effort and row.default_effort in row.reasoning_efforts:
            effort = row.default_effort
            effort_source = "default"

    return AccountCandidate(
        account_id=account.id,
        provider=account.provider,
        status=status,
        running=running,
        max_concurrent=account.max_concurrent,
        eligible=not reasons,
        reasons=reasons,
        model=model,
        model_source=model_source,
        effort=effort,
        effort_source=effort_source,
        snapshot_source=snapshot_source,
        snapshot_stale=snapshot_stale,
        last_used_at=account.last_used_at,
    )


@dataclass
class ExecutionResolution:
    """The resolved execution plan + per-field provenance evidence."""

    provider: str
    account_id: str | None
    model: str | None
    reasoning_effort: str | None
    evidence: dict[str, Any]
    candidates: list[AccountCandidate]
    pick: AccountCandidate | None = None

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account_id": self.account_id,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "evidence": self.evidence,
            "candidates": [c.public() for c in self.candidates],
        }


# Reasons that are pure capability mismatches (vs. health/capacity/etc).
_CAPABILITY_REASONS = frozenset(
    {"model_not_advertised", "model_unresolvable", "effort_no_surface", "effort_unsupported"}
)


def _lru(candidates: list[AccountCandidate]) -> AccountCandidate:
    """LRU among eligible candidates — never-used first, stable id tiebreak."""
    return min(candidates, key=lambda c: (c.last_used_at or "", c.account_id))


def resolve_execution(
    execution: Mapping[str, Any] | None,
    *,
    registry: AccountRegistry,
    scheduler: Any,
    capabilities: Any,
    env: Mapping[str, str] | None = None,
    checks: list[Check] | None = None,
) -> ExecutionResolution:
    """Resolve ``execution`` to a concrete provider/model/effort/account.

    Named accounts are hard-refused when ineligible; ``auto`` picks LRU
    among the eligible set after the full capability filter, and the
    returned ``candidates`` list is the evidence of why every other account
    was excluded.
    """
    env = os.environ if env is None else env
    checks = checks if checks is not None else []
    from control.config import selected_providers

    execution = dict(execution or {})
    provider_req = execution.get("provider")
    model_req = execution.get("model")
    effort_req = execution.get("reasoning_effort")
    account_req = execution.get("account_id")

    provider_req = provider_req if provider_req not in (None, "", "auto") else None
    model_req = model_req if model_req not in (None, "", "auto") else None
    effort_req = effort_req if effort_req not in (None, "", "auto") else None
    account_req = account_req if account_req not in (None, "", "auto") else None

    enabled = (
        ("codex",)
        if getattr(registry, "hosted", False)
        else tuple(p for p in selected_providers(env) if p in CANONICAL_PROVIDERS)
    )

    if provider_req is not None and provider_req not in CANONICAL_PROVIDERS:
        raise TaskRefusal(400, "invalid_provider", f"unknown provider {provider_req!r}")
    if provider_req is not None and provider_req not in enabled:
        raise TaskRefusal(
            400,
            "invalid_provider",
            f"provider {provider_req!r} is not enabled in this deployment",
            checks=checks,
        )

    def _running(account_id: str) -> int:
        counter = getattr(scheduler, "running_count", None)
        if callable(counter):
            try:
                return int(counter(account_id))
            except Exception:
                pass
        try:
            return int(registry.running_count(account_id))
        except Exception:
            return 0

    # -- named account: the eligibility verdict is authoritative --------------
    if account_req is not None:
        account = registry.get(account_req)
        if account is None:
            raise TaskRefusal(
                409,
                "account_unavailable",
                f"account {account_req!r} does not exist",
                checks=checks,
            )
        if provider_req is not None and account.provider != provider_req:
            raise TaskRefusal(
                409,
                "account_unavailable",
                f"account {account_req!r} is provider {account.provider!r}, not {provider_req!r}",
                checks=checks,
            )
        candidate = evaluate_account(
            account,
            model_req=model_req,
            effort_req=effort_req,
            registry=registry,
            capabilities=capabilities,
            running_count=_running,
            env=env,
            enabled_providers=enabled,
        )
        if not candidate.eligible:
            capability_only = set(candidate.reasons) <= _CAPABILITY_REASONS
            code = "unsupported" if capability_only else "account_unavailable"
            status = 400 if capability_only else 409
            raise TaskRefusal(
                status,
                code,
                f"account {account_req!r} is not eligible: {', '.join(candidate.reasons)}",
                checks=checks,
                candidates=[candidate],
            )
        checks.append(
            Check("execution.account", "pass", f"pinned account {account_req!r} is eligible")
        )
        return ExecutionResolution(
            provider=account.provider,
            account_id=account.id,
            model=candidate.model,
            reasoning_effort=candidate.effort,
            evidence={
                "provider": {
                    "requested": provider_req or "auto",
                    "resolved": account.provider,
                    "source": "account",
                },
                "account_id": {
                    "requested": account_req,
                    "resolved": account.id,
                    "source": "requested",
                },
                "model": {
                    "requested": model_req or "auto",
                    "resolved": candidate.model,
                    "source": candidate.model_source,
                },
                "reasoning_effort": {
                    "requested": effort_req or "auto",
                    "resolved": candidate.effort,
                    "source": candidate.effort_source,
                },
            },
            candidates=[candidate],
            pick=candidate,
        )

    # -- auto account: filter every registered account --------------------------
    providers = (provider_req,) if provider_req is not None else enabled
    candidates: list[AccountCandidate] = []
    for provider in providers:
        for account in registry.list(provider):
            candidates.append(
                evaluate_account(
                    account,
                    model_req=model_req,
                    effort_req=effort_req,
                    registry=registry,
                    capabilities=capabilities,
                    running_count=_running,
                    env=env,
                    enabled_providers=enabled,
                )
            )
    eligible = [c for c in candidates if c.eligible]
    if not candidates:
        checks.append(
            Check(
                "execution.account",
                "fail",
                "no accounts registered for the selected providers",
            )
        )
        raise TaskRefusal(
            429,
            "provider_exhausted",
            "no accounts are registered for the requested provider",
            retry_after=60.0,
            checks=checks,
        )
    if not eligible:
        reason_sets = [set(c.reasons) for c in candidates]
        capability_only = all(rs and rs <= _CAPABILITY_REASONS for rs in reason_sets)
        if capability_only:
            detail = "; ".join(f"{c.account_id}: {', '.join(c.reasons)}" for c in candidates)
            checks.append(Check("execution.capability", "fail", f"no account can serve: {detail}"))
            raise TaskRefusal(
                400,
                "unsupported",
                f"no account can serve model={model_req!r} "
                f"reasoning_effort={effort_req!r} ({detail})",
                checks=checks,
                candidates=candidates,
            )
        cooling = [c for c in candidates if "status_cooling" in c.reasons]
        checks.append(
            Check(
                "execution.account",
                "fail",
                "no eligible account: "
                + "; ".join(f"{c.account_id} ({', '.join(c.reasons)})" for c in candidates),
            )
        )
        retry_after = 60.0 if cooling else None
        raise TaskRefusal(
            429,
            "provider_exhausted",
            "no eligible account after provider/runtime/auth/health/capacity/capability filtering",
            retry_after=retry_after,
            checks=checks,
            candidates=candidates,
        )

    pick = _lru(eligible)
    checks.append(
        Check(
            "execution.account",
            "pass",
            f"{len(eligible)} eligible account(s); LRU picked {pick.account_id!r}",
        )
    )
    global_cap = getattr(scheduler, "max_global", None)
    if isinstance(global_cap, int) and global_cap >= 1:
        active = getattr(scheduler, "active_count", None)
        try:
            running = int(active) if active is not None else None
        except Exception:
            running = None
        if running is not None and running >= global_cap:
            checks.append(
                Check(
                    "scheduler.capacity",
                    "warn",
                    f"global concurrency cap reached ({running}/{global_cap})",
                )
            )
    return ExecutionResolution(
        provider=pick.provider,
        account_id=pick.account_id,
        model=pick.model,
        reasoning_effort=pick.effort,
        evidence={
            "provider": {
                "requested": provider_req or "auto",
                "resolved": pick.provider,
                "source": "lru",
            },
            "account_id": {
                "requested": "auto",
                "resolved": pick.account_id,
                "source": "lru",
            },
            "model": {
                "requested": model_req or "auto",
                "resolved": pick.model,
                "source": pick.model_source,
            },
            "reasoning_effort": {
                "requested": effort_req or "auto",
                "resolved": pick.effort,
                "source": pick.effort_source,
            },
        },
        candidates=candidates,
        pick=pick,
    )


# --------------------------------------------------------------------------
# task resolution — source + execution + delivery
# --------------------------------------------------------------------------


@dataclass
class TaskResolution:
    """Everything ``create`` needs: workspace spec, git policy, execution."""

    source: SourceResolution | None
    git: dict[str, Any] | None
    execution: ExecutionResolution
    checks: list[Check]
    warnings: list[str]

    def resolved_payload(self) -> dict[str, Any]:
        return {
            "source": self.source.evidence() if self.source is not None else None,
            "git": self.git,
            "execution": self.execution.public(),
        }

    def public(self, *, ok: bool) -> dict[str, Any]:
        return {
            "ok": ok,
            "checks": [c.public() for c in self.checks],
            "warnings": list(self.warnings),
            "resolved": self.resolved_payload(),
        }


def resolve_task(
    spec: Mapping[str, Any],
    *,
    registry: AccountRegistry,
    scheduler: Any,
    capabilities: Any,
    resolver: RepoResolver,
    env: Mapping[str, str] | None = None,
) -> TaskResolution:
    """Full resolution for both preflight and create (always re-run)."""
    env = os.environ if env is None else env
    checks: list[Check] = []
    warnings: list[str] = []
    source_spec = spec.get("source")
    delivery_spec = spec.get("delivery")

    git = delivery_to_git(delivery_spec)
    if git is not None:
        from control.workspace import WorkspaceError, validate_git_policy

        try:
            validate_git_policy(git)
        except WorkspaceError as exc:
            raise TaskRefusal(400, exc.code, exc.message, checks=checks) from exc
    if git is not None and source_spec is None:
        raise TaskRefusal(
            400, "workspace_invalid", "delivery requires a source declaration", checks=checks
        )

    source: SourceResolution | None = None
    if source_spec is not None:
        if not isinstance(source_spec, Mapping):
            raise TaskRefusal(400, "workspace_invalid", "source must be an object", checks=checks)
        needs_push = bool(git and git.get("push"))
        source, source_checks, source_warnings = resolve_source(
            source_spec, resolver=resolver, env=env, needs_push=needs_push
        )
        checks.extend(source_checks)
        warnings.extend(source_warnings)
        if git is not None:
            checks.append(Check("delivery", "pass", "git delivery policy resolved"))

    execution_spec = spec.get("execution")
    if execution_spec is not None and not isinstance(execution_spec, Mapping):
        raise TaskRefusal(400, "workspace_invalid", "execution must be an object", checks=checks)
    execution = resolve_execution(
        execution_spec,
        registry=registry,
        scheduler=scheduler,
        capabilities=capabilities,
        env=env,
        checks=checks,
    )
    return TaskResolution(
        source=source, git=git, execution=execution, checks=checks, warnings=warnings
    )


# --------------------------------------------------------------------------
# Task persistence
# --------------------------------------------------------------------------


@dataclass
class TaskRecord:
    """One durable Task row.

    ``request`` is the verbatim caller declaration; ``resolved`` is the
    authoritative plan + per-field evidence. ``response`` pins the create
    reply so a replayed ``Idempotency-Key`` resolves after a restart.
    """

    id: str
    owner: str
    status: str
    request: dict[str, Any]
    resolved: dict[str, Any] | None
    agent_id: str | None
    run_id: str | None
    created_at: str
    updated_at: str
    response: dict[str, Any] | None = None
    idempotency: dict[str, Any] | None = None
    # SOR-224: durable status-transition log — ``{status, reason, at}`` per
    # observed change. Task status derives live from the run ledger + the
    # workspace delivery record, and each new derived value is appended so
    # an orchestrator can read the machine-readable history post-restart.
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "request": self.request,
            "resolved": self.resolved,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "transitions": list(self.transitions),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:16]}"


def record_to_dict(record: TaskRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "owner": record.owner,
        "status": record.status,
        "request": record.request,
        "resolved": record.resolved,
        "agent_id": record.agent_id,
        "run_id": record.run_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "response": record.response,
        "idempotency": record.idempotency,
        "transitions": list(record.transitions),
    }


def record_from_dict(data: Mapping[str, Any]) -> TaskRecord:
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise ValueError("task record missing id")
    if not isinstance(data.get("owner"), str):
        raise ValueError("task record missing owner")
    return TaskRecord(
        id=data["id"],
        owner=data["owner"],
        status=str(data.get("status") or "queued"),
        request=dict(data.get("request") or {}),
        resolved=data.get("resolved"),
        agent_id=data.get("agent_id"),
        run_id=data.get("run_id"),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        response=data.get("response"),
        idempotency=data.get("idempotency"),
        transitions=[dict(t) for t in (data.get("transitions") or []) if isinstance(t, dict)],
    )


@runtime_checkable
class TaskStore(Protocol):
    def put(self, record: TaskRecord) -> None: ...

    def get(self, task_id: str) -> TaskRecord | None: ...

    def delete(self, task_id: str) -> None: ...

    def list(self, owner: str | None = None) -> list[TaskRecord]: ...

    def find_by_idempotency(self, owner: str, key: str) -> TaskRecord | None: ...

    def find_by_agent(self, agent_id: str) -> TaskRecord | None: ...


class InMemoryTaskStore:
    """Thread-safe dict store (tests, ephemeral deployments)."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, record: TaskRecord) -> None:
        with self._lock:
            self._items[record.id] = record_to_dict(record)

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            raw = self._items.get(task_id)
        return record_from_dict(raw) if raw is not None else None

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._items.pop(task_id, None)

    def list(self, owner: str | None = None) -> list[TaskRecord]:
        with self._lock:
            items = list(self._items.values())
        out = []
        for raw in items:
            rec = record_from_dict(raw)
            if owner is None or rec.owner == owner:
                out.append(rec)
        return sorted(out, key=lambda r: r.created_at)

    def find_by_idempotency(self, owner: str, key: str) -> TaskRecord | None:
        for rec in self.list(owner):
            meta = rec.idempotency or {}
            if meta.get("key") == key:
                return rec
        return None

    def find_by_agent(self, agent_id: str) -> TaskRecord | None:
        for rec in self.list():
            if rec.agent_id == agent_id:
                return rec
        return None


class FileTaskStore:
    """Local durable store: ``root/<task_id>.json`` (atomic writes)."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def _path(self, task_id: str) -> Path:
        return self._root / f"{task_id}.json"

    def put(self, record: TaskRecord) -> None:
        path = self._path(record.id)
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(record_to_dict(record), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)

    def get(self, task_id: str) -> TaskRecord | None:
        try:
            raw = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"stored task record {task_id} is corrupt: {exc}") from exc
        return record_from_dict(raw)

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._path(task_id).unlink(missing_ok=True)

    def list(self, owner: str | None = None) -> list[TaskRecord]:
        try:
            # Records may use non-``task_`` id namespaces (V2 ``sess_``);
            # anything that isn't a task record fails decode and is skipped.
            paths = sorted(self._root.glob("*.json"))
        except OSError:
            return []
        out: list[TaskRecord] = []
        for path in paths:
            try:
                rec = self.get(path.stem)
            except (OSError, ValueError):
                continue
            if rec is not None and (owner is None or rec.owner == owner):
                out.append(rec)
        return sorted(out, key=lambda r: r.created_at)

    def find_by_idempotency(self, owner: str, key: str) -> TaskRecord | None:
        for rec in self.list(owner):
            meta = rec.idempotency or {}
            if meta.get("key") == key:
                return rec
        return None

    def find_by_agent(self, agent_id: str) -> TaskRecord | None:
        for rec in self.list():
            if rec.agent_id == agent_id:
                return rec
        return None


# SOR-268: per-record read-through TTL for ``task/<id>`` point gets. The
# detail/list/SSE paths re-read the same record several times inside one
# request and the shared SSE hub polls it — the cache collapses those to
# ~one remote get per record per TTL window. Writes go through ``put``
# (write-through), so the only staleness window is a cross-container
# writer's ``put`` — bounded by this TTL, same trade the session listing
# cache already makes.
_TASK_GET_CACHE_TTL_S = env_float("SBX_TASK_GET_CACHE_TTL_S", 0.5)

# ``GET /v2/sessions`` repeatedly reads the same owner index. The index is
# invalidated on every local write, so a short read-through memo removes the
# final owner-doc RPC from polling without hiding this process's mutations.
_TASK_LIST_CACHE_TTL_S = env_float("SBX_TASK_LIST_CACHE_TTL_S", 1.0)

# Agent-less summaries are distrusted for live rows: a stale summary could
# wedge a row at ``queued`` while ``task/<id>`` already went terminal
# (SOR-271). A summary that already reports a *terminal* status cannot
# wedge at queued, but a later retry can be hidden by an owner-doc lost
# update. A bounded rotating validation batch below preserves eventual
# convergence without a full history reread. Mirrors ``_tasks._TASK_TERMINAL`` (kept local: the v1
# module imports this one).
_SUMMARY_TERMINAL = frozenset({"finished", "error", "cancelled", "expired", "delivery_failed"})


def _summary_trusted(summary: dict[str, Any]) -> bool:
    """Owner-doc summary safe to serve as the listing row.

    Bound rows are always trustworthy (the summary rides every ``put``).
    Agent-less rows are trusted only at a terminal status — a live
    agent-less summary could wedge a row at ``queued`` while ``task/<id>``
    went terminal on a write that skipped the doc (SOR-271)."""
    return summary.get("agent_id") is not None or summary.get("status") in _SUMMARY_TERMINAL


def _record_heals(record: TaskRecord) -> bool:
    """A freshly fetched row whose summary the doc would trust next read."""
    return record.agent_id is not None or record.status in _SUMMARY_TERMINAL


# Owner-doc summary cap: ``owner/<owner>`` embeds recent record summaries
# so ``list`` is a single remote read; ids are never dropped — ids whose
# summary aged out are fetched point-wise through a bounded pool.
_TASK_SUMMARY_MAX = env_int("SBX_TASK_SUMMARY_MAX", 500)

# Fanout for backfilling ids whose summary is not in the owner doc (cap
# exceeded or records written by a pre-index deploy).
_TASK_DICT_FANOUT = 8


def _task_summary(record: TaskRecord) -> dict[str, Any]:
    """Owner-doc payload: the whole record minus ``response`` (the pinned
    create reply — only idempotency replay needs it, and that path goes
    through the ``idem/`` point index + full ``task/<id>`` read)."""
    summary = record_to_dict(record)
    summary["response"] = None
    return summary


class ModalDictTaskStore:
    """Production store backed by ``modal.Dict`` (``sbx-tasks``).

    Key layout (SOR-268 indexed read model):

    - ``task/<id>``             -> full record dict (authoritative)
    - ``owner/<owner>``         -> ``{"ids": [...], "records": {id: summary}}``
    - ``agent/<agent_id>``      -> task id (``find_by_agent`` point index)
    - ``idem/<owner>/<hash>``   -> task id (``Idempotency-Key`` replay index)
    - ``__owners__``            -> owner list (``list(None)`` index-of-indexes)

    ``list(owner)`` reads the owner doc — one remote call — instead of
    N+1 serial point gets. ``put`` batches the record + index rows into
    one ``Dict.update`` (a single ``DictUpdateRequest`` RPC) after one
    owner-doc read, so a write costs ~2 round trips instead of ~5. Ids in
    the owner doc whose summary was trimmed (cap) or never embedded
    (pre-index data) are backfilled through a bounded pool — a listing
    can degrade in latency, never in completeness. Lost/corrupt index
    rows self-heal: ``put`` rewrites them from the record, and
    ``find_by_*`` falls back to an owner scan when the point row is
    absent (pre-index records).
    """

    def __init__(self, name: str = TASKS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None
        self._lock = threading.Lock()
        self._get_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._owner_prefetch: dict[str, tuple[float, Any]] = {}
        self._owner_versions: dict[str, int] = {}
        self._owner_list_cache: dict[str, tuple[float, list[str], dict[str, dict[str, Any]]]] = {}
        self._terminal_validation_cursor: dict[str, int] = {}

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    @staticmethod
    def _task_key(task_id: str) -> str:
        return f"task/{task_id}"

    @staticmethod
    def _cancel_key(task_id: str) -> str:
        return f"cancel/{task_id}"

    @staticmethod
    def _owner_key(owner: str) -> str:
        return f"owner/{owner}"

    @staticmethod
    def _agent_key(agent_id: str) -> str:
        digest = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()
        return f"agent/{digest}"

    @staticmethod
    def _idem_key(owner: str, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
        return f"idem/{owner_hash}/{digest}"

    @staticmethod
    def _batch(d: Any, writes: dict[str, Any]) -> None:
        """One-RPC multi-key write via ``Dict.update`` (1.5.5+); older
        clients degrade to per-key puts."""
        update = getattr(d, "update", None)
        if callable(update):
            update(writes)
            return
        for key, value in writes.items():
            d.put(key, value)

    @staticmethod
    def _owner_doc(raw: Any) -> tuple[list[str], dict[str, dict[str, Any]]]:
        """Normalize the owner index row: ``(ids, summaries)`` for both the
        current dict shape and the pre-index bare id list."""
        if isinstance(raw, dict):
            ids = [str(i) for i in raw.get("ids") or []]
            records = {
                str(k): v for k, v in (raw.get("records") or {}).items() if isinstance(v, dict)
            }
            return ids, records
        if isinstance(raw, list):
            return [str(i) for i in raw], {}
        return [], {}

    @staticmethod
    def _idempotency_digest(record: TaskRecord) -> str | None:
        key = (record.idempotency or {}).get("key")
        return hashlib.sha256(key.encode("utf-8")).hexdigest() if isinstance(key, str) else None

    @classmethod
    def _owner_idempotency(cls, raw: Any) -> dict[str, str | None]:
        """Compact immutable key metadata survives full-summary eviction.

        Presence with None means a row is known to have no key. Legacy rows
        without metadata are read once; status freshness is irrelevant here.
        """
        ids, summaries = cls._owner_doc(raw)
        known = raw.get("idempotency_keys", {}) if isinstance(raw, dict) else {}
        id_set = set(ids)
        out = {tid: known[tid] for tid in ids if tid in known}
        for tid, summary in summaries.items():
            if tid in id_set and tid not in out and "idempotency" in summary:
                key = (summary.get("idempotency") or {}).get("key")
                out[tid] = (
                    hashlib.sha256(key.encode("utf-8")).hexdigest()
                    if isinstance(key, str)
                    else None
                )
        return out

    def _trim_summaries(self, records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if len(records) <= _TASK_SUMMARY_MAX:
            return records
        newest = sorted(records.values(), key=lambda r: str(r.get("created_at") or ""))[
            -_TASK_SUMMARY_MAX:
        ]
        keep = {str(r.get("id")) for r in newest}
        return {k: v for k, v in records.items() if k in keep}

    def prefetch_owner(self, owner: str) -> None:
        """Warm the owner-doc read an imminent ``put`` will need.

        The V2 create path runs this concurrently with
        ``find_by_idempotency``: the two reads overlap, so the subsequent
        ``put`` only pays its ``Dict.update`` round trip (SOR-271). The
        staged row is consumed once and only while fresh. Local owner-doc
        writes invalidate staged rows and in-flight observations, so a
        prefetch cannot overwrite a newer local write.
        """
        with self._lock:
            version = self._owner_versions.get(owner, 0)
        try:
            raw = self._d().get(self._owner_key(owner))
        except Exception:
            return
        with self._lock:
            if self._owner_versions.get(owner, 0) != version:
                return
            if len(self._owner_prefetch) > 64:
                self._owner_prefetch.clear()
            self._owner_prefetch[owner] = (time.monotonic(), raw)

    def _owner_written(self, owner: str) -> None:
        """Invalidate snapshots observed before a local owner-doc write.

        Caller holds the write lock; an in-flight prefetch checks this
        generation after its RPC before staging the observed snapshot.
        """
        self._owner_versions[owner] = self._owner_versions.get(owner, 0) + 1
        self._owner_prefetch.pop(owner, None)

    def _owner_doc_for_write(
        self, owner: str, d: Any
    ) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, str | None]]:
        """Owner doc for ``put``'s read-modify-write: a just-prefetched
        row if one was staged, else a fresh read."""
        hit = self._owner_prefetch.pop(owner, None)
        raw = (
            hit[1]
            if hit is not None and time.monotonic() - hit[0] < 2.0
            else d.get(self._owner_key(owner))
        )
        ids, summaries = self._owner_doc(raw)
        return ids, summaries, self._owner_idempotency(raw)

    def put(self, record: TaskRecord) -> None:
        d = self._d()
        task_key = self._task_key(record.id)
        with self._lock:
            if getattr(record, "_response_unloaded", False):
                # A record materialized from the owner-doc summary never
                # loaded the pinned create ``response`` — a write-back
                # (e.g. a status-transition settle) must merge the stored
                # value instead of erasing it (SOR-268 review finding).
                stored = d.get(task_key)
                if isinstance(stored, dict) and stored.get("response") is not None:
                    record.response = stored["response"]
                record._response_unloaded = False
            raw = record_to_dict(record)
            writes: dict[str, Any] = {task_key: raw}
            if record.owner:
                owner_key = self._owner_key(record.owner)
                ids, summaries, key_meta = self._owner_doc_for_write(record.owner, d)
                if record.id not in ids:
                    ids.append(record.id)
                summaries[record.id] = _task_summary(record)
                key_meta[record.id] = self._idempotency_digest(record)
                writes[owner_key] = {
                    "ids": ids,
                    "records": self._trim_summaries(summaries),
                    "idempotency_keys": key_meta,
                }
                self._index_owner(record.owner, writes)
            if record.agent_id:
                writes[self._agent_key(record.agent_id)] = record.id
            meta = record.idempotency or {}
            if isinstance(meta.get("key"), str) and record.owner:
                writes[self._idem_key(record.owner, meta["key"])] = record.id
            if record.owner:
                self._owner_written(record.owner)
            self._batch(d, writes)
        with self._lock:
            self._get_cache[task_key] = (time.monotonic(), raw)
            if record.owner:
                self._owner_list_cache.pop(record.owner, None)

    def mark_cancel_pending(self, task_id: str) -> bool:
        """Persist cancel intent in one Dict.update and perform no reads."""
        try:
            self._batch(
                self._d(),
                {
                    self._cancel_key(task_id): {
                        "task_id": task_id,
                        "at": _iso_now(),
                        "applied": False,
                    }
                },
            )
        except Exception:
            return False
        return True

    def mark_cancel_applied(self, task_id: str) -> None:
        try:
            self._batch(
                self._d(),
                {
                    self._cancel_key(task_id): {
                        "task_id": task_id,
                        "at": _iso_now(),
                        "applied": True,
                    }
                },
            )
        except Exception:
            pass

    def cancel_mark(self, task_id: str) -> dict[str, Any] | None:
        try:
            raw = self._d().get(self._cancel_key(task_id))
        except Exception:
            return None
        return dict(raw) if isinstance(raw, dict) else None

    def _get_uncached(self, task_id: str) -> dict[str, Any] | None:
        raw = self._d().get(self._task_key(task_id))
        with self._lock:
            self._get_cache[self._task_key(task_id)] = (time.monotonic(), raw)
        return raw if isinstance(raw, dict) else None

    def get(self, task_id: str) -> TaskRecord | None:
        task_key = self._task_key(task_id)
        with self._lock:
            cached = self._get_cache.get(task_key)
        if cached is not None and time.monotonic() - cached[0] < _TASK_GET_CACHE_TTL_S:
            raw = cached[1]
        else:
            raw = self._get_uncached(task_id)
        return record_from_dict(raw) if raw is not None else None

    def peek_cached(self, task_id: str) -> TaskRecord | None:
        """Return the last local row without remote I/O, regardless of TTL.

        This is intentionally an ACK-path primitive, not a mutation read.
        Callers may rely only on immutable identity fields unless they have
        an explicit race-safe rule for stale status (the V2 cancel route only
        fast-paths cached non-terminal rows). Mutations keep using
        ``get``/``get_fresh``.
        """
        with self._lock:
            cached = self._get_cache.get(self._task_key(task_id))
        if cached is None or cached[1] is None:
            return None
        return record_from_dict(cached[1])

    def get_fresh(self, task_id: str) -> TaskRecord | None:
        """Uncached point read for mutation paths (read-modify-write must
        not run on a cached row)."""
        raw = self._get_uncached(task_id)
        return record_from_dict(raw) if raw is not None else None

    def delete(self, task_id: str) -> None:
        rec = self.get(task_id)
        d = self._d()
        task_key = self._task_key(task_id)
        pops = [task_key, self._cancel_key(task_id)]
        if rec is not None:
            if rec.agent_id:
                pops.append(self._agent_key(rec.agent_id))
            meta = rec.idempotency or {}
            if rec.owner and isinstance(meta.get("key"), str):
                pops.append(self._idem_key(rec.owner, meta["key"]))
        for key in pops:
            try:
                d.pop(key)
            except KeyError:
                pass
        if rec is not None and rec.owner:
            owner_key = self._owner_key(rec.owner)
            with self._lock:
                owner_raw = d.get(owner_key)
                ids, summaries = self._owner_doc(owner_raw)
                key_meta = self._owner_idempotency(owner_raw)
                key_meta.pop(task_id, None)
                ids = [i for i in ids if i != task_id]
                summaries.pop(task_id, None)
                self._owner_written(rec.owner)
                d.put(owner_key, {"ids": ids, "records": summaries, "idempotency_keys": key_meta})
                self._owner_list_cache.pop(rec.owner, None)
        with self._lock:
            self._get_cache.pop(task_key, None)

    def _pooled_gets(self, task_ids: list[str]) -> list[TaskRecord]:
        """Bounded-parallel point reads for ids missing a doc summary."""
        if not task_ids:
            return []
        with ThreadPoolExecutor(max_workers=_TASK_DICT_FANOUT) as pool:
            raws = list(pool.map(self._get_uncached, task_ids))
        return [record_from_dict(raw) for raw in raws if isinstance(raw, dict)]

    def _backfill_summaries(
        self,
        owner: str,
        records: list[TaskRecord],
        observed: dict[str, dict[str, Any]],
        *,
        key_records: list[TaskRecord] | None = None,
    ) -> None:
        """Merge fetched rows' summaries into the owner doc.

        Best-effort self-heal: additive-only (entries are added or
        refreshed, never removed) and serialized with ``put``'s owner-doc
        read-modify-write through ``self._lock``, so it cannot drop a
        concurrent writer's entries. No ``_trim_summaries`` here — a
        transient over-cap doc is harmless and the next ``put`` re-trims;
        trimming on this path would re-orphan the very rows the backfill
        just healed.
        """
        try:
            d = self._d()
            owner_key = self._owner_key(owner)
            with self._lock:
                owner_raw = d.get(owner_key)
                ids, summaries = self._owner_doc(owner_raw)
                key_meta = self._owner_idempotency(owner_raw)
                id_set = set(ids)
                for rec in key_records if key_records is not None else records:
                    if rec.id in id_set:
                        key_meta[rec.id] = self._idempotency_digest(rec)
                for rec in records:
                    if rec.id not in id_set:
                        # A concurrent ``delete`` dropped the index entry
                        # between the listing read and this write —
                        # re-adding it would resurrect a gone record as a
                        # phantom row on every future list (its trusted
                        # terminal summary would serve without a point
                        # read against the deleted ``task/<id>``).
                        continue
                    # Point reads happen before this lock. A concurrent retry
                    # may have published a newer summary in the meantime;
                    # never overwrite it with the fetched terminal snapshot.
                    if summaries.get(rec.id) != observed.get(rec.id):
                        continue
                    summaries[rec.id] = _task_summary(rec)
                self._owner_written(owner)
                self._batch(
                    d, {owner_key: {"ids": ids, "records": summaries, "idempotency_keys": key_meta}}
                )
                self._owner_list_cache[owner] = (
                    time.monotonic(),
                    list(ids),
                    dict(summaries),
                )
                # A create's ``put`` pops ``_owner_prefetch`` and rewrites
                # the doc from it — a pre-heal staged read would clobber
                # this backfill on every keyed create (the v23 create-ACK
                # regression: ``find_by_idempotency`` re-walked the whole
                # missing set per call). Re-stage the doc just written so
                # the imminent ``put`` merges onto the healed view.
                self._owner_prefetch[owner] = (
                    time.monotonic(),
                    {
                        "ids": list(ids),
                        "records": dict(summaries),
                        "idempotency_keys": dict(key_meta),
                    },
                )
        except Exception:
            return

    def list(self, owner: str | None = None) -> list[TaskRecord]:
        if owner is None:
            # Dict has no key scan — every record goes through an owner
            # index, so listing without owner needs an index of indexes.
            owners = list(self._d().get("__owners__") or [])
            out: list[TaskRecord] = []
            for name in owners:
                out.extend(self.list(str(name)))
            return out
        with self._lock:
            cached = self._owner_list_cache.get(owner)
        if cached is not None and time.monotonic() - cached[0] < _TASK_LIST_CACHE_TTL_S:
            ids, summaries = list(cached[1]), dict(cached[2])
        else:
            ids, summaries = self._owner_doc(self._d().get(self._owner_key(owner)))
            with self._lock:
                self._owner_list_cache[owner] = (time.monotonic(), ids, summaries)
        out: list[TaskRecord] = []
        missing: list[str] = []
        # Owner-doc RMWs can lose another container's retry update. Validate
        # a bounded rotating batch so trusting terminal history never makes
        # an old terminal summary permanent. The RPC count stays independent
        # of total history; all unbound rows converge after a finite scan.
        terminal_ids = [
            tid
            for tid in ids
            if (summary := summaries.get(tid)) is not None
            and summary.get("agent_id") is None
            and summary.get("status") in _SUMMARY_TERMINAL
        ]
        with self._lock:
            start = self._terminal_validation_cursor.get(owner, 0)
            validate = {
                terminal_ids[(start + i) % len(terminal_ids)]
                for i in range(min(8, len(terminal_ids)))
            }
            self._terminal_validation_cursor[owner] = start + len(validate)
        for task_id in ids:
            summary = summaries.get(task_id)
            if summary is not None and _summary_trusted(summary) and task_id not in validate:
                record = record_from_dict(summary)
                # The summary omits ``response`` — flag it so ``put``
                # merges the stored value instead of writing a None that
                # erases the pinned create reply.
                record._response_unloaded = True
                out.append(record)
            else:
                # Agent-less *live* rows can never be trusted off the
                # index: with no bound agent there is nothing to
                # live-aggregate against, so a stale summary write (a
                # lost owner-doc update) would wedge the row at
                # ``queued`` while ``task/<id>`` already went terminal
                # (SOR-271). Read the authoritative row.
                missing.append(task_id)
        fetched = self._pooled_gets(missing)
        out.extend(fetched)
        heal = [
            r
            for r in fetched
            if (_record_heals(r) or r.id in validate) and _task_summary(r) != summaries.get(r.id)
        ]
        if heal:
            # Backfill the fetched rows' summaries so the next listing
            # does not re-pay the point reads — otherwise every pre-index
            # or agent-less row costs one serialized remote get on every
            # list call forever (the production Dict accumulates such
            # rows across deploys; that re-read is the ~8x v23 list
            # regression). Agent-less live rows are skipped: their fresh
            # summary stays untrusted, so the write buys nothing.
            self._backfill_summaries(owner, heal, summaries)
        return sorted(out, key=lambda r: r.created_at)

    def find_by_idempotency(self, owner: str, key: str) -> TaskRecord | None:
        d = self._d()
        with self._lock:
            version = self._owner_versions.get(owner, 0)
        # The point index and the pre-index owner scan are both reads —
        # issue them concurrently; the owner doc is then staged for the
        # imminent ``put``, so the whole dedup + write-warm costs one wall
        # round trip instead of three serial ones (SOR-268 round 3).
        with ThreadPoolExecutor(max_workers=2) as pool:
            idem_fut = pool.submit(d.get, self._idem_key(owner, key))
            owner_fut = pool.submit(d.get, self._owner_key(owner))
            task_id = idem_fut.result()
            owner_raw = owner_fut.result()
        if isinstance(task_id, str) and task_id:
            # Full row, not the summary: the replay response is pinned.
            return self.get_fresh(task_id)
        with self._lock:
            if len(self._owner_prefetch) > 64:
                self._owner_prefetch.clear()
            if self._owner_versions.get(owner, 0) == version:
                self._owner_prefetch[owner] = (time.monotonic(), owner_raw)
        ids, summaries = self._owner_doc(owner_raw)
        known = self._owner_idempotency(owner_raw)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        for tid in ids:
            if known.get(tid) == digest:
                return self.get_fresh(tid)
        # Only pre-migration rows lack immutable key metadata. Full summaries
        # are bounded, but evicting one must not reintroduce a history scan.
        missing = [tid for tid in ids if tid not in known]
        fetched = self._pooled_gets(missing)
        heal = [r for r in fetched if _record_heals(r)]
        if fetched:
            self._backfill_summaries(owner, heal, summaries, key_records=fetched)
        for rec in fetched:
            meta = rec.idempotency or {}
            if meta.get("key") == key:
                return self.get_fresh(rec.id) or rec
        return None

    def find_by_agent(self, agent_id: str) -> TaskRecord | None:
        task_id = self._d().get(self._agent_key(agent_id))
        if isinstance(task_id, str) and task_id:
            return self.get(task_id)
        # Pre-index records: fall back to the owner scan (one read per
        # owner, not one per task).
        for rec in self.list():
            if rec.agent_id == agent_id:
                return self.get_fresh(rec.id) or rec
        return None

    def _index_owner(self, owner: str, writes: dict[str, Any]) -> None:
        """Append ``owner`` to ``__owners__`` inside the same batch when
        unseen; the in-memory ``self._owners`` set makes this a no-op for
        steady-state writes."""
        if owner in getattr(self, "_owners_seen", set()):
            return
        d = self._d()
        owners = list(d.get("__owners__") or [])
        if owner not in owners:
            owners.append(owner)
            writes["__owners__"] = owners
        seen = getattr(self, "_owners_seen", None)
        if seen is None:
            seen = self._owners_seen = set()
        seen.add(owner)


__all__ = [
    "CANONICAL_PROVIDERS",
    "TASKS_DICT_ENV",
    "TASKS_DICT_NAME",
    "TASK_STORE_DIR_ENV",
    "AccountCandidate",
    "CanonicalRepo",
    "ChainRepoResolver",
    "Check",
    "ExecutionResolution",
    "FileTaskStore",
    "GitHubApiResolver",
    "GitLsRemoteResolver",
    "InMemoryTaskStore",
    "ModalDictTaskStore",
    "RepoResolver",
    "SourceResolution",
    "TaskRecord",
    "TaskRefusal",
    "TaskResolution",
    "TaskStore",
    "canonicalize_repo",
    "default_repo_resolver",
    "delivery_to_git",
    "evaluate_account",
    "has_auth_material",
    "new_task_id",
    "record_from_dict",
    "record_to_dict",
    "resolve_execution",
    "resolve_source",
    "resolve_task",
]
