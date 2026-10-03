# Hosted Modal onboarding

SOR-283 adds user-owned Modal connections and a reconcilable Codex runtime
installation. The authenticated Integrations page submits Token ID/Secret or
starts authorization. Workspace verification, `sbx-compute` namespace creation,
versioned `sbx-runtime` image publication and smoke verification update durable
PostgreSQL connection records before Ready.

Set `SBX_CONNECTIONS_MODE=mock` for the deterministic workspace adapter and build
the console with `VITE_HOSTED=1`. Configure `SBX_CONNECTION_ENCRYPTION_KEY` as a
URL-safe base64 encoded random 32-byte key, managed outside the repository.
Keep the same key across process restarts; losing it makes stored connections
unreadable. There is no insecure encryption fallback. Tokens never appear in
responses, progress or logs. AES-GCM binds ciphertext to the user, provider and
connection. Authorization states expire after ten minutes and are consumed once.

`ModalProvider` is the production adapter boundary. Each workspace/image/smoke
call receives a user-owned `ModalContext`; adapters must construct a client from
that context, without ambient operator credentials. OAuth exchange and redirect
are separate adapter operations. Without an injected configured adapter, the
production connection is explicitly unavailable. The fake exercises all steps
and independent workspaces without accessing Modal. Real SDK/OAuth integration,
image publication and smoke acceptance still require external credentials.

Provisioning uses a durable five-minute lease and optimistic versions. Repeating
a Ready installation of the current runtime makes no provider calls. Failures
retain safe progress and permit retry; expired leases permit reconciliation after
a crash. Adapter namespace/image operations must be idempotent. A concurrent
reconnect supersedes the old installation rather than overwriting its credentials.
Business state remains on the VPS PostgreSQL database.
