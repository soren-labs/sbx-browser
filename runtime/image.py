"""Named Modal Image ``sbx-runtime`` (slim, no browser) + provider variants.

Package versions live in ``runtime/packages.txt``. ``Dockerfile.local`` (and
``Dockerfile.devin.local`` / ``Dockerfile.opencode.local`` for the provider
fast paths) are generated from the same file so local docker verification
stays in lockstep with the Modal images. Sandbox lifetime and hardware
(``idle_timeout``, ``timeout``, ``cpu``, ``memory``, ``workdir``, ``tags``,
``secrets``) are **not** set here — the control plane passes them to
``Sandbox.create``.

Provider fast paths (SOR-74 devin; SOR-62/SOR-80 antigravity + grok; Release
0.1 opencode): each named image is ``sbx-runtime`` plus the provider CLI. The
Devin CLI is a pinned, sha256-verified download; ``opencode`` is a pinned npm
package; ``agy`` / ``grok`` are proprietary host binaries taken from the
building machine (never committed to the repo) — the same derivation the
SOR-62 e2e gates verified, gated by the ``agy_version`` / ``grok_version``
pins. ``image_for(provider)`` is the explicit provider → image-name mapping;
``image_manifest()`` emits the machine-readable release metadata doctor and
release-evidence tooling consume (``python -m runtime.image --manifest``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent
REPO_ROOT = RUNTIME_DIR.parent
PACKAGES_TXT = RUNTIME_DIR / "packages.txt"
ENTRYPOINT_SH = RUNTIME_DIR / "entrypoint.sh"
DOCKERFILE_LOCAL = REPO_ROOT / "Dockerfile.local"

APP_NAME = "sbx-runtime"
IMAGE_NAME = "sbx-runtime"
DEVIN_IMAGE_NAME = "sbx-runtime-devin"
# SOR-62/SOR-80 provider fast path: named images = sbx-runtime + the provider
# CLI binary from the build host (see sbx_antigravity_image / sbx_grok_image).
AGY_IMAGE_NAME = "sbx-runtime-antigravity"
GROK_IMAGE_NAME = "sbx-runtime-grok"
# Release 0.1 / SOR-96 OpenCode seam: named image = sbx-runtime + the pinned
# ``opencode-ai`` npm package (public registry artifact, no host binary
# needed — unlike agy/grok).
OPENCODE_IMAGE_NAME = "sbx-runtime-opencode"
AGY_BIN_ENV = "SBX_AGY_BIN"
GROK_BIN_ENV = "SBX_GROK_BIN"
CODEX_BIN_REMOTE = "/usr/local/bin/codex"
DEVIN_BIN_REMOTE = "/usr/local/bin/devin"
AGY_BIN_REMOTE = "/usr/local/bin/agy"
GROK_BIN_REMOTE = "/usr/local/bin/grok"
OPENCODE_BIN_REMOTE = "/usr/local/bin/opencode"
DEFAULT_AGY_BIN = Path.home() / ".local" / "bin" / "agy"
DEFAULT_GROK_BIN = Path.home() / ".local" / "bin" / "grok"
CLI_VERSION_TIMEOUT_S = 15.0
ENTRYPOINT_REMOTE = "/opt/sbx/entrypoint.sh"
RUNTIME_REMOTE = "/opt/sbx/runtime"
INSTALL_DEVIN_REMOTE = f"{RUNTIME_REMOTE}/install-devin.sh"
PYTHONPATH_REMOTE = "/opt/sbx"
MODAL_WORKSPACE = "sorenlab2026"
DOCKERFILE_DEVIN_LOCAL = REPO_ROOT / "Dockerfile.devin.local"
DOCKERFILE_OPENCODE_LOCAL = REPO_ROOT / "Dockerfile.opencode.local"

# Credential relpaths under $HOME (docs/contracts/filesystem.md). Recorded in
# the manifest so doctor can verify the release credential-path contract.
PROVIDER_CREDENTIAL_FILES: dict[str, tuple[str, ...]] = {
    "codex": (".codex/auth.json",),
    "antigravity": (".gemini/antigravity-cli/antigravity-oauth-token",),
    "grok": (".grok/auth.json",),
    "opencode": (".local/share/opencode/auth.json",),
    "devin": (".local/share/devin/credentials.toml",),
}

REQUIRED_APT = (
    "curl",
    "git",
    "ca-certificates",
    "ripgrep",
    "jq",
    "procps",
    "build-essential",
    "python3-pip",
)
FORBIDDEN_APT_PREFIXES = ("chrome", "chromium")


@dataclass(frozen=True)
class PackageSpec:
    python_version: str
    node_major: str
    codex_npm: str
    codex_version: str
    devin_version: str
    devin_base_url: str
    devin_sha256_x86_64: str
    devin_sha256_aarch64: str
    opencode_npm: str
    opencode_version: str
    agy_version: str
    grok_version: str
    apt: tuple[str, ...]

    @property
    def nodesource_setup_url(self) -> str:
        return f"https://deb.nodesource.com/setup_{self.node_major}.x"

    @property
    def codex_npm_spec(self) -> str:
        return f"{self.codex_npm}@{self.codex_version}"

    @property
    def opencode_npm_spec(self) -> str:
        return f"{self.opencode_npm}@{self.opencode_version}"

    @property
    def codex_version_expect(self) -> str:
        return f"codex-cli {self.codex_version}"


def load_packages(path: Path | None = None) -> PackageSpec:
    """Parse ``runtime/packages.txt``."""
    text = (path or PACKAGES_TXT).read_text(encoding="utf-8")
    keys: dict[str, str] = {}
    apt: list[str] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == "apt":
            pkg = line.split()[0]
            apt.append(pkg)
            continue
        if "=" not in line:
            raise ValueError(f"invalid packages.txt line: {raw!r}")
        key, _, value = line.partition("=")
        keys[key.strip()] = value.strip()

    missing = [
        k
        for k in (
            "python_version",
            "node_major",
            "codex_npm",
            "codex_version",
            "devin_version",
            "devin_base_url",
            "devin_sha256_x86_64",
            "devin_sha256_aarch64",
            "opencode_npm",
            "opencode_version",
            "agy_version",
            "grok_version",
        )
        if not keys.get(k)
    ]
    if missing:
        raise ValueError(f"packages.txt missing keys: {missing}")
    apt_tuple = tuple(apt)
    have = set(apt_tuple)
    missing_apt = [p for p in REQUIRED_APT if p not in have]
    if missing_apt:
        raise ValueError(f"packages.txt [apt] missing {missing_apt}")
    for pkg in apt_tuple:
        lower = pkg.lower()
        if any(lower == p or lower.startswith(f"{p}-") for p in FORBIDDEN_APT_PREFIXES):
            raise ValueError(f"sbx-runtime must not install a browser package: {pkg}")
    return PackageSpec(
        python_version=keys["python_version"],
        node_major=keys["node_major"],
        codex_npm=keys["codex_npm"],
        codex_version=keys["codex_version"],
        devin_version=keys["devin_version"],
        devin_base_url=keys["devin_base_url"],
        devin_sha256_x86_64=keys["devin_sha256_x86_64"],
        devin_sha256_aarch64=keys["devin_sha256_aarch64"],
        opencode_npm=keys["opencode_npm"],
        opencode_version=keys["opencode_version"],
        agy_version=keys["agy_version"],
        grok_version=keys["grok_version"],
        apt=apt_tuple,
    )


def devin_runtime_env(work: str = "/work") -> dict[str, str]:
    """HOME/XDG for the Devin sandbox (filesystem.md: ``HOME=$SBX_WORK/home``).

    Devin CLI resolves ``credentials.toml`` under ``$XDG_DATA_HOME/devin``
    (default ``~/.local/share/devin``). Pinning both HOME and the XDG dirs keeps
    the credential location stable no matter which env a sandbox exec inherits.
    """
    home = f"{work}/home"
    return {
        "HOME": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
    }


def devin_install_command(spec: PackageSpec | None = None) -> str:
    """Shell command that installs the pinned Devin CLI bundle inside an image."""
    spec = spec or load_packages()
    return (
        f"SBX_DEVIN_VERSION={spec.devin_version} "
        f"SBX_DEVIN_BASE_URL={spec.devin_base_url} "
        f"SBX_DEVIN_SHA256_X86_64={spec.devin_sha256_x86_64} "
        f"SBX_DEVIN_SHA256_AARCH64={spec.devin_sha256_aarch64} "
        f"bash {INSTALL_DEVIN_REMOTE}"
    )


def _version_grep_pattern(expect: str) -> str:
    """ERE matching ``expect`` as a whole token, not a version prefix.

    Plain substring matching (``grep -F``) false-passes pins that are a
    prefix of a different version — ``1.2.3`` inside ``1.2.30`` or
    ``1.0.24`` inside ``11.0.24``. Boundaries are ``[^0-9.]`` so a match
    cannot be part of a longer version number on either side.
    """
    return r"(^|[^0-9.])" + re.escape(expect) + r"([^0-9.]|$)"


def cli_version_check(cli: str, expect: str) -> str:
    """Image build-step command: ``<cli> --version`` must report ``expect``.

    Runs at image-build time (no cold-start cost). Fails the build when the
    installed CLI is missing, broken, or a different version than the
    packages.txt pin.
    """
    return f"{cli} --version 2>&1 | grep -E {shlex.quote(_version_grep_pattern(expect))}"


def _resolved_spec(spec: PackageSpec | None, providers: set[str], env: Any = None) -> PackageSpec:
    """Spec with ``latest`` requests resolved on the build host (SOR-175).

    Resolution runs once at image build / deploy / codegen time — the
    concrete version is frozen into the rendered Dockerfile / image so no
    sandbox ever installs a floating ``@latest``. See ``runtime.versions``.
    """
    if spec is not None:
        return spec
    from runtime.versions import resolve_versions

    return resolve_versions(env=env, providers=frozenset(providers)).spec


def _assert_concrete(provider: str, spec: PackageSpec) -> None:
    """The provider being built must carry a concrete version — never ``latest``."""
    field = {
        "codex": spec.codex_version,
        "devin": spec.devin_version,
        "opencode": spec.opencode_version,
        "antigravity": spec.agy_version,
        "grok": spec.grok_version,
    }.get(provider)
    if not field or field == "latest":
        raise SystemExit(
            f"{provider} CLI version is unresolved ({field!r}); resolve it via "
            "runtime.versions before building the image"
        )


def render_dockerfile_local(
    spec: PackageSpec | None = None, *, devin: bool = False, opencode: bool = False
) -> str:
    """Dockerfile used for no-cloud docker verification; values from packages.txt.

    ``devin=True`` renders ``Dockerfile.devin.local`` (SOR-74): the same base
    recipe plus the pinned standalone Devin CLI and HOME/XDG pointed at
    ``/work/home``. ``opencode=True`` renders ``Dockerfile.opencode.local``
    (Release 0.1): base recipe plus the pinned ``opencode-ai`` npm package.

    Without an explicit ``spec``, ``latest`` requests are resolved on the
    build host at generation time so the rendered Dockerfile always carries
    concrete versions (SOR-175).
    """
    if devin and opencode:
        raise ValueError("devin and opencode variants are mutually exclusive")
    needed = {"codex"} | ({"devin"} if devin else set()) | ({"opencode"} if opencode else set())
    spec = spec or _resolved_spec(None, needed)
    apt = " ".join(spec.apt)
    env = {
        "DEBIAN_FRONTEND": "noninteractive",
        "SBX_WORK": "/work",
        "PYTHONPATH": PYTHONPATH_REMOTE,
        "HOME": "/work/home",
    }
    if devin:
        env.update(devin_runtime_env())
    elif opencode:
        env.update(devin_runtime_env())
    env_lines = "ENV " + " \\\n    ".join(f"{key}={value}" for key, value in env.items())
    extra_run = ""
    mkdirs = "/work/inbox /work/turns /work/.codex /work/home"
    if devin:
        extra_run = (
            f"\nRUN {devin_install_command(spec)}\n"
            f"RUN {cli_version_check('devin', spec.devin_version)}\n"
        )
    elif opencode:
        extra_run = (
            f"\nRUN npm i -g {spec.opencode_npm_spec}\n"
            f"RUN {cli_version_check('opencode', spec.opencode_version)}\n"
        )
    header = """\
# GENERATED FROM runtime/packages.txt — do not edit by hand.
# Regenerate: python -m runtime.image --write-dockerfile
# Local verification only. Production image: runtime/image.py (Modal debian_slim).
# Sandbox cpu/memory/timeout/idle_timeout/workdir/tags/secrets are NOT baked in;
# the control plane passes them to Sandbox.create."""
    if devin:
        header += "\n# Devin fast path (SOR-74): base recipe + pinned standalone Devin CLI."
    elif opencode:
        header += "\n# OpenCode fast path (Release 0.1): base recipe + pinned opencode-ai npm CLI."
    return f"""\
{header}
FROM python:{spec.python_version}-slim-bookworm

{env_lines}

RUN apt-get update \\
 && apt-get install -y --no-install-recommends {apt} \\
 && curl -fsSL {spec.nodesource_setup_url} | bash - \\
 && apt-get install -y --no-install-recommends nodejs \\
 && npm i -g {spec.codex_npm_spec} \\
 && {cli_version_check("codex", spec.codex_version_expect)} \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

COPY runtime {RUNTIME_REMOTE}
COPY runtime/entrypoint.sh {ENTRYPOINT_REMOTE}

RUN chmod +x {ENTRYPOINT_REMOTE} \\
 && mkdir -p {mkdirs}
{extra_run}
WORKDIR /work
ENTRYPOINT ["{ENTRYPOINT_REMOTE}"]
"""


def write_dockerfile_local(path: Path | None = None, *, spec: PackageSpec | None = None) -> Path:
    dest = path or DOCKERFILE_LOCAL
    dest.write_text(render_dockerfile_local(spec), encoding="utf-8")
    return dest


def write_dockerfile_devin_local(
    path: Path | None = None, *, spec: PackageSpec | None = None
) -> Path:
    dest = path or DOCKERFILE_DEVIN_LOCAL
    dest.write_text(render_dockerfile_local(spec, devin=True), encoding="utf-8")
    return dest


def write_dockerfile_opencode_local(
    path: Path | None = None, *, spec: PackageSpec | None = None
) -> Path:
    dest = path or DOCKERFILE_OPENCODE_LOCAL
    dest.write_text(render_dockerfile_local(spec, opencode=True), encoding="utf-8")
    return dest


def write_dockerfiles_locked(spec: PackageSpec | None = None) -> list[Path]:
    """Regenerate all local Dockerfiles and freeze the resolved set (SOR-175).

    One resolution serves every variant and is recorded in the versions lock
    so the codegen output is reproducible evidence.
    """
    from runtime.versions import resolve_versions, write_lock

    if spec is None:
        resolved = resolve_versions(providers={"codex", "devin", "opencode"})
        write_lock(resolved)
        spec = resolved.spec
    return [
        write_dockerfile_local(spec=spec),
        write_dockerfile_devin_local(spec=spec),
        write_dockerfile_opencode_local(spec=spec),
    ]


def sbx_runtime_image(spec: PackageSpec | None = None):
    """Build the Modal Image object (no network). Caller may ``.build(app)``.

    ``spec`` defaults to resolving the build's ``latest`` requests once on
    the build host (SOR-175) — the image always installs a concrete npm pin.
    """
    import modal

    spec = spec or _resolved_spec(None, {"codex"})
    return (
        modal.Image.debian_slim(python_version=spec.python_version)
        .apt_install(*spec.apt)
        .run_commands(
            f"curl -fsSL {spec.nodesource_setup_url} | bash -",
            "apt-get install -y nodejs",
            f"npm i -g {spec.codex_npm_spec}",
            cli_version_check("codex", spec.codex_version_expect),
        )
        .add_local_file(str(ENTRYPOINT_SH), ENTRYPOINT_REMOTE, copy=True)
        .add_local_dir(RUNTIME_DIR, RUNTIME_REMOTE, copy=True)
        .run_commands(f"chmod +x {ENTRYPOINT_REMOTE}")
        .env(
            {
                "SBX_WORK": "/work",
                "PYTHONPATH": PYTHONPATH_REMOTE,
                "HOME": "/work/home",
            }
        )
        .entrypoint([ENTRYPOINT_REMOTE])
    )


def sbx_devin_image(spec: PackageSpec | None = None):
    """Named Modal Image ``sbx-runtime-devin`` (SOR-74 Devin-only fast path).

    ``sbx-runtime`` plus the pinned standalone Devin CLI bundle (sha256-verified
    by ``runtime/install-devin.sh``) and HOME/XDG rooted at ``$SBX_WORK/home``
    so the restored ``credentials.toml`` is the only auth source. No Devin
    Desktop, no ACP bridge, no ``DEVIN_*`` key env is baked in. The build fails
    if ``devin --version`` does not report the packages.txt pin.
    """
    spec = spec or _resolved_spec(None, {"codex", "devin"})
    return (
        sbx_runtime_image(spec)
        .run_commands(
            devin_install_command(spec),
            cli_version_check("devin", spec.devin_version),
        )
        .env(devin_runtime_env())
    )


def opencode_install_command(spec: PackageSpec | None = None) -> str:
    """Shell command that installs the pinned OpenCode CLI inside an image."""
    spec = spec or load_packages()
    return f"npm i -g {spec.opencode_npm_spec} && opencode --version"


def agent_home_env(work: str = "/work") -> dict[str, str]:
    """``HOME=$SBX_WORK/home`` for provider-CLI images (agy / grok).

    Unlike the Devin bundle these CLIs resolve credentials relative to
    ``$HOME`` only (``.gemini/`` / ``.grok/``), so no XDG pinning is needed.
    """
    return {"HOME": f"{work}/home"}


def _host_cli_bin(env_var: str, default: Path, cli: str) -> Path:
    """Resolve the host CLI binary baked into a provider image.

    The binary is a build-host artifact (never committed): ``env_var``
    overrides the well-known ``~/.local/bin`` default. Symlinks resolve to the
    real file so ``add_local_file`` uploads a regular binary (``grok`` is a
    symlink chain under ``~/.grok``).
    """
    raw = os.environ.get(env_var)
    path = Path(raw).expanduser() if raw else default
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise SystemExit(
            f"{cli} binary not found at {path}; install the {cli} CLI on the "
            f"build host or set {env_var} to its path"
        )
    return resolved


def _cli_version_output(bin_path: Path) -> str:
    """``<bin> --version`` combined output (build-host probe, never in-image)."""
    try:
        proc = subprocess.run(
            [str(bin_path), "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=CLI_VERSION_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"{bin_path} --version timed out after {CLI_VERSION_TIMEOUT_S}s") from None
    except OSError as exc:
        raise SystemExit(f"{bin_path} --version failed: {exc}") from exc
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise SystemExit(f"{bin_path} --version exited {proc.returncode}: {out[:200]}")
    return out


def _assert_host_cli_version(host: Path, cli: str, expected: str) -> str:
    """Fail the build unless the host CLI reports the packages.txt pin."""
    out = _cli_version_output(host)
    if not re.search(_version_grep_pattern(expected), out):
        raise SystemExit(
            f"{cli} --version reports {out!r}; packages.txt pins {expected}. "
            f"Install the pinned {cli} CLI on the build host or update the pin."
        )
    return out


def _cli_image(
    image: Any,
    host_bin: Path,
    remote: str,
    env: dict[str, str],
    *,
    version_expect: str | None = None,
) -> Any:
    """``image`` + one host CLI binary at ``remote`` (755) + env overlay.

    ``version_expect`` adds a build-time ``<remote> --version`` gate so the
    published image provably carries the packages.txt pin.
    """
    layered = (
        image.add_local_file(str(host_bin), remote, copy=True)
        .run_commands(f"chmod 755 {remote}")
        .env(env)
    )
    if version_expect:
        layered = layered.run_commands(cli_version_check(remote, version_expect))
    return layered


def sbx_hosted_runtime_image(spec: PackageSpec | None = None):
    """Codex-first hosted image: existing runner plus its read-only HTTP service."""
    return sbx_runtime_image(spec).pip_install(
        "fastapi>=0.115.0", "uvicorn>=0.32.0", "pyjwt>=2.10.0"
    )


def sbx_antigravity_image(agy_bin: Path | None = None, spec: PackageSpec | None = None):
    """Named Modal Image ``sbx-runtime-antigravity`` (SOR-62/SOR-80 fast path).

    ``sbx-runtime`` plus the host ``agy`` binary at ``/usr/local/bin/agy`` —
    the exact derivation the SOR-62 gate verified — with ``HOME`` rooted at
    ``$SBX_WORK/home`` so the restored ``antigravity-oauth-token`` is the only
    auth source. No credential material is baked into the image. The build
    rejects a host binary whose ``--version`` misses the ``agy_version`` pin.
    """
    spec = spec or _resolved_spec(None, {"codex", "antigravity"})
    host = agy_bin or _host_cli_bin(AGY_BIN_ENV, DEFAULT_AGY_BIN, "agy")
    _assert_host_cli_version(host, "agy", spec.agy_version)
    return _cli_image(
        sbx_runtime_image(spec),
        host,
        AGY_BIN_REMOTE,
        agent_home_env(),
        version_expect=spec.agy_version,
    )


def sbx_grok_image(grok_bin: Path | None = None, spec: PackageSpec | None = None):
    """Named Modal Image ``sbx-runtime-grok`` (SOR-62/SOR-80 fast path).

    ``sbx-runtime`` plus the host ``grok`` binary at ``/usr/local/bin/grok`` —
    the exact derivation the SOR-62 gate verified — with ``HOME`` rooted at
    ``$SBX_WORK/home`` so the restored ``.grok/auth.json`` is the only auth
    source. No credential material is baked into the image. The build rejects
    a host binary whose ``--version`` misses the ``grok_version`` pin.
    """
    spec = spec or _resolved_spec(None, {"codex", "grok"})
    host = grok_bin or _host_cli_bin(GROK_BIN_ENV, DEFAULT_GROK_BIN, "grok")
    _assert_host_cli_version(host, "grok", spec.grok_version)
    return _cli_image(
        sbx_runtime_image(spec),
        host,
        GROK_BIN_REMOTE,
        agent_home_env(),
        version_expect=spec.grok_version,
    )


def sbx_opencode_image(base: Any | None = None, spec: PackageSpec | None = None):
    """Named Modal Image ``sbx-runtime-opencode`` (Release 0.1 seam).

    ``sbx-runtime`` plus the pinned ``opencode-ai`` npm package — fully
    reproducible from ``packages.txt``, no host artifact — with ``HOME`` and
    XDG rooted at ``$SBX_WORK/home`` (same pinning as devin, per SOR-96) so
    the restored ``.local/share/opencode/auth.json`` is the only auth
    source. ``base``/``spec`` exist for no-cloud tests.
    """
    spec = spec or _resolved_spec(None, {"codex", "opencode"})
    image = base if base is not None else sbx_runtime_image(spec)
    return image.run_commands(
        f"npm i -g {spec.opencode_npm_spec}",
        cli_version_check("opencode", spec.opencode_version),
    ).env(devin_runtime_env())


def invoke_control_deploy() -> None:
    """``make deploy``: WP1-C ``control.deploy.deploy()`` if present (SOR-47 / SOR-53).

    Must not call ``control.app.main`` (local uvicorn CLI). WP1-A does not
    implement ``control/deploy.py``; until that module lands, print a hint.
    """
    try:
        from control.deploy import deploy as control_deploy
    except ImportError:
        print("control deploy entry not available yet (WP1-C / SOR-53).")
        print("Build the named image with: make image")
        print(
            "Sandbox parameters (idle_timeout, timeout, cpu, memory, workdir, tags, secrets) "
            "are passed by the control plane at Sandbox.create, not baked into sbx-runtime."
        )
        return
    control_deploy()


def _toml_quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_local_secrets(
    *,
    modal_toml: Path | None = None,
    auth_json: Path | None = None,
    workspace: str = MODAL_WORKSPACE,
) -> None:
    """Write Modal + Codex credentials from env into well-known paths.

    Never prints secret values. Used by ``make secrets``. Does not bake
    credentials into the image.
    """
    token_id = os.environ.get("MODAL_TOKEN_ID") or ""
    token_secret = os.environ.get("MODAL_TOKEN_SECRET") or ""
    auth = os.environ.get("CODEX_AUTH_JSON") or ""
    missing = [
        name
        for name, value in (
            ("MODAL_TOKEN_ID", token_id),
            ("MODAL_TOKEN_SECRET", token_secret),
            ("CODEX_AUTH_JSON", auth),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}")

    toml_path = modal_toml or Path.home() / ".modal.toml"
    auth_path = auth_json or Path.home() / ".codex" / "auth.json"
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(
        f"[{workspace}]\n"
        f"token_id = {_toml_quoted(token_id)}\n"
        f"token_secret = {_toml_quoted(token_secret)}\n"
        "active = true\n",
        encoding="utf-8",
    )
    toml_path.chmod(0o600)
    auth_path.write_text(auth, encoding="utf-8")
    auth_path.chmod(0o600)
    print(f"wrote {toml_path} (workspace {workspace})")
    print(f"wrote {auth_path}")


# provider tag -> (image builder, published name). ``codex`` keeps the P1
# base image; the others are the fast-path variants layered on top of it.
IMAGE_BUILDERS: dict[str, tuple[Any, str]] = {
    "codex": (sbx_runtime_image, IMAGE_NAME),
    "devin": (sbx_devin_image, DEVIN_IMAGE_NAME),
    "antigravity": (sbx_antigravity_image, AGY_IMAGE_NAME),
    "grok": (sbx_grok_image, GROK_IMAGE_NAME),
    "opencode": (sbx_opencode_image, OPENCODE_IMAGE_NAME),
}


def image_for(provider: str) -> str:
    """Explicit provider → published-image-name mapping (Release 0.1).

    The control plane mirrors every name as a ``control.config`` constant
    (``*_IMAGE_NAME``; sync covered by tests) and resolves them in
    ``control/backends/modal.py::_PROVIDER_IMAGE_NAMES``.
    """
    try:
        return IMAGE_BUILDERS[provider][1]
    except KeyError:
        raise KeyError(
            f"unknown image provider {provider!r}; known: {sorted(IMAGE_BUILDERS)}"
        ) from None


def _provider_cli_meta(provider: str, spec: PackageSpec) -> dict[str, Any]:
    """Install source + expected ``--version`` evidence for one provider."""
    if provider == "codex":
        return {
            "cli": "codex",
            "cli_path": CODEX_BIN_REMOTE,
            "install": {"kind": "npm", "package": spec.codex_npm_spec},
            "version": spec.codex_version,
            "expect": spec.codex_version_expect,
        }
    if provider == "devin":
        return {
            "cli": "devin",
            "cli_path": DEVIN_BIN_REMOTE,
            "install": {
                "kind": "bundle",
                "base_url": spec.devin_base_url,
                "version": spec.devin_version,
                "sha256": {
                    "x86_64": spec.devin_sha256_x86_64,
                    "aarch64": spec.devin_sha256_aarch64,
                },
            },
            "version": spec.devin_version,
            "expect": spec.devin_version,
        }
    if provider == "opencode":
        return {
            "cli": "opencode",
            "cli_path": OPENCODE_BIN_REMOTE,
            "install": {"kind": "npm", "package": spec.opencode_npm_spec},
            "version": spec.opencode_version,
            "expect": spec.opencode_version,
        }
    host_specs = {
        "antigravity": (AGY_BIN_ENV, "~/.local/bin/agy", AGY_BIN_REMOTE, spec.agy_version),
        "grok": (GROK_BIN_ENV, "~/.local/bin/grok", GROK_BIN_REMOTE, spec.grok_version),
    }
    env_var, default, remote, version = host_specs[provider]
    return {
        "cli": remote.rsplit("/", 1)[-1],
        "cli_path": remote,
        "install": {
            "kind": "host-binary",
            "env": env_var,
            "default": default,
            # SOR-212/SOR-215: the CLI ships from the build host — the
            # local-assisted lane. Deploy degrades the provider instead of
            # failing when the host binary is absent or wrong-version.
            "local_assisted": True,
        },
        "version": version,
        "expect": version,
    }


def image_manifest(spec: PackageSpec | None = None, resolved: Any | None = None) -> dict[str, Any]:
    """Release metadata for every named runtime image (doctor / evidence).

    Pure function of ``packages.txt`` + the ``IMAGE_BUILDERS`` registry: no
    Modal, no network, no host-CLI probe. ``python -m runtime.image
    --manifest`` prints it as JSON. Doctor (SOR-98) uses ``providers.*.image``
    to detect missing named images and ``providers.*.version_check`` to
    verify a sandbox reports the pinned CLI version; the layout block pins
    the HOME/work contract.

    ``resolved`` is an optional ``runtime.versions.ResolvedVersions`` whose
    entries populate ``providers.*.resolution`` (SOR-175): the requested
    pin/``latest`` vs the concrete frozen version and its provenance.
    """
    spec = spec or load_packages()
    entries = dict(getattr(resolved, "entries", None) or {})
    home_env = {
        "codex": {},
        "devin": devin_runtime_env(),
        "antigravity": agent_home_env(),
        "grok": agent_home_env(),
        # opencode auth.json is an XDG data file: the image and the control
        # plane (control/backends/modal.py::_create_env) both pin HOME+XDG —
        # the same devin_runtime_env overlay.
        "opencode": devin_runtime_env(),
    }
    providers: dict[str, dict[str, Any]] = {}
    for provider in sorted(IMAGE_BUILDERS):
        meta = _provider_cli_meta(provider, spec)
        entry = entries.get(provider)
        if entry is not None:
            resolution = {
                "requested": entry.requested,
                "resolved": entry.version,
                "source": entry.source,
                "evidence": dict(entry.evidence),
            }
        else:
            requested = meta["version"]
            resolution = {
                "requested": requested,
                "resolved": None if requested == "latest" else requested,
                "source": "unresolved" if requested == "latest" else "pin",
                "evidence": {},
            }
        providers[provider] = {
            "image": image_for(provider),
            "cli": meta["cli"],
            "cli_path": meta["cli_path"],
            "install": meta["install"],
            "version": meta["version"],
            "resolution": resolution,
            "version_check": {
                "argv": [meta["cli_path"], "--version"],
                "expect": meta["expect"],
            },
            "env": home_env[provider],
            "credential_files": list(PROVIDER_CREDENTIAL_FILES[provider]),
        }
    return {
        "schema": "sbx-runtime/manifest@1",
        "app": APP_NAME,
        "source": "runtime/packages.txt",
        "base": {
            "python_version": spec.python_version,
            "node_major": spec.node_major,
            "apt": list(spec.apt),
        },
        "layout": {
            "work": "/work",
            "home": "/work/home",
            "codex_home": "/work/.codex",
            "dirs": ["inbox", "turns", "home", ".codex"],
            "runtime": RUNTIME_REMOTE,
            "entrypoint": ENTRYPOINT_REMOTE,
        },
        "providers": providers,
    }


def build_named_image(
    *,
    provider: str = "codex",
    name: str | None = None,
    spec: PackageSpec | None = None,
    env: Any = None,
) -> None:
    """``modal image build`` equivalent: build + publish a named runtime image.

    ``provider`` selects the variant (``sbx-runtime`` for codex; the SOR-74 /
    SOR-80 / Release-0.1 fast-path images otherwise). ``name`` overrides the
    published name so a parallel deploy can build RC images without moving
    the production names; ``SBX_IMAGE_APP`` likewise relocates the build app.
    Requires Modal credentials and, for agy / grok, the provider CLI on the
    build host. Never called from ``make test``.

    ``spec`` is the deployment's resolved ``PackageSpec`` (``sbx deploy``
    resolves once and passes it here); standalone builds resolve on the
    build host and freeze the outcome to the versions lock (SOR-175).
    """
    import modal

    if spec is None:
        from runtime.versions import lock_out_path_for, resolve_versions, write_lock

        resolved = resolve_versions(env=env, providers={provider, "codex"})
        write_lock(resolved, lock_out_path_for(env or os.environ))
        spec = resolved.spec
    _assert_concrete(provider, spec)
    app = modal.App.lookup(os.environ.get("SBX_IMAGE_APP") or APP_NAME, create_if_missing=True)
    try:
        builder, default_name = IMAGE_BUILDERS[provider]
    except KeyError:
        raise SystemExit(f"unknown image provider {provider!r}") from None
    publish_name = name or default_name
    image = builder(spec=spec)
    with modal.enable_output():
        built = image.build(app)
        publish = getattr(built, "publish", None)
        if callable(publish):
            publish(publish_name)
    cli_versions = {
        "devin": spec.devin_version,
        "antigravity": spec.agy_version,
        "grok": spec.grok_version,
        "opencode": spec.opencode_version,
    }
    extra = f", {provider} {cli_versions[provider]}" if provider in cli_versions else ""
    print(
        f"named image {publish_name} ready "
        f"(python {spec.python_version}, node {spec.node_major}, {spec.codex_npm_spec}{extra})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sbx-runtime image helpers")
    parser.add_argument(
        "--write-dockerfile",
        action="store_true",
        help="Regenerate Dockerfile.local + Dockerfile.devin.local + "
        "Dockerfile.opencode.local from packages.txt (no Modal)",
    )
    parser.add_argument(
        "--manifest",
        action="store_true",
        help="Print the provider-image manifest as JSON (doctor/evidence input; no Modal)",
    )
    parser.add_argument(
        "--resolve-versions",
        action="store_true",
        help="Resolve provider CLI versions (pins and 'latest' requests) on the "
        "build host, freeze them to the versions lock, and print the result "
        "as JSON (SOR-175; no Modal)",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(IMAGE_BUILDERS),
        default="codex",
        help="Which named image to build/publish (default: codex -> sbx-runtime)",
    )
    parser.add_argument(
        "--devin",
        action="store_true",
        help="Alias for --provider devin (SOR-74)",
    )
    args = parser.parse_args(argv)
    if args.write_dockerfile:
        for path in write_dockerfiles_locked():
            print(f"wrote {path}")
        return 0
    if args.resolve_versions:
        from runtime.versions import lock_out_path_for, resolve_versions, write_lock

        resolved = resolve_versions()
        lock_path = write_lock(resolved, lock_out_path_for(os.environ))
        payload = resolved.lock_payload()
        payload["lock_path"] = str(lock_path)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.manifest:
        from runtime.versions import resolve_versions

        # Offline resolution: pure evidence — a ``latest`` request reports
        # the last frozen lock version when one exists, never a network probe.
        resolved = resolve_versions(offline=True)
        print(json.dumps(image_manifest(resolved=resolved), indent=2, sort_keys=True))
        return 0
    build_named_image(provider="devin" if args.devin else args.provider)
    return 0


if __name__ == "__main__":
    sys.exit(main())
