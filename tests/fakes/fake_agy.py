#!/usr/bin/env python3
"""Fake Antigravity CLI (``agy -p ... --output-format stream-json``).

Scenarios via ``FAKE_AGY_SCENARIO``: success, resume, nonzero, hang,
badjson, slow, auth_invalid. ``FAKE_AGY_SLOW_SECONDS`` overrides the
silent delay in ``slow`` (default 40). ``FAKE_AGY_SESSION_ID`` overrides
the emitted ``conversation_id``.
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

PROVIDER = "antigravity"
DEFAULT_SESSION_ID = "c3b66b04-872b-4fbe-a3a4-058a026ef20a"

_BOOL_FLAGS = {"-p", "--print", "--yolo", "--json"}
_VALUE_FLAGS = {"--output-format", "-m", "--model", "-C", "--cd", "--resume"}


def _rewrite_session(line: str, session_id: str) -> str:
    def _mutate(obj: dict) -> None:
        for key in ("init", "step_update", "result"):
            inner = obj.get(key)
            if isinstance(inner, dict) and "conversation_id" in inner:
                inner["conversation_id"] = session_id

    return rewrite_json(line, _mutate)


def main() -> None:
    install_term_handler()
    # ``agy models`` is both the auth check and the SOR-204 capability
    # discovery surface — emits the catalog JSON when logged in.
    models_catalog(
        sys.argv[1:],
        ("models",),
        Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
        "antigravity",
        "FAKE_AGY_MODELS_JSON",
    )
    positionals, values, _seen = scan_argv(
        sys.argv[1:], bool_flags=_BOOL_FLAGS, value_flags=_VALUE_FLAGS
    )
    del positionals  # prompt text is ignored by the fake
    run_scenario(
        provider=PROVIDER,
        scenario_env="FAKE_AGY_SCENARIO",
        slow_env="FAKE_AGY_SLOW_SECONDS",
        session_env="FAKE_AGY_SESSION_ID",
        default_session_id=DEFAULT_SESSION_ID,
        is_resume="--resume" in values,
        session_id=values.get("--resume"),
        cwd=Path(values.get("-C") or values.get("--cd") or Path.cwd()),
        rewrite=_rewrite_session,
        hello_content="hello from fake_agy\n",
        resume_content="resumed by fake_agy\n",
    )


if __name__ == "__main__":
    main()
