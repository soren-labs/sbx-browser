# Hosted control-plane state (SOR-282)

Run FastAPI as a normal long-lived process (`uvicorn control.app:app`). Set
`SBX_HOSTED=1`, `SBX_STATE_BACKEND=postgres`, and secret-managed `DATABASE_URL`.
State initialization must succeed before readiness. Missing PostgreSQL fails
closed. Compute selection remains independent: `SBX_BACKEND=modal` selects
sandbox compute, never business-state storage in hosted mode. Local/test defaults
retain existing file/in-memory stores; explicit test injection supports SQLite.

Migration 3 adds indexed, versioned `control_records` documents. Typed PostgreSQL
adapters reuse existing record serializers and lifecycle logic for Sessions,
Runs, transcripts, Tasks, workflow bindings, workspaces, artifacts, revisions,
reviews, checkpoints and environment metadata. Every record operation reads or
writes the database; no Modal Dict or process cache is the authority. Artifact
payloads are encoded in the database, not a VPS temporary directory. Owners are
immutable; related records inherit the Session owner. Workflow scans remain
authoritative when a concurrent index update is lost.

Product authentication resolves browser cookies and personal Bearer keys to a
stable `user_id`. The existing product dependency normalizes its ownership
context to this ID, preserving Session API shapes and reusing all existing
owner checks. Key metadata still uses the real key ID. Two keys of one user
therefore share resources; another user receives 404. Browser mutations enforce
same-origin JSON. Operator keys cannot access hosted product resources; migration
and operator metadata/admin routes retain their existing authorization.

This is a single long-lived control process with database-backed durable state.
Existing store protocols retain last-writer update semantics; multi-process job
dispatch/leader election is a later scaling concern. PostgreSQL is the source
of truth, not an incidental cache. Legacy ownerless records are not silently
assigned to users. Connection-provider secret storage is added in later stages.
