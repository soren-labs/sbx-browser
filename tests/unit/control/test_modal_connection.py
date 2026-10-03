"""User-scoped onboarding with durable encrypted connections and provider failures."""

import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from control.connections import ConnectionStore, SecretVault
from control.hosted_auth import HostedAuthError
from control.modal_connection import RUNTIME_VERSION, FakeModalProvider, ModalConnectionService
from fastapi.testclient import TestClient


@pytest.fixture
def setup(tmp_path):
    now = [1700000000.0]
    auth = AuthStore(AuthDatabase(path=tmp_path / "auth.db"), clock=lambda: now[0])
    users = [auth.create_user() for _ in range(2)]
    vault = SecretVault(secrets.token_bytes(32))
    store = ConnectionStore(auth, vault)
    provider = FakeModalProvider()
    return auth, users, vault, store, provider, now


def credentials():
    return {"token_id": "REDACTED", "token_secret": secrets.token_urlsafe(32)}


def test_manual_onboarding_encrypted_idempotent_and_reconstructed(setup):
    auth, users, vault, store, provider, _ = setup
    creds = credentials()
    connection = store.connect(users[0].id, "modal", creds)
    assert creds["token_secret"] not in repr(connection)
    assert creds["token_secret"].encode() not in auth.database._path.read_bytes()
    service = ModalConnectionService(store, provider)
    ready = service.provision(users[0].id)
    assert ready["state"] == "ready"
    assert ready["metadata"]["runtime_version"] == RUNTIME_VERSION
    assert ready["metadata"]["progress"] == ["verify", "namespace", "image", "smoke"]
    assert "credential_cipher" not in ready
    restored = ConnectionStore(AuthStore(AuthDatabase(path=auth.database._path)), vault)
    assert restored.credentials(restored.get(users[0].id, "modal")) == creds
    assert ModalConnectionService(restored, provider).provision(users[0].id) == ready
    assert len(provider.calls) == 4
    assert restored.get(users[1].id, "modal") is None
    other = store.connect(users[1].id, "modal", credentials())
    assert service.provision(users[1].id)["metadata"]["workspace"] != ready["metadata"]["workspace"]
    with pytest.raises(HostedAuthError, match="connection_unavailable"):
        vault.open(connection.credential_cipher, context=other.context)


@pytest.mark.parametrize("step", ["verify", "namespace", "image", "smoke"])
def test_provider_failure_is_safe_and_reconciles(setup, step):
    _, users, _, store, provider, _ = setup
    store.connect(users[0].id, "modal", credentials())
    provider.fail_step = step
    service = ModalConnectionService(store, provider)
    with pytest.raises(HostedAuthError, match="modal_provisioning_failed"):
        service.provision(users[0].id)
    failed = store.get(users[0].id, "modal")
    assert failed.state == "failed" and "lease_until" not in failed.metadata
    provider.fail_step = None
    assert service.provision(users[0].id)["state"] == "ready"


def test_oauth_state_owner_expiry_replay_and_concurrent_consumption(setup):
    _, users, _, store, provider, now = setup
    service = ModalConnectionService(store, provider)
    state = service.authorize(users[0].id)["state"]
    with pytest.raises(HostedAuthError, match="invalid_authorization"):
        service.callback(users[1].id, state, f"mock:{users[1].id}")
    service.callback(users[0].id, state, f"mock:{users[0].id}")
    with pytest.raises(HostedAuthError, match="invalid_authorization"):
        service.callback(users[0].id, state, f"mock:{users[0].id}")
    state = service.authorize(users[0].id)["state"]
    now[0] += 600
    with pytest.raises(HostedAuthError, match="invalid_authorization"):
        service.callback(users[0].id, state, f"mock:{users[0].id}")
    state = service.authorize(users[0].id)["state"]

    def consume(_):
        try:
            store.consume_authorization(users[0].id, "modal", state)
            return True
        except HostedAuthError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(consume, range(4))) == 1


def test_provision_lease_and_reconnect_cas(setup):
    _, users, _, store, provider, now = setup
    record = store.connect(users[0].id, "modal", credentials())
    record.state = "provisioning"
    record.metadata["lease_until"] = now[0] + 300
    store.save(record)
    service = ModalConnectionService(store, provider)
    with pytest.raises(HostedAuthError, match="provisioning_in_progress"):
        service.provision(users[0].id)
    now[0] += 300
    assert service.provision(users[0].id)["state"] == "ready"
    store.connect(users[0].id, "modal", credentials())
    with pytest.raises(HostedAuthError, match="connection_changed"):
        store.save(record)


def test_api_login_connect_ready_and_other_user_isolation(setup):
    auth, users, vault, _, provider, _ = setup
    app = create_app(
        auth_store=auth,
        hosted=True,
        state_backend="postgres",
        connection_vault=vault,
        modal_provider=provider,
    )
    keys = PersistentApiKeyStore(auth)
    headers = [{"Authorization": f"Bearer {keys.create(user_id=u.id)[1]}"} for u in users]
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/hosted/connections/modal").status_code == 401
        creds = credentials()
        response = client.post("/hosted/connections/modal", json=creds, headers=headers[0])
        assert response.status_code == 200
        assert creds["token_secret"] not in response.text
        response = client.post("/hosted/connections/modal/provision", json={}, headers=headers[0])
        assert response.json()["connection"]["state"] == "ready"
        assert (
            client.get("/hosted/connections/modal", headers=headers[1]).json()["connection"] is None
        )
        assert (
            client.post(
                "/hosted/connections/modal/provision", json={}, headers=headers[1]
            ).status_code
            == 409
        )
        authz = client.post(
            "/hosted/connections/modal/authorize", json={}, headers=headers[1]
        ).json()
        assert (
            client.post(
                "/hosted/connections/modal/mock-approve",
                json={"state": authz["state"]},
                headers=headers[1],
            ).status_code
            == 200
        )
        assert (
            client.post("/hosted/connections/modal/provision", json={}, headers=headers[1]).json()[
                "connection"
            ]["state"]
            == "ready"
        )


def test_missing_configuration_fails_explicitly(setup):
    auth, users, _, _, _, _ = setup
    app = create_app(auth_store=auth, hosted=True, state_backend="postgres")
    token = PersistentApiKeyStore(auth).create(user_id=users[0].id)[1]
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        assert not client.get("/hosted/connections/modal", headers=headers).json()["configured"]
        assert (
            client.post("/hosted/connections/modal/authorize", json={}, headers=headers).status_code
            == 503
        )
        assert (
            client.post(
                "/hosted/connections/modal", json=credentials(), headers=headers
            ).status_code
            == 503
        )
