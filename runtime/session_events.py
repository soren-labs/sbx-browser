"""Provider-activity → Session-event normalization for ``/v2`` SSE (SOR-256).

The canonical ``events.jsonl`` stream mixes provider events
(``thread.started``, ``item.*``, ``turn.*``) with runner-internal
``sbx.*`` frames. The V2 stream renames them into Session vocabulary and
drops internal fields (account ids, CLI exit codes, provider thread
bindings); payload content the product already shows — message text,
commands, file-change paths, usage — passes through untouched.

``n`` (the session-relative turn number) is annotated on every event so a
client never has to track ``sbx.turn_started`` boundaries itself.
"""

from __future__ import annotations

from typing import Any

# Event types emitted on the /v2 session stream.
SESSION_EVENT_TYPES = (
    "session.status",
    "session.meta",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "turn.finished",
    "item.started",
    "item.updated",
    "item.completed",
    "error",
)


def track_turn(obj: dict[str, Any], current_turn: int) -> int:
    """``sbx.turn_started`` is the turn-boundary marker (like /v1)."""
    if obj.get("type") == "sbx.turn_started":
        try:
            return int(obj.get("n") or 0)
        except (TypeError, ValueError):
            return current_turn
    return current_turn


def normalize(obj: dict[str, Any], current_turn: int) -> dict[str, Any] | None:
    """Map one canonical event object to its Session-vocabulary frame.

    ``None`` means the frame is an internal marker with no Session
    counterpart (provider ``thread.started`` / ``turn.started`` — the
    runner's ``sbx.turn_started`` already covers the turn boundary).
    """
    t = obj.get("type")
    if t == "sbx.session_meta":
        out: dict[str, Any] = {
            "type": "session.meta",
            "provider": obj.get("provider"),
            "model": obj.get("model"),
        }
        # account_id is an internal namespace — dropped.
    elif t == "sbx.turn_started":
        out = {"type": "turn.started"}
    elif t == "sbx.turn_finished":
        out = {
            "type": "turn.finished",
            "status": obj.get("status"),
            "duration_s": obj.get("duration_s"),
            "usage": obj.get("usage"),
        }
        # exit_code is a CLI-runner internal — dropped.
    elif t == "sbx.error":
        out = {"type": "error", "message": obj.get("message")}
    elif t in ("item.started", "item.updated", "item.completed"):
        out = {"type": t, "item": obj.get("item")}
    elif t in ("turn.completed", "turn.failed", "error"):
        out = dict(obj)
    elif t == "thread.started" or t == "turn.started":
        return None
    else:
        # Unknown/future types pass through so newer providers still stream.
        out = dict(obj)
    if current_turn:
        out["n"] = current_turn
    return out
