#!/usr/bin/env python3
"""Fake Codex CLI (0.153.0-shaped ``exec --json`` / ``exec resume``).

Scenarios are selected with ``FAKE_CODEX_SCENARIO``:
success, resume, nonzero, hang, badjson, slow, auth_invalid.

``FAKE_CODEX_SLOW_SECONDS`` overrides the silent delay after ``thread.started``
in the ``slow`` scenario (default 40).
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from pathlib import Path

# The other fake CLIs rely on script-mode sys.path[0]; codex_spy.py exec's
# this file via runpy, which doesn't add the directory — pin it explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_native import emit_catalog  # noqa: E402

DEFAULT_THREAD_ID = "01a09a36-b4fb-7f90-b96e-42adeefa05e0"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "events"

_BOOL_FLAGS = {
    "--json",
    "--experimental-json",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "--last",
    "--all",
    "--ephemeral",
    "--ignore-user-config",
    "--ignore-rules",
    "--strict-config",
}
_VALUE_FLAGS = {
    "-C",
    "--cd",
    "-m",
    "--model",
    "-o",
    "--output-last-message",
    "--output-schema",
    "--sandbox",
    "-s",
    "--color",
    "-c",
    "--config",
    "--profile",
    "-p",
    "--thread-source",
    "--image",
    "-i",
    "--add-dir",
}


def _fixture(scenario: str) -> Path:
    path = FIXTURE_DIR / f"{scenario}.jsonl"
    if not path.is_file():
        print(f"unknown scenario fixture: {scenario}", file=sys.stderr)
        sys.exit(1)
    return path


def _auth_status() -> None:
    """``codex login status`` — the provider CLI's own auth check.

    Reflects the restored credential file only: ``$CODEX_HOME`` (else
    ``$HOME/.codex``) ``auth.json`` must exist and hold a JSON object.
    """
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    auth = home / "auth.json"
    try:
        ok = auth.is_file() and isinstance(json.loads(auth.read_text()), dict)
    except (OSError, json.JSONDecodeError):
        ok = False
    if ok:
        print("Logged in using ChatGPT")
        sys.exit(0)
    print("Not logged in")
    sys.exit(1)


def _models() -> None:
    """``codex models`` — SOR-204 capability discovery catalog.

    Reflects the restored credential file only (same check as
    ``login status``), then emits the catalog JSON.
    """
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    auth = home / "auth.json"
    try:
        ok = auth.is_file() and isinstance(json.loads(auth.read_text()), dict)
    except (OSError, json.JSONDecodeError):
        ok = False
    if not ok:
        print("Not logged in")
        sys.exit(1)
    emit_catalog("codex", "FAKE_CODEX_MODELS_JSON")


def parse_argv(argv: list[str]) -> dict:
    tokens = list(argv[1:])
    if tokens and Path(tokens[0]).name in {"codex", "fake_codex.py"}:
        tokens = tokens[1:]
    if tokens[:2] == ["login", "status"]:
        _auth_status()
    if tokens[:1] == ["models"]:
        _models()
    if not tokens or tokens[0] != "exec":
        print("expected: exec [--json] ... [resume] [PROMPT]", file=sys.stderr)
        sys.exit(2)
    tokens = tokens[1:]

    cwd: Path | None = None
    is_resume = False
    positionals: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "resume":
            is_resume = True
            i += 1
            continue
        if tok in _BOOL_FLAGS:
            i += 1
            continue
        if tok in _VALUE_FLAGS:
            if i + 1 >= len(tokens):
                print(f"flag {tok} requires a value", file=sys.stderr)
                sys.exit(2)
            if tok in {"-C", "--cd"}:
                cwd = Path(tokens[i + 1])
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        positionals.append(tok)
        i += 1

    session_id: str | None = None
    prompt_token: str | None = None
    if is_resume:
        if positionals:
            session_id = positionals[0]
        if len(positionals) > 1:
            prompt_token = positionals[1]
    else:
        prompt_token = positionals[0] if positionals else None

    # Real Codex: piped stdin waits for EOF even when a positional prompt is
    # given, then appends it as a <stdin> block. Runner must pass DEVNULL.
    stdin_block = ""
    if not sys.stdin.isatty():
        stdin_block = sys.stdin.read()
    if prompt_token == "-":
        prompt = stdin_block
    elif prompt_token is not None:
        prompt = prompt_token
    else:
        prompt = stdin_block

    return {
        "cwd": cwd or Path.cwd(),
        "is_resume": is_resume,
        "session_id": session_id,
        "prompt": prompt,
    }


def emit_line(line: str) -> None:
    sys.stdout.write(line if line.endswith("\n") else line + "\n")
    sys.stdout.flush()


def rewrite_thread_id(line: str, thread_id: str) -> str:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return line.rstrip("\n")
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return line.rstrip("\n")
    if obj.get("type") == "thread.started":
        obj["thread_id"] = thread_id
        return json.dumps(obj, ensure_ascii=False)
    return stripped


def install_term_handler() -> None:
    def _handler(_signum: int, _frame: object) -> None:
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def replay(
    path: Path,
    *,
    thread_id: str,
    hang_after_first: bool = False,
    pause_after_first: float = 0.0,
) -> None:
    first = True
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        emit_line(rewrite_thread_id(raw, thread_id))
        if first:
            first = False
            if hang_after_first:
                install_term_handler()
                time.sleep(3600)
                return
            if pause_after_first > 0:
                install_term_handler()
                time.sleep(pause_after_first)
                install_term_handler()  # keep handler for the rest of the run


def write_hello(cwd: Path) -> None:
    (cwd / "hello.txt").write_text("hello from fake_codex\n", encoding="utf-8")


def append_hello(cwd: Path) -> None:
    path = cwd / "hello.txt"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("resumed by fake_codex\n")


def main() -> None:
    install_term_handler()
    parsed = parse_argv(sys.argv)
    cwd: Path = parsed["cwd"]
    cwd.mkdir(parents=True, exist_ok=True)
    os.chdir(cwd)

    scenario = os.environ.get("FAKE_CODEX_SCENARIO", "success")
    thread_id = parsed["session_id"] or os.environ.get("FAKE_CODEX_THREAD_ID", DEFAULT_THREAD_ID)

    if scenario == "resume":
        if not parsed["is_resume"]:
            emit_line(
                json.dumps({"type": "error", "message": "resume scenario requires exec resume"})
            )
            print("FAKE_CODEX_SCENARIO=resume requires `exec resume`", file=sys.stderr)
            sys.exit(1)
        hello = cwd / "hello.txt"
        if not hello.is_file():
            emit_line(json.dumps({"type": "thread.started", "thread_id": thread_id}))
            emit_line(json.dumps({"type": "error", "message": "hello.txt not found"}))
            print("hello.txt not found", file=sys.stderr)
            sys.exit(1)
        append_hello(cwd)
        replay(_fixture("resume"), thread_id=thread_id)
        sys.exit(0)

    if scenario == "hang":
        replay(_fixture("hang"), thread_id=thread_id, hang_after_first=True)
        sys.exit(0)

    if scenario == "slow":
        delay = float(os.environ.get("FAKE_CODEX_SLOW_SECONDS", "40"))
        if not parsed["is_resume"]:
            write_hello(cwd)
        replay(_fixture("slow"), thread_id=thread_id, pause_after_first=delay)
        sys.exit(0)

    if scenario == "nonzero":
        replay(_fixture("nonzero"), thread_id=thread_id)
        print("fake_codex nonzero scenario", file=sys.stderr)
        sys.exit(1)

    if scenario == "auth_invalid":
        replay(_fixture("auth_invalid"), thread_id=thread_id)
        print("fake_codex auth_invalid scenario", file=sys.stderr)
        sys.exit(1)

    if scenario == "badjson":
        if not parsed["is_resume"]:
            write_hello(cwd)
        replay(_fixture("badjson"), thread_id=thread_id)
        sys.exit(0)

    # Extension point: a scenario outside the built-in set replays
    # ``fixtures/events/<scenario>.jsonl`` when one exists (e.g. SOR-130's
    # ``structured`` output fixture). Pinned to [a-z0-9_] so the env value
    # can never traverse out of the fixture tree.
    if not parsed["is_resume"] and re.fullmatch(r"[a-z0-9_]+", scenario):
        custom = FIXTURE_DIR / f"{scenario}.jsonl"
        if custom.is_file():
            write_hello(cwd)
            replay(custom, thread_id=thread_id)
            sys.exit(0)

    # success (and unknown → success fixture if present)
    if parsed["is_resume"]:
        # success + resume subcommand: still a first-class exec resume, no file requirement
        replay(_fixture("success"), thread_id=thread_id)
        sys.exit(0)

    write_hello(cwd)
    replay(_fixture("success"), thread_id=thread_id)
    sys.exit(0)


if __name__ == "__main__":
    main()
