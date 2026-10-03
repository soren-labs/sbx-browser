"""Modal App entry: ASGI + reaper cron.

Tests import ``control.app`` only. This module imports ``modal`` and must not
be imported by unit/integration tests (no Modal connection).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import modal

from control.app import create_app
from control.config import (
    MODAL_APP_NAME,
    app_secret_names,
    control_warmth_config,
    remote_env_overlay,
)
from control.reaper import _bounded_call, sweep_plane

_APP_NAME = os.environ.get("SBX_MODAL_APP_NAME", MODAL_APP_NAME)
app = modal.App(_APP_NAME)

# Secret names resolve at deploy time so a parallel deployment (the bootstrap
# config's ``secrets.*`` values) mounts its own Secrets instead of sharing the
# contract defaults. The shared Codex Secret mounts only when ``codex`` is a
# selected provider (``SBX_PROVIDERS``) — an unselected provider's credential
# must never block a deploy (SOR-115).
_secrets = [modal.Secret.from_name(name) for name in app_secret_names()]

# SOR-138: the entrypoint mount only ships the function's own package
# (``control``). ``control.app`` transitively imports ``runtime.runner.*``
# (contract/run_store/api_v1) and ``control/backends/modal.py`` lazily
# imports ``runtime.image``, so ``runtime`` must be part of the image's
# source mount for a fresh deploy to start without a detached fixup.
CONTROL_IMAGE = (
    modal.Image.debian_slim(python_version="3.12")
    # SOR-223: the Task API's repo probe falls back to ``git ls-remote`` for
    # non-github.com remotes (github.com goes through the REST API).
    .apt_install("git")
    .pip_install(
        "fastapi",
        "httpx",
        "pydantic",
        "uvicorn",
        "anyio",
        "starlette",
        # GitHub App JWT signing (control.github_app) needs PyJWT's crypto
        # extra for RS256 — same constraint as pyproject.toml.
        "pyjwt[crypto]>=2.10.0",
        "psycopg[binary]>=3.2.0,<4",
        "argon2-cffi>=25.1.0,<26",
    )
    # SOR-211 + SOR-266: ship the V2 Session Console build (``console/dist``)
    # with the control plane — the deployed app's URL serves it at "/" on
    # the same origin as ``/v1``. The legacy ``web/`` static UI no longer
    # ships in the image at all. Copied into the image (not mounted) so the
    # deploy needs no runtime mount resolution; a missing ``console/dist``
    # fails the deploy loudly instead of falling back to the legacy UI.
    # Build steps must precede deferred ``add_local_*`` mounts — a
    # ``copy=True`` layer after ``add_local_python_source`` raises
    # InvalidError at deploy time (SOR-219 acceptance). Dev checkouts carry
    # node_modules / playwright output under console/ — never bake them in.
    .add_local_dir(
        Path(
            os.environ.get("SBX_CONSOLE_DIST")
            or Path(__file__).resolve().parents[1] / "console" / "dist"
        ).resolve(),
        remote_path="/root/console",
        copy=True,
        ignore=lambda p: (
            "node_modules" in p.parts or "test-results" in p.parts or "playwright-report" in p.parts
        ),
    )
    .add_local_file(
        Path(__file__).with_name("hosted_auth.html"),
        remote_path="/root/control/hosted_auth.html",
        copy=True,
    )
    .add_local_file(
        Path(__file__).with_name("hosted_auth.js"),
        remote_path="/root/control/hosted_auth.js",
        copy=True,
    )
    .add_local_python_source("runtime")
)

# Deploy-time names/tunables the remote functions must see (dict/secret/image
# names, account seeding). ``remote_env_overlay`` is an allowlist — credential
# material only ever arrives through the Secret mounts above.
_REMOTE_ENV = {
    **remote_env_overlay(app_name=_APP_NAME),
    # SOR-266: the React console build is the only product UI at "/"; the
    # env is explicit so a missing /root/console fails startup loudly
    # rather than silently falling back to the legacy web/ directory.
    "SBX_CONSOLE_DIR": "/root/console",
}

# SOR-203: web-function autoscaler warmth, resolved at deploy time from
# SBX_CONTROL_SCALEDOWN_WINDOW_S / SBX_CONTROL_MIN_CONTAINERS /
# SBX_CONTROL_BUFFER_CONTAINERS (defaults: 300s warm tail, no always-on
# containers). Deliberately *not* applied to ``reap_cron`` — a 5-minute
# cron's own startup latency is irrelevant and warming it would be pure
# cost. The Agent Sandbox lifecycle (``Sandbox.create`` timers) is
# untouched by these knobs.
_WARMTH = control_warmth_config()


@app.function(
    image=CONTROL_IMAGE,
    secrets=_secrets,
    env=_REMOTE_ENV,
    scaledown_window=_WARMTH.scaledown_window_s,
    min_containers=_WARMTH.min_containers or None,
    buffer_containers=_WARMTH.buffer_containers or None,
)
# Long-lived SSE connections are inputs too. Leave burst headroom for short
# API requests while the autoscaler brings up another container; twenty
# streams must not occupy every input and force unrelated reads to cold-start.
@modal.concurrent(max_inputs=64, target_inputs=32)
@modal.asgi_app()
def fastapi_app():
    os.environ.setdefault("SBX_BACKEND", "modal")
    return create_app()


# Whole-cron wall for ``create_app`` — the app build does a dozen lazy
# Dict/client resolutions; one stalled RPC must not eat the tick.
_BUILD_BOUND_S = 90
# Ops heartbeat Dict: the durable proof that the cron actually ran —
# ``sbx-control-ops["reap:last"]`` carries the tick's start/finish and
# action counts so invocation is verifiable from outside Modal's logs
# (SOR-271 round-4: "verify the reaper is actually invoked").
_OPS_DICT_NAME = f"{_APP_NAME}-ops"


def _ops_heartbeat(key: str, payload: dict) -> None:
    """Best-effort durable marker; a failed write never kills the tick."""

    def _put() -> None:
        ops = modal.Dict.from_name(_OPS_DICT_NAME, create_if_missing=True)
        ops[key] = payload

    _bounded_call(lambda: _put() or True, 15)


@app.function(
    image=CONTROL_IMAGE,
    schedule=modal.Cron("*/5 * * * *"),
    secrets=_secrets,
    env=_REMOTE_ENV,
)
def reap_cron() -> None:
    os.environ.setdefault("SBX_BACKEND", "modal")
    started_at = datetime.now(UTC)
    _ops_heartbeat(
        "reap:last",
        {"started_at": started_at.isoformat(), "finished_at": None},
    )
    web = _bounded_call(create_app, _BUILD_BOUND_S)
    if web is None:
        # App build threw or never returned — log + mark, never wedge.
        print(f"[reap] create_app exceeded {_BUILD_BOUND_S}s — tick skipped")
        _ops_heartbeat(
            "reap:last",
            {
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "error": "create_app_bounded",
            },
        )
        return
    plane = web.state.plane
    # SOR-139: this plane owns no turn watchers, so every ``running`` record
    # is watcher-less — settle those with written turn evidence into
    # FINISHED + idle before the reaper judges staleness. The reconcile
    # phase must never starve the reaper: a throwing tick here killed every
    # sweep before it ran, which is how >10min zombies survived every cron
    # tick (SOR-271 round-3 — fix the invocation path, not just resilience).
    # SOR-271 round-4: ``sweep_plane`` time-bounds BOTH phases — a remote
    # call that never returns (wedged sandbox read, stalled Dict RPC)
    # degraded every prior tick identically and silently.
    summary = sweep_plane(
        plane,
        v1_state=getattr(web.state, "v1_state", None),
        # SOR-63: expired cooldowns return accounts to rotation; absent on
        # app.state until the registry is wired (P2-D bootstrap).
        account_registry=getattr(web.state, "account_registry", None),
        log=print,
    )
    _ops_heartbeat(
        "reap:last",
        {
            "started_at": summary["now"].isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "settled_turns": len(summary["settled_turns"]),
            "action_kinds": summary["action_kinds"],
            "elapsed_s": summary["elapsed_s"],
            "app": _APP_NAME,
            "scan": summary["scan"],
            # SOR-271 round-5 observability: how much of the plane the
            # sweep actually enumerated + what it skipped, so "reaper ran
            # but saw nothing" is visible instead of silently identical
            # to "reaper never ran".
            "records_seen": summary["records_seen"],
            "handles_seen": summary["handles_seen"],
            "skipped": summary["skipped"],
        },
    )
