# runner CLI

Sandbox 内驱动 agent CLI 会话的进程入口。可执行文件名约定为 `runner`（测试假件：`tests/fakes/stub_runner.py`）。工作目录语义见 `filesystem.md`；事件见 `events.md`。v1 行为与 Linear `SOR-30` 一致；v2（`SOR-59` 起）扩展为多 provider，向后兼容。

环境：`SBX_WORK`（生产 `/work`）、`HOME=$SBX_WORK/home`、`CODEX_HOME=$HOME/.codex`、`CODEX_BIN`（默认 `codex`，测试指向 `tests/fakes/fake_codex.py`）、`SBX_WORKDIR`（可选，SOR-174：声明的 workspace workdir，相对 `$SBX_WORK`；未设置时 provider CLI cwd = `$SBX_WORK`）。

## Provider

`--provider` 取值：`codex | antigravity | grok | opencode | devin`。每个 provider 由 `runtime/runner/adapter.py` 的 `AgentAdapter` 驱动：argv 构造、事件翻译、原生 session id 提取、健康判定。

## 凭证注入

凭证经环境变量 `SBX_ACCOUNT_CREDENTIAL` 注入，值为 JSON blob：

```json
{"provider": "codex", "files": {".codex/auth.json": "<file content>"}}
```

- `files` 的 key 是相对 `$HOME` 的路径；`init` 时逐个还原到 `$HOME` 下，文件权限 **600**。
- 内容可以是原文或 base64（由控制面约定）；blob 的 `provider` 必须与 `--provider` 一致，否则 `init` 失败。
- 还原完成后，runner **不得**让 CLI 子进程继承 `SBX_ACCOUNT_CREDENTIAL`（codex 写入 `shell_environment_policy.exclude`；其他 provider 由 adapter 在子进程 env 中剔除）。
- v1 兼容输入：`CODEX_AUTH_JSON`（或 `$SBX_WORK/auth.json`）仅在 `--provider codex` 时仍被接受，写入 `$CODEX_HOME/auth.json`。
- `SBX_ACCOUNT_ID`（可选）：当前账号 id，记入 `session.json.account_id` 与 `sbx.session_meta`。

## 命令

### `runner init --provider P --model M [--auth auth_json|provider] [--reasoning-effort E]`

SOR-179：`--reasoning-effort` 为可选的规范 effort 级别（SOR-204 扩展为 `none|minimal|low|medium|high|xhigh|max`），记入 `session.json.reasoning_effort` 并作用于该 session 的每一轮（首轮与 resume 轮）。provider 无法兑现的组合由 init 显式失败（退出码 2），绝不静默忽略；规范级别之外的取值同样失败。是否真正可用由账号的 capability 目录决定（SOR-204），并非全部级别对每个 provider/模型都开放。原生映射：codex 写入 `config.toml` 的 `model_reasoning_effort`（首轮与 resume 均生效）；antigravity / grok 由 adapter 在 argv 传 `--effort <E>`。

1. 创建 `$HOME`（=`$SBX_WORK/home`）与 `$CODEX_HOME`（=`$HOME/.codex`），写入 `config.toml`（最小内容）：

   ```toml
   model = "<M>"
   approval_policy = "never"
   sandbox_mode = "danger-full-access"

   [shell_environment_policy]
   exclude = ["CODEX_AUTH_JSON", "SBX_PROVIDER_API_KEY", "SBX_ACCOUNT_CREDENTIAL"]
   ```

   凭证经环境变量注入后写入 `$CODEX_HOME/auth.json`，随后 **不** 让 Codex 子进程继承 `CODEX_AUTH_JSON` / `SBX_ACCOUNT_CREDENTIAL`。
2. 按上节还原 `SBX_ACCOUNT_CREDENTIAL` blob（**不得**把真实 token 写入仓库或日志）。`--auth` 保留一版作 codex 兼容别名（仅 `--provider codex` 有意义）：
   - `auth_json`：v1 路径——`CODEX_AUTH_JSON` 或 `$SBX_WORK/auth.json` → `$CODEX_HOME/auth.json`；无输入时写占位文件；所有 token 字段必须是 `REDACTED`；文件权限 **600**
   - `provider`：写入 provider 登录占位（token 字段同样 `REDACTED`），权限同样 600，不发起网络登录
3. 写入 `$SBX_WORK/AGENTS.md`（sandbox 内说明，不是仓库根 `AGENTS.md`）
4. 初始化空的 `events.jsonl`、`events.raw.jsonl`、`session.json`（`native_session_id` 空、`codex_session_id` 别名同值、`provider`、`account_id`、`reasoning_effort`、`turn` 0）、`inbox/`、`turns/`

### `runner turn --n N --message-file F [--max-seconds S]`

- 默认 `--max-seconds` **900**。
- 将 `F` 复制为 `$SBX_WORK/inbox/<N>.md`。`$PROMPT` 取该文件全文。
- 第 `N` 轮调用该 provider adapter 的 `first_turn_argv`（`N=1` 且无 `native_session_id`）或 `resume_argv`（后续轮，id 来自 `session.json.native_session_id`）。**stdin 关闭**，prompt 只作位置参数，不用 `-`。
- **provider CLI 工作目录**（SOR-174）：进程 cwd = `$SBX_WORK/$SBX_WORKDIR`（声明了已 prepare 的 workspace 时），否则 `$SBX_WORK`；首轮与 resume 轮一致。带目录参数的 CLI 同步指向该目录——codex 首轮 `-C`、opencode `--dir`、devin ACP `session/new` / `session/load` 的 `cwd`；无目录参数的 CLI（antigravity、grok、devin `acp` 进程、codex `exec resume`）只依赖进程 cwd。`$SBX_WORK` 仍是 runner 状态根（`session.json`、`events*.jsonl`、`inbox/`、`turns/`、`home/`）。
- Codex provider 第 1 轮调用（`<workdir>` = 上述 CLI 工作目录）：

  ```
  $CODEX_BIN exec --json --skip-git-repo-check -C <workdir> \
    --dangerously-bypass-approvals-and-sandbox -m <model> \
    "$PROMPT"
  ```

  第 2 轮及以后：

  ```
  $CODEX_BIN exec resume --json --skip-git-repo-check -C <workdir> \
    --dangerously-bypass-approvals-and-sandbox <session_id> \
    "$PROMPT"
  ```

  `session_id` 来自 `session.json.native_session_id`（即首轮 `thread.started.thread_id`；`codex_session_id` 别名）。后续轮 `thread.started.thread_id` 与首轮相同。
- CLI stdout **逐行原样追加**到 `$SBX_WORK/events.raw.jsonl`；每行经 adapter `translate` 归一化后（Codex 为恒等透传，且经脱敏）追加到 `$SBX_WORK/events.jsonl`，同时写 runner 自己的 stdout。以 `\n` 为界切行；无法解析的行计为坏行并继续，最终退出码 4。
- 第 1 轮在 `sbx.turn_started` 之前插入一次 `sbx.session_meta{provider, model, account_id, reasoning_effort}`（不含凭证；`reasoning_effort` 为 session.json 中 init 记下的值，未声明时为 null）。每轮在 CLI 输出前后插入 `sbx.turn_started` / `sbx.turn_finished{status,exit_code,duration_s,usage}`；异常插入 `sbx.error`。
- 解析首个原生 session 标记（Codex：`thread.started.thread_id`）、`turn.completed.usage`、最终 `agent_message`，写入 `turns/<N>.json`，更新 `session.json`（`native_session_id`、别名 `codex_session_id`、`turn`）。
- 软超时：到达 `S` 秒后对 CLI 进程 **SIGTERM**，再等 **30 s** 收尾；仍未退出则 SIGKILL。超时退出码 3。
- adapter `health_from` 判定 `auth_invalid` 时退出码 5（见下）。

### `runner export-credentials`

- 把当前 `$HOME` 下该 provider 的凭证文件（adapter `credential_files`）重新打包为凭证 blob，JSON 打到 **stdout**，退出码 0。
- 与注入的 blob 无变化时输出 **空**（stdout 为空）。
- 控制面读 stdout 更新该账号的 Secret；**任何实现都不得把该 stdout 写进日志或事件流**。

### `runner stop`

终止当前正在进行的 `turn`（对 CLI 子进程 SIGTERM → 宽限 → SIGKILL），不删除 `$SBX_WORK`。

## 进程约束

1. **必须关闭 stdin**。Codex 在 stdin 为打开的管道时会等待 EOF 后才继续（help 原文：*If stdin is piped and a prompt is also provided, stdin is appended as a `<stdin>` block*；P0 在 Sandbox 内 25 s 超时复现挂死）。runner 启动 CLI 时：
   - Python：`stdin=subprocess.DEVNULL`
   - shell：`</dev/null`
2. **prompt 只通过位置参数传递，不使用 `-`。**
3. **stdout 按行消费**。一个 chunk 可能含多行 JSON；以 `\n` 切分后再 `json.loads`。控制面子进程见 `control/backend.py`（`Process.stdout` 保证按行产出；Modal 实现必须 `sb.exec(..., bufsize=1)`）。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 2 | provider CLI 进程非 0 |
| 3 | 超时 |
| 4 | 事件流中出现无法解析的非 JSON 行（坏 JSON） |
| 5 | 认证失效（adapter `health_from` 判 `auth_invalid`） |

（无退出码 1 的契约语义；假件内部错误可用 1，但不作为跨包约定。）

```canonical-yaml
commands:
  - init
  - turn
  - stop
  - export-credentials
providers:
  - codex
  - antigravity
  - grok
  - opencode
  - devin
exit_codes:
  "0": success
  "2": cli_nonzero
  "3": timeout
  "4": bad_json
  "5": auth_invalid
error_codes:
  - 400
  - 401
  - 404
  - 409
  - 429
error_subcodes:
  - unauthorized
  - not_found
  - invalid_provider
  - turn_in_progress
  - session_not_runnable
  - account_busy
  - account_unavailable
  - provider_exhausted
  - concurrency_limit
paths:
  - inbox/<n>.md
  - turns/<n>.json
  - events.jsonl
  - events.raw.jsonl
  - session.json
workdir_env: SBX_WORKDIR
default_workdir: repo
codex_events:
  - thread.started
  - turn.started
  - item.started
  - item.updated
  - item.completed
  - turn.completed
  - turn.failed
  - error
item_types:
  - agent_message
  - command_execution
  - file_change
  - reasoning
  - error
runner_events:
  - sbx.turn_started
  - sbx.turn_finished
  - sbx.error
  - sbx.session_meta
usage_fields:
  - input_tokens
  - cached_input_tokens
  - output_tokens
usage_fields_optional:
  - cache_write_input_tokens
  - reasoning_output_tokens
keepalives_s: 15
sse:
  id: events.jsonl line number
  event: type
  data: json
  keepalive: ": keepalive"
```
