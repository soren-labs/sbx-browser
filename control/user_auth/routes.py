"""Same-origin browser account API. Session secrets only travel in cookies."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from control.api_v1.errors import V1ApiError, V1Route
from control.user_auth.service import AuthService


@dataclass(frozen=True)
class AuthConfig:
    secure: bool = True
    ttl: int = 604800
    origin: str | None = None

    @property
    def cookie(self) -> str:
        return "__Host-sbx_session" if self.secure else "sbx_session"

    @classmethod
    def from_env(cls) -> AuthConfig:
        secure = os.environ.get("SBX_AUTH_COOKIE_SECURE", "true").lower()
        if secure not in {"true", "false"}:
            raise ValueError("SBX_AUTH_COOKIE_SECURE must be true or false")
        if secure == "false" and os.environ.get("SBX_BACKEND") == "modal":
            raise ValueError("Modal requires secure authentication cookies")
        ttl = int(os.environ.get("SBX_AUTH_SESSION_TTL_S", "604800"))
        if not 300 <= ttl <= 604800:
            raise ValueError("SBX_AUTH_SESSION_TTL_S must be between 300 and 604800")
        origin = os.environ.get("SBX_AUTH_ORIGIN") or None
        if origin:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or (secure == "true" and parsed.scheme != "https")
            ):
                raise ValueError("SBX_AUTH_ORIGIN must be an exact public origin")
            origin = origin.rstrip("/")
        return cls(secure == "true", ttl, origin)


def get_auth(request: Request) -> AuthService:
    return request.app.state.user_auth


def same_origin(request: Request) -> None:
    config = request.app.state.auth_config
    expected = config.origin or str(request.base_url).rstrip("/")
    # No suffix/substring matching; do not trust forwarded headers here.
    if request.headers.get("origin") != expected or request.headers.get("sec-fetch-site") in {
        "cross-site",
        "same-site",
    }:
        raise V1ApiError(403, "forbidden", "same-origin request required")


def browser_session(request: Request) -> dict:
    config = request.app.state.auth_config
    row = get_auth(request).session(request.cookies.get(config.cookie))
    if not row:
        raise V1ApiError(401, "unauthorized", "missing or invalid session")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        same_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(csrf.encode(), row["csrf"].encode()):
            raise V1ApiError(403, "forbidden", "invalid CSRF token")
    return row


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    # SecretStr hides values in repr; V1Route validation only exposes field locations.
    password: SecretStr = Field(min_length=1, max_length=256)


class CreateKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="", max_length=100, pattern=r"^[^\x00-\x1f\x7f]*$")


router = APIRouter(prefix="/auth", tags=["user-auth"], route_class=V1Route)


def session_body(service: AuthService, row: dict) -> dict:
    user = service.user(row["owner"])
    return {
        "user": {"id": user["owner"], "email": user["email"], "created_at": user["created_at"]},
        "session": {"expires_at": row["expires"], "csrf_token": row["csrf"]},
    }


def set_session(request: Request, response: Response, user: dict) -> dict:
    service = get_auth(request)
    config = request.app.state.auth_config
    old_token = request.cookies.get(config.cookie)
    old_row = service.session(old_token)
    # Rotate on every successful login/registration, revoke any prior browser session.
    if old_row:
        service.revoke_session(old_token, old_row)
    token, row = service.new_session(user, config.ttl)
    response.set_cookie(
        config.cookie,
        token,
        max_age=config.ttl,
        httponly=True,
        secure=config.secure,
        samesite="lax",
        path="/",
    )
    return session_body(service, row)


def limit_ip(request: Request, category: str, maximum: int) -> None:
    # Use the trusted ASGI peer, never an arbitrary X-Forwarded-For header.
    peer = request.client.host if request.client else "unknown"
    get_auth(request).limit(category, peer, maximum)


@router.post("/register", status_code=201)
def register(
    body: Credentials, request: Request, response: Response, _origin: None = Depends(same_origin)
) -> dict:
    limit_ip(request, "register-ip", 10)
    user = get_auth(request).register(body.email, body.password.get_secret_value())
    return set_session(request, response, user)


@router.post("/login")
def login(
    body: Credentials, request: Request, response: Response, _origin: None = Depends(same_origin)
) -> dict:
    limit_ip(request, "login-ip", 30)
    user = get_auth(request).login(body.email, body.password.get_secret_value())
    return set_session(request, response, user)


@router.get("/me")
def me(request: Request, session: dict = Depends(browser_session)) -> dict:
    return session_body(get_auth(request), session)


@router.post("/logout", status_code=204)
def logout(request: Request, session: dict = Depends(browser_session)) -> Response:
    config = request.app.state.auth_config
    get_auth(request).revoke_session(request.cookies[config.cookie], session)
    response = Response(status_code=204)
    response.delete_cookie(
        config.cookie, path="/", secure=config.secure, httponly=True, samesite="lax"
    )
    return response


@router.post("/session/rotate")
def rotate(request: Request, response: Response, session: dict = Depends(browser_session)) -> dict:
    service = get_auth(request)
    token = request.cookies[request.app.state.auth_config.cookie]
    # Claim once across processes. If issuing a replacement fails, re-login;
    # an old session must never be resurrected by a competing request.
    if not service.revoke_session(token, session):
        raise V1ApiError(401, "unauthorized", "missing or invalid session")
    return set_session(request, response, service.user(session["owner"]))


@router.get("/api-keys")
def list_keys(request: Request, session: dict = Depends(browser_session)) -> dict:
    return {"api_keys": get_auth(request).keys(session["owner"])}


@router.post("/api-keys", status_code=201)
def create_key(body: CreateKey, request: Request, session: dict = Depends(browser_session)) -> dict:
    service = get_auth(request)
    service.limit("create-key", session["owner"], 20)
    row, token = service.create_key(session["owner"], body.label)
    return {**service.key_public(row), "key": token}


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_key(key_id: str, request: Request, session: dict = Depends(browser_session)) -> Response:
    if not get_auth(request).revoke_key(session["owner"], key_id):
        raise V1ApiError(404, "not_found", "api key not found")
    return Response(status_code=204)
