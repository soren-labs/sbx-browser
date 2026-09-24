"""Every (method, path) in docs/contracts/api-v1.yaml is served by the app."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "api-v1.yaml"


def _normalize(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _contract_ops() -> set[tuple[str, str]]:
    data = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    ops: set[tuple[str, str]] = set()
    for path, item in data["paths"].items():
        for method, spec in item.items():
            if not isinstance(spec, dict):
                continue
            ops.add((method.lower(), _normalize(path)))
    return ops


def _app_ops(app) -> set[tuple[str, str]]:
    ops: set[tuple[str, str]] = set()
    for path, item in app.openapi()["paths"].items():
        if not path.startswith("/v1"):
            continue
        for method in item:
            if method in ("get", "post", "put", "delete", "patch"):
                ops.add((method, _normalize(path)))
    return ops


def test_contract_routes_all_registered(v1_env) -> None:
    contract_ops = _contract_ops()
    assert len(contract_ops) == 39  # 32 paths, some with two methods
    app_ops = _app_ops(v1_env.app)
    missing = contract_ops - app_ops
    assert not missing, f"contract routes not implemented: {sorted(missing)}"


@pytest.mark.parametrize(
    "method,path",
    sorted(_contract_ops()),
)
def test_every_route_rejects_missing_auth(client: TestClient, method: str, path: str) -> None:
    concrete = re.sub(r"\{[^}]+\}", "missing", path)
    resp = getattr(client, method)(concrete)
    assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"


def test_error_body_shape(v1_env) -> None:
    """Canonical body {error:{code,message,retry_after?}} for /v1 errors."""
    client = TestClient(v1_env.app)
    resp = client.get("/v1/agents")
    body = resp.json()
    assert set(body) == {"error"}
    assert {"code", "message"} <= set(body["error"])
