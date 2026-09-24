"""SOR-204: ``/v1/capabilities`` routes + catalog-driven agent validation.

The catalog is the capability truth — seeded here through injected
``CapabilityService`` doubles (scripted probes) and one end-to-end refresh
that exercises the real sandbox probe against the fake provider CLIs.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from control.capabilities import (
    CapabilityService,
    DiscoveryOutcome,
    InMemoryCapabilityStore,
    SandboxCapabilityProbe,
    cli_versions_from_env,
    normalize_model,
)
from tests.unit.api_v1.conftest import create_agent, seed_account


@pytest.fixture(autouse=True)
def _all_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBX_PROVIDERS", "codex,antigravity,grok,opencode,devin")


class _ScriptedProbe:
    def __init__(self, outcome: DiscoveryOutcome) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def discover(self, account: Any, blob: Any) -> DiscoveryOutcome:
        self.calls.append(account.id)
        return self.outcome


def _caps_service(v1_env, outcome: DiscoveryOutcome) -> CapabilityService:
    svc = CapabilityService(
        v1_env.registry,
        store=InMemoryCapabilityStore(),
        probe=_ScriptedProbe(outcome),
        env={},
    )
    v1_env.app.state.capabilities = svc
    return svc


def _account_row(payload: dict[str, Any], provider: str, account_id: str) -> dict[str, Any]:
    for entry in payload["providers"]:
        if entry["provider"] != provider:
            continue
        for account in entry["accounts"]:
            if account["id"] == account_id:
                return account
    raise AssertionError(f"{provider}/{account_id} not in capabilities payload")


def _grok_outcome() -> DiscoveryOutcome:
    entries = [
        normalize_model(
            "grok",
            {
                "id": "grok-4.7",
                "display": "Grok 4.7",
                "family": "grok-4",
                "aliases": ["grok-latest"],
                "reasoning_efforts": ["low", "medium", "high"],
                "default_effort": "medium",
            },
        ),
        normalize_model("grok", {"id": "grok-4.6-mini", "availability": "unavailable"}),
    ]
    return DiscoveryOutcome(
        ok=True, plan="supergrok", families=("grok-4",), models=tuple(e for e in entries if e)
    )


class TestCapabilityRoutes:
    def test_get_reports_declared_fallback(self, client, auth, v1_env) -> None:
        seed_account(v1_env, "acct-grok-1", provider="grok", models=("grok-4.6",))
        resp = client.get("/v1/capabilities", headers=auth)
        assert resp.status_code == 200
        grok = _account_row(resp.json(), "grok", "acct-grok-1")
        assert grok["source"] == "declared"
        assert [m["model"] for m in grok["models"]] == ["grok-4.6"]

    def test_refresh_requires_admin(self, client, auth, v1_env) -> None:
        seed_account(v1_env, "acct-grok-1", provider="grok")
        resp = client.post("/v1/capabilities/refresh", headers=auth)
        assert resp.status_code == 403

    def test_admin_refresh_populates_catalog(self, client, admin_auth, v1_env) -> None:
        seed_account(v1_env, "acct-grok-1", provider="grok", models=("grok-4.6",), secret_name="s")
        _caps_service(v1_env, _grok_outcome())
        resp = client.post("/v1/capabilities/refresh", headers=admin_auth)
        assert resp.status_code == 200
        account = _account_row(resp.json(), "grok", "acct-grok-1")
        assert account["source"] == "cli"
        assert account["plan"] == "supergrok"
        assert account["stale"] is False
        models = {m["model"]: m for m in account["models"]}
        assert models["grok-4.7"]["display"] == "Grok 4.7"
        assert models["grok-4.7"]["aliases"] == ["grok-latest"]
        assert models["grok-4.7"]["reasoning_efforts"] == ["low", "medium", "high"]
        assert models["grok-4.7"]["default_effort"] == "medium"
        assert models["grok-4.6-mini"]["availability"] == "unavailable"

    def test_account_refresh_endpoint(self, client, admin_auth, v1_env) -> None:
        seed_account(v1_env, "acct-grok-1", provider="grok", secret_name="s")
        _caps_service(v1_env, _grok_outcome())
        resp = client.post("/v1/accounts/acct-grok-1/capabilities/refresh", headers=admin_auth)
        assert resp.status_code == 200
        caps = resp.json()["capabilities"]
        assert caps["source"] == "cli"
        assert caps["stale"] is False

    def test_account_refresh_404(self, client, admin_auth, v1_env) -> None:
        resp = client.post("/v1/accounts/acct-missing/capabilities/refresh", headers=admin_auth)
        assert resp.status_code == 404

    def test_delete_account_invalidates_report(self, client, admin_auth, v1_env) -> None:
        seed_account(v1_env, "acct-grok-1", provider="grok", models=("grok-4.6",), secret_name="s")
        svc = _caps_service(v1_env, _grok_outcome())
        client.post("/v1/accounts/acct-grok-1/capabilities/refresh", headers=admin_auth)
        assert svc._store.get_report("acct-grok-1") is not None
        resp = client.delete("/v1/accounts/acct-grok-1", headers=admin_auth)
        assert resp.status_code == 204
        assert svc._store.get_report("acct-grok-1") is None

    def test_models_route_carries_catalog_shape(self, client, auth, admin_auth, v1_env) -> None:
        seed_account(v1_env, "acct-grok-1", provider="grok", models=("grok-4.6",))
        _caps_service(v1_env, _grok_outcome())
        client.post("/v1/capabilities/refresh", headers=admin_auth)
        resp = client.get("/v1/models", headers=auth)
        assert resp.status_code == 200
        rows = {m["model"]: m for m in resp.json()["models"] if m["provider"] == "grok"}
        assert rows["grok-4.7"]["display"] == "Grok 4.7"
        assert rows["grok-4.7"]["family"] == "grok-4"
        assert rows["grok-4.7"]["source"] == "cli"
        assert rows["grok-4.7"]["stale"] is False
        assert rows["grok-4.7"]["accounts_available"] == 1
        assert rows["grok-4.6-mini"]["accounts_available"] == 0


class TestCatalogDrivenValidation:
    def _seed(self, v1_env) -> CapabilityService:
        seed_account(v1_env, "acct-grok-1", provider="grok", models=("grok-4.6",), secret_name="s")
        return _caps_service(v1_env, _grok_outcome())

    def test_explicit_unknown_model_is_machine_refusal(self, client, auth, v1_env) -> None:
        svc = self._seed(v1_env)
        svc.refresh(v1_env.registry.get("acct-grok-1"))
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "hi"},
                "agent": {"provider": "grok", "account_id": "acct-grok-1", "model": "grok-9"},
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "unsupported"

    def test_alias_resolves_to_canonical_model(self, client, auth, v1_env) -> None:
        svc = self._seed(v1_env)
        svc.refresh(v1_env.registry.get("acct-grok-1"))
        body = create_agent(
            client,
            auth,
            agent={"provider": "grok", "account_id": "acct-grok-1", "model": "grok-latest"},
        )
        assert body["agent"]["model"] == "grok-4.7"

    def test_unexposed_effort_is_refused(self, client, auth, v1_env) -> None:
        svc = self._seed(v1_env)
        svc.refresh(v1_env.registry.get("acct-grok-1"))
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "hi"},
                "agent": {
                    "provider": "grok",
                    "account_id": "acct-grok-1",
                    "model": "grok-4.7",
                    "reasoning_effort": "xhigh",
                },
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "unsupported"

    def test_exposed_effort_accepted(self, client, auth, v1_env) -> None:
        svc = self._seed(v1_env)
        svc.refresh(v1_env.registry.get("acct-grok-1"))
        body = create_agent(
            client,
            auth,
            agent={
                "provider": "grok",
                "account_id": "acct-grok-1",
                "model": "grok-4.7",
                "reasoning_effort": "high",
            },
        )
        assert body["agent"]["reasoning_effort"] == "high"

    def test_default_model_comes_from_catalog(self, client, auth, v1_env) -> None:
        svc = self._seed(v1_env)
        svc.refresh(v1_env.registry.get("acct-grok-1"))
        body = create_agent(client, auth, agent={"provider": "grok", "account_id": "acct-grok-1"})
        # first available catalog entry, not the seed default
        assert body["agent"]["model"] == "grok-4.7"


class TestEndToEndDiscovery:
    def test_real_probe_through_api(
        self, client, admin_auth, v1_env, repo_root, stub_runner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Full path: registry blob → sandbox init → fake grok CLI → catalog."""
        monkeypatch.setenv("GROK_BIN", str(repo_root / "tests" / "fakes" / "fake_grok.py"))
        seed_account(v1_env, "acct-grok-1", provider="grok", models=("grok-4.6",), secret_name="s")
        v1_env.registry.put_credential_blob(
            "acct-grok-1", {"provider": "grok", "files": {".grok/auth.json": '{"t":"x"}'}}
        )
        probe = SandboxCapabilityProbe(
            v1_env.backend,
            v1_env.app.state.plane.runner_cmd,
            bin_env=os.environ,
        )
        v1_env.app.state.capabilities = CapabilityService(
            v1_env.registry,
            store=InMemoryCapabilityStore(),
            probe=probe,
            cli_versions=cli_versions_from_env(),
            env=dict(os.environ),
        )
        resp = client.post("/v1/accounts/acct-grok-1/capabilities/refresh", headers=admin_auth)
        assert resp.status_code == 200
        caps = resp.json()["capabilities"]
        assert caps["source"] == "cli"
        assert caps["stale"] is False
        assert {m["model"] for m in caps["models"]} == {"grok-4.6", "grok-4.7"}
