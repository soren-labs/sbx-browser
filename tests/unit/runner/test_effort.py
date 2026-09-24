"""SOR-179: canonical reasoning_effort — levels, capability, native mapping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from runtime.runner.adapters.antigravity import AntigravityAdapter
from runtime.runner.adapters.grok import GrokAdapter
from runtime.runner.effort import (
    CANONICAL_EFFORTS,
    effort_error,
    normalize_effort,
    supported_efforts,
)
from tests.unit.runner.conftest import MODEL, init_runner, load_json, run_runner, write_message

PROVIDERS_NO_EFFORT = ("opencode", "devin")


def _write_session(work: Path, **fields: object) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / "session.json").write_text(json.dumps(fields), encoding="utf-8")


class TestEffortModule:
    def test_canonical_order(self) -> None:
        assert CANONICAL_EFFORTS == (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        )

    @pytest.mark.parametrize("provider", ["codex", "antigravity", "grok"])
    def test_supported_providers(self, provider: str) -> None:
        assert supported_efforts(provider) == CANONICAL_EFFORTS
        assert effort_error(provider, "high") is None

    @pytest.mark.parametrize("provider", PROVIDERS_NO_EFFORT)
    def test_unsupported_providers(self, provider: str) -> None:
        assert supported_efforts(provider) == ()
        assert provider in (effort_error(provider, "low") or "")

    def test_no_effort_never_refused(self) -> None:
        for provider in ("codex", *PROVIDERS_NO_EFFORT):
            assert effort_error(provider, None) is None

    def test_normalize_passthrough_and_strip(self) -> None:
        assert normalize_effort(None) is None
        assert normalize_effort("  ") is None
        assert normalize_effort("high") == "high"

    def test_normalize_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="turbo"):
            normalize_effort("turbo")


class TestInitEffort:
    def test_codex_init_writes_native_config(self, work: Path, runner_env: dict[str, str]) -> None:
        result = run_runner(
            ["init", "--auth", "auth_json", "--model", MODEL, "--reasoning-effort", "high"],
            runner_env,
        )
        assert result.returncode == 0, result.stderr
        config = (work / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert 'model_reasoning_effort = "high"' in config
        session = load_json(work / "session.json")
        assert session["reasoning_effort"] == "high"

    def test_init_without_effort(self, work: Path, runner_env: dict[str, str]) -> None:
        init_runner(runner_env)
        config = (work / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert "model_reasoning_effort" not in config
        session = load_json(work / "session.json")
        assert session["reasoning_effort"] is None

    @pytest.mark.parametrize("provider", PROVIDERS_NO_EFFORT)
    def test_unsupported_provider_init_fails(
        self, work: Path, runner_env: dict[str, str], provider: str
    ) -> None:
        result = run_runner(
            [
                "init",
                "--auth",
                "auth_json",
                "--model",
                MODEL,
                "--provider",
                provider,
                "--reasoning-effort",
                "low",
            ],
            runner_env,
        )
        assert result.returncode == 1
        assert "reasoning_effort" in result.stderr
        # Failed init leaves no session.json — nothing is silently dropped.
        assert not (work / "session.json").is_file()

    def test_unknown_level_init_fails(self, work: Path, runner_env: dict[str, str]) -> None:
        result = run_runner(
            [
                "init",
                "--auth",
                "auth_json",
                "--model",
                MODEL,
                "--reasoning-effort",
                "turbo",
            ],
            runner_env,
        )
        assert result.returncode == 1
        assert "turbo" in result.stderr


class TestAdapterEffortArgv:
    @pytest.mark.parametrize(
        ("adapter_cls", "resume_flag"),
        [(AntigravityAdapter, "--conversation"), (GrokAdapter, "--resume")],
    )
    def test_first_and_resume_argv_carry_effort(
        self,
        adapter_cls,
        resume_flag: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SBX_WORK", str(tmp_path))
        _write_session(tmp_path, model=MODEL, reasoning_effort="medium")
        adapter = adapter_cls()

        argv = adapter.first_turn_argv("hi", MODEL)
        assert argv[argv.index("--effort") + 1] == "medium"

        argv = adapter.resume_argv("next", "native-id")
        assert argv[argv.index(resume_flag) + 1] == "native-id"
        assert argv[argv.index("--effort") + 1] == "medium"

    @pytest.mark.parametrize("adapter_cls", [AntigravityAdapter, GrokAdapter])
    def test_no_effort_omits_flag(
        self, adapter_cls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SBX_WORK", str(tmp_path))
        _write_session(tmp_path, model=MODEL)
        adapter = adapter_cls()
        assert "--effort" not in adapter.first_turn_argv("hi", MODEL)
        assert "--effort" not in adapter.resume_argv("next", "native-id")


class TestSessionMetaEffort:
    def test_session_meta_reports_effort(self, work: Path, runner_env: dict[str, str]) -> None:
        result = run_runner(
            ["init", "--auth", "auth_json", "--model", MODEL, "--reasoning-effort", "low"],
            runner_env,
        )
        assert result.returncode == 0, result.stderr
        message = write_message(work)
        result = run_runner(["turn", "--n", "1", "--message-file", str(message)], runner_env)
        assert result.returncode == 0, result.stderr
        meta = next(
            e
            for e in (
                json.loads(line)
                for line in (work / "events.jsonl").read_text().splitlines()
                if line.strip().startswith("{")
            )
            if e.get("type") == "sbx.session_meta"
        )
        assert meta["reasoning_effort"] == "low"
