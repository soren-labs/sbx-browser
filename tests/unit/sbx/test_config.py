"""Single config source + env overrides (SOR-98 must-deliver #1)."""

from __future__ import annotations

import pytest
from control.config import (
    ACCOUNTS_DICT_NAME,
    MODAL_APP_NAME,
    RUNTIME_IMAGE_NAME,
    V1_BOOTSTRAP_SECRET_NAME,
)
from sbx_fakes import make_cfg, make_env

from sbx.config import (
    BootstrapConfig,
    load,
    load_file_values,
    save,
    validate_providers,
)
from sbx.errors import BootstrapError


def test_defaults_come_from_control_config(tmp_path) -> None:
    cfg = make_cfg(tmp_path, write=False)
    assert cfg.config.modal_app_name == MODAL_APP_NAME
    assert cfg.config.accounts_dict == ACCOUNTS_DICT_NAME
    assert cfg.config.bootstrap_secret == V1_BOOTSTRAP_SECRET_NAME
    assert cfg.config.image_codex == RUNTIME_IMAGE_NAME
    assert cfg.config.providers == ()  # SOR-210: default is platform-only
    assert all(source == "default" for source in cfg.sources.values())
    assert cfg.file_exists is False


def test_save_and_reload_roundtrip(tmp_path) -> None:
    config = BootstrapConfig(
        modal_profile="acme",
        api_base_url="https://acme--sbx-control-fastapi-app.modal.run",
        providers=("codex", "devin"),
    )
    path = tmp_path / "config.toml"
    save(config, path)
    cfg = load(path, env={})
    assert cfg.file_exists
    assert cfg.config == config
    assert cfg.sources["modal_profile"] == "file"
    assert cfg.sources["providers"] == "file"


def test_auth_database_config_only_persists_secret_name(tmp_path) -> None:
    from control.config import app_secret_names, remote_env_overlay

    config = BootstrapConfig(auth_database_secret="sbx-auth-database")
    path = tmp_path / "config.toml"
    save(config, path)
    restored = load(path, env={}).config
    assert restored.auth_database_secret == "sbx-auth-database"
    assert "sbx-auth-database" in restored.secret_names()
    env = {**restored.deploy_env(), "DATABASE_URL": "REDACTED"}
    assert "sbx-auth-database" in app_secret_names(env)
    assert remote_env_overlay(env)["SBX_AUTH_DATABASE_SECRET_NAME"] == "sbx-auth-database"
    assert "DATABASE_URL" not in restored.deploy_env()
    assert "DATABASE_URL" not in remote_env_overlay(env)
    assert "REDACTED" not in path.read_text()


@pytest.mark.parametrize("secret_name", [V1_BOOTSTRAP_SECRET_NAME, "tenant-bootstrap"])
def test_auth_database_secret_cannot_share_bootstrap_secret(secret_name) -> None:
    from control.config import app_secret_names

    with pytest.raises(BootstrapError) as error:
        BootstrapConfig(bootstrap_secret=secret_name, auth_database_secret=secret_name)
    assert error.value.code == "auth_database_secret_conflict"
    # The direct Modal deploy entry must also reject conflicting env names.
    with pytest.raises(ValueError, match="must differ"):
        app_secret_names(
            {
                "SBX_V1_BOOTSTRAP_SECRET_NAME": secret_name,
                "SBX_AUTH_DATABASE_SECRET_NAME": secret_name,
            }
        )


def test_auth_database_secret_conflict_is_rejected_from_file_and_env(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[secrets]\nbootstrap = "shared"\nauth_database = "shared"\n')
    with pytest.raises(BootstrapError, match="must differ"):
        load_file_values(path)
    with pytest.raises(BootstrapError, match="must differ"):
        load(path, env={})
    # Env overrides can create a conflict with a valid file configuration.
    save(BootstrapConfig(auth_database_secret="sbx-auth-database"), path)
    with pytest.raises(BootstrapError, match="must differ"):
        load(path, env={"SBX_AUTH_DATABASE_SECRET_NAME": V1_BOOTSTRAP_SECRET_NAME})


def test_env_overrides_beat_file(tmp_path) -> None:
    save(
        BootstrapConfig(modal_profile="file-profile", modal_app_name="file-app"),
        tmp_path / "c.toml",
    )
    cfg = load(
        tmp_path / "c.toml",
        env={"SBX_MODAL_APP_NAME": "env-app", "MODAL_PROFILE": "env-profile"},
    )
    assert cfg.config.modal_app_name == "env-app"
    assert cfg.config.modal_profile == "env-profile"
    assert cfg.sources["modal_app_name"] == "env"
    assert cfg.sources["modal_profile"] == "env"


def test_env_providers_and_base_url(tmp_path) -> None:
    cfg = load(
        tmp_path / "missing.toml",
        env={"SBX_PROVIDERS": "codex, devin", "SBX_BASE_URL": "https://x.modal.run"},
    )
    assert cfg.config.providers == ("codex", "devin")
    assert cfg.config.api_base_url == "https://x.modal.run"


def test_load_file_values_ignores_env(tmp_path) -> None:
    save(BootstrapConfig(modal_profile="file-profile"), tmp_path / "c.toml")
    values = load_file_values(tmp_path / "c.toml")
    assert values.modal_profile == "file-profile"
    # init must not freeze env overrides into the file
    env_cfg = load(tmp_path / "c.toml", env={"SBX_MODAL_PROFILE": "env-profile"})
    assert env_cfg.config.modal_profile == "env-profile"


def test_config_paths_from_env(tmp_path) -> None:
    env = make_env(tmp_path)
    cfg = load(env=env)
    assert cfg.path == tmp_path / "config.toml"


def test_max_concurrent_env_and_file(tmp_path) -> None:
    cfg = load(tmp_path / "missing.toml", env={"SBX_MAX_CONCURRENT": "4"})
    assert cfg.config.max_concurrent == 4
    assert cfg.sources["max_concurrent"] == "env"
    save(BootstrapConfig(max_concurrent=6), tmp_path / "c.toml")
    cfg = load(tmp_path / "c.toml", env={})
    assert cfg.config.max_concurrent == 6
    assert cfg.sources["max_concurrent"] == "file"


def test_max_concurrent_unset_stays_absent(tmp_path) -> None:
    """An unset cap must not leak into the file or the deploy env."""
    config = BootstrapConfig()
    save(config, tmp_path / "c.toml")
    text = (tmp_path / "c.toml").read_text()
    assert "max_concurrent" not in text
    assert "SBX_MAX_CONCURRENT" not in config.deploy_env()


def test_max_concurrent_reaches_deploy_env(tmp_path) -> None:
    env = BootstrapConfig(max_concurrent=3).deploy_env()
    assert env["SBX_MAX_CONCURRENT"] == "3"


def test_max_concurrent_rejects_nonpositive(tmp_path) -> None:
    with pytest.raises(ValueError):
        load(tmp_path / "missing.toml", env={"SBX_MAX_CONCURRENT": "0"})
    with pytest.raises(ValueError):
        load(tmp_path / "missing.toml", env={"SBX_MAX_CONCURRENT": "bogus"})


def test_lifecycle_fields_env_and_file(tmp_path) -> None:
    """SOR-132/SOR-134 + SOR-135: the lifecycle chain resolves file → env → absent."""
    cfg = load(
        tmp_path / "missing.toml",
        env={
            "SBX_IDLE_TIMEOUT_S": "3600",
            "SBX_SANDBOX_IDLE_TIMEOUT_S": "5400",
            "SBX_TURN_MAX_SECONDS": "1200",
        },
    )
    assert cfg.config.idle_timeout_s == 3600
    assert cfg.config.sandbox_idle_timeout_s == 5400
    assert cfg.config.turn_max_seconds == 1200
    assert cfg.sources["idle_timeout_s"] == "env"
    assert cfg.sources["sandbox_idle_timeout_s"] == "env"
    save(
        BootstrapConfig(
            idle_timeout_s=3600,
            sandbox_idle_timeout_s=5400,
            sandbox_timeout_s=28800,
            run_grace_s=120,
        ),
        tmp_path / "c.toml",
    )
    cfg = load(tmp_path / "c.toml", env={})
    assert cfg.config.idle_timeout_s == 3600
    assert cfg.config.sandbox_idle_timeout_s == 5400
    assert cfg.config.sandbox_timeout_s == 28800
    assert cfg.config.run_grace_s == 120
    assert cfg.config.create_grace_s is None
    assert cfg.sources["idle_timeout_s"] == "file"
    assert cfg.sources["sandbox_idle_timeout_s"] == "file"


def test_lifecycle_fields_unset_stay_absent(tmp_path) -> None:
    """Unset lifecycle knobs must not leak into the file or deploy env —
    the remote contract defaults win over an absent local value."""
    config = BootstrapConfig()
    save(config, tmp_path / "c.toml")
    text = (tmp_path / "c.toml").read_text()
    for key in (
        "idle_timeout_s",
        "sandbox_idle_timeout_s",
        "turn_max_seconds",
        "sandbox_timeout_s",
        "create_grace_s",
        "run_grace_s",
    ):
        assert key not in text
    env = config.deploy_env()
    for name in (
        "SBX_IDLE_TIMEOUT_S",
        "SBX_SANDBOX_IDLE_TIMEOUT_S",
        "SBX_TURN_MAX_SECONDS",
        "SBX_SANDBOX_TIMEOUT_S",
        "SBX_CREATE_GRACE_S",
        "SBX_RUN_GRACE_S",
    ):
        assert name not in env


def test_lifecycle_fields_reach_deploy_env() -> None:
    env = BootstrapConfig(
        idle_timeout_s=3600,
        sandbox_idle_timeout_s=5400,
        turn_max_seconds=1200,
        sandbox_timeout_s=28800,
        create_grace_s=600,
        run_grace_s=120,
    ).deploy_env()
    assert env["SBX_IDLE_TIMEOUT_S"] == "3600"
    assert env["SBX_SANDBOX_IDLE_TIMEOUT_S"] == "5400"
    assert env["SBX_TURN_MAX_SECONDS"] == "1200"
    assert env["SBX_SANDBOX_TIMEOUT_S"] == "28800"
    assert env["SBX_CREATE_GRACE_S"] == "600"
    assert env["SBX_RUN_GRACE_S"] == "120"


def test_lifecycle_fields_reject_nonpositive(tmp_path) -> None:
    for env_name in (
        "SBX_IDLE_TIMEOUT_S",
        "SBX_SANDBOX_IDLE_TIMEOUT_S",
        "SBX_TURN_MAX_SECONDS",
        "SBX_RUN_GRACE_S",
    ):
        with pytest.raises(ValueError):
            load(tmp_path / "missing.toml", env={env_name: "0"})
        with pytest.raises(ValueError):
            load(tmp_path / "missing.toml", env={env_name: "bogus"})


def test_control_warmth_env_and_file(tmp_path) -> None:
    """SOR-203: the warmth knobs resolve file → env → absent like the
    other deploy tunables."""
    cfg = load(
        tmp_path / "missing.toml",
        env={
            "SBX_CONTROL_SCALEDOWN_WINDOW_S": "600",
            "SBX_CONTROL_MIN_CONTAINERS": "1",
            "SBX_CONTROL_BUFFER_CONTAINERS": "0",
        },
    )
    assert cfg.config.control_scaledown_window_s == 600
    assert cfg.config.control_min_containers == 1
    assert cfg.config.control_buffer_containers == 0
    assert cfg.sources["control_scaledown_window_s"] == "env"
    save(
        BootstrapConfig(control_scaledown_window_s=900, control_min_containers=1),
        tmp_path / "c.toml",
    )
    cfg = load(tmp_path / "c.toml", env={})
    assert cfg.config.control_scaledown_window_s == 900
    assert cfg.config.control_min_containers == 1
    assert cfg.config.control_buffer_containers is None
    assert cfg.sources["control_scaledown_window_s"] == "file"


def test_control_warmth_unset_stays_absent(tmp_path) -> None:
    """Unset warmth knobs must not leak into the file or the deploy env —
    the ``control.config`` defaults win over an absent local value."""
    config = BootstrapConfig()
    save(config, tmp_path / "c.toml")
    text = (tmp_path / "c.toml").read_text()
    env = config.deploy_env()
    for key, name in (
        ("control_scaledown_window_s", "SBX_CONTROL_SCALEDOWN_WINDOW_S"),
        ("control_min_containers", "SBX_CONTROL_MIN_CONTAINERS"),
        ("control_buffer_containers", "SBX_CONTROL_BUFFER_CONTAINERS"),
    ):
        assert key not in text
        assert name not in env


def test_control_warmth_reaches_deploy_env() -> None:
    """The resolved warmth reaches the ``modal deploy`` subprocess env —
    ``control.modal_app`` bakes it into the function's autoscaler config
    at deploy time (it is not remote env)."""
    env = BootstrapConfig(
        control_scaledown_window_s=600,
        control_min_containers=1,
        control_buffer_containers=2,
    ).deploy_env()
    assert env["SBX_CONTROL_SCALEDOWN_WINDOW_S"] == "600"
    assert env["SBX_CONTROL_MIN_CONTAINERS"] == "1"
    assert env["SBX_CONTROL_BUFFER_CONTAINERS"] == "2"


def test_control_warmth_rejects_negative(tmp_path) -> None:
    """Zero is legal (explicit scale-to-zero); negatives are not."""
    for env_name in (
        "SBX_CONTROL_SCALEDOWN_WINDOW_S",
        "SBX_CONTROL_MIN_CONTAINERS",
        "SBX_CONTROL_BUFFER_CONTAINERS",
    ):
        with pytest.raises(ValueError):
            load(tmp_path / "missing.toml", env={env_name: "-1"})
        with pytest.raises(ValueError):
            load(tmp_path / "missing.toml", env={env_name: "bogus"})


def test_github_bridge_persists_via_file(tmp_path) -> None:
    """SOR-133: gate + Secret *name* round-trip through config.toml."""
    config = BootstrapConfig(github_ephemeral=True, github_secret_name="sbx-github")
    path = tmp_path / "c.toml"
    save(config, path)
    text = path.read_text()
    assert "[github]" in text
    assert "ephemeral = true" in text
    assert 'secret_name = "sbx-github"' in text
    cfg = load(path, env={})
    assert cfg.config == config
    assert cfg.sources["github_ephemeral"] == "file"
    assert cfg.sources["github_secret_name"] == "file"


def test_github_bridge_env_overrides_file(tmp_path) -> None:
    save(
        BootstrapConfig(github_ephemeral=True, github_secret_name="file-secret"),
        tmp_path / "c.toml",
    )
    cfg = load(
        tmp_path / "c.toml",
        env={"SBX_GITHUB_EPHEMERAL": "0", "SBX_GITHUB_SECRET_NAME": "env-secret"},
    )
    assert cfg.config.github_ephemeral is False
    assert cfg.config.github_secret_name == "env-secret"
    assert cfg.sources["github_ephemeral"] == "env"
    assert cfg.sources["github_secret_name"] == "env"


def test_github_bridge_env_arms_without_file(tmp_path) -> None:
    cfg = load(
        tmp_path / "missing.toml",
        env={"SBX_GITHUB_EPHEMERAL": "1", "SBX_GITHUB_SECRET_NAME": "sbx-github"},
    )
    assert cfg.config.github_ephemeral is True
    assert cfg.config.github_secret_name == "sbx-github"


def test_github_bridge_rejects_nonboolean(tmp_path) -> None:
    with pytest.raises(ValueError):
        load(tmp_path / "missing.toml", env={"SBX_GITHUB_EPHEMERAL": "maybe"})


def test_github_bridge_reaches_deploy_env() -> None:
    """Resolved gate + name replay into the remote env — never a token."""
    env = BootstrapConfig(github_ephemeral=True, github_secret_name="sbx-github").deploy_env()
    assert env["SBX_GITHUB_EPHEMERAL"] == "1"
    assert env["SBX_GITHUB_SECRET_NAME"] == "sbx-github"
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env


def test_github_bridge_defaults_stay_out_of_deploy_env() -> None:
    env = BootstrapConfig().deploy_env()
    assert "SBX_GITHUB_EPHEMERAL" not in env
    assert "SBX_GITHUB_SECRET_NAME" not in env


def test_github_secret_name_not_a_managed_secret() -> None:
    """The bridge Secret is operator-managed — ``secret_names()`` must not
    claim it (uninstall would otherwise delete it on --purge-credentials)."""
    cfg = BootstrapConfig(github_ephemeral=True, github_secret_name="sbx-github")
    assert "sbx-github" not in cfg.secret_names()


def test_secret_names_are_provider_aware() -> None:
    """SOR-116: the shared Codex Secret is required iff codex is enabled."""
    codex = BootstrapConfig(providers=("codex",))
    assert codex.secret_names() == (
        "sbx-codex-auth",
        "sbx-basic-auth",
        "sbx-v1-bootstrap",
    )
    devin = BootstrapConfig(providers=("devin",))
    assert "sbx-codex-auth" not in devin.secret_names()
    assert devin.secret_names() == ("sbx-basic-auth", "sbx-v1-bootstrap")
    mixed = BootstrapConfig(providers=("devin", "codex"))
    assert "sbx-codex-auth" in mixed.secret_names()


def test_deploy_env_forwards_provider_set() -> None:
    env = BootstrapConfig(providers=("codex", "devin")).deploy_env()
    assert env["SBX_PROVIDERS"] == "codex,devin"

    # SOR-210: the empty default omits SBX_PROVIDERS so the remote overlay
    # never materializes an empty-string provider set.
    assert "SBX_PROVIDERS" not in BootstrapConfig().deploy_env()


def test_validate_providers_accepts_known_set() -> None:
    validate_providers(("codex",))
    validate_providers(("devin", "grok"))


def test_validate_providers_accepts_empty() -> None:
    """SOR-210: an empty set is a valid platform-only deploy."""
    validate_providers(())


def test_validate_providers_rejects_unknown() -> None:
    with pytest.raises(BootstrapError) as exc:
        validate_providers(("codex", "bogus"))
    assert exc.value.code == "invalid_providers"
    assert "bogus" in exc.value.message
    assert "codex" in (exc.value.hint or "")
