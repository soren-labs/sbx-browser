# sbx-browser

Self-hosted orchestration for coding-agent CLIs. Give SBX a task, optional
repository, and delivery target; it runs the provider's official CLI in an
isolated Modal Sandbox and keeps the task, runs, revisions, delivery and
review state behind one API.

Supported providers include Codex, Devin, Antigravity, Grok and OpenCode.
SBX uses your own Modal workspace and your own provider subscriptions.

> **v0.1.1 · public alpha.** The public `/v1` API is usable today but may
> still evolve before 1.0.

## Self-host in three steps

```bash
git clone https://github.com/soren-labs/sbx-browser.git
cd sbx-browser
./sbx deploy
```

On a fresh machine, `./sbx` bootstraps `uv`/Python as needed and `deploy`
initializes config, opens Modal authentication when required, creates the
platform, and verifies `/v1/me`. A platform-only deploy is valid: connect
providers afterwards.

```bash
./sbx auth login --provider devin
./sbx github connect        # optional; needed for private GitHub repos/PRs
./sbx open                  # signed-in web console
```

Then create a task with the API or SDK:

```python
from sbx.sdk import SbxClient

client = SbxClient()  # SBX_BASE_URL + SBX_API_KEY
created = client.tasks.create(
    "Fix the flaky test and add a regression test",
    source={"repo": "https://github.com/owner/repo"},
    delivery={"pull_request": {}},
)
task = client.tasks.wait(created.task.id)
print(task.status)
```

## Use an existing SBX

If someone already operates the deployment, you only need:

```text
SBX_BASE_URL
SBX_API_KEY
```

Start with `docs-site/src/content/docs/getting-started/quick-start.mdx` or the
published documentation site. The task-oriented SDK covers create, wait,
follow-up, cancel/retry, revisions, delivery, review and merge without raw
HTTP workarounds.

## Documentation

The public docs live in [`docs-site/`](docs-site/), built with Astro
Starlight. The REST reference is generated from the runtime OpenAPI contract;
canonical error/reference data is checked for drift in CI.

```bash
make docs-check       # build + links + generated-reference checks
make docs-dev         # local docs server
make docs-deploy      # deploy the static site to Modal
```

Machine-readable entry points are `/llms.txt`, `/llms-full.txt` and
`/openapi.json` on a built docs site.

## Development

```bash
make lint
make test
make test-e2e
make console-dev
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[SECURITY.md](SECURITY.md) for vulnerability reporting.

License selection is pending owner decision; [LICENSE](LICENSE) is currently
a placeholder, not a grant.

Account/session authentication and optional user-owned API keys are documented in [control/README.md](control/README.md#user-accounts-and-browser-sessions-pr-a). Production authentication requires shared PostgreSQL; OAuth and the login/register UI are follow-up PRs.
