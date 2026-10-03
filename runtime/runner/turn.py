"""``runner turn`` and ``runner stop``.

P2 (SOR-62/SOR-72): provider dispatch through ``AgentAdapter``. Native stdout
lines go to ``events.raw.jsonl``; ``adapter.translate`` output (canonical
events, Codex shape) goes to ``events.jsonl`` and runner stdout. Codex is the
identity translation, so its stream is unchanged.

Bad-line rule (SOR-80): only a non-empty line that carries no JSON object
(unparseable text or non-object JSON) counts as bad JSON. Parseable objects
of unknown/non-terminal kinds translate to ``sbx.noop`` and are dropped
here — forward-compatible provider events never fail a turn.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from runtime.runner.adapter import get_adapter
from runtime.runner.codex import iter_codex_stdout, start_codex
from runtime.runner.constants import (
    DEFAULT_MAX_SECONDS,
    EXIT_AUTH_INVALID,
    EXIT_BAD_JSON,
    EXIT_CODEX,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_TIMEOUT,
    NOOP_EVENT_TYPE,
    STATUS_AUTH_INVALID,
    STATUS_BAD_JSON,
    STATUS_CODEX_ERROR,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    TERM_GRACE_S,
)
from runtime.runner.contract import (
    ContractError,
    contract_instruction,
    evaluate_output,
    load_contract_file,
)
from runtime.runner.credentials import CredentialError, restore_credential_blob, sandbox_home
from runtime.runner.events import (
    TurnState,
    parse_event_line,
    redact_line,
    redact_obj,
    redact_text,
)
from runtime.runner.workspace import (
    agent_workdir,
    atomic_write,
    codex_home,
    emit,
    emit_native,
    ensure_layout,
    load_session,
    save_session,
    work_root,
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cmd_stop() -> int:
    root = work_root()
    session = load_session(root)
    raw_pid = session.get("pid")
    if raw_pid is None:
        return EXIT_OK
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        session["pid"] = None
        save_session(root, session)
        return EXIT_OK
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        session["pid"] = None
        save_session(root, session)
        return EXIT_OK
    deadline = time.monotonic() + TERM_GRACE_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline and _pid_alive(pid):
            time.sleep(0.05)
    session = load_session(root)
    session["pid"] = None
    save_session(root, session)
    return EXIT_OK


def _stderr_tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-limit:]


def _finish_status(
    *, timed_out: bool, bad_json: bool, cli_rc: int | None, health: str
) -> tuple[str, int]:
    if timed_out:
        return STATUS_TIMEOUT, EXIT_TIMEOUT
    if bad_json:
        return STATUS_BAD_JSON, EXIT_BAD_JSON
    if cli_rc is None or cli_rc != 0:
        if health == "auth_invalid":
            return STATUS_AUTH_INVALID, EXIT_AUTH_INVALID
        return STATUS_CODEX_ERROR, EXIT_CODEX
    return STATUS_SUCCESS, EXIT_OK


def cmd_turn(
    *,
    n: int,
    message_file: str,
    max_seconds: int = DEFAULT_MAX_SECONDS,
    output_contract: str | None = None,
) -> int:
    root = work_root()
    ensure_layout(root)
    home = codex_home(root)
    started = time.monotonic()
    session = load_session(root)
    provider = session.get("provider") or "codex"
    # Hosted turns receive a fresh access-only lease, including resumed turns.
    if os.environ.get("SBX_HOSTED_CREDENTIAL_LEASE") == "1":
        try:
            restore_credential_blob(sandbox_home(root), provider=provider, codex_home=home)
        except CredentialError:
            emit(root, {"type": "sbx.error", "message": "invalid credential lease"})
            return EXIT_INTERNAL
    try:
        adapter = get_adapter(provider)
    except KeyError as exc:
        emit(root, {"type": "sbx.error", "message": str(exc)})
        return EXIT_INTERNAL
    native_id = session.get("native_session_id") or session.get("codex_session_id")
    requested_id = str(native_id) if native_id else None
    state = TurnState(thread_id=native_id)
    timed_out = False
    error_hint = ""  # runner-side terminal cause (timeout/spawn/stale/bad json)
    proc = None

    def _on_timeout() -> None:
        nonlocal timed_out, error_hint
        timed_out = True
        error_hint = f"turn {n} exceeded {max_seconds}s"
        emit(root, {"type": "sbx.error", "message": error_hint})

    try:
        src = Path(message_file)
        prompt = src.read_text(encoding="utf-8")
    except OSError as exc:
        emit(root, {"type": "sbx.error", "message": f"cannot read message file: {exc}"})
        return EXIT_INTERNAL

    inbox = root / "inbox" / f"{n}.md"
    atomic_write(inbox, prompt)

    # SOR-130: an output contract augments the provider-facing prompt only —
    # inbox/<n>.md keeps the user text verbatim. An unreadable/malformed
    # contract file fails closed (EXIT_INTERNAL, no turn payload) rather
    # than running uncontracted.
    contract: dict | None = None
    if output_contract is not None:
        try:
            contract = load_contract_file(output_contract)
        except (OSError, ContractError) as exc:
            emit(root, {"type": "sbx.error", "message": f"invalid output contract: {exc}"})
            return EXIT_INTERNAL
        prompt += contract_instruction(contract["schema"])

    if n == 1:
        emit(
            root,
            {
                "type": "sbx.session_meta",
                "provider": provider,
                "model": session.get("model"),
                "account_id": session.get("account_id"),
                "reasoning_effort": session.get("reasoning_effort"),
            },
        )
    emit(root, {"type": "sbx.turn_started", "n": n})

    model = session.get("model")
    if requested_id:
        argv = adapter.resume_argv(prompt, requested_id)
    else:
        argv = adapter.first_turn_argv(prompt, model if isinstance(model, str) and model else "")
    # Custom/fake CODEX_BIN keeps its existing exec protocol unless opted in.
    # Native Codex uses app-server deltas; SBX_CODEX_TRANSPORT=exec is a fallback.
    transport = os.environ.get("SBX_CODEX_TRANSPORT", "")
    if provider == "codex" and (
        transport == "app-server" or (not transport and "CODEX_BIN" not in os.environ)
    ):
        argv = [sys.executable, "-m", "runtime.runner.codex_stream"]
        if isinstance(model, str) and model:
            argv += ["--model", model]
        if requested_id:
            argv += ["--resume", requested_id]
        argv += ["--", prompt]
    stderr_path = root / "turns" / f"{n}.stderr"
    # SOR-174: provider CLIs run inside the declared workspace workdir
    # ($SBX_WORK/$SBX_WORKDIR) while runner state stays under ``root``.
    cli_cwd = agent_workdir(root)

    try:
        proc = start_codex(argv, work=root, home=home, stderr_path=stderr_path, cwd=cli_cwd)
    except OSError as exc:
        error_hint = f"failed to start provider CLI: {exc}"
        emit(root, {"type": "sbx.error", "message": error_hint})
        status, code = _finish_status(timed_out=False, bad_json=False, cli_rc=1, health="unknown")
        _write_turn_finished(
            root,
            n=n,
            session=session,
            state=state,
            status=status,
            code=code,
            health="unknown",
            duration_s=round(time.monotonic() - started, 3),
            error_hint=error_hint,
            contract=contract,
        )
        return code

    session["pid"] = proc.pid
    save_session(root, session)

    def _forward_term(_signum: int, _frame: object) -> None:
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                return

    signal.signal(signal.SIGTERM, _forward_term)
    signal.signal(signal.SIGINT, _forward_term)

    def _record_thread() -> None:
        if (
            state.thread_id
            and (requested_id is None or state.thread_id == requested_id)
            and session.get("native_session_id") != state.thread_id
        ):
            session["native_session_id"] = state.thread_id
            session["codex_session_id"] = state.thread_id
            session["pid"] = proc.pid
            save_session(root, session)

    observed_id: str | None = None  # thread.started emitted on this turn's stream

    for line in iter_codex_stdout(
        proc, max_seconds=max_seconds, grace_s=TERM_GRACE_S, on_timeout=_on_timeout
    ):
        safe = redact_line(line)
        if safe:
            emit_native(root, safe)
        events = adapter.translate(line)
        if not events and line.strip():
            # Adapter contract: ``[]`` means the line carried no JSON
            # object event (unparseable text or non-object JSON) — that is
            # a bad line. A parseable object always translates to >=1
            # event (``sbx.noop`` for unknown/non-terminal kinds); the
            # parse check here keeps even a non-compliant adapter from
            # mistaking a forward-compatible event for bad JSON.
            obj, bad = parse_event_line(line)
            if bad or obj is None:
                state.bad_json_lines += 1
                if not error_hint:
                    error_hint = "bad json in event stream"
                emit(root, {"type": "sbx.error", "message": "bad json in event stream"})
        for event in events:
            if not isinstance(event, dict) or event.get("type") == NOOP_EVENT_TYPE:
                continue
            event = redact_obj(event)
            emit(root, event)
            if event.get("type") == "thread.started":
                tid = event.get("thread_id")
                if isinstance(tid, str) and tid:
                    observed_id = tid
            state.consume_obj(event)
        _record_thread()

    cli_rc = proc.wait()
    duration = round(time.monotonic() - started, 3)
    health = "ok" if cli_rc == 0 else adapter.health_from(cli_rc, _stderr_tail(stderr_path))
    # Stale resume: the provider CLI exited 0 but never confirmed the requested
    # session id (agy warns on stderr and opens a new conversation). Never
    # adopt a replacement id; fail the turn instead of forking the session.
    stale_resume = requested_id is not None and observed_id != requested_id
    if stale_resume:
        state.thread_id = None
    status, code = _finish_status(
        timed_out=timed_out,
        bad_json=state.bad_json_lines > 0,
        cli_rc=cli_rc,
        health=health,
    )
    if stale_resume and code == EXIT_OK:
        error_hint = f"provider did not resume session {requested_id}"
        emit(root, {"type": "sbx.error", "message": error_hint})
        status, code = STATUS_CODEX_ERROR, EXIT_CODEX
    session = load_session(root)
    if state.thread_id:
        session["native_session_id"] = state.thread_id
        session["codex_session_id"] = state.thread_id
    session["turn"] = n
    _write_turn_finished(
        root,
        n=n,
        session=session,
        state=state,
        status=status,
        code=code,
        health=health,
        duration_s=duration,
        error_hint=error_hint,
        contract=contract,
    )
    return code


def _write_turn_finished(
    root: Path,
    *,
    n: int,
    session: dict,
    state: TurnState,
    status: str,
    code: int,
    health: str,
    duration_s: float,
    error_hint: str = "",
    contract: dict | None = None,
) -> None:
    session["pid"] = None
    session["turn"] = n
    if state.thread_id:
        session["native_session_id"] = state.thread_id
        session["codex_session_id"] = state.thread_id
    save_session(root, session)
    turn_path = root / "turns" / f"{n}.json"
    detail = "" if status == STATUS_SUCCESS else (error_hint or redact_text(state.last_error))
    payload = {
        "n": n,
        "codex_session_id": state.thread_id or session.get("codex_session_id"),
        "native_session_id": state.thread_id or session.get("native_session_id"),
        "status": status,
        "exit_code": code,
        "health": health,
        "duration_s": duration_s,
        "usage": dict(state.usage),
        "message": redact_text(state.last_message),
        "error": detail or None,
        "bad_json_lines": state.bad_json_lines,
    }
    if contract is not None:
        # SOR-130: turn evidence carries the normalized verdict. The runner
        # reports but does not enforce — the control plane re-evaluates the
        # recorded message and applies ``enforcement`` at terminal persist,
        # so a strict violation can never ride a forged payload to FINISHED.
        contract_meta = {
            "enforcement": contract["enforcement"],
            "schema_digest": contract["schema_digest"],
        }
        if status == STATUS_SUCCESS:
            verdict = evaluate_output(str(payload["message"] or ""), contract["schema"])
            payload["structured_output"] = verdict["value"]
            payload["output_contract"] = {
                **contract_meta,
                "status": verdict["status"],
                "extraction": verdict["extraction"],
                "violations": verdict["violations"],
            }
        else:
            payload["structured_output"] = None
            payload["output_contract"] = {
                **contract_meta,
                "status": "skipped",
                "extraction": None,
                "violations": [],
            }
    # The payload is written before the terminal event so a crashed write
    # can never emit a success the evidence does not back — turns/<n>.json
    # is the authoritative outcome the control plane persists from.
    atomic_write(turn_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    emit(
        root,
        {
            "type": "sbx.turn_finished",
            "status": status,
            "exit_code": code,
            "duration_s": duration_s,
            "usage": dict(state.usage),
        },
    )
