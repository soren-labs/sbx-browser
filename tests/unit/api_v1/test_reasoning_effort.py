"""``POST /v1/agents`` canonical ``reasoning_effort`` (SOR-179).

A declared canonical effort is durable agent metadata: it rides the init
argv into ``session.json``, echoes on the agent view and every run, and is
inherited by follow-up turns (provider resume included). A
provider/effort combination with no native surface is a machine-readable
``unsupported``, never silently ignored; ``GET /v1/models`` reports the
levels each provider honors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from runtime.runner.effort import CANONICAL_EFFORTS
from tests.unit.api_v1.conftest import create_agent, seed_account, wait_run, wait_sandbox
from tests.unit.api_v1.test_verify import RecordingBackend, _argv_opt

PROVIDERS_ALL = ("codex", "antigravity", "grok", "opencode", "devin")
PROVIDERS_EFFORT = ("codex", "antigravity", "grok")
PROVIDERS_NO_EFFORT = ("opencode", "devin")


@pytest.fixture
def spy(v1_env) -> RecordingBackend:
    backend = RecordingBackend(v1_env.backend)
    v1_env.app.state.plane.backend = backend
    return backend


def _post(client, auth, **overrides):
    body = {"prompt": {"text": "hi"}, "agent": {"provider": "codex"}}
    body.update(overrides)
    return client.post("/v1/agents", json=body, headers=auth)


def _init_exec(spy: RecordingBackend):
    for argv, env in spy.execs:
        if "init" in argv:
            return argv, env
    raise AssertionError("runner init was never exec'd")


class TestReasoningEffortValidation:
    @pytest.mark.parametrize("level", list(CANONICAL_EFFORTS))
    def test_canonical_levels_accepted(self, client, auth, spy, level) -> None:
        body = create_agent(client, auth, agent={"provider": "codex", "reasoning_effort": level})
        assert body["agent"]["reasoning_effort"] == level

    @pytest.mark.parametrize("provider", PROVIDERS_NO_EFFORT)
    def test_unsupported_provider_is_machine_refusal(
        self, client, auth, v1_env, spy, provider
    ) -> None:
        seed_account(v1_env, f"acct-{provider}-1", provider=provider)
        resp = _post(
            client,
            auth,
            agent={"provider": provider, "reasoning_effort": "high"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "unsupported"
        # Validation precedes provisioning — no sandbox was created.
        assert spy.specs == []

    def test_non_canonical_level_is_400(self, client, auth, spy) -> None:
        resp = _post(client, auth, agent={"provider": "codex", "reasoning_effort": "turbo"})
        assert resp.status_code == 400
        assert spy.specs == []

    def test_omitted_effort_unchanged(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth)
        assert body["agent"]["reasoning_effort"] is None
        rec = wait_sandbox(v1_env, body["agent"]["id"])
        assert rec.reasoning_effort is None
        assert "reasoning_effort" not in (rec.sandbox_tags or {})
        argv, _env = _init_exec(spy)
        assert "--reasoning-effort" not in argv


class TestReasoningEffortDurability:
    def test_effort_reaches_init_argv_session_and_tags(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth, agent={"provider": "codex", "reasoning_effort": "high"})
        agent_id = body["agent"]["id"]
        rec = wait_sandbox(v1_env, agent_id)
        assert rec.status in ("idle", "running")
        assert rec.reasoning_effort == "high"
        assert rec.sandbox_tags["reasoning_effort"] == "high"

        argv, _env = _init_exec(spy)
        assert _argv_opt(argv, "--reasoning-effort") == "high"
        session = json.loads((Path(rec.sandbox_root) / "session.json").read_text())
        assert session["reasoning_effort"] == "high"

    def test_agent_get_echoes_effort(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth, agent={"provider": "codex", "reasoning_effort": "low"})
        got = client.get(f"/v1/agents/{body['agent']['id']}", headers=auth)
        assert got.status_code == 200
        assert got.json()["reasoning_effort"] == "low"


class TestReasoningEffortRuns:
    def test_run_ledger_carries_effort(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth, agent={"provider": "codex", "reasoning_effort": "medium"})
        agent_id = body["agent"]["id"]
        run = wait_run(client, auth, agent_id, "run-1")
        assert run["reasoning_effort"] == "medium"

    def test_followup_runs_inherit_effort(self, client, auth, v1_env, spy) -> None:
        body = create_agent(client, auth, agent={"provider": "codex", "reasoning_effort": "high"})
        agent_id = body["agent"]["id"]
        wait_run(client, auth, agent_id, "run-1")
        resp = client.post(
            f"/v1/agents/{agent_id}/runs",
            json={"prompt": {"text": "follow up"}},
            headers=auth,
        )
        assert resp.status_code == 201
        assert resp.json()["reasoning_effort"] == "high"
        run = wait_run(client, auth, agent_id, "run-2")
        assert run["status"] == "FINISHED"
        assert run["reasoning_effort"] == "high"


class TestReasoningEffortCapabilityReporting:
    def test_models_report_supported_efforts(self, client, auth, v1_env, monkeypatch) -> None:
        monkeypatch.setenv("SBX_PROVIDERS", ",".join(PROVIDERS_ALL))
        seed_account(v1_env, "acct-agy-1", provider="antigravity", models=("gemini-3.8",))
        seed_account(v1_env, "acct-grok-1", provider="grok", models=("grok-4.6",))
        seed_account(v1_env, "acct-oc-1", provider="opencode", models=("oc-1",))
        seed_account(v1_env, "acct-devin-1", provider="devin", models=("swe-2-high",))
        resp = client.get("/v1/models", headers=auth)
        assert resp.status_code == 200
        efforts = {m["provider"]: m["reasoning_efforts"] for m in resp.json()["models"]}
        for provider in PROVIDERS_EFFORT:
            assert efforts[provider] == list(CANONICAL_EFFORTS)
        for provider in PROVIDERS_NO_EFFORT:
            assert efforts[provider] == []
