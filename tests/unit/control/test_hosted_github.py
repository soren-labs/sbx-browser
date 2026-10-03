"""Installation ownership, repo-scoped credentials and the existing revision domain."""

import hashlib
import secrets

import pytest
from control.app import create_app
from control.artifacts import ArtifactManifest, InMemoryArtifactStore
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from control.connections import ConnectionStore, SecretVault
from control.github_app import GitHubAppError
from control.hosted_github import FakeGitHubClient, HostedGitHub, UserRepoResolver
from control.revisions import InMemoryRevisionStore, Revision, RevisionError, RevisionService
from control.tasks import TaskRefusal, canonicalize_repo
from fastapi.testclient import TestClient


@pytest.fixture
def setup(tmp_path):
    auth = AuthStore(AuthDatabase(path=tmp_path / "auth.db"))
    users = [auth.create_user() for _ in range(2)]
    vault = SecretVault(secrets.token_bytes(32))
    github = HostedGitHub(ConnectionStore(auth, vault), mock=True)
    return auth, users, vault, github


def connect(service):
    state = service.begin_authorization()["state"]
    return service.complete_authorization(service._client.installation_id, state)


def test_installations_repo_probes_and_mints_are_user_scoped_after_restart(setup):
    auth, users, vault, github = setup
    services = [github.for_user(user.id) for user in users]
    installs = [connect(service) for service in services]
    assert set(installs[0].repositories).isdisjoint(installs[1].repositories)
    mine = f"https://github.com/{installs[0].repositories[0]}"
    theirs = f"https://github.com/{installs[1].repositories[0]}"
    token = services[0].sandbox_token(mine)
    assert token and services[0].sandbox_token() is None
    assert services[0].sandbox_token(theirs) is None
    assert services[0]._client.mints[-1]["repositories"] == ["alpha"]
    probe = UserRepoResolver(services[0])
    assert probe.default_branch(canonicalize_repo(mine)) == "main"
    assert len(probe.resolve_ref(canonicalize_repo(mine), "main")) == 40
    with pytest.raises(TaskRefusal):
        probe.access(canonicalize_repo(theirs))
    with pytest.raises(TaskRefusal):
        probe.access(canonicalize_repo("/etc"))
    restored = HostedGitHub(
        ConnectionStore(AuthStore(AuthDatabase(path=auth.database._path)), vault), mock=True
    )
    assert (
        restored.for_user(users[0].id).status()["installations"]
        == services[0].status()["installations"]
    )
    assert restored.for_user(users[1].id).sandbox_token(mine) is None
    assert token.encode() not in auth.database._path.read_bytes()


def test_callback_cannot_attach_other_users_installation(setup):
    _, users, _, github = setup
    service = github.for_user(users[0].id)
    state = service.begin_authorization()["state"]
    other = FakeGitHubClient(users[1].id)
    with pytest.raises(GitHubAppError, match="installation not reported"):
        service.complete_authorization(other.installation_id, state)
    assert service.status()["installations"] == []
    with pytest.raises(GitHubAppError):
        service.complete_authorization(service._client.installation_id, state)


def test_existing_revision_delivery_review_merge_with_owned_fake_remote(setup, monkeypatch):
    _, users, _, github = setup
    service = github.for_user(users[0].id)
    record = connect(service)
    repo = f"https://github.com/{record.repositories[0]}"
    store, artifacts = InMemoryRevisionStore(), InMemoryArtifactStore()
    engine = RevisionService(
        store, artifacts, env={}, remote=service.remote, env_for_repo=service.git_env
    )
    base, head = "a" * 40, "b" * 40
    patch = b"diff --git a/file b/file\n+hello\n"
    artifact = ArtifactManifest(
        artifact_id="owned-art",
        base_sha=base,
        head_sha=head,
        repo=repo,
        created_at="t",
        producer_agent_id="author",
        payloads={"patch.diff": hashlib.sha256(patch).hexdigest()},
    )
    artifacts.put(artifact, {"patch.diff": patch})
    revision = Revision(
        revision_id="owned-revision",
        agent_id="author",
        n=1,
        repo=repo,
        base_sha=base,
        head_sha=head,
        artifact_id=artifact.artifact_id,
        created_at="t",
        updated_at="t",
    )
    store.put_revision(revision)

    def push(repo, branch, **kwargs):
        assert kwargs["env"]["GH_TOKEN"]
        service.remote(repo).record_push(branch, head)
        return head

    monkeypatch.setattr("control.revisions.push_payload", push)
    engine.deliver(revision, overrides={"pull_request": {"title": "Owned change", "draft": True}})
    with pytest.raises(RevisionError):
        engine.merge(revision)
    engine.deliver(revision, overrides={"pull_request": {"draft": False}})
    engine.add_review(
        revision,
        reviewer_identity="agent:reviewer",
        reviewer_agent_id="reviewer",
        verdict="approve",
    )
    assert engine.merge(revision).delivery["merged"] is True
    with pytest.raises(GitHubAppError):
        github.for_user(users[1].id).remote(repo)


def test_hosted_and_existing_github_routes_share_user_installations(setup, monkeypatch):
    auth, users, vault, _ = setup
    monkeypatch.setenv("SBX_CONNECTIONS_MODE", "mock")
    app = create_app(auth_store=auth, connection_vault=vault, hosted=True, state_backend="postgres")
    headers = [
        {"Authorization": f"Bearer {PersistentApiKeyStore(auth).create(user_id=u.id)[1]}"}
        for u in users
    ]
    with TestClient(app) as client:
        state = client.post(
            "/hosted/connections/github/authorize", json={}, headers=headers[0]
        ).json()["state"]
        assert (
            client.post(
                "/hosted/connections/github/mock-approve", json={"state": state}, headers=headers[1]
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/hosted/connections/github/mock-approve", json={"state": state}, headers=headers[0]
            ).status_code
            == 200
        )
        assert (
            len(client.get("/hosted/repositories", headers=headers[0]).json()["repositories"]) == 2
        )
        assert client.get("/hosted/repositories", headers=headers[1]).json()["repositories"] == []
        assert len(client.get("/v1/github/app", headers=headers[0]).json()["installations"]) == 1
        assert client.get("/v1/github/app", headers=headers[1]).json()["installations"] == []
        installation = client.get("/v1/github/app", headers=headers[0]).json()["installations"][0][
            "installation_id"
        ]
        assert (
            client.request(
                "DELETE",
                f"/hosted/connections/github/installations/{installation}",
                json={},
                headers=headers[1],
            ).status_code
            == 404
        )
        assert client.get("/v1/github/install/callback?code=REDACTED").status_code == 401


def test_backend_injects_only_owned_repo_token_and_never_ambient_credentials(setup):
    from pathlib import Path

    from control.backend import SandboxHandle
    from control.hosted_auth import HostedAuthError
    from control.hosted_github import GitHubScopedBackend
    from control.workspace import InMemoryWorkspaceStore

    _, users, _, github = setup
    service = github.for_user(users[0].id)
    installation = connect(service)
    repo = f"https://github.com/{installation.repositories[0]}"
    captured = []

    class Backend:
        def exec(self, handle, argv, env=None):
            captured.append(env)

    backend = GitHubScopedBackend(Backend(), github, InMemoryWorkspaceStore())
    handle = SandboxHandle("sandbox", Path("/work"), {"owner": users[0].id})
    backend.exec(handle, ["git", "status"], env={"GH_TOKEN": "REDACTED", "SBX_GITHUB_REPO": repo})
    assert captured[0]["GH_TOKEN"] != "REDACTED"
    assert "SBX_GITHUB_REPO" not in captured[0]
    backend.exec(handle, ["echo", "ok"], env={"GH_TOKEN": "REDACTED"})
    assert "GH_TOKEN" not in captured[1]
    other = SandboxHandle("other", Path("/work"), {"owner": users[1].id})
    with pytest.raises(HostedAuthError, match="repository_not_found"):
        backend.exec(other, ["git", "status"], env={"SBX_GITHUB_REPO": repo})
    assert len(captured) == 2
