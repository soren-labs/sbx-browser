"""runner turn: six fake_codex scenarios + real_multiturn replay + spy."""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.runner.conftest import (
    DEFAULT_THREAD,
    USAGE_FIELDS,
    init_runner,
    load_json,
    parsed_events,
    run_runner,
    write_message,
)


def _turn(
    env: dict[str, str],
    work: Path,
    *,
    n: int = 1,
    max_seconds: int = 30,
    text: str = "hello from test",
    timeout: float = 25.0,
) -> tuple[int, dict, list[dict]]:
    msg = write_message(work, text)
    result = run_runner(
        ["turn", "--n", str(n), "--message-file", str(msg), "--max-seconds", str(max_seconds)],
        env,
        timeout=timeout,
    )
    turn_path = work / "turns" / f"{n}.json"
    turn = load_json(turn_path) if turn_path.is_file() else {}
    return result.returncode, turn, parsed_events(work)


def test_success_scenario(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    code, turn, events = _turn(runner_env, work)
    assert code == 0
    types = [e["type"] for e in events]
    assert types[0] == "sbx.session_meta"
    assert types[1] == "sbx.turn_started"
    assert "thread.started" in types
    assert types[-1] == "sbx.turn_finished"
    assert events[-1]["status"] == "success"
    assert events[-1]["exit_code"] == 0
    assert set(events[-1]["usage"]) == USAGE_FIELDS
    assert turn["codex_session_id"] == DEFAULT_THREAD
    assert turn["status"] == "success"
    assert turn["exit_code"] == 0
    assert set(turn["usage"]) == USAGE_FIELDS
    assert turn["usage"]["cached_input_tokens"] >= 10000
    assert "hello.txt" in turn["message"] or turn["message"]
    assert (work / "inbox" / "1.md").is_file()
    assert (work / "hello.txt").read_text(encoding="utf-8") == "hello from fake_codex\n"
    assert (work / "turns" / "1.stderr").is_file()
    session = load_json(work / "session.json")
    assert session["codex_session_id"] == DEFAULT_THREAD
    assert session["turn"] == 1
    assert session["pid"] is None


def test_resume_scenario(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "success"
    code1, turn1, _ = _turn(runner_env, work, n=1)
    assert code1 == 0
    runner_env["FAKE_CODEX_SCENARIO"] = "resume"
    code2, turn2, events2 = _turn(runner_env, work, n=2, text="continue")
    assert code2 == 0
    assert turn1["codex_session_id"] == turn2["codex_session_id"] == DEFAULT_THREAD
    text = (work / "hello.txt").read_text(encoding="utf-8")
    assert "hello from fake_codex" in text
    assert "resumed by fake_codex" in text
    types = [e["type"] for e in events2]
    assert "sbx.turn_started" in types
    assert "thread.started" in types
    started = [e for e in events2 if e.get("type") == "thread.started"]
    assert started and started[0]["thread_id"] == DEFAULT_THREAD
    assert set(turn2["usage"]) == USAGE_FIELDS


def test_hosted_followup_restores_rotated_access_lease(work, runner_env):
    import secrets

    init_runner(runner_env)
    runner_env["SBX_HOSTED_CREDENTIAL_LEASE"] = "1"
    tokens = [secrets.token_urlsafe(24), secrets.token_urlsafe(24)]
    for n, token in enumerate(tokens, 1):
        content = json.dumps({"tokens": {"access_token": token}})
        runner_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(
            {"provider": "codex", "files": {".codex/auth.json": content}}
        )
        code, _, _ = _turn(runner_env, work, n=n)
        assert code == 0
        auth_file = Path(runner_env["CODEX_HOME"]) / "auth.json"
        assert auth_file.read_text() == content
        assert auth_file.stat().st_mode & 0o777 == 0o600
        assert token not in (work / "events.jsonl").read_text()


def test_nonzero_scenario(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "nonzero"
    code, turn, events = _turn(runner_env, work)
    assert code == 2
    assert turn["status"] == "codex_error"
    assert turn["exit_code"] == 2
    finished = [e for e in events if e.get("type") == "sbx.turn_finished"]
    assert finished and finished[-1]["status"] == "codex_error"
    types = [e["type"] for e in events]
    assert "turn.failed" in types


def test_hang_timeout_scenario(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "hang"
    code, turn, events = _turn(runner_env, work, max_seconds=1, timeout=15.0)
    assert code == 3
    assert turn["status"] == "timeout"
    assert turn["exit_code"] == 3
    assert turn["codex_session_id"] == DEFAULT_THREAD
    types = [e["type"] for e in events]
    assert "sbx.error" in types
    finished = [e for e in events if e.get("type") == "sbx.turn_finished"]
    assert finished[-1]["status"] == "timeout"
    assert set(finished[-1]["usage"]) == USAGE_FIELDS


def test_badjson_scenario(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "badjson"
    code, turn, events = _turn(runner_env, work)
    assert code == 4
    assert turn["status"] == "bad_json"
    assert turn["exit_code"] == 4
    assert turn["bad_json_lines"] >= 1
    assert turn["codex_session_id"] == DEFAULT_THREAD
    assert turn["message"]
    errors = [e for e in events if e.get("type") == "sbx.error"]
    assert errors
    # Parser must keep going after the bad line.
    types = [e["type"] for e in events]
    assert "turn.completed" in types
    assert types[-1] == "sbx.turn_finished"


def test_slow_scenario_completes(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "slow"
    runner_env["FAKE_CODEX_SLOW_SECONDS"] = "1"
    code, turn, events = _turn(runner_env, work, max_seconds=20, timeout=25.0)
    assert code == 0, turn
    assert turn["status"] == "success"
    assert (work / "hello.txt").is_file()
    types = [e["type"] for e in events]
    assert types[0] == "sbx.session_meta"
    assert types[1] == "sbx.turn_started"
    assert types[-1] == "sbx.turn_finished"
    assert set(turn["usage"]) == USAGE_FIELDS


def test_real_multiturn_fixture_replay(
    work: Path, runner_env: dict[str, str], repo_root: Path
) -> None:
    init_runner(runner_env)
    fixture = repo_root / "tests" / "fixtures" / "events" / "real_multiturn.jsonl"
    replay = Path(__file__).resolve().parent / "replay_codex.py"
    runner_env["CODEX_BIN"] = str(replay)
    runner_env["FAKE_CODEX_FIXTURE"] = str(fixture)
    code, turn, events = _turn(runner_env, work, text="replay")
    assert code == 0
    assert turn["codex_session_id"] == DEFAULT_THREAD
    assert turn["status"] == "success"
    assert turn["message"] == "DONE"
    assert set(turn["usage"]) == USAGE_FIELDS
    assert turn["usage"]["input_tokens"] == 25996 + 26100 + 26200
    assert turn["usage"]["cached_input_tokens"] == 22016 + 22100 + 22200
    assert turn["usage"]["cache_write_input_tokens"] == 0
    assert turn["usage"]["output_tokens"] == 325 + 80 + 10
    assert turn["usage"]["reasoning_output_tokens"] == 238 + 40 + 0
    threads = [e for e in events if e.get("type") == "thread.started"]
    assert len(threads) == 3
    assert {e["thread_id"] for e in threads} == {DEFAULT_THREAD}


def test_redacts_secrets_from_events(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    leak = Path(__file__).resolve().parent / "leak_codex.py"
    runner_env["CODEX_BIN"] = str(leak)
    code, turn, events = _turn(runner_env, work)
    assert code == 0
    raw = (work / "events.jsonl").read_text(encoding="utf-8")
    assert "sk-THISLEAKEDVALUE12" not in raw
    assert "sk-THISLEAKEDVALUE12" not in json.dumps(turn)
    items = [
        e["item"]
        for e in events
        if e.get("type") == "item.completed" and isinstance(e.get("item"), dict)
    ]
    assert items and items[0]["api_key"] == "REDACTED"


def test_spy_stdin_devnull_and_no_auth_json_inherit(
    work: Path, runner_env: dict[str, str], fake_codex: Path
) -> None:
    init_runner(runner_env)
    spy = Path(__file__).resolve().parent / "codex_spy.py"
    dump = work / "spy.json"
    runner_env["CODEX_BIN"] = str(spy)
    runner_env["CODEX_SPY_TARGET"] = str(fake_codex)
    runner_env["CODEX_SPY_OUT"] = str(dump)
    runner_env["CODEX_AUTH_JSON"] = json.dumps({"tokens": {"access_token": "REDACTED"}})
    code, _turn_doc, _ = _turn(runner_env, work)
    assert code == 0
    spy_data = json.loads(dump.read_text(encoding="utf-8"))
    assert spy_data["has_CODEX_AUTH_JSON"] is False
    assert spy_data["stdin_target"] == "/dev/null"
    assert spy_data["argv"][-1] != "-"
    assert "--json" in spy_data["argv"]
    assert "exec" in spy_data["argv"]
    assert "resume" not in spy_data["argv"]
