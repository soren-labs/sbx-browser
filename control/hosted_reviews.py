"""Independent review Sessions over the existing V2 execution and revision gate."""

from control.api_v1 import deps
from control.api_v2.routes import create_session
from control.api_v2.schemas import CreateSessionRequest
from control.hosted_auth import HostedAuthError
from control.revisions import RevisionError

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes", "comment"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"message": {"type": "string", "minLength": 1}},
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "findings"],
    "additionalProperties": False,
}


def author_revision(request, owner, session_id, ref=None):
    task = request.app.state.task_store.get(session_id)
    if task is None or task.owner != owner or not task.agent_id:
        raise HostedAuthError("not_found", 404)
    revisions = deps.get_revisions(request)
    try:
        revision = revisions.resolve(task.agent_id, ref)
    except RevisionError as exc:
        raise HostedAuthError(exc.code, exc.status_code) from None
    if (
        revision.status != "ready"
        or not revision.delivery
        or revision.delivery.get("status") != "delivered"
    ):
        raise HostedAuthError("revision_not_delivered", 409)
    return task, revision, revisions


def start_review(request, key, session_id, ref=None):
    task, revision, _ = author_revision(request, key.id, session_id, ref)
    branch = revision.delivery.get("branch")
    if not branch:
        raise HostedAuthError("revision_not_delivered", 409)
    body = CreateSessionRequest.model_validate(
        {
            "title": f"Review: {task.request.get('name') or 'coding Session'}",
            "prompt": (
                f"SBX independent review. Review commit {revision.head_sha} "
                f"and the delivered branch {branch}. Inspect the diff against "
                f"{revision.base_sha}, run relevant tests, and report actual bugs "
                "and requested fixes. Do not modify files. "
                "Return the verdict and findings as JSON."
            ),
            "repository": {"repo": revision.repo, "ref": branch},
            "execution": {"provider": "codex"},
            "advanced": {"output_contract": {"schema": REVIEW_SCHEMA, "enforcement": "strict"}},
        }
    )
    plane, v1, run_states = (
        deps.get_plane(request),
        deps.get_v1_state(request),
        deps.get_run_states(request),
    )
    result = create_session(
        body,
        request,
        key=key,
        plane=plane,
        registry=deps.get_registry(request),
        scheduler=deps.get_scheduler(request),
        v1=v1,
        run_states=run_states,
        reporter=deps.get_run_reporter(request),
        workflows=deps.get_workflow_service(request, plane, v1, run_states),
        resources_registry=deps.get_resources(request),
        capabilities=deps.get_capabilities(request),
        task_store=deps.get_task_store(request),
        resolver=deps.get_repo_resolver(request),
        idempotency_key=None,
    )
    reviewer_id = result["session"]["id"]
    request.app.state.database_records.put_owned(
        "hosted_review_sessions",
        reviewer_id,
        key.id,
        {
            "author_session_id": session_id,
            "revision_id": revision.revision_id,
            "reviewed_head_sha": revision.head_sha,
            "reviewer_session_id": reviewer_id,
            "created_at": request.app.state.auth_store.clock(),
        },
    )
    return {"session_id": reviewer_id, "reviewed_head_sha": revision.head_sha}


def review_status(request, owner, session_id):
    records = request.app.state.database_records
    link = records.get("hosted_review_sessions", session_id, owner=owner)
    if link is None:
        raise HostedAuthError("not_found", 404)
    task = request.app.state.task_store.get(session_id)
    if task is None or task.owner != owner:
        raise HostedAuthError("not_found", 404)
    if not task.agent_id:
        return {**link, "status": "running"}
    deps.get_plane(request).get(task.agent_id)
    run = request.app.state.run_store.get(task.agent_id, 1)
    if run is None or not run.terminal:
        return {**link, "status": "running"}
    if (
        run.status != "FINISHED"
        or (run.contract_result or {}).get("status") != "valid"
        or not isinstance(run.structured_output, dict)
    ):
        return {**link, "status": "failed", "error": "review_output_invalid"}
    revisions = deps.get_revisions(request)
    revision = revisions.get(link["revision_id"])
    with revisions._lock:
        review = revisions.find_review_by_idempotency(
            revision.agent_id, owner, f"hosted-review:{session_id}"
        )
        if review is None:
            review = revisions.add_review(
                revision,
                reviewer_identity=f"session:{session_id}",
                reviewer_agent_id=task.agent_id,
                reviewer_run_id=f"{task.agent_id}:run-1",
                verdict=run.structured_output["verdict"],
                findings=run.structured_output["findings"],
                idempotency={"key_id": owner, "key": f"hosted-review:{session_id}"},
            )
    return {**link, "status": "completed", "revision_n": revision.n, "review": review.public()}
