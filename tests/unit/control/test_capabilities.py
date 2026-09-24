"""SOR-204: provider capability discovery — parse, normalize, cache, service.

Covers the payload parsers (fixture files), the normalized report shape
(provider/account/model/display/family/aliases/efforts/default/availability/
source/refreshed_at/stale), TTL + credential/CLI-change staleness, the
last-good fallback, and the sandbox probe against LocalProcessBackend +
stub_runner + fake CLIs — no real credentials anywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from control.backend import LocalProcessBackend
from control.capabilities import (
    DEFAULT_TTL_S,
    AccountCapabilities,
    CapabilityService,
    DiscoveryOutcome,
    FileCapabilityStore,
    InMemoryCapabilityStore,
    SandboxCapabilityProbe,
    credential_fingerprint,
    infer_family,
    normalize_model,
    parse_models_payload,
    provider_discovery_argv,
    select_capability_store,
)
from control.ports import Account
from runtime.runner.effort import CANONICAL_EFFORTS
from tests.fakes.fake_ports import InMemoryAccountRegistry

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "capabilities"


def _account(
    provider: str = "codex",
    models: tuple[str, ...] = ("gpt-5.6-luna",),
    account_id: str = "acct-1",
) -> Account:
    return Account(
        id=account_id,
        provider=provider,
        label=account_id,
        models=models,
        created_at="2026-01-01T00:00:00+00:00",
    )


class _ScriptedProbe:
    def __init__(self, outcome: DiscoveryOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    def discover(self, account: Account, blob: dict[str, Any] | None) -> DiscoveryOutcome:
        self.calls += 1
        return self.outcome


def _service(
    registry: InMemoryAccountRegistry | None = None,
    *,
    probe: Any = None,
    store: Any = None,
    ttl_s: int = DEFAULT_TTL_S,
    cli_versions: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> CapabilityService:
    return CapabilityService(
        registry or InMemoryAccountRegistry(),
        store=store or InMemoryCapabilityStore(),
        probe=probe,
        ttl_s=ttl_s,
        cli_versions=cli_versions if cli_versions is not None else {},
        env=env if env is not None else {},
    )


class TestInferFamily:
    @pytest.mark.parametrize(
        ("model", "family"),
        [
            ("swe-2-high", "swe-2"),
            ("swe-2-max", "swe-2"),
            ("swe-1.5", "swe-1.5"),
            ("openai/gpt-5.6-luna", "openai"),
            ("opencode/muse-spark-1.3-contributor-free", "opencode"),
            ("grok-4.6-mini", "grok-4.6"),
            ("grok-4.6", "grok-4.6"),
            ("gpt-5.6-luna", "gpt-5.6-luna"),
        ],
    )
    def test_family(self, model: str, family: str) -> None:
        assert infer_family(model) == family


class TestNormalizeModel:
    def test_full_entry(self) -> None:
        entry = normalize_model(
            "codex",
            {
                "id": "gpt-5.6-luna",
                "display": "GPT-5.6 Luna",
                "family": "gpt-5.6",
                "aliases": ["gpt-5.6"],
                "reasoning_efforts": ["low", "high"],
                "default_effort": "high",
                "availability": "unavailable",
                "free": True,
            },
        )
        assert entry is not None
        assert entry.model == "gpt-5.6-luna"
        assert entry.display == "GPT-5.6 Luna"
        assert entry.family == "gpt-5.6"
        assert entry.aliases == ("gpt-5.6",)
        assert entry.reasoning_efforts == ("low", "high")
        assert entry.default_effort == "high"
        assert entry.availability == "unavailable"
        assert entry.free is True
        assert entry.matches("gpt-5.6") and entry.matches("gpt-5.6-luna")

    def test_absent_efforts_fall_back_to_provider_surface(self) -> None:
        codex = normalize_model("codex", {"id": "m"})
        devin = normalize_model("devin", {"id": "m"})
        assert codex is not None and codex.reasoning_efforts == CANONICAL_EFFORTS
        assert devin is not None and devin.reasoning_efforts == ()

    def test_explicit_efforts_intersect_canonical_in_order(self) -> None:
        entry = normalize_model(
            "codex", {"id": "m", "reasoning_efforts": ["high", "turbo", "low", "xhigh"]}
        )
        assert entry is not None
        assert entry.reasoning_efforts == ("low", "high", "xhigh")

    def test_default_effort_must_be_exposed(self) -> None:
        entry = normalize_model(
            "codex", {"id": "m", "reasoning_efforts": ["low"], "default_effort": "high"}
        )
        assert entry is not None and entry.default_effort is None

    def test_string_entry_and_empty_id(self) -> None:
        assert normalize_model("grok", "grok-4.6") is not None
        assert normalize_model("grok", {"name": ""}) is None
        assert normalize_model("grok", 42) is None


class TestParseModelsPayload:
    def test_json_document(self) -> None:
        raw, meta = parse_models_payload(
            "devin", (FIXTURES / "devin-models.json").read_text(encoding="utf-8")
        )
        assert meta["plan"] == "pro"
        assert meta["families"] == ("swe-2", "swe-1.5")
        assert {e["id"] for e in raw} == {
            "swe-2-medium",
            "swe-2-high",
            "swe-2-max",
            "swe-1.5",
        }

    def test_opencode_free_models(self) -> None:
        raw, meta = parse_models_payload(
            "opencode", (FIXTURES / "opencode-models.json").read_text(encoding="utf-8")
        )
        ids = {e["id"] for e in raw}
        assert "opencode/muse-spark-1.3-contributor-free" in ids
        assert meta["plan"] == "zen-free"

    def test_embedded_json_line_among_banner_output(self) -> None:
        raw, meta = parse_models_payload(
            "antigravity", (FIXTURES / "agy-models-mixed.txt").read_text(encoding="utf-8")
        )
        assert meta["plan"] == "team"
        assert {e["id"] for e in raw} == {"gemini-3.8-flash-low", "gemini-3.8-pro"}

    def test_plain_text_line_listing(self) -> None:
        raw, meta = parse_models_payload(
            "grok", (FIXTURES / "grok-models.txt").read_text(encoding="utf-8")
        )
        assert meta["plan"] is None
        ids = {e["id"] for e in raw}
        assert {"grok-4.6", "grok-4.7", "grok-4.6-mini"} <= ids

    def test_bare_json_list(self) -> None:
        raw, _meta = parse_models_payload("codex", '["gpt-5.6-luna", {"id": "gpt-5.3-codex"}]')
        assert "gpt-5.6-luna" in raw

    def test_empty_output(self) -> None:
        raw, meta = parse_models_payload("codex", "")
        assert raw == [] and meta["plan"] is None


class TestDiscoveryArgv:
    def test_default_tail(self) -> None:
        argv = provider_discovery_argv("grok", env={})
        assert argv == ["grok", "models"]

    def test_bin_override_and_py_interpreter(self) -> None:
        argv = provider_discovery_argv("devin", env={"DEVIN_BIN": "/fakes/fake_devin.py"})
        assert argv == [sys.executable, "/fakes/fake_devin.py", "models"]

    def test_discovery_argv_override(self) -> None:
        argv = provider_discovery_argv(
            "codex", env={"SBX_CODEX_DISCOVERY_ARGV": "models list --json"}
        )
        assert argv == ["codex", "models", "list", "--json"]

    def test_unknown_provider(self) -> None:
        assert provider_discovery_argv("claude", env={}) is None


class TestStores:
    def test_file_store_round_trip(self, tmp_path: Path) -> None:
        store = FileCapabilityStore(tmp_path / "caps")
        report = _account().id
        store.put_report(report, {"source": "cli", "models": []})
        assert store.get_report(report) == {"source": "cli", "models": []}
        assert dict(store.iter_reports())[report] == {"source": "cli", "models": []}
        store.delete_report(report)
        assert store.get_report(report) is None

    def test_file_store_rejects_bad_id(self, tmp_path: Path) -> None:
        store = FileCapabilityStore(tmp_path / "caps")
        with pytest.raises(ValueError):
            store.put_report("../escape", {})

    def test_inmemory_store(self) -> None:
        store = InMemoryCapabilityStore()
        store.put_report("a", {"x": 1})
        assert store.get_report("a") == {"x": 1}
        store.delete_report("a")
        assert store.get_report("a") is None

    def test_select_local_file_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SBX_BACKEND", "local")
        monkeypatch.setenv("SBX_CAPABILITY_STORE_DIR", str(tmp_path / "caps"))
        store = select_capability_store()
        assert isinstance(store, FileCapabilityStore)
        assert store._root == tmp_path / "caps"


class TestCredentialFingerprint:
    def test_stable_and_order_independent(self) -> None:
        a = credential_fingerprint({"files": {"x": "1", "y": "2"}, "provider": "grok"})
        b = credential_fingerprint({"provider": "grok", "files": {"y": "2", "x": "1"}})
        assert a == b and a is not None and len(a) == 16

    def test_none_for_empty(self) -> None:
        assert credential_fingerprint(None) is None
        assert credential_fingerprint({}) is None


class TestServiceReports:
    def test_declared_fallback_when_no_report(self) -> None:
        account = _account("grok", models=("grok-4.6",))
        svc = _service()
        report = svc.report(account)
        assert report is not None
        assert report.source == "declared"
        assert [m.model for m in report.models] == ["grok-4.6"]

    def test_refresh_persists_cli_report(self) -> None:
        entry = normalize_model("devin", {"id": "swe-2-max"})
        assert entry is not None
        outcome = DiscoveryOutcome(ok=True, plan="pro", families=("swe-2",), models=(entry,))
        registry = InMemoryAccountRegistry()
        account = _account("devin", models=("swe-2-high",), account_id="acct-d1")
        registry.put(account)
        registry.put_credential_blob(account.id, {"provider": "devin", "files": {"x": "y"}})
        store = InMemoryCapabilityStore()
        svc = _service(registry, probe=_ScriptedProbe(outcome), store=store)
        report = svc.refresh(account)
        assert report is not None
        assert report.source == "cli"
        assert report.plan == "pro"
        assert [m.model for m in report.models] == ["swe-2-max"]
        assert report.credential_fingerprint is not None
        assert store.get_report(account.id)["source"] == "cli"

    def test_failed_refresh_keeps_last_good_with_error(self) -> None:
        registry = InMemoryAccountRegistry()
        account = _account("grok", models=("grok-4.6",), account_id="acct-g1")
        registry.put(account)
        good = _ScriptedProbe(
            DiscoveryOutcome(ok=True, models=(normalize_model("grok", {"id": "grok-4.7"}),))
        )
        svc = _service(registry, probe=good)
        svc.refresh(account)
        svc._probe = _ScriptedProbe(DiscoveryOutcome(ok=False, error="discovery_failed"))
        report = svc.refresh(account)
        assert report is not None
        assert report.stale is True
        assert report.error == "discovery_failed"
        assert [m.model for m in report.models] == ["grok-4.7"]  # last good survives

    def test_ttl_staleness(self) -> None:
        registry = InMemoryAccountRegistry()
        account = _account()
        registry.put(account)
        svc = _service(registry, ttl_s=0)
        svc._store.put_report(
            account.id,
            AccountCapabilities(
                account_id=account.id,
                provider=account.provider,
                source="cli",
                refreshed_at="2020-01-01T00:00:00+00:00",
                stale=False,
                models=(normalize_model("codex", "gpt-5.6-luna"),),
            ).to_dict(),
        )
        report = svc.report(account)
        assert report is not None and report.stale is True

    def test_credential_change_invalidates(self) -> None:
        registry = InMemoryAccountRegistry()
        account = _account("grok", account_id="acct-g2")
        registry.put(account)
        registry.put_credential_blob(account.id, {"provider": "grok", "files": {"a": "1"}})
        svc = _service(registry)
        svc._store.put_report(
            account.id,
            AccountCapabilities(
                account_id=account.id,
                provider="grok",
                source="cli",
                refreshed_at="2999-01-01T00:00:00+00:00",
                stale=False,
                credential_fingerprint=credential_fingerprint(
                    {"provider": "grok", "files": {"a": "1"}}
                ),
            ).to_dict(),
        )
        report = svc.report(account)
        assert report is not None and report.stale is False
        registry.put_credential_blob(account.id, {"provider": "grok", "files": {"a": "2"}})
        report = svc.report(account)
        assert report is not None and report.stale is True

    def test_cli_version_change_invalidates(self) -> None:
        registry = InMemoryAccountRegistry()
        account = _account("codex", account_id="acct-c9")
        registry.put(account)
        svc = _service(registry, cli_versions={"codex": "2.0"})
        svc._store.put_report(
            account.id,
            AccountCapabilities(
                account_id=account.id,
                provider="codex",
                source="cli",
                refreshed_at="2999-01-01T00:00:00+00:00",
                stale=False,
                cli_version="1.0",
            ).to_dict(),
        )
        report = svc.report(account)
        assert report is not None and report.stale is True

    def test_no_probe_marks_declared_with_error(self) -> None:
        account = _account()
        svc = _service(probe=None)
        report = svc.refresh(account)
        assert report is not None
        assert report.source == "declared"
        assert report.error == "probe_unavailable"

    def test_invalidate_drops_stored_report(self) -> None:
        registry = InMemoryAccountRegistry()
        account = _account("grok", models=("grok-4.6",))
        registry.put(account)
        svc = _service(
            registry,
            probe=_ScriptedProbe(
                DiscoveryOutcome(ok=True, models=(normalize_model("grok", {"id": "grok-4.7"}),))
            ),
        )
        svc.refresh(account)
        svc.invalidate(account.id)
        report = svc.report(account)
        assert report is not None and report.source == "declared"


class TestResolution:
    def _svc_with_catalog(self) -> tuple[CapabilityService, Account]:
        registry = InMemoryAccountRegistry()
        account = _account("codex", models=("gpt-5.6-luna",), account_id="acct-m")
        registry.put(account)
        store = InMemoryCapabilityStore()
        store.put_report(
            account.id,
            AccountCapabilities(
                account_id=account.id,
                provider="codex",
                source="cli",
                refreshed_at="2999-01-01T00:00:00+00:00",
                stale=False,
                models=(
                    normalize_model(
                        "codex",
                        {
                            "id": "gpt-5.6-luna",
                            "aliases": ["luna"],
                            "reasoning_efforts": ["low", "high"],
                            "default_effort": "low",
                        },
                    ),
                    normalize_model("codex", {"id": "gpt-5.3-codex"}),
                    normalize_model("codex", {"id": "gpt-4-old", "availability": "unavailable"}),
                ),
            ).to_dict(),
        )
        return _service(registry, store=store), account

    def test_explicit_model_normalizes(self) -> None:
        svc, account = self._svc_with_catalog()
        model, refusal = svc.resolve_model(account, "gpt-5.3-codex")
        assert refusal is None and model == "gpt-5.3-codex"

    def test_alias_resolves_to_canonical(self) -> None:
        svc, account = self._svc_with_catalog()
        model, refusal = svc.resolve_model(account, "luna")
        assert refusal is None and model == "gpt-5.6-luna"

    def test_unknown_model_refused(self) -> None:
        svc, account = self._svc_with_catalog()
        model, refusal = svc.resolve_model(account, "gpt-9")
        assert model is None and "gpt-9" in (refusal or "")

    def test_unavailable_model_refused(self) -> None:
        svc, account = self._svc_with_catalog()
        model, refusal = svc.resolve_model(account, "gpt-4-old")
        assert model is None and "unavailable" in (refusal or "")

    def test_default_is_first_available(self) -> None:
        svc, account = self._svc_with_catalog()
        model, refusal = svc.resolve_model(account, None)
        assert refusal is None and model == "gpt-5.6-luna"

    def test_empty_catalog_passthrough(self) -> None:
        account = _account("codex", models=())
        svc = _service()
        assert svc.resolve_model(account, "anything") == ("anything", None)

    def test_effort_gate_per_model(self) -> None:
        svc, account = self._svc_with_catalog()
        assert svc.effort_refusal("codex", account, "high", "gpt-5.6-luna") is None
        refusal = svc.effort_refusal("codex", account, "medium", "gpt-5.6-luna")
        assert refusal is not None and "medium" in refusal

    def test_effort_none_never_refused(self) -> None:
        svc, account = self._svc_with_catalog()
        assert svc.effort_refusal("codex", account, None, "gpt-5.6-luna") is None
        assert svc.effort_refusal("devin", None, "high", None) is not None


class TestSandboxCapabilityProbe:
    """End-to-end: runner init restores the blob, the fake provider CLI
    enumerates the account's catalog — the CLI's answer is the truth."""

    def _service(
        self, registry: InMemoryAccountRegistry, stub_runner: Path, bin_env: dict[str, str]
    ) -> CapabilityService:
        backend = LocalProcessBackend()
        probe = SandboxCapabilityProbe(backend, [sys.executable, str(stub_runner)], bin_env=bin_env)
        return _service(registry, probe=probe)

    def test_grok_catalog(self, tmp_path: Path, stub_runner: Path, repo_root: Path) -> None:
        registry = InMemoryAccountRegistry()
        account = _account("grok", models=("grok-4.6",), account_id="acct-grok-1")
        registry.put(account)
        registry.put_credential_blob(
            account.id, {"provider": "grok", "files": {".grok/auth.json": '{"t":"x"}'}}
        )
        svc = self._service(
            registry, stub_runner, {"GROK_BIN": str(repo_root / "tests" / "fakes" / "fake_grok.py")}
        )
        report = svc.refresh(account)
        assert report is not None
        assert report.source == "cli"
        assert report.error is None
        assert {m.model for m in report.models} == {"grok-4.6", "grok-4.7"}

    def test_devin_tiers_and_families(
        self, tmp_path: Path, stub_runner: Path, repo_root: Path
    ) -> None:
        registry = InMemoryAccountRegistry()
        account = _account("devin", models=("swe-2-high",), account_id="acct-devin-1")
        registry.put(account)
        registry.put_credential_blob(
            account.id,
            {
                "provider": "devin",
                "files": {".local/share/devin/credentials.toml": 'token="x"'},
            },
        )
        svc = self._service(
            registry,
            stub_runner,
            {"DEVIN_BIN": str(repo_root / "tests" / "fakes" / "fake_devin.py")},
        )
        report = svc.refresh(account)
        assert report is not None and report.source == "cli"
        assert report.plan == "pro"
        assert set(report.families) == {"swe-2", "swe-1.5"}
        assert {m.model for m in report.models} >= {"swe-2-medium", "swe-2-high", "swe-2-max"}

    def test_opencode_zen_free_models(
        self, tmp_path: Path, stub_runner: Path, repo_root: Path
    ) -> None:
        registry = InMemoryAccountRegistry()
        account = _account("opencode", models=(), account_id="acct-oc-1")
        registry.put(account)
        registry.put_credential_blob(
            account.id,
            {"provider": "opencode", "files": {".local/share/opencode/auth.json": "{}"}},
        )
        svc = self._service(
            registry,
            stub_runner,
            {"OPENCODE_BIN": str(repo_root / "tests" / "fakes" / "fake_opencode.py")},
        )
        report = svc.refresh(account)
        assert report is not None and report.source == "cli"
        ids = {m.model for m in report.models}
        assert "opencode/muse-spark-1.3-contributor-free" in ids
        free = {m.model for m in report.models if m.free}
        assert "opencode/muse-spark-1.3-contributor-free" in free

    def test_models_json_override(
        self, tmp_path: Path, stub_runner: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "FAKE_GROK_MODELS_JSON",
            json.dumps({"models": [{"id": "grok-9-experimental"}]}),
        )
        registry = InMemoryAccountRegistry()
        account = _account("grok", models=("grok-4.6",), account_id="acct-grok-9")
        registry.put(account)
        registry.put_credential_blob(
            account.id, {"provider": "grok", "files": {".grok/auth.json": '{"t":"x"}'}}
        )
        svc = self._service(
            registry, stub_runner, {"GROK_BIN": str(repo_root / "tests" / "fakes" / "fake_grok.py")}
        )
        report = svc.refresh(account)
        assert report is not None
        assert {m.model for m in report.models} == {"grok-9-experimental"}

    def test_no_credential_is_no_credential(self, stub_runner: Path) -> None:
        registry = InMemoryAccountRegistry()
        account = _account("grok", models=("grok-4.6",), account_id="acct-naked")
        registry.put(account)
        svc = self._service(registry, stub_runner, {})
        report = svc.refresh(account)
        assert report is not None
        assert report.stale is True
        assert report.error == "no_credential"
        # falls back to the declared catalog
        assert [m.model for m in report.models] == ["grok-4.6"]

    def test_logged_out_cli_maps_auth_invalid(
        self, tmp_path: Path, stub_runner: Path, repo_root: Path
    ) -> None:
        """Blob restores but the CLI rejects the credential → auth_invalid,
        last-good report kept with the error recorded."""
        registry = InMemoryAccountRegistry()
        account = _account("grok", models=("grok-4.6",), account_id="acct-dead")
        registry.put(account)
        registry.put_credential_blob(
            account.id, {"provider": "grok", "files": {".grok/auth.json": '{"t":"x"}'}}
        )
        # A CLI that always rejects: rc != 0 with a fail marker →
        # auth_invalid rather than a generic discovery error.
        reject = f"{sys.executable} -c \"import sys; print('Not logged in'); sys.exit(1)\""
        svc = self._service(registry, stub_runner, {"GROK_BIN": reject})
        report = svc.refresh(account)
        assert report is not None
        assert report.stale is True
        assert report.error == "auth_invalid"
        # The account's declared catalog remains the last-good truth.
        assert [m.model for m in report.models] == ["grok-4.6"]
