#!/usr/bin/env python3
"""Fake Grok Build CLI (``grok -p ... --output-format streaming-json``).

Scenarios via ``FAKE_GROK_SCENARIO``: success, resume, nonzero, hang,
badjson, slow, auth_invalid. ``FAKE_GROK_SLOW_SECONDS`` overrides the
silent delay in ``slow`` (default 40). ``FAKE_GROK_SESSION_ID`` overrides
the emitted ``sessionId``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _fake_native import (
    install_term_handler,
    models_catalog,
    rewrite_json,
    run_scenario,
    scan_argv,
)

PROVIDER = "grok"
DEFAULT_SESSION_ID = "01a09b11-0000-7f90-b96e-42adeefa05e0"

_BOOL_FLAGS = {"-p", "--print", "--resume", "--last"}
_VALUE_FLAGS = {"--output-format", "-m", "--model", "-C", "--cd", "-s", "--session"}


def _rewrite_session(line: str, session_id: str) -> str:
    def _mutate(obj: dict) -> None:
        if "sessionId" in obj:
            obj["sessionId"] = session_id

    return rewrite_json(line, _mutate)


def main() -> None:
    install_term_handler()
    # ``grok models`` doubles as auth check and SOR-204 discovery — the
    # catalog JSON keeps the ``Logged in`` marker classify_auth_output
    # requires for grok.
    models_catalog(
        sys.argv[1:],
        ("models",),
        Path.home() / ".grok" / "auth.json",
        "grok",
        "FAKE_GROK_MODELS_JSON",
    )
    positionals, values, seen = scan_argv(
        sys.argv[1:], bool_flags=_BOOL_FLAGS, value_flags=_VALUE_FLAGS
    )
    del positionals
    session_id = values.get("--session") or values.get("-s")
    run_scenario(
        provider=PROVIDER,
        scenario_env="FAKE_GROK_SCENARIO",
        slow_env="FAKE_GROK_SLOW_SECONDS",
        session_env="FAKE_GROK_SESSION_ID",
        default_session_id=DEFAULT_SESSION_ID,
        is_resume=session_id is not None or "--resume" in seen or "--last" in seen,
        session_id=session_id,
        cwd=Path(values.get("-C") or values.get("--cd") or Path.cwd()),
        rewrite=_rewrite_session,
        hello_content="hello from fake_grok\n",
        resume_content="resumed by fake_grok\n",
    )


if __name__ == "__main__":
    main()
