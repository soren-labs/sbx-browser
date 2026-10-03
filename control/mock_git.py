"""Credential-free local Git transport for explicitly mocked hosted GitHub."""

import hashlib
import os
import subprocess
import threading
from pathlib import Path

_LOCK = threading.Lock()


def bare_repository(owner: str, slug: str) -> Path:
    identity = hashlib.sha256(f"{owner}:{slug}".encode()).hexdigest()
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    repo = root / "sbx-browser/mock-github" / identity
    with _LOCK:
        if (repo / "HEAD").exists():
            return repo
        repo.mkdir(parents=True, mode=0o700)
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(repo),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Mock SBX",
            "GIT_AUTHOR_EMAIL": "mock@example.test",
            "GIT_COMMITTER_NAME": "Mock SBX",
            "GIT_COMMITTER_EMAIL": "mock@example.test",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
        }
        subprocess.run(
            ["git", "init", "--bare", "-q", "--initial-branch=main", str(repo)], env=env, check=True
        )
        tree = subprocess.check_output(
            ["git", "-C", str(repo), "mktree"], input=b"", env=env
        ).strip()
        commit = subprocess.check_output(
            ["git", "-C", str(repo), "commit-tree", tree.decode(), "-m", "Initial mock repository"],
            env=env,
        ).strip()
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/heads/main", commit.decode()],
            env=env,
            check=True,
        )
    return repo


def rewrite_env(repo: Path, canonical: str, env: dict) -> dict:
    out = dict(env)
    index = int(out.get("GIT_CONFIG_COUNT", "0"))
    for suffix in (".git", ""):
        out[f"GIT_CONFIG_KEY_{index}"] = f"url.{repo.as_uri()}.insteadOf"
        out[f"GIT_CONFIG_VALUE_{index}"] = canonical + suffix
        index += 1
    out["GIT_CONFIG_COUNT"] = str(index)
    return out
