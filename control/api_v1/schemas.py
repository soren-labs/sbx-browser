"""Pydantic request bodies and contract-shaped serializers for ``/v1``.

Field names follow ``docs/contracts/api-v1.yaml`` exactly: ``agent ≙ session``,
``run ≙ turn``. Provider is a ``Literal`` so an unknown value fails request
validation and surfaces as canonical ``400 invalid_provider``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt

ProviderId = Literal["codex", "antigravity", "grok", "opencode", "devin"]
# Canonical effort ladder (SOR-204). Whether a level is actually honored
# is a per-account/per-model capability decision — the schema accepts the
# full ladder and create-time validation refuses unexposed levels.
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
VALID_SCOPES = ("agents", "admin")

USAGE_REQUIRED = ("input_tokens", "cached_input_tokens", "output_tokens")
USAGE_OPTIONAL = ("cache_write_input_tokens", "reasoning_output_tokens")


class Prompt(BaseModel):
    text: str = Field(min_length=1)


class AgentSpec(BaseModel):
    provider: ProviderId
    account_id: str | None = "auto"
    model: str | None = None
    # SOR-179: canonical reasoning effort, agent-scoped — every run
    # (incl. resume turns) inherits it. Providers without a native effort
    # surface refuse the combination as ``unsupported`` at create time.
    reasoning_effort: ReasoningEffort | None = None


_COMMIT_SHA = r"^[0-9a-f]{40}$"


class WorkspaceDecl(BaseModel):
    """SOR-83 workspace declaration on agent create (``api-v1.yaml``)."""

    repo: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    base_sha: str = Field(pattern=_COMMIT_SHA)


class GitPolicy(BaseModel):
    """SOR-128 git collaboration policy on agent create (``api-v1.yaml``).

    Optional — requires a ``workspace`` declaration. ``branch`` is the work
    branch the workspace materializes and publishes under (default
    ``sbx/<agent_id>``); ``push`` allows the publish endpoint to push it to
    the repo's remote; ``auto_create_pr`` (requires ``push``) opens a pull
    request to ``target`` (default ``workspace.base_ref``) on publish.
    ``auto_publish`` (requires ``push``, SOR-178) publishes automatically
    when a run finishes successfully; ``merge`` (requires
    ``auto_create_pr``) allows the merge endpoint — which still refuses
    without an independent exact-sha review pin. Ref-name safety and the
    push/PR dependencies are enforced in the domain layer so violations
    surface as ``workspace_invalid``.
    """

    model_config = ConfigDict(extra="forbid")

    branch: str | None = None
    push: bool = False
    auto_create_pr: bool = False
    auto_publish: bool = False
    merge: bool = False
    target: str | None = None
    draft: bool = False
    title: str | None = None
    body: str | None = None


class PullRequestRef(BaseModel):
    """SOR-128 reviewer handoff reference: a remote ref + pinned head.

    ``ref`` is fetched from the workspace repo's origin (``refs/pull/<n>/head``,
    ``pull/<n>/head``, or a branch name); ``head_sha`` pins the exact commit
    it must resolve to — drift fails closed as ``head_sha_mismatch``.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    head_sha: str = Field(pattern=_COMMIT_SHA)


class HandoffRef(BaseModel):
    """SOR-83/SOR-128 cross-agent handoff reference: exactly one of the
    ref fields.

    ``artifact_id`` consumes a durable artifact package; ``head_sha`` checks
    out an exact commit in the declared repo; ``pull_request`` fetches a
    remote ref pinned to an exact head (SOR-128 reviewer start).
    ``workspace`` is only used by ``POST /v1/agents/{id}/handoff`` when the
    agent has no recorded workspace yet.
    """

    artifact_id: str | None = None
    head_sha: str | None = Field(default=None, pattern=_COMMIT_SHA)
    pull_request: PullRequestRef | None = None
    workspace: WorkspaceDecl | None = None


class WorkflowMetadata(BaseModel):
    """Caller workflow binding for an agent (SOR-84 C1).

    ``task_id`` names the task inside ``workflow_id``; ``role`` is a
    free-form label (``worker`` / ``reviewer`` / ...); ``parent_task_id``
    optionally links a sub-task to its parent within the same workflow.
    """

    workflow_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    role: str = Field(min_length=1, max_length=64)
    parent_task_id: str | None = Field(default=None, max_length=256)


class OutputContract(BaseModel):
    """SOR-130: optional JSON Schema output contract for a run.

    ``schema`` is the JSON Schema the run's final output must satisfy (the
    runner's deterministic validator subset — unsupported keywords are
    refused at request time). ``enforcement`` selects the verdict handling:

    - ``strict`` (default): invalid/malformed output → run ``ERROR`` with
      ``contract_violation`` — never a silent success.
    - ``warn``: invalid/malformed output → run stays ``FINISHED`` but
      carries the same ``contract_violation`` diagnostic.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    json_schema: dict[str, Any] = Field(alias="schema")
    enforcement: Literal["strict", "warn"] = "strict"


class SessionCompute(BaseModel):
    """SOR-181 per-agent sandbox compute sizing (``api-v1.yaml``).

    Independent of ``resources`` (credential/config refs) — ``cpu`` is a
    Modal core count or ``[min, max]`` request/limit pair,
    ``memory_mib`` the same in MiB. Scalars pin ``min == max``; omitted
    fields resolve to the canonical defaults (``cpu=[1, 2]``,
    ``memory_mib=[1024, 8192]``). Malformed, inverted, or out-of-bounds
    values fail as ``invalid_compute``.
    """

    model_config = ConfigDict(extra="forbid")

    # Strict types keep JSON booleans (and non-integral floats) from
    # silently coercing into a sizing — those are malformed declarations.
    cpu: StrictFloat | list[StrictFloat] | None = None
    memory_mib: StrictInt | list[StrictInt] | None = None


class SessionResources(BaseModel):
    """SOR-129 per-agent resource refs on agent create (``api-v1.yaml``).

    ``secrets`` names Modal Secrets to attach to this agent's sandbox only
    (allowlist-validated — never account credential Secrets); ``mcp`` names
    MCP server entries in the deployment registry. Both are *references* —
    no secret value ever crosses the API. MCP refs on a provider with no
    MCP channel are refused as ``unsupported``.
    """

    model_config = ConfigDict(extra="forbid")

    secrets: list[str] = Field(default_factory=list, max_length=32)
    mcp: list[str] = Field(default_factory=list, max_length=32)


class CreateAgentRequest(BaseModel):
    prompt: Prompt
    agent: AgentSpec
    name: str | None = None
    idle_timeout_s: int | None = Field(default=None, ge=1)
    workspace: WorkspaceDecl | None = None
    handoff: HandoffRef | None = None
    git: GitPolicy | None = None
    metadata: WorkflowMetadata | None = None
    output_contract: OutputContract | None = None
    resources: SessionResources | None = None
    compute: SessionCompute | None = None


class CreateRunRequest(BaseModel):
    prompt: Prompt
    # Optional task re-binding (SOR-84): a follow-up may carry the same
    # workflow metadata shape as agent create.
    metadata: WorkflowMetadata | None = None
    output_contract: OutputContract | None = None


class CreateArtifactRequest(BaseModel):
    """``POST /v1/agents/{id}/artifacts`` body (all optional)."""

    run_id: str | None = None
    test_command: str | None = None


class ReviewWorkspaceRequest(BaseModel):
    """``POST /v1/agents/{id}/workspace/review`` body.

    ``head_sha`` pins the exact commit reviewed; omitted means "the recorded
    head". A mismatch with the recorded head is ``head_sha_mismatch``.

    ``comment`` (SOR-128) additionally posts a machine-readable *comment*
    on the workspace's recorded pull request — deliberately never a formal
    GitHub review approval (all sandboxes share one GitHub identity, so an
    approval would read as the author approving their own work). Commenting
    needs the agent's live sandbox and the opt-in GitHub bridge.
    """

    head_sha: str | None = Field(default=None, pattern=_COMMIT_SHA)
    comment: str | None = Field(default=None, min_length=1)


class CreateAccountRequest(BaseModel):
    provider: ProviderId
    label: str = Field(min_length=1)
    credential: dict[str, Any] | None = None
    max_concurrent: int = Field(default=1, ge=1)
    models: list[str] = Field(default_factory=list)


class CreateApiKeyRequest(BaseModel):
    label: str = ""
    scopes: list[str] | None = None


class GitHubAppAuthorizeCallbackRequest(BaseModel):
    """SOR-177: the browser-side install completion — ``state`` is the
    single-use capability issued by the authorize step (the redirect itself
    cannot carry a Bearer key)."""

    model_config = ConfigDict(extra="forbid")
    installation_id: int = Field(ge=1)
    state: str = Field(min_length=1)


def usage_public(usage: dict[str, Any] | None) -> dict[str, int] | None:
    """Usage with the three required fields defaulted to 0.

    ``None`` stays ``None``: usage that was never measured reports as
    unavailable (``null``), never as fabricated zeros (SOR-84).
    """
    if usage is None:
        return None
    out = {key: int(usage.get(key) or 0) for key in USAGE_REQUIRED}
    for key in USAGE_OPTIONAL:
        if key in usage and usage[key] is not None:
            out[key] = int(usage[key])
    return out


def agent_public(
    pub: dict[str, Any],
    meta: Any | None,
    *,
    usage: dict[str, Any] | None,
    metadata: dict[str, Any] | None = None,
    compute: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``api.yaml`` Session dict + ``AgentMeta`` -> ``api-v1.yaml`` Agent.

    ``usage`` is the session record's measured usage — pass
    ``SessionRecord.usage`` (``None`` while unmeasured → ``null``), not the
    zero-filled ``pub["usage"]`` the ``/api`` contract requires.
    ``metadata`` echoes the workflow binding when one is attached.
    """
    return {
        "id": pub["id"],
        "name": (meta.name if meta and meta.name else pub.get("title") or "untitled"),
        "provider": (meta.provider if meta else None) or pub.get("provider") or "codex",
        "account_id": (meta.account_id if meta else None) or pub.get("account_id") or "auto",
        "model": pub["model"],
        # SOR-179: the declared canonical effort (null when undeclared).
        "reasoning_effort": (getattr(meta, "reasoning_effort", None) if meta else None)
        or pub.get("reasoning_effort"),
        "status": pub["status"],
        "created_at": pub["created_at"],
        "updated_at": pub["updated_at"],
        "usage": usage_public(usage),
        "cost_estimate_usd": pub.get("cost_estimate_usd", 0.0),
        "metadata": metadata,
        # SOR-129: echo the declared resource *refs* (names only — never
        # values, never the resolved MCP config templates).
        "resources": getattr(meta, "resources", None) if meta is not None else None,
        # SOR-181: the resolved compute spec from the durable session
        # record — ``null`` only for records predating SOR-181.
        "compute": compute,
    }


def account_public(account: Any, running: int) -> dict[str, Any]:
    """``ports.Account`` -> ``api-v1.yaml`` Account (never credential material)."""
    return {
        "id": account.id,
        "provider": account.provider,
        "label": account.label,
        "status": account.status,
        "max_concurrent": account.max_concurrent,
        "running": running,
        "models": list(account.models),
        "created_at": account.created_at,
        "last_used_at": account.last_used_at,
        "cooldown_until": account.cooldown_until,
        "last_error": account.last_error,
    }


def api_key_public(key: Any) -> dict[str, Any]:
    """``ports.ApiKey`` -> ``api-v1.yaml`` ApiKey (hashes only, no plaintext)."""
    return {
        "id": key.id,
        "label": key.label,
        "scopes": list(key.scopes),
        "created_at": key.created_at,
        "revoked_at": key.revoked_at,
    }
