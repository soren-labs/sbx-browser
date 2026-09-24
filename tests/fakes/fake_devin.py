#!/usr/bin/env python3
"""Fake Devin CLI (``devin -p ...`` / ``devin --resume <id>``).

Scenarios via ``FAKE_DEVIN_SCENARIO``: success, resume, nonzero, hang,
badjson, slow, auth_invalid. ``FAKE_DEVIN_SLOW_SECONDS`` overrides the
silent delay in ``slow`` (default 40). ``FAKE_DEVIN_SESSION_ID`` overrides
the emitted ``session_id``.

NOTE: Devin CLI does not document a structured JSON streaming mode; the
emitted NDJSON shape is a provisional WP0 contract placeholder pending
SOR-60 real-CLI evidence.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _fake_native import (
    auth_check,
    install_term_handler,
    models_catalog,
    rewrite_json,
    run_scenario,
    scan_argv,
)

PROVIDER = "devin"
DEFAULT_SESSION_ID = "devin-session-01a09b11"

_BOOL_FLAGS = {"-p", "--print", "--acp", "--no-git"}
_VALUE_FLAGS = {"-m", "--model", "-C", "--cd", "--resume", "--session", "--export"}


def _rewrite_session(line: str, session_id: str) -> str:
    def _mutate(obj: dict) -> None:
        if "session_id" in obj:
            obj["session_id"] = session_id

    return rewrite_json(line, _mutate)


def main() -> None:
    install_term_handler()
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    auth_check(
        sys.argv[1:],
        ("auth", "status"),
        data_home / "devin" / "credentials.toml",
    )
    # SOR-204 discovery: ``devin models`` emits the account catalog (the
    # fake's default carries the swe-2 medium/high/max tier + families).
    models_catalog(
        sys.argv[1:],
        ("models",),
        data_home / "devin" / "credentials.toml",
        "devin",
        "FAKE_DEVIN_MODELS_JSON",
    )
    positionals, values, _seen = scan_argv(
        sys.argv[1:], bool_flags=_BOOL_FLAGS, value_flags=_VALUE_FLAGS
    )
    del positionals
    session_id = values.get("--resume") or values.get("--session")
    run_scenario(
        provider=PROVIDER,
        scenario_env="FAKE_DEVIN_SCENARIO",
        slow_env="FAKE_DEVIN_SLOW_SECONDS",
        session_env="FAKE_DEVIN_SESSION_ID",
        default_session_id=DEFAULT_SESSION_ID,
        is_resume=session_id is not None,
        session_id=session_id,
        cwd=Path(values.get("-C") or values.get("--cd") or Path.cwd()),
        rewrite=_rewrite_session,
        hello_content="hello from fake_devin\n",
        resume_content="resumed by fake_devin\n",
    )


if __name__ == "__main__":
    main()
