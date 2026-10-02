"""``sbx deploy``: idempotent pipeline, secrets wired, actionable failures."""

from __future__ import annotations

import json

import pytest
from sbx_fakes import FAKE_CONSOLE_MANIFEST, FAKE_GIT_SHA, FakePlane, make_cfg, make_env, make_v1

from sbx.config import BootstrapConfig, key_path, load
from sbx.deploy import deploy, read_deploy_state
from sbx.errors import BootstrapError
from sbx.keys import fingerprint, read_key


def _deploy(tmp_path, plane, *, env=None, config=None, **kwargs):
    env = env or make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, config=config)
    transport, http = make_v1()
    report = deploy(
        cfg,
        plane,
        env=env,
        transport=transport,
        sleep=lambda s: None,
        **kwargs,
    )
    return report, env, http


def test_deploy_happy_path(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    report, env, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))

    token = read_key(key_path(env))
    assert token is not None
    # control only ever receives the token inside the Secret env; storage is hash-only
    assert plane.secrets["sbx-v1-bootstrap"]["SBX_V1_BOOTSTRAP_KEY"] == token
    assert "sbx-basic-auth" in plane.secrets
    for name in (
        "sbx-sessions",
        "sbx-runs",
        "sbx-accounts",
        "sbx-workflows",
        "sbx-artifacts",
        "sbx-workspaces",
    ):
        assert name in plane.dicts
    assert plane.image_calls == ["codex"]
    assert plane.image_names["codex"] == "sbx-runtime"
    assert plane.apps["sbx-control"].startswith("https://")
    assert report.base_url == plane.apps["sbx-control"]
    # deployed URL is persisted into the single config source
    assert load(tmp_path / "config.toml", env={}).config.api_base_url == report.base_url
    state = read_deploy_state(env)
    assert state["version"] and state["app_url"] == report.base_url
    assert state["key_fingerprint"] == fingerprint(token)
    assert report.key_created and not report.key_rotated


def test_deploy_requires_configured_auth_database_secret(tmp_path) -> None:
    plane = FakePlane()
    config = BootstrapConfig(auth_database_secret="sbx-auth-database")
    with pytest.raises(BootstrapError) as error:
        _deploy(tmp_path, plane, config=config)
    assert error.value.code == "auth_database_secret_missing"
    assert plane.image_calls == []
    assert plane.apps == {}


def test_deploy_forwards_database_secret_name_without_url(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-auth-database"] = {"DATABASE_URL": "REDACTED"}
    report, _, _ = _deploy(
        tmp_path, plane, config=BootstrapConfig(auth_database_secret="sbx-auth-database")
    )
    assert report.base_url
    assert plane.deploy_env["SBX_AUTH_DATABASE_SECRET_NAME"] == "sbx-auth-database"
    assert "DATABASE_URL" not in plane.deploy_env


@pytest.mark.parametrize("shared", ["sbx-v1-bootstrap", "tenant-bootstrap"])
def test_deploy_rejects_shared_auth_secret_before_bootstrap_rotation(tmp_path, shared) -> None:
    plane = FakePlane()
    before = {"SBX_V1_BOOTSTRAP_KEY": "REDACTED", "DATABASE_URL": "REDACTED"}
    plane.secrets[shared] = dict(before)
    env = make_env(
        tmp_path,
        {"SBX_AUTH_DATABASE_SECRET_NAME": shared, "SBX_V1_BOOTSTRAP_SECRET_NAME": shared},
    )
    assert not key_path(env).exists()  # a missing local key would trigger rotation
    with pytest.raises(BootstrapError) as error:
        _deploy(tmp_path, plane, env=env)
    assert error.value.code == "auth_database_secret_conflict"
    assert plane.secrets[shared] == before
    assert plane.secret_create_calls == 0
    assert plane.deploy_calls == 0
    assert plane.image_calls == []
    assert not key_path(env).exists()


def test_bootstrap_rotation_preserves_separate_auth_database_secret(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-v1-bootstrap"] = {"SBX_V1_BOOTSTRAP_KEY": "REDACTED"}
    plane.secrets["sbx-auth-database"] = {"DATABASE_URL": "REDACTED"}
    report, env, _ = _deploy(
        tmp_path, plane, config=BootstrapConfig(auth_database_secret="sbx-auth-database")
    )
    assert report.key_rotated
    assert plane.secrets["sbx-auth-database"] == {"DATABASE_URL": "REDACTED"}
    assert plane.secrets["sbx-v1-bootstrap"] == {"SBX_V1_BOOTSTRAP_KEY": read_key(key_path(env))}


def test_deploy_is_idempotent(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(tmp_path)
    _deploy(tmp_path, plane, env=env, config=BootstrapConfig(providers=("codex",)))
    first_token = read_key(key_path(env))
    dicts_before = {k: dict(v) for k, v in plane.dicts.items()}
    plane.dicts["sbx-sessions"]["session/abc"] = {"id": "abc"}

    report2, _, _ = _deploy(tmp_path, plane, env=env, config=BootstrapConfig(providers=("codex",)))
    assert read_key(key_path(env)) == first_token  # key not reminted
    assert plane.secrets["sbx-v1-bootstrap"]["SBX_V1_BOOTSTRAP_KEY"] == first_token
    assert plane.dicts["sbx-sessions"]["session/abc"] == {"id": "abc"}
    assert set(plane.dicts) == set(dicts_before)
    assert not report2.key_created


def test_basic_secret_uses_env_names_control_reads(tmp_path) -> None:
    """The Secret keys must match ``control.config.basic_credentials`` —
    a name the app never reads would silently fall back to sbx/sbx."""
    import os
    import stat

    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    _, env, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))
    secret_env = plane.secrets["sbx-basic-auth"]
    assert set(secret_env) == {"SBX_BASIC_USER", "SBX_BASIC_PASS"}

    from control.config import basic_credentials

    saved = {k: os.environ.get(k) for k in ("SBX_BASIC_USER", "SBX_BASIC_PASS")}
    os.environ.update(secret_env)
    try:
        assert basic_credentials() == (secret_env["SBX_BASIC_USER"], secret_env["SBX_BASIC_PASS"])
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    from sbx.config import basic_auth_path

    assert stat.S_IMODE(os.stat(basic_auth_path(env)).st_mode) == 0o600


def test_legacy_basic_password_env_still_read() -> None:
    """Pre-0.1 docs named the variable SBX_BASIC_PASSWORD; secrets created
    from those docs must not silently degrade to the default password."""
    import os

    from control.config import basic_credentials

    for key in ("SBX_BASIC_USER", "SBX_BASIC_PASS", "SBX_API_PASSWORD"):
        os.environ.pop(key, None)
    os.environ["SBX_BASIC_PASSWORD"] = "legacy-pass"
    try:
        assert basic_credentials() == ("sbx", "legacy-pass")
    finally:
        os.environ.pop("SBX_BASIC_PASSWORD", None)


def test_deploy_missing_codex_secret_degrades_not_fails(tmp_path) -> None:
    """SOR-217: a missing provider credential Secret degrades codex — the
    Platform deploy completes, the runtime record carries the reason, and
    the remediation stays actionable in the step detail."""
    plane = FakePlane()
    report, _, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))

    assert report.base_url  # deploy succeeded — platform healthy
    assert report.degraded_providers is not None and "codex" in report.degraded_providers
    assert "sbx-codex-auth" in report.degraded_providers["codex"]
    step = next(s for s in report.steps if s.name == "secret:codex")
    assert "modal secret create sbx-codex-auth" in step.detail
    record = plane.dicts["sbx-runtime"]["runtime/codex"]
    assert record["status"] == "degraded"
    assert "sbx-codex-auth" in record["detail"]
    # The image still builds — only the app-level Secret mount is dropped.
    assert plane.image_calls == ["codex"]
    assert plane.deploy_env["SBX_DEGRADED_PROVIDERS"] == "codex"


def test_deploy_degraded_codex_skips_app_secret_mount(tmp_path) -> None:
    """The degraded set unmounts the absent codex Secret so ``modal deploy``
    itself cannot fail on it (``control.config.app_secret_names``)."""
    from control.config import app_secret_names

    env = {"SBX_PROVIDERS": "codex", "SBX_DEGRADED_PROVIDERS": "codex"}
    assert "sbx-codex-auth" not in app_secret_names(env)
    # a non-degraded codex still requires the mount
    assert "sbx-codex-auth" in app_secret_names({"SBX_PROVIDERS": "codex"})


def test_deploy_missing_codex_secret_guides_login_on_clean_home(tmp_path) -> None:
    """When no local credential exists the remediation starts at the
    official login, not a bare secret-create command (SOR-115)."""
    plane = FakePlane()
    report, _, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))
    step = next(s for s in report.steps if s.name == "secret:codex")
    assert "codex login" in step.detail
    assert "~/.codex/auth.json" in step.detail
    assert "modal secret create sbx-codex-auth" in step.detail


def test_deploy_non_codex_providers_skip_codex_secret(tmp_path) -> None:
    """Only selected providers gate onboarding: a devin-only deploy must
    not require ``sbx-codex-auth`` (SOR-115)."""
    plane = FakePlane()  # no sbx-codex-auth — and none needed
    config = BootstrapConfig(providers=("devin",))
    report, _, _ = _deploy(tmp_path, plane, config=config)
    assert plane.image_calls == ["devin"]
    names = [s.name for s in report.steps]
    assert "secret:codex" not in names
    assert plane.deploy_env["SBX_PROVIDERS"] == "devin"


def test_deploy_missing_modal_auth_is_actionable(tmp_path) -> None:
    plane = FakePlane(workspace=None)
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))
    assert exc.value.code == "modal_auth_missing"
    assert "modal token new" in (exc.value.hint or "")


def test_degraded_image_build_recovers_on_redeploy(tmp_path) -> None:
    """SOR-217: a provider image build failure degrades the provider — the
    platform deploy completes — and the next deploy retries the build."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.fail_on.add("ensure_image")
    report, _, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))
    assert report.base_url  # platform deployed despite the failed image
    assert "codex" in (report.degraded_providers or {})
    assert plane.dicts["sbx-runtime"]["runtime/codex"]["status"] == "degraded"
    step = next(s for s in report.steps if s.name == "image:codex")
    assert "not built" in step.detail

    plane.fail_on.clear()
    report, env, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))
    assert report.base_url
    assert not report.degraded_providers
    names = [s.name for s in report.steps]
    assert "image:codex" in names
    assert plane.dicts["sbx-runtime"]["runtime/codex"]["status"] == "ready"


def test_stale_remote_secret_rotates_with_new_key(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.secrets["sbx-v1-bootstrap"] = {"SBX_V1_BOOTSTRAP_KEY": "sbx_oldtoken"}
    report, env, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))
    token = read_key(key_path(env))
    assert plane.secrets["sbx-v1-bootstrap"]["SBX_V1_BOOTSTRAP_KEY"] == token
    assert report.key_rotated


def test_deploy_multi_provider_images(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    config = BootstrapConfig(providers=("codex", "devin"))
    _deploy(tmp_path, plane, config=config)
    assert plane.image_calls == ["codex", "devin"]


def test_deploy_materializes_imported_account_secret(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.dicts["sbx-accounts"] = {
        "account/devin-1": {
            "id": "devin-1",
            "provider": "devin",
            "secret_name": "sbx-acct-devin-1",
        },
        "credential/devin-1": {
            "provider": "devin",
            "files": {".local/share/devin/credentials.toml": "credential-v1"},
        },
    }
    _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex", "devin")))
    raw = plane.secrets["sbx-acct-devin-1"]["SBX_ACCOUNT_CREDENTIAL"]
    assert json.loads(raw)["provider"] == "devin"
    assert "credential-v1" in raw

    plane.dicts["sbx-accounts"]["credential/devin-1"] = {
        "provider": "devin",
        "files": {".local/share/devin/credentials.toml": "credential-v2"},
    }
    _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex", "devin")))
    raw2 = plane.secrets["sbx-acct-devin-1"]["SBX_ACCOUNT_CREDENTIAL"]
    assert "credential-v2" in raw2 and "credential-v1" not in raw2


def test_deploy_devin_only_does_not_require_codex_secret(tmp_path) -> None:
    """SOR-116 gate: providers=[devin] fresh deploy needs no sbx-codex-auth."""
    plane = FakePlane()  # no secrets at all
    report, _, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("devin",)))
    assert "sbx-codex-auth" not in plane.secrets
    assert plane.image_calls == ["devin"]
    # the Codex preflight never runs — no step, no Secret lookup
    assert all(s.name != "secret:codex" for s in report.steps)
    assert plane.deploy_env["SBX_PROVIDERS"] == "devin"


def test_deploy_mixed_providers_degrades_only_codex(tmp_path) -> None:
    """SOR-217: missing codex credential degrades codex alone — devin still
    builds ready and the platform deploy completes."""
    plane = FakePlane()
    report, _, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex", "devin")))
    assert report.base_url
    assert set(report.degraded_providers or {}) == {"codex"}
    assert (
        "modal secret create sbx-codex-auth"
        in next(s for s in report.steps if s.name == "secret:codex").detail
    )
    assert plane.dicts["sbx-runtime"]["runtime/codex"]["status"] == "degraded"
    assert plane.dicts["sbx-runtime"]["runtime/devin"]["status"] == "ready"
    assert plane.image_calls == ["codex", "devin"]  # image build unaffected


def test_deploy_empty_providers_platform_only(tmp_path) -> None:
    """SOR-210: providers=() deploys the core platform only — no provider
    images, no provider credential gates, no codex Secret — and the durable
    core state stays idempotent."""
    plane = FakePlane()
    env = make_env(tmp_path)
    report, _, _ = _deploy(tmp_path, plane, env=env, config=BootstrapConfig(providers=()))

    assert report.base_url
    assert plane.image_calls == []  # zero provider images built
    assert "sbx-codex-auth" not in plane.secrets  # no provider credential gate
    assert plane.deploy_calls == 1
    # durable core state still materialized
    for name in (
        "sbx-sessions",
        "sbx-runs",
        "sbx-accounts",
        "sbx-workflows",
        "sbx-artifacts",
        "sbx-workspaces",
    ):
        assert name in plane.dicts
    assert "sbx-v1-bootstrap" in plane.secrets and "sbx-basic-auth" in plane.secrets
    # provider version resolution is skipped, not failed
    assert report.cli_versions is None
    assert read_deploy_state(env)["app_url"] == report.base_url
    assert "cli_versions" not in read_deploy_state(env)

    # idempotent re-run: same key, no extra images, dicts untouched
    first_token = read_key(key_path(env))
    plane.dicts["sbx-sessions"]["session/abc"] = {"id": "abc"}
    report2, _, _ = _deploy(tmp_path, plane, env=env, config=BootstrapConfig(providers=()))
    assert read_key(key_path(env)) == first_token
    assert plane.image_calls == []
    assert plane.dicts["sbx-sessions"]["session/abc"] == {"id": "abc"}
    assert not report2.key_created


def test_deploy_github_bridge_secret_preflight(tmp_path) -> None:
    """SOR-117: a named GitHub bridge Secret must exist when the gate is
    armed — fail-before-write, never a silently inert remote bridge."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(
        tmp_path,
        {"SBX_GITHUB_EPHEMERAL": "1", "SBX_GITHUB_SECRET_NAME": "sbx-github"},
    )
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane, env=env, config=BootstrapConfig(providers=("codex",)))
    assert exc.value.code == "secret_missing"
    assert "sbx-github" in str(exc.value)
    assert plane.secret_create_calls == 0  # fail-before-write

    plane.secrets["sbx-github"] = {"GH_TOKEN": "REDACTED_GITHUB"}
    report, _, _ = _deploy(tmp_path, plane, env=env, config=BootstrapConfig(providers=("codex",)))
    assert any(s.name == "secret:github" for s in report.steps)


def test_deploy_github_bridge_preflight_from_config_file(tmp_path) -> None:
    """SOR-133: gate + Secret name resolve from config.toml (not only env),
    and the resolved pair is replayed into the deploy env for the remote
    app — still without ever touching the token value."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(tmp_path)  # no SBX_GITHUB_* env — file is the source
    config = BootstrapConfig(github_ephemeral=True, github_secret_name="sbx-github")
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane, env=env, config=config)
    assert exc.value.code == "secret_missing"
    assert "sbx-github" in str(exc.value)
    assert plane.secret_create_calls == 0  # fail-before-write

    plane.secrets["sbx-github"] = {"GH_TOKEN": "REDACTED_GITHUB"}
    report, _, _ = _deploy(tmp_path, plane, env=env, config=config)
    assert any(s.name == "secret:github" for s in report.steps)
    assert plane.deploy_env["SBX_GITHUB_EPHEMERAL"] == "1"
    assert plane.deploy_env["SBX_GITHUB_SECRET_NAME"] == "sbx-github"
    assert all("REDACTED_GITHUB" not in v for v in plane.deploy_env.values())


def test_deploy_github_gate_alone_needs_no_secret(tmp_path) -> None:
    """The local-gate path (token in the control-plane env, no named Secret)
    deploys unchanged — the GitHub preflight is opt-in, not ambient."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(tmp_path, {"SBX_GITHUB_EPHEMERAL": "1"})
    report, _, _ = _deploy(tmp_path, plane, env=env, config=BootstrapConfig(providers=("codex",)))
    assert all(s.name != "secret:github" for s in report.steps)


def test_deploy_unknown_provider_fails_before_any_write(tmp_path) -> None:
    plane = FakePlane()
    with pytest.raises(BootstrapError) as exc:
        _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex", "bogus")))
    assert exc.value.code == "invalid_providers"
    assert "bogus" in exc.value.message
    assert plane.secret_create_calls == 0
    assert plane.deploy_calls == 0


def test_deploy_missing_enabled_account_secret_degrades(tmp_path) -> None:
    """SOR-217: an enabled provider's referenced-but-absent Secret degrades
    the provider — the deploy completes and the reason is recorded."""
    plane = FakePlane()
    plane.dicts["sbx-accounts"] = {
        "account/devin-1": {
            "id": "devin-1",
            "provider": "devin",
            "secret_name": "sbx-acct-devin-1",
        },
        # no credential blob → the materialize step cannot satisfy it
    }
    report, _, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("devin",)))
    assert report.base_url
    assert "devin" in (report.degraded_providers or {})
    assert "sbx-acct-devin-1" in report.degraded_providers["devin"]
    step = next(s for s in report.steps if s.name == "credentials:preflight")
    assert "sbx-acct-devin-1" in step.detail
    assert "control.onboarding" in step.detail


def test_deploy_ignores_disabled_provider_account_secret(tmp_path) -> None:
    """A devin account's missing Secret is no codex-only prerequisite."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.dicts["sbx-accounts"] = {
        "account/devin-1": {
            "id": "devin-1",
            "provider": "devin",
            "secret_name": "sbx-acct-devin-1",
        },
    }
    report, _, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))
    assert report.base_url
    assert "sbx-acct-devin-1" not in plane.secrets  # not materialized either


def test_deploy_does_not_overwrite_custom_account_secret(tmp_path) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.secrets["customer-managed"] = {"SBX_ACCOUNT_CREDENTIAL": "external"}
    plane.dicts["sbx-accounts"] = {
        "account/grok-1": {
            "id": "grok-1",
            "provider": "grok",
            "secret_name": "customer-managed",
        },
        "credential/grok-1": {
            "provider": "grok",
            "files": {".grok/auth.json": "stored"},
        },
    }
    _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex", "grok")))
    assert plane.secrets["customer-managed"] == {"SBX_ACCOUNT_CREDENTIAL": "external"}


# --------------------------------------------------------------- SOR-175


def test_deploy_freezes_cli_versions_and_passes_resolved_spec(tmp_path) -> None:
    """SOR-175: deploy resolves CLI versions once, freezes them to the
    state-dir lock, passes the concrete spec into every image build, and
    records the evidence in the deploy state."""
    from runtime.image import load_packages

    from sbx.config import state_dir

    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    report, env, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))

    step = next(s for s in report.steps if s.name == "versions")
    assert step.detail

    spec = load_packages()
    assert report.cli_versions == {"codex": spec.codex_version}
    # every image build receives the same frozen spec — never a per-image
    # or per-sandbox re-resolution
    assert plane.image_specs["codex"] is not None
    assert plane.image_specs["codex"].codex_version == spec.codex_version
    assert "@latest" not in plane.image_specs["codex"].codex_npm_spec

    lock_path = state_dir(env) / "cli-versions.json"
    assert lock_path.exists()
    lock = json.loads(lock_path.read_text())
    assert lock["schema"] == "sbx-runtime/cli-versions@1"
    assert lock["providers"]["codex"]["version"] == spec.codex_version
    assert lock["providers"]["codex"]["source"] == "pin"

    state = read_deploy_state(env)
    assert state["versions_lock"] == str(lock_path)
    assert state["cli_versions"]["providers"]["codex"]["version"] == spec.codex_version


def test_deploy_resolution_scoped_to_enabled_providers(tmp_path) -> None:
    """A codex-only deploy must never probe for agy/grok host binaries."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    report, env, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))
    state = read_deploy_state(env)
    assert set(state["cli_versions"]["providers"]) == {"codex"}
    assert report.cli_versions is not None and set(report.cli_versions) == {"codex"}


def test_deploy_versions_lock_replays_frozen_set(tmp_path) -> None:
    """Rollback: ``--versions-lock`` (or SBX_VERSIONS_LOCK) pins the earlier
    deployment's resolved versions into the new deployment's images."""
    from runtime.image import load_packages
    from runtime.versions import write_lock

    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}

    # A previous deployment's frozen lock — codex pinned one minor back.
    import dataclasses

    from runtime.versions import resolve_versions

    resolved = resolve_versions(load_packages(), {}, providers={"codex"})
    entries = dict(resolved.entries)
    e = entries["codex"]
    entries["codex"] = dataclasses.replace(e, version="0.0.1-old")
    old = dataclasses.replace(
        resolved,
        spec=dataclasses.replace(resolved.spec, codex_version="0.0.1-old"),
        entries=entries,
    )
    lock_path = write_lock(old, tmp_path / "old-lock.json")

    report, env, _ = _deploy(
        tmp_path, plane, config=BootstrapConfig(providers=("codex",)), versions_lock=str(lock_path)
    )
    assert report.cli_versions == {"codex": "0.0.1-old"}
    assert plane.image_specs["codex"].codex_version == "0.0.1-old"

    state = read_deploy_state(env)
    assert state["cli_versions"]["providers"]["codex"]["source"] == "lock"
    assert state["cli_versions"]["providers"]["codex"]["version"] == "0.0.1-old"


def test_deploy_console_step_records_release_evidence(tmp_path) -> None:
    """SOR-266: deploy reports a console build step and persists the
    SHA-bound frontend manifest the SOR-260 gate can audit."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    report, env, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))

    names = [s.name for s in report.steps]
    assert "console" in names
    assert "verify:console" in names
    frontend = read_deploy_state(env)["frontend"]
    assert frontend["source"] == "console/dist"
    assert frontend["git_sha"] == FAKE_GIT_SHA
    assert frontend["primary_asset"]["path"] == "assets/index-deadbeef.js"
    assert frontend["primary_asset"]["sha256"]
    assert frontend["manifest"] == "build-manifest.json"
    assert frontend["served_url"] == report.base_url
    assert frontend["deployed_at"]


def test_deploy_fails_loudly_when_root_is_not_console(tmp_path) -> None:
    """A deployment still serving the legacy UI (or 404) must fail verify —
    the SOR-260 regression cannot pass silently."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, config=BootstrapConfig(providers=("codex",)))
    transport, _ = make_v1(console=False)
    with pytest.raises(BootstrapError, match="not the V2 Session Console") as exc:
        deploy(cfg, plane, env=env, transport=transport, sleep=lambda s: None)
    assert exc.value.code == "deploy_verify_failed"


def test_deploy_fails_on_stale_console_manifest(tmp_path) -> None:
    """The served manifest's git_sha must equal the deployed source SHA —
    a stale baked console/dist is a hard failure."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, config=BootstrapConfig(providers=("codex",)))
    stale = {**FAKE_CONSOLE_MANIFEST, "git_sha": "0" * 40}
    transport, _ = make_v1(console_manifest=stale)
    with pytest.raises(BootstrapError, match="manifest git_sha") as exc:
        deploy(cfg, plane, env=env, transport=transport, sleep=lambda s: None)
    assert exc.value.code == "deploy_verify_failed"


def test_deploy_fails_fast_when_console_dist_missing(tmp_path) -> None:
    """An explicit SBX_CONSOLE_DIST pointing at nothing aborts before any
    remote write — never a silent web/ fallback."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(tmp_path, extra={"SBX_CONSOLE_DIST": str(tmp_path / "nope")})
    cfg = make_cfg(tmp_path, env=env, config=BootstrapConfig(providers=("codex",)))
    with pytest.raises(BootstrapError) as exc:
        deploy(cfg, plane, env=env, transport=make_v1()[0], sleep=lambda s: None)
    assert exc.value.code == "console_dist_missing"
    assert not plane.apps
