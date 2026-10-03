"""Rotating grants, durable claims, three contenders and ambiguous refresh recovery."""

import secrets
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from control.auth_store import AuthDatabase, AuthStore
from control.codex_broker import CodexBroker, FakeCodexProvider, ProviderUnauthorized
from control.connections import ConnectionStore, SecretVault
from control.hosted_accounts import HostedAccounts, HostedScheduling
from control.hosted_auth import HostedAuthError
from control.scheduler import ScheduleRefused
from control.store import InMemoryStore


@pytest.fixture
def setup(tmp_path):
    now = [1700000000.0]
    auth = AuthStore(AuthDatabase(path=tmp_path / "auth.db"), clock=lambda: now[0])
    user = auth.create_user()
    vault = SecretVault(secrets.token_bytes(32))
    store = ConnectionStore(auth, vault)
    provider = FakeCodexProvider(store)
    broker = CodexBroker(store, provider)
    state = broker.authorize(user.id)["state"]
    broker.callback(user.id, state, f"mock:{user.id}")
    return broker, provider, user, now, vault


def test_three_sessions_use_one_rotation_across_broker_reconstruction(setup):
    broker, provider, user, now, vault = setup
    old = broker.lease(user.id)
    old_refresh = broker.store.credentials(broker.store.get(user.id, "codex"))["refresh_token"]
    now[0] += 250
    barrier = threading.Barrier(3)

    def start_session(_):
        auth = AuthStore(AuthDatabase(path=broker.store.auth.database._path), clock=lambda: now[0])
        contender = CodexBroker(ConnectionStore(auth, vault), provider)
        barrier.wait(timeout=5)
        return contender.lease(user.id)

    with ThreadPoolExecutor(max_workers=3) as pool:
        leases = list(pool.map(start_session, range(3)))
    assert provider.calls == 1
    assert len({lease.access_token for lease in leases}) == 1
    assert {lease.credential_version for lease in leases} == {old.credential_version + 1}
    current = broker.store.credentials(broker.store.get(user.id, "codex"))
    assert current["refresh_token"] != old_refresh
    for lease in leases:
        assert "refresh_token" not in str(lease.blob())
        assert lease.access_token not in repr(lease)
        assert lease.access_token.encode() not in broker.store.auth.database._path.read_bytes()
    restored_store = ConnectionStore(
        AuthStore(AuthDatabase(path=broker.store.auth.database._path), clock=lambda: now[0]), vault
    )
    restored = CodexBroker(restored_store, FakeCodexProvider(restored_store))
    assert restored.lease(user.id).access_token == leases[0].access_token
    now[0] += 250
    assert restored.lease(user.id).credential_version == leases[0].credential_version + 1


def test_omitted_refresh_token_preserves_existing_and_proactive_refresh(setup):
    broker, provider, user, now, _ = setup
    before = broker.store.credentials(broker.store.get(user.id, "codex"))["refresh_token"]
    provider.omit_refresh = True
    now[0] += 250
    broker.refresh_due()
    assert provider.calls == 1
    assert broker.store.credentials(broker.store.get(user.id, "codex"))["refresh_token"] == before
    now[0] += 250
    broker.lease(user.id)
    assert provider.calls == 2


def test_revocation_and_transient_cooldown_are_durable_safe_states(setup):
    broker, provider, user, now, _ = setup
    now[0] += 250
    provider.transient = True
    with pytest.raises(HostedAuthError, match="codex_refresh_unavailable"):
        broker.lease(user.id)
    with pytest.raises(HostedAuthError, match="codex_refresh_cooldown"):
        broker.lease(user.id)
    assert provider.calls == 1
    provider.transient = False
    provider.revoked = True
    now[0] += 30
    with pytest.raises(HostedAuthError, match="codex_reauth_required"):
        broker.lease(user.id)
    assert broker.store.get(user.id, "codex").state == "reauth_required"
    with pytest.raises(HostedAuthError, match="codex_connection_required"):
        broker.lease(user.id)
    assert provider.calls == 2


def test_one_reactive_retry_and_concurrent_stale_401_joins_new_version(setup):
    broker, provider, user, _, _ = setup
    old = broker.lease(user.id)
    calls = []

    def operation(lease):
        calls.append(lease.credential_version)
        if lease.credential_version == old.credential_version:
            raise ProviderUnauthorized()
        return "ok"

    assert broker.execute(user.id, operation) == "ok"
    assert len(calls) == 2 and provider.calls == 1
    assert (
        broker.lease(user.id, rejected_version=old.credential_version).credential_version
        == calls[-1]
    )
    assert provider.calls == 1
    count = []

    def always_401(lease):
        count.append(lease)
        raise ProviderUnauthorized()

    with pytest.raises(ProviderUnauthorized):
        broker.execute(user.id, always_401)
    assert len(count) == 2


def test_refreshing_visible_reconnect_wins_and_stale_response_cannot_overwrite(setup):
    broker, provider, user, now, _ = setup
    entered, release = threading.Event(), threading.Event()
    provider.before_refresh = lambda: (entered.set(), release.wait(timeout=5))
    now[0] += 250
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(broker.lease, user.id)
        assert entered.wait(timeout=5)
        assert broker.store.get(user.id, "codex").public()["state"] == "refreshing"
        state = broker.authorize(user.id)["state"]
        reconnected = broker.callback(user.id, state, f"mock:{user.id}")
        release.set()
        with pytest.raises(HostedAuthError, match="connection_changed"):
            result.result(timeout=5)
    assert broker.store.get(user.id, "codex").credential_cipher == reconnected.credential_cipher
    assert broker.lease(user.id)


def test_expired_refresh_claim_is_contained_without_replaying_old_grant(setup):
    broker, provider, user, now, _ = setup
    record = broker.store.get(user.id, "codex")
    record.state = "refreshing"
    record.metadata["refresh_until"] = now[0]
    broker.store.save(record)
    with pytest.raises(HostedAuthError, match="codex_reauth_required"):
        broker.lease(user.id)
    assert provider.calls == 0
    assert broker.store.get(user.id, "codex").state == "reauth_required"


def test_owned_registry_three_slots_cooldown_and_no_sandbox_writeback(setup):
    broker, _, user, _, _ = setup
    accounts = HostedAccounts(broker)
    scheduler = HostedScheduling(accounts, InMemoryStore()).for_user(user.id)
    leases = [scheduler.acquire(provider="codex") for _ in range(3)]
    assert {lease.account.id for lease in leases} == {broker.store.get(user.id, "codex").id}
    with pytest.raises(ScheduleRefused):
        scheduler.acquire(provider="codex")
    for lease in leases:
        lease.release()
    account = leases[0].account
    assert accounts.scoped("other").get(account.id) is None
    assert accounts.scoped("other").get_credential_blob(account.id) is None
    with pytest.raises(PermissionError):
        accounts.put_credential_blob(account.id, {"token": "REDACTED"})
    scheduler.report_failure(account.id, "rate_limited", retry_after=30)
    assert scheduler.decide(provider="codex").account is None
    assert scheduler.max_global == 5


def test_disable_removes_cipher_and_reauthorization_is_one_use(setup):
    broker, _, user, _, _ = setup
    assert broker.disable(user.id).credential_cipher is None
    with pytest.raises(HostedAuthError):
        broker.lease(user.id)
    state = broker.authorize(user.id)["state"]
    assert broker.callback(user.id, state, f"mock:{user.id}").state == "connected"
    with pytest.raises(HostedAuthError):
        broker.callback(user.id, state, f"mock:{user.id}")
