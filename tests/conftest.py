"""Shared pytest fixtures. Strip cloud credentials so tests never depend on them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_CLOUD_PREFIXES = (
    # Provider + platform credentials/config.
    "MODAL_",
    "OPENAI_",
    "CODEX_",
    "XAI_",
    "ANTHROPIC_",
    "OPENCODE_",
    "GEMINI_",
    "GOOGLE_",
    "DEVIN_",
    "GROK_",
    "AGY_",
    "CLOUDFLARE_",
    "AWS_",
    "AZURE_",
    "HF_",
    # Control-plane credential bridges.
    "SBX_BASIC_",
    "SBX_ACCOUNT_",
    "SBX_PROVIDER_",
    "SBX_LINEAR_",
    "LINEAR_",
    # SOR-127 env-cache gate/setup — ambient values must never arm the
    # snapshot cache in tests.
    "SBX_ENV_",
    # SOR-175 version-resolution knobs (locks, registries, overrides) are
    # per-test inputs — ambient values must never pick versions in tests.
    "SBX_VERSIONS_",
    # Fake-runner knobs are set per-test; ambient values must never leak in.
    "FAKE_",
)
_CLOUD_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_AUTH_JSON",
        "CODEX_HOME",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "MODAL_TOKEN",
        "SBX_ACCOUNT_CREDENTIAL",
        "DEVIN_API_KEY",
        "XDG_RUNTIME_DIR",
        # Provider-specific auth bridges.
        "ACP_BACKEND",
        "WINDSURF_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        # Control-plane secrets and credential-forwarding triggers.
        "SBX_V1_BOOTSTRAP_KEY",
        "DATABASE_URL",
        "SBX_AUTH_DB_PATH",
        "SBX_AUTH_DATABASE_SECRET_NAME",
        "SBX_GITHUB_EPHEMERAL",
        "SBX_GITHUB_SECRET_NAME",
        # SOR-177: ambient GitHub App identity/key material must never leak
        # into tests — the private key is credential material.
        "SBX_GITHUB_APP_ID",
        "SBX_GITHUB_APP_SLUG",
        "SBX_GITHUB_APP_PRIVATE_KEY",
        "SBX_GITHUB_APP_SECRET_NAME",
        "SBX_GITHUB_APP_DICT",
        "SBX_GITHUB_APP_STORE_DIR",
        "SBX_GITHUB_APP_API_URL",
        "SBX_LINEAR_MCP_EPHEMERAL",
        "SBX_API_USER",
        "SBX_API_PASSWORD",
        # Ambient CLI/deployment pointers on a dev host must never steer
        # tests at a real deployment or leak a real API key.
        "SBX_BASE_URL",
        "SBX_API_KEY",
        "SBX_RESOURCE_SECRETS",
        # An ambient work dir must never redirect sandbox writes off tmp_path.
        "SBX_WORK",
        # Ambient backend/transport selectors are per-test inputs, not host
        # state — ``SBX_BACKEND=modal`` in a shell would otherwise import
        # ``modal`` at collection time.
        "SBX_DEVIN_TRANSPORT",
        "SBX_BACKEND",
        # SOR-116: ambient provider selection must not leak into tests —
        # ``SBX_PROVIDERS`` gates deploy preconditions and remote seeding.
        "SBX_PROVIDERS",
        # Ambient store-dir overrides would redirect the import-time app's
        # local stores off the XDG-isolated home.
        "SBX_RUN_STORE_DIR",
        "SBX_ARTIFACT_STORE_DIR",
        "SBX_WORKSPACE_STORE_DIR",
        "SBX_WORKFLOW_STORE_DIR",
        # SOR-175 provider-version overrides / resolution inputs — tests set
        # them explicitly; ambient values must never pick a CLI version.
        "SBX_CODEX_VERSION",
        "SBX_DEVIN_VERSION",
        "SBX_OPENCODE_VERSION",
        "SBX_AGY_VERSION",
        "SBX_GROK_VERSION",
        "SBX_NPM_REGISTRY",
        "SBX_DEVIN_BASE_URL",
        "SBX_DEVIN_SHA256_X86_64",
        "SBX_DEVIN_SHA256_AARCH64",
        # Build-host binary overrides (agy/grok) are per-test inputs too.
        "SBX_AGY_BIN",
        "SBX_GROK_BIN",
    }
)
# Deliberately NOT scrubbed: SBX_V1_API_KEY / SBX_V1_BASE_URL /
# SBX_POOL_GATE_REAL are the opt-in inputs of the real acceptance gate
# (tests/acceptance, outside testpaths); tests/e2e_modal overrides this
# fixture when SBX_E2E_MODAL=1.


def _is_cloud_key(key: str) -> bool:
    return key in _CLOUD_KEYS or key.startswith(_CLOUD_PREFIXES)


# Collection-time scrub (SOR-55/SOR-101): ``control.app`` builds a FastAPI app
# at import, and ambient host env (``SBX_V1_BOOTSTRAP_KEY``,
# ``SBX_BACKEND=modal``, provider credentials) would otherwise leak into —
# or break — collection before any fixture runs. The autouse fixture below
# re-applies the scrub per-test so late monkeypatch snapshots stay clean.
# ``SBX_E2E_MODAL=1`` is the documented opt-out: the Modal e2e suite needs
# the real credentials it is handed.
if os.environ.get("SBX_E2E_MODAL") != "1":
    for _key in list(os.environ):
        if _is_cloud_key(_key):
            os.environ.pop(_key, None)


@pytest.fixture(autouse=True)
def _no_cloud_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Drop host credentials and isolate HOME/XDG (SOR-55/SOR-56)."""
    for key in list(os.environ):
        if _is_cloud_key(key):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("SBX_BACKEND", "local")


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def fake_codex(repo_root: Path) -> Path:
    return repo_root / "tests" / "fakes" / "fake_codex.py"


@pytest.fixture
def stub_runner(repo_root: Path) -> Path:
    return repo_root / "tests" / "fakes" / "stub_runner.py"
