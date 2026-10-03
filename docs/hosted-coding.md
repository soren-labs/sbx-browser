# Hosted coding loop

The hosted console selects repositories from the authenticated user's GitHub
installation. Its existing V2 Session composer selects ref, Codex connection,
model, reasoning effort and delivery intent. Progress, command/test evidence,
changed files/diffs, follow-ups, cancellation and retry use the existing Session
API and runner.

Changes can be delivered as a draft PR, then updated to ready. The Review panel
launches a separate Codex Session against the delivered branch. The server pins
its target revision and head before dispatch and enforces a strict JSON output
contract. Only a successful, validated reviewer run records a verdict through
the existing RevisionService. Results and requested fixes survive reconstruction;
polling/replay records each reviewer once. New author revisions stale prior
reviews. Follow-ups implement fixes; update the PR and launch another review.

Merge retains the existing independent-review and exact-commit gate, plus the
console confirmation dialog. Draft PRs cannot merge. Credentials, filesystem
paths and transport details are absent from the product flow.

With `SBX_CONNECTIONS_MODE=mock`, user-owned GitHub repositories are private local
bare repositories. Runtime clones and durable artifact pushes execute real Git
operations through an explicit transport seam. Fake upstream PR metadata is
persistent and owner-scoped. `tests/fakes/hosted_codex.py` runs a deterministic
local greeting test, commits changes and produces requested-fix/approval review
results. Production does not enable this fake transport.

Acceptance: `tests/unit/test_hosted_workflow.py` and
`tests/e2e/test_hosted_workflow_browser.py` exercise coding, changes, tests, draft
PR, independent review, follow-up fix, ready PR, re-review and merge. Browser
streaming is direct to the fake sandbox HTTP runtime; durable history lives on
the API server. Real GitHub permissions/PRs and subscribed Codex execution still
require separate credential-backed acceptance.
