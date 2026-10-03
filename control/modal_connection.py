"""BYO Modal provisioning. Every provider call carries a user-owned context."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from control.connections import ConnectionStore
from control.hosted_auth import HostedAuthError, _write

RUNTIME_VERSION = "sbx-runtime-hosted-codex-v1"


@dataclass(frozen=True)
class ModalContext:
    user_id: str
    connection_id: str
    credentials: dict[str, str] = field(repr=False)


class ModalProvider(Protocol):
    configured: bool
    mock: bool

    def verify_workspace(self, context: ModalContext) -> str: ...
    def ensure_namespace(self, context: ModalContext, workspace: str) -> str: ...
    def publish_runtime(self, context: ModalContext, namespace: str, version: str) -> str: ...
    def smoke(self, context: ModalContext, image: str) -> None: ...
    def authorization_url(self, state: str) -> str: ...
    def exchange(self, user_id: str, code: str) -> dict[str, str]: ...


class UnconfiguredModalProvider:
    configured = False
    mock = False

    def __getattr__(self, _):
        def unavailable(*args, **kwargs):
            raise HostedAuthError("modal_not_configured", 503)

        return unavailable


class FakeModalProvider:
    """Deterministic fake workspace; state is reconstructible from user/id/version."""

    configured = True
    mock = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_step: str | None = None

    def _call(self, step: str, context: ModalContext) -> str:
        self.calls.append((step, context.user_id))
        if self.fail_step == step:
            raise RuntimeError("fake workspace unavailable")
        return hashlib.sha256(context.user_id.encode()).hexdigest()[:16]

    def verify_workspace(self, context: ModalContext) -> str:
        if not context.credentials.get("token_id") or not context.credentials.get("token_secret"):
            raise HostedAuthError("invalid_modal_credentials", 400)
        return f"fake-workspace-{self._call('verify', context)}"

    def ensure_namespace(self, context: ModalContext, workspace: str) -> str:
        self._call("namespace", context)
        return f"{workspace}/sbx-compute"

    def publish_runtime(self, context: ModalContext, namespace: str, version: str) -> str:
        identity = self._call("image", context)
        return f"fake-image-{identity}-{hashlib.sha256(version.encode()).hexdigest()[:8]}"

    def smoke(self, context: ModalContext, image: str) -> None:
        identity = self._call("smoke", context)
        if not image.startswith(f"fake-image-{identity}-"):
            raise HostedAuthError("modal_owner_mismatch", 403)

    def authorization_url(self, state: str) -> str:
        return "/integrations?modal_authorization=mock"

    def exchange(self, user_id: str, code: str) -> dict[str, str]:
        # Fake authorization codes are user-bound. Nothing here imitates a
        # real Modal token or calls Modal's ambient client/environment.
        if code != f"mock:{user_id}":
            raise HostedAuthError("invalid_authorization", 400)
        return {
            "token_id": f"mock:{user_id}",
            "token_secret": hashlib.sha256(code.encode()).hexdigest(),
        }


class ModalConnectionService:
    def __init__(self, store: ConnectionStore, provider: ModalProvider) -> None:
        self.store, self.provider = store, provider

    def authorize(self, user_id: str) -> dict[str, Any]:
        if not self.provider.configured:
            raise HostedAuthError("modal_oauth_not_configured", 503)
        state = self.store.begin_authorization(user_id, "modal")
        return {
            "state": state,
            "authorization_url": self.provider.authorization_url(state),
            "mock": self.provider.mock,
        }

    def callback(self, user_id: str, state: str, code: str):
        self.store.consume_authorization(user_id, "modal", state)
        try:
            credentials = self.provider.exchange(user_id, code)
        except HostedAuthError:
            raise
        except Exception:
            raise HostedAuthError("modal_authorization_failed", 503) from None
        return self.store.connect(user_id, "modal", credentials)

    def provision(self, user_id: str) -> dict[str, Any]:
        auth = self.store.auth
        with _write(auth, f"connection:{user_id}:modal") as conn:
            record = self.store.get(user_id, "modal", conn=conn)
            if record is None or record.state == "disabled":
                raise HostedAuthError("modal_connection_required", 409)
            if (
                record.state == "ready"
                and record.metadata.get("runtime_version") == RUNTIME_VERSION
            ):
                return record.public()
            if record.metadata.get("lease_until", 0) > auth.clock():
                raise HostedAuthError("provisioning_in_progress", 409)
            record.state = "provisioning"
            record.metadata.update({"lease_until": auth.clock() + 300, "progress": []})
            self.store.save(record, conn=conn)
        try:
            context = ModalContext(user_id, record.id, self.store.credentials(record))
            for step in ("verify", "namespace", "image", "smoke"):
                record.metadata["step"] = step
                self.store.save(record)
                if step == "verify":
                    record.metadata["workspace"] = self.provider.verify_workspace(context)
                elif step == "namespace":
                    record.metadata["namespace"] = self.provider.ensure_namespace(
                        context, record.metadata["workspace"]
                    )
                elif step == "image":
                    record.metadata["image"] = self.provider.publish_runtime(
                        context, record.metadata["namespace"], RUNTIME_VERSION
                    )
                    record.metadata["runtime_version"] = RUNTIME_VERSION
                else:
                    self.provider.smoke(context, record.metadata["image"])
                record.metadata["progress"].append(step)
                self.store.save(record)
        except Exception:
            record.state = "failed"
            record.metadata["error"] = "modal_provisioning_failed"
            record.metadata.pop("lease_until", None)
            self.store.save(record)
            raise HostedAuthError("modal_provisioning_failed", 503) from None
        record.state = "ready"
        record.metadata.pop("lease_until", None)
        record.metadata.pop("error", None)
        self.store.save(record)
        return record.public()
