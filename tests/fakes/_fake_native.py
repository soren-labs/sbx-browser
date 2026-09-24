"""Shared machinery for the non-Codex fake provider CLIs (SOR-59).

Each fake_*_<provider>.py script parses its own argv shape, then delegates
to :func:`run_scenario`, which replays ``tests/fixtures/events/<provider>/
<scenario>.jsonl`` with the same timing / file / exit-code semantics as
``fake_codex.py``.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path

FIXTURE_BASE = Path(__file__).resolve().parent.parent / "fixtures" / "events"

SCENARIOS = ("success", "resume", "nonzero", "hang", "badjson", "slow", "auth_invalid")


def fixture(provider: str, scenario: str) -> Path:
    path = FIXTURE_BASE / provider / f"{scenario}.jsonl"
    if not path.is_file():
        print(f"unknown scenario fixture: {provider}/{scenario}", file=sys.stderr)
        sys.exit(1)
    return path


def emit_line(line: str) -> None:
    sys.stdout.write(line if line.endswith("\n") else line + "\n")
    sys.stdout.flush()


def install_term_handler() -> None:
    def _handler(_signum: int, _frame: object) -> None:
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def scan_argv(
    tokens: list[str],
    *,
    bool_flags: set[str],
    value_flags: set[str],
    subcommand: str | None = None,
) -> tuple[list[str], dict[str, str], set[str]]:
    """Split argv into (positionals, flag values, bool flags seen)."""
    tokens = list(tokens)
    if subcommand is not None:
        if not tokens or tokens[0] != subcommand:
            print(f"expected subcommand: {subcommand}", file=sys.stderr)
            sys.exit(2)
        tokens = tokens[1:]
    positionals: list[str] = []
    values: dict[str, str] = {}
    seen: set[str] = set()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in bool_flags:
            seen.add(tok)
            i += 1
        elif tok in value_flags:
            if i + 1 >= len(tokens):
                print(f"flag {tok} requires a value", file=sys.stderr)
                sys.exit(2)
            values[tok] = tokens[i + 1]
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            positionals.append(tok)
            i += 1
    return positionals, values, seen


def rewrite_json(line: str, mutator: Callable[[dict], None]) -> str:
    """Apply ``mutator`` to a JSON line; pass non-JSON through unchanged."""
    stripped = line.strip()
    if not stripped.startswith("{"):
        return line.rstrip("\n")
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return line.rstrip("\n")
    mutator(obj)
    return json.dumps(obj, ensure_ascii=False)


def replay(
    path: Path,
    *,
    rewrite: Callable[[str], str] | None = None,
    hang_after_first: bool = False,
    pause_after_first: float = 0.0,
) -> None:
    first = True
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        emit_line(rewrite(raw) if rewrite else raw)
        if first:
            first = False
            if hang_after_first:
                install_term_handler()
                time.sleep(3600)
                return
            if pause_after_first > 0:
                install_term_handler()
                time.sleep(pause_after_first)
                install_term_handler()


def write_hello(cwd: Path, content: str) -> None:
    (cwd / "hello.txt").write_text(content, encoding="utf-8")


def append_hello(cwd: Path, content: str) -> None:
    with (cwd / "hello.txt").open("a", encoding="utf-8") as fh:
        fh.write(content)


# Default capability catalogs for the SOR-204 ``<cli> models`` discovery
# subcommand — what each provider account advertises when no
# ``FAKE_<P>_MODELS_JSON`` override is set.
DEFAULT_MODEL_CATALOGS: dict[str, dict] = {
    "codex": {
        "models": [
            {"id": "gpt-5.6-luna"},
            {"id": "gpt-5.3-codex"},
        ],
    },
    "devin": {
        "plan": "pro",
        "families": ["swe-2", "swe-1.5"],
        "models": [
            {"id": "swe-2-medium"},
            {"id": "swe-2-high"},
            {"id": "swe-2-max"},
            {"id": "swe-1.5"},
        ],
    },
    "antigravity": {
        "models": [
            {"id": "gemini-3.8-flash-low"},
            {"id": "gemini-3.8-pro"},
        ],
    },
    "grok": {
        "models": [
            {"id": "grok-4.6"},
            {"id": "grok-4.7"},
        ],
    },
    "opencode": {
        "models": [
            {"id": "opencode/muse-spark-1.3-contributor-free", "free": True},
            {"id": "opencode/claude-sonnet-4-5", "free": True},
            {"id": "openai/gpt-5.6-luna"},
        ],
    },
}


def emit_catalog(provider: str, env_json: str) -> None:
    """Print the provider's capability catalog JSON and exit 0.

    The document always carries a ``status: "Logged in"`` marker so the
    same output still satisfies ``classify_auth_output`` for the
    marker-checked providers (grok/devin) — SOR-204 discovery and the
    auth check share the ``models`` subcommand there.
    """
    raw = os.environ.get(env_json)
    if raw:
        print(raw)
        sys.exit(0)
    doc = {"status": "Logged in", **DEFAULT_MODEL_CATALOGS.get(provider, {"models": []})}
    print(json.dumps(doc))
    sys.exit(0)


def models_catalog(
    argv: list[str],
    subcommand: tuple[str, ...],
    credential: Path,
    provider: str,
    env_json: str,
) -> None:
    """Dispatch the SOR-204 ``<cli> models`` discovery subcommand.

    Same logged-out surface as :func:`auth_check`; when the credential is
    present the CLI emits the account's capability catalog as JSON.
    """
    if tuple(argv[: len(subcommand)]) != subcommand:
        return
    try:
        ok = credential.is_file() and bool(credential.read_bytes().strip())
    except OSError:
        ok = False
    if not ok:
        print("Not logged in")
        sys.exit(1)
    emit_catalog(provider, env_json)


def auth_check(argv: list[str], subcommand: tuple[str, ...], credential: Path) -> None:
    """Dispatch the provider's auth-check subcommand; no-op for other argv.

    Reflects only the restored credential file — present and non-empty
    means logged in. The credential itself is never printed.
    """
    if tuple(argv[: len(subcommand)]) != subcommand:
        return
    try:
        ok = credential.is_file() and bool(credential.read_bytes().strip())
    except OSError:
        ok = False
    if ok:
        print("Logged in")
        sys.exit(0)
    print("Not logged in")
    sys.exit(1)


def run_scenario(
    *,
    provider: str,
    scenario_env: str,
    slow_env: str,
    session_env: str,
    default_session_id: str,
    is_resume: bool,
    session_id: str | None,
    cwd: Path,
    rewrite: Callable[[str, str], str],
    hello_content: str,
    resume_content: str,
) -> None:
    """Common scenario dispatch mirroring fake_codex.main()."""
    cwd.mkdir(parents=True, exist_ok=True)
    os.chdir(cwd)

    scenario = os.environ.get(scenario_env, "success")
    sid = session_id or os.environ.get(session_env, default_session_id)
    rw = lambda line: rewrite(line, sid)  # noqa: E731

    if scenario == "resume":
        if not is_resume:
            emit_line(
                json.dumps(
                    {"type": "error", "message": "resume scenario requires a resume invocation"}
                )
            )
            print(f"{scenario_env}=resume requires a resume invocation", file=sys.stderr)
            sys.exit(1)
        hello = cwd / "hello.txt"
        if not hello.is_file():
            print("hello.txt not found", file=sys.stderr)
            sys.exit(1)
        append_hello(cwd, resume_content)
        replay(fixture(provider, "resume"), rewrite=rw)
        sys.exit(0)

    if scenario == "hang":
        replay(fixture(provider, "hang"), rewrite=rw, hang_after_first=True)
        sys.exit(0)

    if scenario == "slow":
        delay = float(os.environ.get(slow_env, "40"))
        if not is_resume:
            write_hello(cwd, hello_content)
        replay(fixture(provider, "slow"), rewrite=rw, pause_after_first=delay)
        sys.exit(0)

    if scenario in {"nonzero", "auth_invalid"}:
        replay(fixture(provider, scenario), rewrite=rw)
        print(f"{provider} {scenario} scenario", file=sys.stderr)
        sys.exit(1)

    if scenario == "badjson":
        if not is_resume:
            write_hello(cwd, hello_content)
        replay(fixture(provider, "badjson"), rewrite=rw)
        sys.exit(0)

    # Extension point: a scenario outside the built-in set replays
    # ``fixtures/events/<provider>/<scenario>.jsonl`` when one exists
    # (e.g. SOR-130's ``structured`` output fixtures). Path components are
    # pinned so the env value can never traverse out of the fixture tree.
    if re.fullmatch(r"[a-z0-9_]+", scenario):
        custom = FIXTURE_BASE / provider / f"{scenario}.jsonl"
        if custom.is_file():
            if not is_resume:
                write_hello(cwd, hello_content)
            replay(custom, rewrite=rw)
            sys.exit(0)

    # success (and unknown scenarios fall back to the success fixture)
    if is_resume:
        replay(fixture(provider, "success"), rewrite=rw)
        sys.exit(0)
    write_hello(cwd, hello_content)
    replay(fixture(provider, "success"), rewrite=rw)
    sys.exit(0)
