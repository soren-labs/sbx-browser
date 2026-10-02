# Persistent user/auth foundation

Product auth uses `control/auth_store.py` and the append-only migrations in
`control/auth_schema.py`. This foundation adds no login routes or UI.

| Table | Authority |
| --- | --- |
| `users` | Stable user ID, optional normalized unique email, display name |
| `user_sessions` | User ID, SHA-256 token verifier, absolute expiry, revocation |
| `oauth_accounts` | Unique `(provider, provider_subject)` mapped to one user |
| `api_keys` | SHA-256 key verifier, optional user ID, scopes, expiry, revocation |

These are separate from sandbox session records and operator-managed AI-provider
accounts. Email is metadata, not verified identity. Linking an OAuth identity
requires a trusted server-side caller; an existing identity cannot be reassigned
to another user, and identities never auto-link by matching email. Subjects are
case-sensitive and provider names are normalized. Foreign keys enforce owners.

## Deployment

On Modal, provide **`DATABASE_URL` for an external PostgreSQL database** reachable
from the control plane. Use a dedicated application database and a role able to
create/alter its auth schema; use TLS (`sslmode=require`, or your database's
certificate verification settings). The database must have durable storage and
operator-managed backups. Modal Dicts expire inactive entries and are not the
authority for this auth data.

Store the URL only in a Modal Secret, created through your normal secret-management
workflow. The example below names the Secret without including a database password:

```toml
[secrets]
auth_database = "sbx-auth-database"
```

Equivalent deploy-host setting: `SBX_AUTH_DATABASE_SECRET_NAME=sbx-auth-database`.
The named Secret must contain `DATABASE_URL`; `sbx deploy` checks that the Secret
exists and the control app mounts it. Use a separate auth database Secret;
**never put `DATABASE_URL` in the bootstrap Secret**. Bootstrap rotation replaces
that Secret with only `SBX_V1_BOOTSTRAP_KEY`. Config validation rejects
`secrets.auth_database` / `SBX_AUTH_DATABASE_SECRET_NAME` equal to the configured
bootstrap Secret name, including its default `sbx-v1-bootstrap`. Never place the
URL in `config.toml`, deployment env metadata, a sandbox Secret, source code or
logs. The URL is absent
from the remote-env allowlist. It is not forwarded to sandbox runners.

Server startup requires successful database/schema initialization before accepting
any requests, including the bootstrap `/v1/me` deployment probe. An unreachable
database, missing migration privileges or unsupported schema prevents startup.
Initialization runs synchronously before lifespan readiness. The credential
refresher starts only after it succeeds and stops on lifespan shutdown, including
when a nested router fails. Importing or constructing an app starts no refresher.
Modal auth fails closed if `DATABASE_URL` is absent or invalid; it never falls back
to SQLite or an in-memory store. PostgreSQL uses psycopg 3 with bounded connection,
statement and lock timeouts. Each operation opens and closes a short transaction;
this first PR has no connection pool.

Local mode defaults to `$XDG_STATE_HOME/sbx-browser/auth.sqlite3` (fallback
`$HOME/.local/state/sbx-browser/auth.sqlite3`). Override with an absolute
`SBX_AUTH_DB_PATH`. The SQLite database is created with mode `0600`, enforces foreign
keys and survives process reconstruction. It is for local development/tests, not
a file inside a disposable Modal container. If `DATABASE_URL` is set locally,
PostgreSQL is used instead. Tests strip these ambient settings and use temporary
storage without Modal or cloud connections.

## Migrations and rollout

`AuthDatabase.initialize()` applies missing numbered migrations transactionally;
server startup requires it before serving requests, while construction remains
lazy for module imports. Normal operations also ensure initialization. A PostgreSQL
transaction advisory lock (SQLite `BEGIN IMMEDIATE` locally) serializes concurrent
cold starts. `auth_schema_migrations` records each applied version. Reopening is
idempotent; a failed migration rolls back, and newer/noncontiguous schemas are
rejected. Append migrations; never edit applied versions. Back up the database
before upgrading. The role/search path must remain the same across deployments.

The existing `/v1/api-keys` response fields, scopes and one-time plaintext creation
remain unchanged. New keys and console-exchange keys are durable. Previously
issued **in-memory** keys cannot be recovered during rollout and must be reissued;
the deployment bootstrap credential remains available for this operation.

`SBX_V1_BOOTSTRAP_KEY` remains an operator-only, env-authoritative hash overlay. It
creates no user and is never stored as an ordinary product key. Rotating/removing
the Secret on redeploy removes the old bootstrap authority while preserving
product keys. Its ID retains the existing `key_bootstrap_<digest-prefix>` form.
As before, API revocation of this operator key lasts until restart; permanent
revocation requires rotating/removing the deployment Secret. HTTP Basic operator
access is unchanged.

`sbx open` still creates a one-use short-lived console grant and exchanges it for
an admin/agents key. Tickets remain process-local, so a restart between issuance
and exchange requires issuing another ticket. Redeemed keys now survive restart.
Legacy operator-issued keys and console keys have a null user ID; they are not
automatically assigned to a future end user.

## Server-side primitives and PR2

- `AuthStore.create_user/get_user/find_user_by_email` manage user records.
- `create_session/lookup_session/revoke_session` manage user sessions. Tokens use
  256 random bits; only SHA-256 verifiers are stored. Lookup rejects expired and
  revoked sessions, including exactly at the expiry boundary. Revocation requires
  the owner ID. No session credential is accepted as an API key.
- `link_oauth_account/lookup_oauth_account/list_oauth_accounts` manage external
  identities with database-enforced uniqueness and idempotent links.
- `PersistentApiKeyStore` implements the frozen `ApiKeyStore` port. Server-side
  callers can additionally pass `user_id` and optional `ttl_s` at creation, and
  filter listing/revocation by owner. Keys also use 256 random bits. Only creation
  returns plaintext; list/lookup never recover it. Revoked rows retain metadata.

PR2 should add email/password credential hashing and verification, verified-email
policy, Google/GitHub authorization callbacks with state/PKCE, explicit identity
linking, secure cookie/CSRF handling, logout and user-scoped key endpoints. Use
the authenticated user ID with the owner-filtered key primitives; existing admin
routes remain operator-wide. Additive migrations can extend the existing user
model for password/verification metadata. Provider connections, password reset,
email delivery, credential cleanup/retention jobs and production AI-provider OAuth
are separate follow-ups. No such flows are implemented here.

For optional cloud-free driver verification, point `SBX_TEST_POSTGRES_BIN` at local
PostgreSQL binaries and run
`uv run pytest tests/integration/control/test_auth_postgres.py`. The fixture creates
and destroys its own cluster and private Unix socket, including an actual database
restart; it never uses an existing database or `DATABASE_URL`. These checks skip
when binaries are unavailable in the normal test environment.
