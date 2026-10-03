"""User-scoped browser auth; operator Basic/Bearer credentials never authorize it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from control.auth_email import EmailDeliveryUnavailable
from control.auth_store import AuthStorageUnavailable, User, UserSession
from control.hosted_auth import (
    OTP_TTL_S,
    RESEND_COOLDOWN_S,
    SESSION_TTL_S,
    HostedAuthError,
    HostedAuthService,
)

COOKIE_NAME = "__Host-sbx_session"


class AuthRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            from control.api_v1.errors import V1ApiError
            from control.github_app import GitHubAppError

            try:
                response = await handler(request)
            except V1ApiError as exc:
                response = JSONResponse({"error": exc.code}, status_code=exc.status_code)
            except GitHubAppError as exc:
                response = JSONResponse({"error": exc.code}, status_code=exc.status_code)
            except RequestValidationError:
                # FastAPI's default validation response includes rejected input,
                # potentially echoing passwords, OTPs and registration tokens.
                response = JSONResponse({"error": "invalid_request"}, status_code=422)
            except HostedAuthError as exc:
                headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
                response = JSONResponse(
                    {"error": exc.code}, status_code=exc.status, headers=headers
                )
            except (EmailDeliveryUnavailable, AuthStorageUnavailable):
                response = JSONResponse({"error": "auth_unavailable"}, status_code=503)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        return safe_handler


def same_origin_json(request: Request) -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if (origin is not None and origin != expected) or request.headers.get(
        "sec-fetch-site"
    ) == "cross-site":
        raise HostedAuthError("invalid_origin", 403)
    # JSON-only mutations prevent cross-origin form submissions, including
    # login CSRF. Same-origin fetch adds this header; no permissive CORS exists.
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HostedAuthError("json_required", 415)


router = APIRouter(
    prefix="/auth",
    tags=["hosted-auth"],
    route_class=AuthRoute,
    dependencies=[Depends(same_origin_json)],
)


class AuthBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailBody(AuthBody):
    email: str = Field(min_length=3, max_length=254)


class VerifyBody(AuthBody):
    challenge_id: str = Field(min_length=1, max_length=128)
    # Shape validation belongs in verify() so malformed guesses count too.
    code: SecretStr = Field(max_length=128)


class PasswordBody(AuthBody):
    registration_token: SecretStr = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=12, max_length=128)


class LoginBody(EmailBody):
    password: SecretStr = Field(min_length=1, max_length=128)


def service(request: Request) -> HostedAuthService:
    return request.app.state.hosted_auth


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def browser_session(request: Request) -> UserSession:
    session = service(request).auth.lookup_session(request.cookies.get(COOKIE_NAME, ""))
    if session is None:
        raise HostedAuthError("unauthorized", 401)
    return session


def current_user(request: Request, session: UserSession = Depends(browser_session)) -> User:
    user = service(request).auth.get_user(session.user_id)
    if user is None:
        raise HostedAuthError("unauthorized", 401)
    return user


def _sign_in(request: Request, response: Response, token: str) -> None:
    # New random sessions prevent fixation; signing in also revokes the old
    # browser credential without affecting this user's other devices.
    auth = service(request).auth
    previous = auth.lookup_session(request.cookies.get(COOKIE_NAME, ""))
    if previous is not None:
        auth.revoke_session(previous.id, user_id=previous.user_id)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_S,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


@router.post("/register", status_code=202)
def register(body: EmailBody, request: Request) -> dict[str, Any]:
    challenge = service(request).register(body.email, ip=_ip(request))
    return {
        "challenge_id": challenge,
        "expires_in_s": OTP_TTL_S,
        "resend_after_s": RESEND_COOLDOWN_S,
    }


@router.post("/verify")
def verify(body: VerifyBody, request: Request) -> dict[str, str]:
    token = service(request).verify(
        body.challenge_id, body.code.get_secret_value(), ip=_ip(request)
    )
    return {"registration_token": token}


@router.post("/password", status_code=201)
def set_password(body: PasswordBody, request: Request, response: Response) -> dict[str, Any]:
    user, _, token = service(request).set_password(
        body.registration_token.get_secret_value(),
        body.password.get_secret_value(),
        ip=_ip(request),
    )
    _sign_in(request, response, token)
    return {"user": asdict(user)}


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    user, _, token = service(request).login(
        body.email, body.password.get_secret_value(), ip=_ip(request)
    )
    _sign_in(request, response, token)
    return {"user": asdict(user)}


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict[str, Any]:
    return {"user": asdict(user)}


@router.post("/logout", status_code=204)
def logout(request: Request) -> Response:
    # Idempotent, including expired/missing sessions.
    auth = service(request).auth
    session = auth.lookup_session(request.cookies.get(COOKIE_NAME, ""))
    if session is not None:
        auth.revoke_session(session.id, user_id=session.user_id)
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.get("", include_in_schema=False)
def auth_page() -> Response:
    response = FileResponse(Path(__file__).with_name("hosted_auth.html"), media_type="text/html")
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


@router.get("/app.js", include_in_schema=False)
def auth_script() -> Response:
    return FileResponse(Path(__file__).with_name("hosted_auth.js"), media_type="text/javascript")
