"""Agent endpoints: create/list/get/delete, scheduler errors, filters, usage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from control.devin_pool import DevinAccountPool
from tests.unit.api_v1.conftest import create_agent, seed_account, wait_run, wait_sandbox


class TestCreateAgent:
    def test_create_runs_first_turn(self, client, auth) -> None:
        body = create_agent(client, auth, name="demo")
        agent, run = body["agent"], body["run"]
        assert agent["name"] == "demo"
        assert agent["provider"] == "codex"
        assert agent["account_id"] == "acct-codex-1"  # scheduler pick, not "auto"
        assert agent["status"] in ("creating", "running", "idle")
        assert agent["model"] == "gpt-5.6-luna"
        assert run["id"] == "run-1"
        assert run["agent_id"] == agent["id"]
        # SOR-82 A2: the first run is queued CREATING; the background worker
        # provisions and dispatches it.
        assert run["status"] in ("CREATING", "RUNNING", "FINISHED")
        assert wait_run(client, auth, agent["id"], "run-1")["status"] == "FINISHED"

    def test_named_account_is_used(self, client, auth, v1_env) -> None:
        seed_account(v1_env, "acct-codex-2", max_concurrent=5)
        body = create_agent(client, auth, agent={"provider": "codex", "account_id": "acct-codex-2"})
        assert body["agent"]["account_id"] == "acct-codex-2"
        assert v1_env.registry.get("acct-codex-2").last_used_at is not None

    def test_provider_devin_accepted(self, client, auth, v1_env) -> None:
        seed_account(v1_env, "acct-devin-1", provider="devin", models=("swe-2-high",))
        body = create_agent(client, auth, agent={"provider": "devin"})
        assert body["agent"]["provider"] == "devin"
        assert body["agent"]["account_id"] == "acct-devin-1"

    def test_provider_devin_pool_wires_sandbox_and_releases_lease(
        self, client, auth, v1_env
    ) -> None:
        seed_account(
            v1_env,
            "acct-devin-1",
            provider="devin",
            max_concurrent=8,
            models=("swe-2-high",),
            secret_name="sbx-acct-devin-1",
        )
        pool = DevinAccountPool(v1_env.registry, account_id="acct-devin-1")
        v1_env.app.state.scheduler = pool
        body = create_agent(
            client,
            auth,
            agent={
                "provider": "devin",
                "account_id": "auto",
                "model": "swe-2-high",
            },
        )
        agent_id = body["agent"]["id"]
        assert pool.active_count == 1
        rec = wait_sandbox(v1_env, agent_id)
        assert rec.sandbox_tags["provider"] == "devin"
        assert rec.sandbox_tags["account_id"] == "acct-devin-1"
        session = json.loads((Path(rec.sandbox_root) / "session.json").read_text())
        assert session["provider"] == "devin"
        assert session["account_id"] == "acct-devin-1"
        deleted = client.delete(f"/v1/agents/{agent_id}", headers=auth)
        assert deleted.status_code == 200
        assert pool.active_count == 0

    def test_devin_pool_hard_cap_and_delete_release(self, client, auth, v1_env) -> None:
        seed_account(
            v1_env,
            "acct-devin-1",
            provider="devin",
            max_concurrent=8,
            secret_name="sbx-acct-devin-1",
        )
        pool = DevinAccountPool(v1_env.registry, account_id="acct-devin-1")
        v1_env.app.state.scheduler = pool
        v1_env.app.state.plane.max_concurrent = 8
        agent_ids = []
        for _ in range(8):
            body = create_agent(client, auth, agent={"provider": "devin"})
            agent_ids.append(body["agent"]["id"])
        assert pool.active_count == 8
        refused = client.post(
            "/v1/agents",
            json={"prompt": {"text": "ninth"}, "agent": {"provider": "devin"}},
            headers=auth,
        )
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "provider_exhausted"
        for agent_id in agent_ids:
            assert client.delete(f"/v1/agents/{agent_id}", headers=auth).status_code == 200
        assert pool.active_count == 0

    def test_provider_devin_without_account_exhausts(self, client, auth) -> None:
        resp = client.post(
            "/v1/agents",
            json={"prompt": {"text": "hi"}, "agent": {"provider": "devin"}},
            headers=auth,
        )
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "provider_exhausted"
        assert resp.json()["error"]["retry_after"] > 0

    def test_unknown_provider_is_400(self, client, auth) -> None:
        resp = client.post(
            "/v1/agents",
            json={"prompt": {"text": "hi"}, "agent": {"provider": "bogus"}},
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_provider"

    def test_malformed_body_is_400(self, client, auth) -> None:
        resp = client.post("/v1/agents", json={"agent": {"provider": "codex"}}, headers=auth)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_provider"

    def test_named_account_busy_is_409(self, client, auth, v1_env) -> None:
        v1_env.registry.set_running("acct-codex-1", 1)  # max_concurrent == 1
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "hi"},
                "agent": {"provider": "codex", "account_id": "acct-codex-1"},
            },
            headers=auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "account_busy"

    def test_named_account_unavailable_is_409(self, client, auth, v1_env) -> None:
        v1_env.registry.mark_status("acct-codex-1", "disabled")
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "hi"},
                "agent": {"provider": "codex", "account_id": "acct-codex-1"},
            },
            headers=auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "account_unavailable"
        resp = client.post(
            "/v1/agents",
            json={
                "prompt": {"text": "hi"},
                "agent": {"provider": "codex", "account_id": "acct-missing"},
            },
            headers=auth,
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "account_unavailable"

    def test_concurrency_limit_is_429(self, client, auth, v1_env) -> None:
        v1_env.app.state.plane.max_concurrent = 1
        create_agent(client, auth)
        resp = client.post(
            "/v1/agents",
            json={"prompt": {"text": "again"}, "agent": {"provider": "codex"}},
            headers=auth,
        )
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "concurrency_limit"


class TestModelDefaults:
    """Omitted ``AgentSpec.model`` resolves per provider/account; explicit wins."""

    @pytest.mark.parametrize(
        ("provider", "account_id", "models", "expected"),
        [
            ("codex", "acct-codex-1", None, "gpt-5.6-luna"),  # conftest seed
            ("devin", "acct-devin-1", ("swe-2-high", "swe-2-medium"), "swe-2-high"),
            ("antigravity", "acct-agy-1", ("gemini-3.8-flash-low",), "gemini-3.8-flash-low"),
            ("grok", "acct-grok-1", ("grok-4.6",), "grok-4.6"),
        ],
    )
    def test_omitted_model_uses_account_default(
        self, client, auth, v1_env, provider, account_id, models, expected
    ) -> None:
        if models is not None:
            seed_account(v1_env, account_id, provider=provider, models=models)
        body = create_agent(client, auth, agent={"provider": provider})
        agent = body["agent"]
        assert agent["model"] == expected
        rec = wait_sandbox(v1_env, agent["id"])
        session = json.loads((Path(rec.sandbox_root) / "session.json").read_text())
        assert session["model"] == expected

    @pytest.mark.parametrize(
        ("provider", "account_id", "models", "explicit"),
        [
            ("codex", "acct-codex-1", None, "gpt-5.3-codex"),
            ("devin", "acct-devin-1", ("swe-2-high", "swe-2-medium"), "swe-2-medium"),
            (
                "antigravity",
                "acct-agy-1",
                ("gemini-3.8-flash-low", "gemini-3.8-pro"),
                "gemini-3.8-pro",
            ),
            ("grok", "acct-grok-1", ("grok-4.6", "grok-4.7"), "grok-4.7"),
        ],
    )
    def test_explicit_model_is_preserved(
        self, client, auth, v1_env, provider, account_id, models, explicit
    ) -> None:
        if models is not None:
            seed_account(v1_env, account_id, provider=provider, models=models)
        body = create_agent(client, auth, agent={"provider": provider, "model": explicit})
        agent = body["agent"]
        assert agent["model"] == explicit
        rec = wait_sandbox(v1_env, agent["id"])
        session = json.loads((Path(rec.sandbox_root) / "session.json").read_text())
        assert session["model"] == explicit

    def test_omitted_model_falls_back_to_provider_default(self, client, auth, v1_env) -> None:
        # Accounts that advertise no models get the provider seed default;
        # codex keeps the gpt-5.6-luna backward-compatible default.
        seed_account(v1_env, "acct-devin-bare", provider="devin")
        seed_account(v1_env, "acct-codex-bare", provider="codex")
        devin = create_agent(
            client, auth, agent={"provider": "devin", "account_id": "acct-devin-bare"}
        )
        codex = create_agent(
            client, auth, agent={"provider": "codex", "account_id": "acct-codex-bare"}
        )
        assert devin["agent"]["model"] == "swe-2-high"
        assert codex["agent"]["model"] == "gpt-5.6-luna"


class TestAgentReadDelete:
    def test_get_and_delete(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        got = client.get(f"/v1/agents/{agent['id']}", headers=auth)
        assert got.status_code == 200
        assert got.json()["id"] == agent["id"]
        deleted = client.delete(f"/v1/agents/{agent['id']}", headers=auth)
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "closed"
        # history stays readable after close
        got = client.get(f"/v1/agents/{agent['id']}", headers=auth)
        assert got.status_code == 200
        assert got.json()["status"] == "closed"

    def test_get_missing_is_404(self, client, auth) -> None:
        resp = client.get("/v1/agents/nope", headers=auth)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_delete_missing_is_404(self, client, auth) -> None:
        resp = client.delete("/v1/agents/nope", headers=auth)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_usage_shape(self, client, auth) -> None:
        agent = create_agent(client, auth)["agent"]
        wait_run(client, auth, agent["id"], "run-1")
        resp = client.get(f"/v1/agents/{agent['id']}/usage", headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"usage", "cost_estimate_usd", "sandbox_seconds"}
        usage = body["usage"]
        assert usage["input_tokens"] >= 0
        assert usage["cached_input_tokens"] >= 0
        assert usage["output_tokens"] >= 0
        assert body["cost_estimate_usd"] >= 0
        assert body["sandbox_seconds"] >= 0

    def test_usage_missing_agent_is_404(self, client, auth) -> None:
        resp = client.get("/v1/agents/nope/usage", headers=auth)
        assert resp.status_code == 404


class TestListAgents:
    def test_list_and_filters(self, client, auth, v1_env) -> None:
        seed_account(v1_env, "acct-devin-1", provider="devin")
        a1 = create_agent(client, auth, name="one")["agent"]
        a2 = create_agent(client, auth, name="two", agent={"provider": "devin"})["agent"]

        all_agents = client.get("/v1/agents", headers=auth).json()
        ids = {a["id"] for a in all_agents["agents"]}
        assert {a1["id"], a2["id"]} <= ids
        assert "next_cursor" in all_agents

        by_provider = client.get("/v1/agents?provider=devin", headers=auth).json()
        assert [a["id"] for a in by_provider["agents"]] == [a2["id"]]

        by_account = client.get("/v1/agents?account_id=acct-devin-1", headers=auth).json()
        assert [a["id"] for a in by_account["agents"]] == [a2["id"]]

        by_status = client.get("/v1/agents?status=closed", headers=auth).json()
        assert by_status["agents"] == []
        client.delete(f"/v1/agents/{a1['id']}", headers=auth)
        by_status = client.get("/v1/agents?status=closed", headers=auth).json()
        assert [a["id"] for a in by_status["agents"]] == [a1["id"]]

    def test_bad_provider_filter_is_400(self, client, auth) -> None:
        resp = client.get("/v1/agents?provider=bogus", headers=auth)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_provider"

    def test_cursor(self, client, auth) -> None:
        create_agent(client, auth)
        resp = client.get("/v1/agents?cursor=0", headers=auth)
        assert resp.status_code == 200
        resp = client.get("/v1/agents?cursor=zzz", headers=auth)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_provider"
