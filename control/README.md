# control/

`sbx-control`：会话 API、状态机、SSE、reaper。后端抽象 `backend.py` 在 WP0 合入后冻结。API 契约见 `docs/contracts/api.yaml`（覆盖 Linear `SOR-31`）。

## 布局

| 文件 | 说明 |
| --- | --- |
| `backend.py` | **冻结** `SandboxBackend` / `LocalProcessBackend` / `ModalBackend` 骨架 |
| `backends/modal.py` | 生产 `ModalBackend`（P0 签名；测试不实例化、不调用） |
| `app.py` | 纯 FastAPI。测试只导入这里。本地 `python -m control.app`（uvicorn），**不是** deploy |
| `deploy.py` | `deploy()`：`python -m modal deploy -m control.modal_app`。`make deploy` / WP1-A `invoke_control_deploy` 入口 |
| `modal_app.py` | `@modal.asgi_app()` + `@modal.concurrent(max_inputs=20)` + reaper `Cron("*/5 * * * *")`；`CONTROL_IMAGE` 带 FastAPI 栈 |
| `store.py` | `SessionStore` / `InMemoryStore` / `ModalDictStore` |
| `run_store.py` | SOR-82/A1 durable run ledger：`RunRecord` / `RunLedger`（terminal 单调不可逆）/ `InMemoryRunStore` / `FileRunStore` / `ModalDictRunStore`（`sbx-runs` Dict） |
| `reaper.py` | 纯函数 `reap(store, backend, now)` |
| `service.py` | 状态机 `creating → idle ⇄ running → closed \| timed_out \| lost` |

## 本地运行

```bash
SBX_BACKEND=local SBX_RUNNER_CMD="python tests/fakes/stub_runner.py" \
  uv run python -m control.app
```

默认 HTTP Basic 为本地假口令 `sbx` / `sbx`（不是生产凭证）。生产从 Modal Secret `sbx-basic-auth`（`SBX_BASIC_USER` / `SBX_BASIC_PASS`）读取。Codex 凭证来自 Secret `sbx-codex-auth`（键 `CODEX_AUTH_JSON`），或进程环境里的 `CODEX_AUTH_JSON`（`Secret.from_dict`）。

Modal 部署：

```bash
uv run python -c "from control.deploy import deploy; deploy()"
```

## 环境变量

| 变量 | 含义 |
| --- | --- |
| `SBX_BACKEND` | `local`（默认）或 `modal` |
| `SBX_RUNNER_CMD` | runner 可执行前缀；WP1-B 未合入时指向 `tests/fakes/stub_runner.py` |
| `SBX_SSE_KEEPALIVE_SECONDS` | SSE `: keepalive` 间隔，默认 15 |
| `SBX_MAX_CONCURRENT` | live agent/sandbox 上限：每 key 默认 2，scheduler 全局默认 8；`creating`/`idle`/`running` 都占槽位——idle 等 follow-up 的 agent 关闭（`DELETE /v1/agents/{id}`）或被回收前仍占槽 |
| `SBX_IDLE_TIMEOUT_S` | 空闲回收阈值，默认 1800 |
| `SBX_RUN_STORE_DIR` | 本地 run ledger 落盘目录；默认 `$XDG_STATE_HOME/sbx-browser/runs`（modal 后端用 `sbx-runs` Dict） |

## User accounts and browser sessions (PR A)

`control/user_auth/` is the account identity subsystem; provider accounts and
credentials remain separate. Registration creates a stable `usr_*` owner and a
browser session, **no API key**. Both that user's cookies and explicitly created
developer keys resolve to the same owner in `/v1` and `/v2`. Normal users only
receive `agents` permission. Frozen protocols/contracts are unchanged.

| Endpoint | Request / behavior |
| --- | --- |
| `POST /auth/register` | JSON `{email, password}`; 201, signed-in user/session |
| `POST /auth/login` | Same JSON; 200, signed-in user/session; generic failure |
| `GET /auth/me` | Current `{user: {id, email, created_at}, session: {expires_at, csrf_token}}` |
| `POST /auth/logout` | 204; revoke current session and clear cookie |
| `POST /auth/session/rotate` | Revoke current session, issue fresh cookie + CSRF token |
| `POST /auth/api-keys` | JSON `{label}` (optional label); 201 metadata + `key` **once** |
| `GET /auth/api-keys` | Own metadata only, including `revoked_at`; no secrets/verifiers |
| `DELETE /auth/api-keys/{id}` | 204, idempotent for own keys; other owners get 404 |

The SPA sends cookies (`credentials: 'same-origin'`), obtains `csrf_token` from
`/auth/me`, and sends `X-CSRF-Token` on every cookie-authenticated mutation.
Keep that CSRF token in memory; the session token is never returned in JSON and
never stored in JS/localStorage. Login and registration require an exact Origin,
including when no session exists. All other cookie-authenticated mutations require
both exact Origin and the session's CSRF token. Missing/null/cross-site origins
are rejected; GET remains usable after a future OAuth top-level redirect.
Responses carrying browser auth data are `Cache-Control: no-store`.

Email policy: trim surrounding whitespace and lowercase ASCII mailboxes; reject
malformed/Unicode addresses (IDNs can use punycode). Do not rewrite dots or +tags.
Passwords are 12–256 characters, unmodified, no control characters; Argon2id
uses a random salt, 64 MiB memory, 3 passes, one lane (`argon2-cffi`). Unknown
emails use a dummy verifier and receive the same login error as wrong passwords.
Email verification, reset/recovery and social login are follow-ups; this PR's
email is an account identifier, not proof of mailbox ownership. Future OAuth
must not implicitly link an unverified email to a local identity.

Sessions contain 256 bits of entropy; only SHA-256 verifiers are persisted.
`__Host-sbx_session` is HttpOnly, Secure, Path=/, no Domain, SameSite=Lax. Sessions
have a fixed expiry (default/max 7 days), without sliding renewal. Login replaces
and revokes the browser's previous session; explicit rotation creates a new
bounded lifetime and CSRF token. Logout affects the current session; other devices
remain signed in. Revocation checks use durable rows with no per-process auth
cache. Already authorized in-flight work is not cancelled by logout.
Developer keys also use 256-bit secrets with SHA-256 verifiers and permanent
revocation. They have `agents` scope only and survive session logout.

| Configuration | Default / deployment requirement |
| --- | --- |
| `SBX_AUTH_DATABASE_URL` | PostgreSQL DSN; **required on Modal and any multi-host deployment**; put in a Secret, never the function env overlay |
| `SBX_AUTH_SECRET_NAME` | Optional named Modal Secret to mount the DSN on control + reaper; alternatively use an existing mounted control Secret |
| `SBX_AUTH_ORIGIN` | Exact public HTTPS origin, e.g. `https://sbx.example`; set in production behind proxies/custom domains; otherwise request base origin |
| `SBX_AUTH_COOKIE_SECURE` | `true`; `false` is local HTTP development only and is rejected on Modal |
| `SBX_AUTH_SESSION_TTL_S` | `604800`; allowed range 300–604800 |
| `SBX_AUTH_SQLITE_PATH` | Local-only `$XDG_STATE_HOME/sbx/auth.sqlite3` (fallback `$HOME/.local/state`); private directory/file permissions |

Production requires shared PostgreSQL reachable by every control container and
reaper, with TLS (`sslmode=verify-full` and appropriate CA configuration), backups,
and privileges to initialize `sbx_auth` and its indexes. `psycopg[binary]>=3.2.0`
and `argon2-cffi>=23.1.0` ship in the Python dependencies and Modal control image.
No database URL or user-auth material is forwarded into worker sandboxes.
SQLite supports multiple local processes on one host; do not put it on a shared
Modal Volume/network filesystem. Modal Dict is unsuitable for permanent identity:
[entries expire after seven days of inactivity](https://modal.com/docs/sdk/py/latest/Dict).
Sandbox/task/provider persistence continues to use the existing stores.

A primary key plus atomic `INSERT ... ON CONFLICT DO NOTHING` reserves each
normalized email with its complete user row. Sessions and keys are immutable;
revocations are independent insert-only tombstones, so concurrent issuance cannot
overwrite a revocation. First-start PostgreSQL DDL is serialized with an advisory
transaction lock. Rate slots use the same atomic insert primitive: at most 10
login attempts/email and 30/ASGI peer per 15-minute fixed window, 10 registrations
per peer, and 20 key creations/user. Successful attempts count too; window
boundaries can allow two adjacent windows' quotas. Four password jobs/process
bound memory/CPU; operators should add edge abuse controls for public deployments.
The peer is the trusted ASGI client address, not an untrusted X-Forwarded-For;
behind a proxy, configure trusted forwarding or expect a shared peer quota.
429 auth errors have `rate_limited`, `retry_after` and `Retry-After`.

Modal's existing reaper prunes expired session/revocation/rate rows every five
minutes. Standalone/local deployments can schedule `AuthService.store.prune`
with the current Unix timestamp. User records, keys and permanent key revocations
are never pruned. Use a fresh dedicated database/schema per deployment; do not
share identity storage between staging and production.

Legacy bootstrap/admin Bearer keys, `/v1/api-keys` operator APIs, CLI and console
grants keep their existing behavior. Their owner namespace remains distinct;
legacy non-admin keys retain access to legacy deployment resources but cannot
read normal users' agents/artifacts. Operator admin access remains privileged.
Explicit Authorization takes priority over cookies, and an invalid/revoked key
never falls back to a browser session. User artifact listings filter by owner
before pagination; this currently scans producer records/manifests and can need
an owner index at larger scale. Orphan artifacts without a producer owner fail
closed for normal users. The console UI is unchanged for follow-up PR C; OAuth
is follow-up PR B. GitHub repository authorization behavior is unchanged.

Tests are cloud-free. Optional PostgreSQL integration tests accept only a
passwordless loopback `SBX_TEST_AUTH_POSTGRES_URL` whose database name begins
`sbx_auth_test_`; use a disposable local server. The default lane uses SQLite
and skips those two PostgreSQL cases.
