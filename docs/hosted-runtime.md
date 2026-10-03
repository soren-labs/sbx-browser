# Hosted Session compute and direct events

A hosted Session requires its user's Ready Modal runtime and owned Codex
connection. Existing Session creation, repository preparation, runner execution,
run recording and history remain in use. `HostedModalBackend` passes a decrypted
user-owned ModalContext to every compute operation; operator credentials and
managed secrets are not mounted. Sandbox tags and PostgreSQL linkage record the
owner, connection, workspace, image/runtime version and agent. Codex access leases
are injected by the hosted credential seam, without refresh tokens.

`ComputeProvider` is the production SDK boundary. It creates/executes/polls/lists/
terminates only within the supplied context and supplies a sandbox HTTP endpoint.
Use `runtime.image.sbx_hosted_runtime_image` to extend the existing Codex runner
image with FastAPI/uvicorn/PyJWT. Start `uvicorn runtime.http_service:from_env
--factory --host 0.0.0.0 --port <runtime-port>` with the sandbox id,
`SBX_RUNTIME_CONNECT_KEY`, `SBX_RUNTIME_OWNER`, `SBX_RUNTIME_AGENT_ID`, `SBX_WORK`
and explicit `SBX_BROWSER_ORIGINS`. Modal account credentials never enter that
process. The adapter must provide HTTPS ingress and stop it with the sandbox.
Without a configured compute adapter, production compute is explicitly unavailable.

Mock compute delegates to the existing LocalProcessBackend and starts a real
loopback HTTP service on a distinct origin. It uses the actual runner/fake CLI in
browser tests, without Modal networking. Its loopback-only development CORS is
separate from the production origin allowlist. App shutdown stops fake HTTP
servers; reconstruction can recreate ingress for a surviving local sandbox.

`POST /hosted/sessions/{session_id}/connect` verifies task and agent ownership,
then issues a sixty-second read-only signed grant. The signing key is unique per
sandbox and encrypted in PostgreSQL. The runtime validates owner, Session,
audience, scope and bounded expiry. No query-string credentials are used. A
stream ends at expiry; the browser reconnects with a new grant and Last-Event-ID.
Terminal/WebSocket are advertised as disabled future seams, not implemented.

The console uses browser cookie auth in hosted mode. It attempts direct fetch SSE,
waits briefly for provisioning, and falls back to relayed V2 SSE when needed. Both
streams use `runtime.session_events` normalization and canonical line cursors.
Durable Session status is still read from the VPS; history/replay survives sandbox
or ingress loss. Hosted detail caching is disabled to prevent browser account
switches from reading an earlier user's local cache.

Real user Modal creation/image binding, HTTPS ingress/CORS, sandbox lifecycle and
long-running Codex access expiry still require production credential acceptance.
