"""User-owned adapters over the existing GitHub App and revision services."""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any

from control import github
from control.auth_store import _digest, _iso
from control.connections import ConnectionStore
from control.github_app import (
    GitHubAppConfig,
    GitHubAppError,
    GitHubAppService,
    InMemoryGitHubAppStore,
    record_from_dict,
    record_to_dict,
)
from control.hosted_auth import HostedAuthError
from control.postgres_state import DatabaseRecords
from control.tasks import GitHubApiResolver, TaskRefusal, canonicalize_repo


class UserGitHubStore(InMemoryGitHubAppStore):
    """Keep existing App interface; authoritative metadata lives in PostgreSQL."""

    def __init__(self, connections: ConnectionStore, owner: str):
        super().__init__()
        self.connections, self.owner = connections, owner
        self.records = DatabaseRecords(connections.auth.database)

    def _id(self, installation_id):
        return f"{self.owner}:{installation_id}"

    def list(self):
        return [
            record_from_dict(row[1])
            for row in self.records.rows("github_installations", owner=self.owner)
        ]

    def get(self, installation_id):
        data = self.records.get("github_installations", self._id(installation_id), owner=self.owner)
        return record_from_dict(data) if data else None

    def put(self, record):
        self.records.put_owned(
            "github_installations",
            self._id(record.installation_id),
            self.owner,
            record_to_dict(record),
        )

    def delete(self, installation_id):
        self.records.delete("github_installations", self._id(installation_id), owner=self.owner)

    def put_state(self, state, expires_epoch):
        auth = self.connections.auth
        with auth.database.transaction() as conn:
            auth.database.execute(
                conn,
                "INSERT INTO connection_authorizations "
                "(state_hash, user_id, provider, expires_at) VALUES (?, ?, ?, ?)",
                (_digest(state), self.owner, "github", expires_epoch),
            )

    def pop_state(self, state):
        try:
            self.connections.consume_authorization(self.owner, "github", state)
            return self.connections.auth.clock() + 1
        except HostedAuthError:
            return None


class FakeGitHubClient:
    """Installation enumeration is user-bound, including during callback validation."""

    def __init__(self, owner: str, clock=time.time):
        self.owner, self.clock = owner, clock
        suffix = hashlib.sha256(owner.encode()).hexdigest()[:12]
        self.installation_id = int(suffix, 16)
        self.login = f"mock-{suffix}"
        self.repositories = [f"{self.login}/alpha", f"{self.login}/tools"]
        self.mints: list[dict[str, Any]] = []

    def list_installations(self):
        return [
            {
                "id": self.installation_id,
                "account": {"login": self.login, "type": "User"},
                "repository_selection": "selected",
            }
        ]

    def create_installation_token(self, installation_id, *, repositories=None):
        if installation_id != self.installation_id:
            raise GitHubAppError("not_found", "installation not found", status_code=404)
        if repositories and any(
            f"{self.login}/{repo}" not in self.repositories for repo in repositories
        ):
            raise GitHubAppError("not_found", "repository not found", status_code=404)
        self.mints.append({"installation_id": installation_id, "repositories": repositories})
        identity = f"{self.owner}:{installation_id}:{repositories}:{int(self.clock() // 3600)}"
        return (
            f"mock-installation-{hashlib.sha256(identity.encode()).hexdigest()}",
            self.clock() + 3600,
        )

    def installation_repositories(self, token):
        return "selected", list(self.repositories)

    def delete_installation(self, installation_id):
        return None


class HostedGitHubService(GitHubAppService):
    """Only authenticated, owner-bound installation callbacks are supported in hosted mode."""

    def __init__(
        self,
        connections: ConnectionStore,
        owner: str,
        client: Any = None,
        config: GitHubAppConfig | None = None,
        mock=False,
    ):
        self.owner, self.mock = owner, mock
        self.connections = connections
        super().__init__(
            config or GitHubAppConfig(),
            UserGitHubStore(connections, owner),
            client,
            clock=connections.auth.clock,
            records_ttl_s=0,
        )

    def status(self):
        return {
            "configured": self.configured,
            "mock": self.mock,
            "installations": [r.public() for r in self._store.list()],
        }

    def begin_authorization(self):
        if self.mock:
            state = self.connections.begin_authorization(self.owner, "github")
            return {
                "state": state,
                "authorize_url": "/integrations?github_authorization=mock",
                "mock": True,
            }
        return {**super().begin_authorization(), "mock": False}

    def complete_authorization(self, installation_id, state):
        # Injected real clients must enumerate installations verified for this
        # OAuth user, rather than returning every installation of the SBX App.
        record = super().complete_authorization(installation_id, state)
        connection = self.connections.connect(self.owner, "github", {})
        connection.state = "connected"
        connection.metadata = {"connected_at": _iso(self.connections.auth.clock())}
        self.connections.save(connection)
        return record

    def sandbox_token(self, repo=None):
        # Never mint a token for an entire installation without repository context.
        return super().sandbox_token(repo) if repo else None

    def begin_install(self, **kwargs):
        return self.begin_authorization()

    def complete_broker(self, code):
        raise GitHubAppError(
            "github_app_state", "use an authenticated user callback", status_code=403
        )

    def begin_manifest(self, *args, **kwargs):
        raise GitHubAppError("forbidden", "App configuration is operator-only", status_code=403)

    complete_manifest = begin_manifest

    def _record_installation(self, installation):
        # Enumerate all-repo installations too; hosted selectors and access
        # checks use GitHub's explicit current list, never an account wildcard.
        return super()._record_installation({**installation, "repository_selection": "selected"})

    def remote(self, repo):
        from control.github_remote import RemoteGitHub

        token = self.sandbox_token(repo)
        if not token:
            raise GitHubAppError("not_found", "repository not found", status_code=404)
        return FakeGitHubRemote(self, repo) if self.mock else RemoteGitHub(token)

    def git_env(self, repo):
        token = self.sandbox_token(repo)
        if not token:
            raise TaskRefusal(404, "not_found", "repository not found")
        return {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": os.environ.get("HOME", ""),
            "GH_TOKEN": token,
            "SBX_GITHUB_BROKER_URL": "off",
        }

    def mock_repo(self, repo):
        from control.mock_git import bare_repository

        canonical = canonicalize_repo(repo)
        if not self.mock or self.installation_for_repo(canonical.slug) is None:
            raise TaskRefusal(404, "not_found", "repository not found")
        return bare_repository(self.owner, canonical.slug)

    def ls_remote(self, repo, ref, **kwargs):
        from control.github_remote import ls_remote

        if ref != "HEAD" and not ref.startswith("refs/"):
            ref = f"refs/heads/{ref}"
        return ls_remote(str(self.mock_repo(repo)), ref, env={})

    def push_payload(self, repo, branch, **kwargs):
        from control.github_remote import push_payload

        kwargs["env"] = {}
        sha = push_payload(str(self.mock_repo(repo)), branch, **kwargs)
        self.remote(repo).record_push(branch, sha)
        return sha


class HostedGitHub:
    def __init__(self, connections: ConnectionStore, *, mock=False, factory=None):
        self.connections, self.mock, self.factory = connections, mock, factory

    def for_user(self, owner: str) -> HostedGitHubService:
        if self.factory:
            return self.factory(self.connections, owner)
        client = FakeGitHubClient(owner, self.connections.auth.clock) if self.mock else None
        config = GitHubAppConfig("mock", "sbx-mock", "REDACTED") if self.mock else None
        return HostedGitHubService(self.connections, owner, client, config, mock=self.mock)


class UserRepoResolver:
    def __init__(self, service: HostedGitHubService, source=None):
        self.service, self.source = service, source

    def _resolver(self, repo):
        if repo.kind != "github" or self.service.installation_for_repo(repo.slug) is None:
            raise TaskRefusal(404, "not_found", "repository not found")
        return self.source or GitHubApiResolver(env=self.service.git_env(repo.canonical))

    def default_branch(self, repo):
        source = self._resolver(repo)
        return "main" if self.service.mock and self.source is None else source.default_branch(repo)

    def resolve_ref(self, repo, ref):
        source = self._resolver(repo)
        return (
            self.service.ls_remote(repo.canonical, ref)
            if self.service.mock and self.source is None
            else source.resolve_ref(repo, ref)
        )

    def access(self, repo):
        source = self._resolver(repo)
        return (
            {"read": "yes", "push": "yes", "source": "mock-installation"}
            if self.service.mock and self.source is None
            else source.access(repo)
        )


class GitHubScopedBackend:
    """Reuses compute backend while replacing ambient GitHub credentials per exec."""

    def __init__(self, source, github_connections: HostedGitHub, workspace_store):
        self.source, self.github_connections, self.workspace_store = (
            source,
            github_connections,
            workspace_store,
        )

    def __getattr__(self, name):
        return getattr(self.source, name)

    def create(self, spec):
        from dataclasses import replace

        return self.source.create(replace(spec, tags={**spec.tags, "hosted": "1"}))

    def exec(self, handle, argv, env=None):
        outgoing = {k: v for k, v in (env or {}).items() if not github.owns_env_key(k)}
        broker = getattr(self, "codex_broker", None)
        if broker is not None:
            for key in ("CODEX_AUTH_JSON", "SBX_ACCOUNT_CREDENTIAL", "SBX_PROVIDER_API_KEY"):
                outgoing.pop(key, None)
            if "init" in argv or "turn" in argv or "resume" in argv:
                import json

                lease = broker.lease(handle.tags.get("owner", ""))
                outgoing["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(lease.blob())
                outgoing["SBX_ACCOUNT_ID"] = lease.connection_id
                outgoing["SBX_HOSTED_CREDENTIAL_LEASE"] = "1"
        repo = outgoing.pop("SBX_GITHUB_REPO", None)
        workspace = self.workspace_store.get(handle.tags.get("session_id", ""))
        repo = repo or (workspace.repo if workspace else None)
        if repo and canonicalize_repo(repo).kind == "github":
            service = self.github_connections.for_user(handle.tags.get("owner", ""))
            token = service.sandbox_token(repo)
            if not token:
                raise HostedAuthError("repository_not_found", 404)
            outgoing.update(
                github.exec_env(env={"SBX_GITHUB_EPHEMERAL": "1", "GH_TOKEN": token}, repo=repo)
            )
            if service.mock:
                from control.mock_git import rewrite_env

                outgoing = rewrite_env(
                    service.mock_repo(repo), canonicalize_repo(repo).canonical, outgoing
                )
        return self.source.exec(handle, argv, env=outgoing)


class FakeGitHubRemote:
    """Persistent fake upstream PR state, separated by user and repository."""

    def __init__(self, service: HostedGitHubService, repo: str):
        self.service, self.repo = service, repo
        self.slug = canonicalize_repo(repo).slug
        self.records = service._store.records

    def _check(self, slug):
        if slug != self.slug or self.service.installation_for_repo(self.slug) is None:
            raise GitHubAppError("not_found", "repository not found", status_code=404)

    def _key(self, number):
        return f"{self.service.owner}:{self.slug}:{number}"

    def _pulls(self):
        return [
            value
            for _, value in self.records.rows("github_pulls", owner=self.service.owner)
            if value["repository"] == self.slug
        ]

    def record_push(self, branch, sha):
        key = f"{self.service.owner}:{self.slug}:{branch}"
        self.records.put_owned("github_branches", key, self.service.owner, {"sha": sha})
        for pull in self._pulls():
            if pull["head"]["ref"] == branch and pull["state"] == "open":
                pull["head"]["sha"] = sha
                self._save(pull)

    def _save(self, pull):
        self.records.put_owned("github_pulls", self._key(pull["number"]), self.service.owner, pull)
        return pull

    def get_pull(self, slug, number):
        self._check(slug)
        pull = self.records.get("github_pulls", self._key(number), owner=self.service.owner)
        if pull is None:
            from control.github_remote import RemoteGitHubError

            raise RemoteGitHubError("not_found", "pull request not found", status=404)
        return pull

    def find_pull(self, slug, head_branch):
        self._check(slug)
        return next(
            (p for p in self._pulls() if p["head"]["ref"] == head_branch and p["state"] == "open"),
            None,
        )

    def create_pull(self, slug, *, title, body, head, base, draft=False):
        self._check(slug)
        number = len(self._pulls()) + 1
        branch = self.records.get(
            "github_branches", f"{self.service.owner}:{slug}:{head}", owner=self.service.owner
        )
        if branch is None:
            raise GitHubAppError("not_found", "branch not found", status_code=404)
        return self._save(
            {
                "repository": slug,
                "number": number,
                "title": title,
                "body": body,
                "head": {"ref": head, "sha": branch["sha"]},
                "base": {"ref": base},
                "state": "open",
                "draft": draft,
                "merged": False,
                "html_url": f"https://github.com/{slug}/pull/{number}",
            }
        )

    def update_pull(self, slug, number, **changes):
        pull = self.get_pull(slug, number)
        for field in ("title", "body", "draft"):
            if changes.get(field) is not None:
                pull[field] = changes[field]
        if changes.get("base"):
            pull["base"] = {"ref": changes["base"]}
        return self._save(pull)

    def create_comment(self, slug, number, body):
        pull = self.get_pull(slug, number)
        pull.setdefault("comments", []).append(body)
        self._save(pull)
        return {"html_url": f"{pull['html_url']}#issuecomment-{len(pull['comments'])}"}

    def merge_pull(self, slug, number, sha=None):
        pull = self.get_pull(slug, number)
        if pull["draft"] or pull["state"] != "open" or pull["head"]["sha"] != sha:
            from control.github_remote import RemoteGitHubError

            raise RemoteGitHubError("merge_not_allowed", "pull cannot merge", status=409)
        pull.update({"merged": True, "state": "closed"})
        self._save(pull)
        return {"merged": True, "sha": hashlib.sha1(f"merge:{sha}".encode()).hexdigest()}
