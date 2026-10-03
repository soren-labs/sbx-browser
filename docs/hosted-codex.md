# Hosted Codex credential broker

Codex / ChatGPT plan authorization is independent of Modal and GitHub.
Integrations starts a user-bound expiring authorization state and exchanges a
provider code on the VPS. `CodexProvider` supplies redirect, exchange and refresh
operations; the production default is explicitly unavailable. Mock mode uses a
deterministic rotating fake, storing fake upstream grant hashes durably too.

The shared connection vault encrypts the full credential document. The broker
issues short access leases containing no refresh token. Runtime credentials use
the existing `SBX_ACCOUNT_CREDENTIAL` restoration shape. Hosted sandboxes cannot
write authoritative credential state: legacy CredentialSync and the sandbox CLI
refresh worker are disabled in hosted mode. The existing account scheduler and
capability types remain intact, exposed through owner-filtered registry views.
Each Codex connection allows three concurrent Sessions; each user's scheduler and
compute capacity have a five-sandbox bound. Existing cooldown feedback is reused.

Refresh runs proactively every fifteen seconds, near expiry (sixty-second margin)
and on demand. A database-serialized, committed claim exposes Refreshing while
upstream work is in flight. Contenders reread and await that claim instead of
rotating independently. CAS commits the encrypted replacement and credential
version together. Omitted refresh tokens preserve the prior grant. A reconnect or
disable supersedes any stale success or failure. Provider adapters must bound
network requests below the sixty-second claim lifetime (recommended fifteen
seconds) and classify InvalidGrant separately from transient/rate-limit errors.

Revocation enters Reauth required. Transient failures preserve the encrypted
grant and apply a durable cooldown. If a process dies after claiming rotation,
the expired claim requires reauthorization: replaying a possibly consumed refresh
token would be unsafe. A successful upstream rotation followed by database
failure likewise leaves the durable claim for containment. No token material or
upstream error body is logged. `execute` retries one authentication failure with
a fresh credential; simultaneous stale failures join the already rotated version.

Design references studied for SOR-285:
[Sub2API OAuth refresh](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/oauth_refresh_api.go)
uses lock/reread/versioned persistence, retained credential fields and race
recovery. Its
[credential persistence boundary](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/account_credentials_persistence.go)
centralizes credential writes. SBX applies those patterns to its existing
PostgreSQL connection/CAS foundation, with fail-closed interrupted-claim recovery.
No third-party code is copied.

Real authorization redirects, provider expiry/rotation semantics and authenticated
Codex execution require the production adapter and later credential validation.
The fake covers rotation, omission, three contenders, revocation, transient
failure, reactive retry, reconstruction, stale reconnect and interrupted claims.
