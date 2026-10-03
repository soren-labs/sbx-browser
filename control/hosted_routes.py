"""Hosted user surfaces, sharing browser/API-key user resolution."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import Field, SecretStr

from control.api_v1.deps import agents_key
from control.hosted_auth import HostedAuthError
from control.hosted_auth_routes import AuthBody, AuthRoute, same_origin_json
from control.ports import ApiKey

router = APIRouter(
    prefix="/hosted",
    tags=["hosted"],
    route_class=AuthRoute,
    dependencies=[Depends(same_origin_json)],
)


def user_id(key: ApiKey = Depends(agents_key)) -> str:
    owner = getattr(key, "user_id", None)
    if owner is None:
        raise HostedAuthError("user_authentication_required", 403)
    return owner


class ModalTokenBody(AuthBody):
    token_id: SecretStr = Field(min_length=1, max_length=1024)
    token_secret: SecretStr = Field(min_length=1, max_length=4096)


class CallbackBody(AuthBody):
    state: SecretStr = Field(min_length=1, max_length=256)
    code: SecretStr = Field(min_length=1, max_length=4096)


class StateBody(AuthBody):
    state: SecretStr = Field(min_length=1, max_length=256)


@router.get("/connections/modal")
def modal_status(request: Request, owner: str = Depends(user_id)) -> dict[str, Any]:
    service = request.app.state.modal_connections
    record = service.store.get(owner, "modal")
    return {
        "connection": record.public() if record else None,
        "configured": service.provider.configured,
        "mock": service.provider.mock,
    }


@router.post("/connections/modal")
def connect_modal(body: ModalTokenBody, request: Request, owner: str = Depends(user_id)):
    service = request.app.state.modal_connections
    if not service.provider.configured:
        raise HostedAuthError("modal_not_configured", 503)
    credentials = {
        "token_id": body.token_id.get_secret_value(),
        "token_secret": body.token_secret.get_secret_value(),
    }
    return {"connection": service.store.connect(owner, "modal", credentials).public()}


@router.post("/connections/modal/authorize")
def authorize_modal(request: Request, owner: str = Depends(user_id)):
    return request.app.state.modal_connections.authorize(owner)


@router.post("/connections/modal/callback")
def callback_modal(body: CallbackBody, request: Request, owner: str = Depends(user_id)):
    record = request.app.state.modal_connections.callback(
        owner, body.state.get_secret_value(), body.code.get_secret_value()
    )
    return {"connection": record.public()}


@router.post("/connections/modal/mock-approve")
def mock_modal_approve(body: StateBody, request: Request, owner: str = Depends(user_id)):
    service = request.app.state.modal_connections
    if not service.provider.mock:
        raise HostedAuthError("not_found", 404)
    record = service.callback(owner, body.state.get_secret_value(), f"mock:{owner}")
    return {"connection": record.public()}


@router.post("/connections/modal/provision")
def provision_modal(request: Request, owner: str = Depends(user_id)):
    return {"connection": request.app.state.modal_connections.provision(owner)}


class GitHubCallbackBody(StateBody):
    installation_id: int = Field(gt=0)


@router.get("/connections/github")
def github_status(request: Request, owner: str = Depends(user_id)):
    return request.app.state.github_connections.for_user(owner).status()


@router.post("/connections/github/authorize")
def github_authorize(request: Request, owner: str = Depends(user_id)):
    return request.app.state.github_connections.for_user(owner).begin_authorization()


@router.post("/connections/github/callback")
def github_callback(body: GitHubCallbackBody, request: Request, owner: str = Depends(user_id)):
    service = request.app.state.github_connections.for_user(owner)
    record = service.complete_authorization(body.installation_id, body.state.get_secret_value())
    return {"installation": record.public()}


@router.post("/connections/github/mock-approve")
def github_mock_approve(body: StateBody, request: Request, owner: str = Depends(user_id)):
    service = request.app.state.github_connections.for_user(owner)
    if not service.mock:
        raise HostedAuthError("not_found", 404)
    record = service.complete_authorization(
        service._client.installation_id, body.state.get_secret_value()
    )
    return {"installation": record.public()}


@router.get("/repositories")
def github_repositories(request: Request, owner: str = Depends(user_id)):
    service = request.app.state.github_connections.for_user(owner)
    return {
        "repositories": [
            {
                "repo": f"https://github.com/{repo}",
                "name": repo,
                "installation_id": installation.installation_id,
            }
            for installation in service._store.list()
            if not installation.suspended
            for repo in installation.repositories
        ]
    }


@router.delete("/connections/github/installations/{installation_id}")
def github_disconnect(installation_id: int, request: Request, owner: str = Depends(user_id)):
    service = request.app.state.github_connections.for_user(owner)
    if service._store.get(installation_id) is None:
        raise HostedAuthError("not_found", 404)
    return service.revoke(installation_id)


@router.get("/connections/codex")
def codex_status(request: Request, owner: str = Depends(user_id)):
    broker = request.app.state.codex_broker
    record = broker.store.get(owner, "codex")
    return {
        "connection": record.public() if record else None,
        "configured": broker.provider.configured,
        "mock": broker.provider.mock,
    }


@router.post("/connections/codex/authorize")
def codex_authorize(request: Request, owner: str = Depends(user_id)):
    return request.app.state.codex_broker.authorize(owner)


@router.post("/connections/codex/callback")
def codex_callback(body: CallbackBody, request: Request, owner: str = Depends(user_id)):
    record = request.app.state.codex_broker.callback(
        owner, body.state.get_secret_value(), body.code.get_secret_value()
    )
    return {"connection": record.public()}


@router.post("/connections/codex/mock-approve")
def codex_mock_approve(body: StateBody, request: Request, owner: str = Depends(user_id)):
    broker = request.app.state.codex_broker
    if not broker.provider.mock:
        raise HostedAuthError("not_found", 404)
    return {
        "connection": broker.callback(
            owner, body.state.get_secret_value(), f"mock:{owner}"
        ).public()
    }


@router.post("/connections/codex/refresh")
def codex_refresh(request: Request, owner: str = Depends(user_id)):
    broker = request.app.state.codex_broker
    broker.lease(owner)
    return {"connection": broker.store.get(owner, "codex").public()}


@router.delete("/connections/codex")
def codex_disable(request: Request, owner: str = Depends(user_id)):
    return {"connection": request.app.state.codex_broker.disable(owner).public()}


@router.post("/sessions/{session_id}/connect")
def sandbox_connect(session_id: str, request: Request, owner: str = Depends(user_id)):
    task = request.app.state.task_store.get(session_id)
    if task is None or task.owner != owner:
        raise HostedAuthError("not_found", 404)
    agent = request.app.state.plane.store.get(task.agent_id) if task.agent_id else None
    if agent is None or agent.owner != owner or agent.handle() is None:
        raise HostedAuthError("sandbox_unavailable", 409)
    return request.app.state.plane.backend.connect(agent.handle())
