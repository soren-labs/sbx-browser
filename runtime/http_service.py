"""Sandbox read-only SSE data plane. No business database or provider credentials."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import jwt
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from runtime.session_events import normalize, track_turn


def create_runtime_app(
    *,
    root: Path,
    key: str,
    sandbox_id: str,
    owner: str,
    agent_id: str,
    origins: list[str],
    mock=False,
    clock=time.time,
) -> FastAPI:
    app = FastAPI(title="SBX sandbox live runtime")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+" if mock else None,
        allow_methods=["GET"],
        allow_headers=["Authorization", "Last-Event-ID"],
        allow_credentials=False,
    )

    def grant(authorization: str):
        scheme, _, token = authorization.partition(" ")
        try:
            if scheme.lower() != "bearer":
                raise ValueError()
            claims = jwt.decode(
                token,
                key,
                algorithms=["HS256"],
                audience=f"sbx-runtime:{sandbox_id}",
                options={
                    "require": ["exp", "iat", "sub", "sid", "scope"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
            if (
                claims["sub"] != owner
                or claims["sid"] != agent_id
                or claims["scope"] != "events"
                or not claims["iat"] - 5 <= clock() < claims["exp"]
                or not 0 < claims["exp"] - claims["iat"] <= 60
            ):
                raise ValueError()
            return claims
        except Exception:
            raise HTTPException(401, "invalid runtime grant") from None

    @app.get("/sessions/{sid}/events")
    async def events(
        sid: str, authorization: str = Header(default=""), last_event_id: str = Header(default="0")
    ):
        claims = grant(authorization)
        if sid != agent_id:
            raise HTTPException(404, "session not found")
        try:
            after = max(0, int(last_event_id))
        except ValueError:
            raise HTTPException(400, "invalid event cursor") from None

        async def stream():
            current_turn, line_id, pending = 0, 0, ""
            path = root / "events.jsonl"
            # Open only the fixed events file. Cursors count canonical lines
            # exactly like relayed V2 SSE, so fallback/resume cannot duplicate.
            file: Any = None
            try:
                while clock() < claims["exp"]:
                    if file is None and path.is_file():
                        file = path.open(encoding="utf-8", errors="replace")
                    chunk = file.read(65536) if file else ""
                    if chunk:
                        pending += chunk
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            line_id += 1
                            try:
                                obj = json.loads(line)
                                current_turn = track_turn(obj, current_turn)
                                event = normalize(obj, current_turn)
                            except (ValueError, TypeError, AttributeError):
                                continue
                            if line_id > after and event is not None:
                                yield f"id: {line_id}\ndata: {json.dumps(event)}\n\n"
                    else:
                        yield ": keepalive\n\n"
                        await asyncio.sleep(0.1)
            finally:
                if file:
                    file.close()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.get("/capabilities")
    def capabilities():
        return {"transport": "sse", "terminal": False, "websocket": False}

    return app


def from_env():
    import os

    return create_runtime_app(
        root=Path(os.environ["SBX_WORK"]),
        key=os.environ["SBX_RUNTIME_CONNECT_KEY"],
        sandbox_id=os.environ["SBX_RUNTIME_SANDBOX_ID"],
        owner=os.environ["SBX_RUNTIME_OWNER"],
        agent_id=os.environ["SBX_RUNTIME_AGENT_ID"],
        origins=os.environ.get("SBX_BROWSER_ORIGINS", "").split(","),
    )
