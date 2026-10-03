# Hosted Alpha authentication (SOR-281, Stage 1)

Open `/auth` on the control-plane origin to register, verify a six-digit email
code, set a password, sign in and sign out. The page keeps verification grants
and entered credentials in memory. It does not store them in browser storage.
Passwords require 12–128 characters. Alpha has no social login or password reset.

Stage 1 provides identity and browser authentication. Existing `/api`, `/v1` and
`/v2` operator APIs still require their existing Basic/Bearer credentials. A browser
session confers no operator scope. Sandbox ownership and connection to the main
console are Stage 2 work; this page ends at the signed-in identity view.

## HTTP flow

All POST requests require `Content-Type: application/json`. Browser requests must
be same-origin; cross-origin `Origin`/Fetch Metadata and form submissions are
rejected, including login/logout. API callers without an `Origin` may use JSON.
Do not enable permissive CORS for these routes. Responses use `Cache-Control:
no-store`. Validation failures return only `invalid_request`, never rejected input.

| Route | Request | Result |
| --- | --- | --- |
| `POST /auth/register` | `email` | 202: `challenge_id`, `expires_in_s`, `resend_after_s` |
| `POST /auth/verify` | `challenge_id`, `code` | 200: one-use `registration_token` |
| `POST /auth/password` | `registration_token`, `password` | 201: `user`, signed-in cookie |
| `POST /auth/login` | `email`, `password` | 200: `user`, new signed-in cookie |
| `GET /auth/me` | Browser cookie | 200: current `user`; 401 otherwise |
| `POST /auth/logout` | Empty JSON object | 204: revoke current session, clear cookie |

Email is trimmed/lowercased, matching the existing foundation. Registration for
an existing user returns the same 202 shape but sends no code and cannot replace
credentials or claim an existing foundation/OAuth identity. Wrong passwords and
unknown users both return `invalid_credentials` and perform Argon2 verification.
Existing unverified foundation users need a future explicit migration policy.

Codes expire after 10 minutes, with a 60-second resend cooldown and at most five
guesses per challenge. Malformed guesses count. Successful verification consumes
the OTP and issues a 256-bit password-setup grant valid for 10 minutes. Resending
replaces the challenge and invalidates its old OTP and grant. Setting a password
consumes the grant once. User, credential and initial session creation share one
transaction. Exact expiry boundaries fail. PostgreSQL row/advisory locks and
SQLite write transactions serialize consumption across independent processes.

Passwords and OTPs use independently salted Argon2id hashes: 19 MiB, two
iterations, one lane. See the [argon2-cffi API](https://argon2-cffi.readthedocs.io/en/stable/howto.html)
and [OWASP password-storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
Grants and session tokens use SHA-256 verifiers; plaintext is never stored in the
database. OTP hashes are removed on verification; grant hashes on consumption.

## Browser sessions

The host-only `__Host-sbx_session` cookie has `HttpOnly`, `Secure`, `SameSite=Lax`,
`Path=/`, no `Domain`, and a seven-day maximum age. Serve the hosted page over
HTTPS, with the ASGI request scheme/host matching the public origin. Configure
trusted proxy forwarding correctly rather than disabling origin checks. Tests
use an HTTPS TestClient origin. Signing in rotates/revokes the browser's previous
session. Logout affects that browser session only; other devices/users retain
their own sessions. The server enforces expiry and revocation from durable storage.

## Mock email and production seam

`EmailSender.send_verification(email=..., code=..., expires_in_s=...)` is the
production adapter seam. A future provider adapter (for example Resend) should
deliver this message and raise on failure; credentials stay in deployment secrets.
Provider exceptions are replaced with a safe 503 error. Delivery failure removes
that challenge and permits immediate retry, subject to rate limits.

`MockEmailSender` is an explicit, deterministic in-process outbox. It makes no
network requests, writes no files, logs no codes and hides codes in message reprs.
Tests pass it to `create_app(email_sender=sender)` and call
`sender.latest_code(email)` to exercise the real HTTP flow. Generated codes remain
cryptographically random; deterministic mock delivery never means a fixed OTP.
Local development defaults to this sender; `app.state.hosted_auth.sender` exposes
the outbox to trusted in-process development tooling. There is no public code
retrieval endpoint. Do not expose this object via HTTP or print it to logs.

Modal defaults to disabled email delivery and returns 503 for new registrations
until a sender is injected. `SBX_AUTH_EMAIL_MODE=mock` explicitly enables the
test/dev outbox on Modal; `disabled` works in either mode. Unsupported values
fail at app construction. The mock outbox is intentionally lost on restart;
verification challenges and their authority remain in the database. Tests retain
delivered messages independently while reconstructing the app.

## Durable limits and deployment

The default `DatabaseAuthRateLimiter` consumes email and connection-peer-IP
budgets in durable fixed 15-minute windows. Register allows 5/email and 20/IP;
verify 20/email and 100/IP; password setup 10/email and 50/IP; login 10/email and
50/IP. Cooldown/limit responses are 429 with `Retry-After`. Counters use hashed
bucket identifiers, storing no raw IP address. Client `X-Forwarded-For` is never
read directly. A deployment's trusted proxy must provide the peer IP correctly.
`create_app(auth_rate_limiter=...)` can inject an edge/distributed limiter via
`AuthRateLimiter.check(action=..., email=..., ip=...)`.

Migration 2 appends credentials, challenges and limit tables to the existing auth
schema. Migration 1 is unchanged; existing users, sessions and keys survive.
Hosted Modal auth continues to require external durable PostgreSQL via
`DATABASE_URL`, with fail-closed startup and no in-memory fallback. See
[persistent auth deployment](auth-persistence.md) for secrets, backups and local
SQLite isolation. Challenge/counter cleanup jobs are a later operational follow-up.

## Verification

`make lint` and `make test` cover the unit/HTTP flow, failure paths, concurrency,
expiry, limits, migration preservation and process reconstruction without cloud
credentials. Optional real PostgreSQL tests create their own local cluster and
private socket, including database restart and concurrent OTP/grant consumption:

```bash
SBX_TEST_POSTGRES_BIN=/path/to/postgresql/bin \
  uv run pytest tests/integration/control/test_auth_postgres.py
```

The optional Chromium smoke exercises the page against the real local app and
reads its mock outbox in-process. Install the browser separately, then run:

```bash
uv run --with playwright playwright install chromium
uv run --with playwright pytest tests/e2e/test_hosted_auth_browser.py
```
