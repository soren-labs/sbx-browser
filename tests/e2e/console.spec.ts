import { expect, test } from "@playwright/test";
import {
  connect,
  createAgent,
  devInfo,
  KEY,
  run,
  shot,
  waitAgent,
  waitRun,
} from "./helpers";

// One throwaway control plane per run: tests build on each other's state.
test.describe.configure({ mode: "serial" });

test.describe("web console against a real local /v1 control plane", () => {
  test("connect: rejects a bad key, accepts the bootstrap key", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("connect-view")).toBeVisible();
    await shot(page, "console_01_connect.png");

    await page.getByTestId("connect-key").fill("sbx_not_a_real_key");
    await page.getByTestId("connect-submit").click();
    await expect(page.getByTestId("connect-error")).toBeVisible();

    await page.getByTestId("connect-key").fill(KEY);
    await page.getByTestId("connect-submit").click();
    await expect(page.getByTestId("app-ready")).toBeVisible();
    await expect(page.getByTestId("identity")).toContainText("admin");
    await expect(page.getByTestId("agents-empty")).toBeVisible();
    await shot(page, "console_02_agents_empty.png");
  });

  test("create an agent, stream run 1, follow up, cancel, reload", async ({ page }) => {
    await connect(page);
    await page.goto("/#/agents/new");
    await page.getByTestId("f-prompt").fill("Write hello.txt containing hi");
    await expect(page.getByTestId("request-preview")).toContainText("Write hello.txt containing hi");
    await page.locator('[data-testid=f-effort] [data-value="high"]').click();
    await expect(page.getByTestId("request-preview")).toContainText('"reasoning_effort": "high"');
    await page.getByTestId("preview-tabs").getByText("cURL").click();
    await expect(page.getByTestId("request-preview")).toContainText("curl -X POST");
    await page.getByTestId("f-name").fill("hello agent");
    await shot(page, "console_03_new_agent.png");
    await page.getByTestId("create-agent").click();

    await expect(page.getByTestId("agent-title")).toHaveText("hello agent", { timeout: 30_000 });
    await waitRun(page, "run-1", "FINISHED");
    const first = run(page, "run-1");
    await expect(first.getByTestId("user-message")).toContainText("Write hello.txt containing hi");
    await expect(first.getByTestId("agent-message")).toBeVisible();
    await expect(first.getByTestId("file-block")).toContainText("hello.txt");
    await first.getByTestId("command-block").locator("summary").click();
    await expect(first.getByTestId("command-output")).toBeVisible();
    await waitAgent(page, "idle");
    await expect(page.getByTestId("usage-card")).toContainText("25,996");
    await shot(page, "console_04_conversation.png");

    // A hanging follow-up exercises the live state and Cancel.
    await page.getByTestId("composer").fill("please hang for a while");
    await page.getByTestId("send").click();
    await waitRun(page, "run-2", "RUNNING");
    await expect(run(page, "run-2").getByTestId("working")).toBeVisible();
    await expect(page.getByTestId("composer")).toBeDisabled();
    await shot(page, "console_05_running.png");
    await page.getByTestId("cancel-run").click();
    await waitRun(page, "run-2", "CANCELLED");

    await page.getByTestId("composer").fill("Now continue the work");
    await page.getByTestId("composer").press("Enter");
    await waitRun(page, "run-3", "FINISHED");
    await expect(run(page, "run-3").getByTestId("user-message")).toContainText("Now continue");

    const messages = await page.getByTestId("agent-message").count();
    const commands = await page.getByTestId("command-block").count();
    await page.reload();
    await waitRun(page, "run-3", "FINISHED");
    await expect(page.getByTestId("agent-message")).toHaveCount(messages, { timeout: 20_000 });
    await expect(page.getByTestId("command-block")).toHaveCount(commands);
    await expect(page.getByTestId("run")).toHaveCount(3);
  });

  test("create agent links provider, account, model and effort (SOR-204)", async ({ page }) => {
    await connect(page);
    await page.goto("/#/agents/new");
    await expect(page.getByTestId("page-title")).toBeVisible();
    // Provider → Account: the seeded codex seat appears in the dynamic select.
    const account = page.getByTestId("f-account");
    await expect(account.locator("option")).not.toHaveCount(1);
    await expect(account.locator("option").first()).toContainText("Auto");
    // Pinning the account narrows the model list to its catalog.
    const accountOptions = await account.locator("option").allInnerTexts();
    const pinned = accountOptions.find((o) => o.includes("codex-1"));
    expect(pinned, "seeded codex account in the Account select").toBeTruthy();
    await account.selectOption({ label: pinned! });
    // Provider → Model: catalog entries are selectable options.
    const model = page.getByTestId("f-model");
    await expect(model.locator("option")).not.toHaveCount(1);
    // Model → Effort: codex exposes the full canonical ladder.
    for (const v of ["none", "minimal", "low", "medium", "high", "xhigh", "max"]) {
      await expect(
        page.locator(`[data-testid=f-effort] [data-value="${v}"]`),
      ).toBeEnabled();
    }
    await page.locator('[data-testid=f-effort] [data-value="minimal"]').click();
    await expect(page.getByTestId("request-preview")).toContainText(
      '"reasoning_effort": "minimal"',
    );
    // A provider with no effort surface disables every level.
    await page.locator('[data-provider="devin"]').click();
    await expect(
      page.locator('[data-testid=f-effort] [data-value="high"]'),
    ).toBeDisabled();
  });

  test("a failing run shows the structured run error", async ({ page }) => {
    await connect(page);
    await createAgent(page, "this one should fail", "failing agent");
    await waitRun(page, "run-1", "ERROR");
    const err = run(page, "run-1").getByTestId("run-error");
    await expect(err).toContainText("runtime_error");
    await expect(err).toContainText("provider");
    await shot(page, "console_06_run_error.png");

    await page.getByTestId("close-agent").click();
    await page.getByTestId("confirm-ok").click();
    await waitAgent(page, "closed");
    await expect(page.getByTestId("readonly-banner")).toBeVisible();
    await expect(page.getByTestId("composer")).toBeDisabled();
  });

  test("repository agent: git policy, review pin, publish, snapshot, artifact", async ({ page }) => {
    await connect(page);
    const { demo_workspace: ws } = await devInfo(page);
    await page.goto("/#/agents/new");
    await page.getByTestId("f-prompt").fill("Add a greeting test");
    await page.getByTestId("f-name").fill("repo agent");
    await page.getByTestId("toggle-repo").check({ force: true });
    await page.getByTestId("f-repo").fill(ws.repo);
    await page.getByTestId("f-baseRef").fill(ws.base_ref);
    await page.getByTestId("f-baseSha").fill("not-a-sha");
    await page.getByTestId("create-agent").click();
    await expect(page.getByTestId("form-error")).toBeVisible();
    await page.getByTestId("f-baseSha").fill(ws.base_sha);
    await page.getByTestId("toggle-git").check({ force: true });
    await page.getByTestId("git-branch").fill("sbx/greeting");
    await page.getByTestId("toggle-workflow").check({ force: true });
    await page.getByTestId("f-workflowId").fill("wf-e2e");
    await page.getByTestId("f-taskId").fill("implement");
    await expect(page.getByTestId("request-preview")).toContainText('"branch": "sbx/greeting"');
    await page.getByTestId("create-agent").click();
    await expect(page.getByTestId("agent-title")).toHaveText("repo agent", { timeout: 30_000 });
    await waitRun(page, "run-1", "FINISHED");
    await waitAgent(page, "idle");

    await page.getByTestId("tab-workspace").click();
    await expect(page.getByTestId("ws-pipeline")).toBeVisible();
    await expect(page.getByTestId("ws-record")).toContainText(ws.base_sha.slice(0, 12));
    await page.getByTestId("ws-review").click();
    await page.getByTestId("review-submit").click();
    await expect(page.getByTestId("ws-record")).toContainText("current");
    await page.getByTestId("ws-publish").click();
    await expect(page.getByTestId("ws-pipeline")).toContainText("sbx/greeting");
    await shot(page, "console_07_workspace.png");

    await page.getByTestId("snapshot").click();
    await page.getByTestId("snapshot-test").fill("python -c \"print('ok')\"");
    await page.getByTestId("snapshot-submit").click();
    await expect(page.getByTestId("artifacts-table")).toBeVisible({ timeout: 20_000 });
    await page.getByTestId("artifact-row").first().click();
    await expect(page.getByTestId("artifact-title")).toContainText("art-");
    await expect(page.getByTestId("artifact-files")).toContainText("app.py");
    const download = page.waitForEvent("download");
    await page.getByTestId("download-patch.diff").click();
    expect((await download).suggestedFilename()).toContain("patch.diff");
    await expect(page.getByTestId("artifact-handoff")).toHaveAttribute("href", /handoff_artifact=art-/);
    await shot(page, "console_08_artifact.png");
  });

  test("workflow recovery view and scoped cleanup", async ({ page }) => {
    await connect(page);
    await page.getByTestId("nav-workflows").click();
    await expect(page.getByTestId("workflows-table")).toContainText("wf-e2e");
    await page.getByText("wf-e2e").first().click();
    await expect(page.getByTestId("workflow-title")).toHaveText("wf-e2e");
    await expect(page.getByTestId("workflow-agents")).toContainText("implement");
    await shot(page, "console_09_workflow.png");
    await page.getByTestId("close-workflow").click();
    await page.getByTestId("confirm-ok").click();
    await expect(page.getByRole("dialog")).toContainText("Workflow cleanup");
  });

  test("agents list filters, capacity and GitHub posture", async ({ page }) => {
    await connect(page);
    await expect(page.getByTestId("agent-row")).toHaveCount(3);
    await page.locator('[data-testid=agents-filter] [data-value="ended"]').click();
    await expect(page.getByTestId("agent-row")).toHaveCount(2);
    await page.locator('[data-testid=agents-filter] [data-value="all"]').click();
    await page.getByTestId("agents-search").fill("hello");
    await expect(page.getByTestId("agent-row")).toHaveCount(1);
    await page.getByTestId("agents-search").fill("");
    await shot(page, "console_10_agents.png");

    await page.getByTestId("nav-capacity").click();
    for (const provider of ["codex", "devin", "antigravity", "grok", "opencode"]) {
      await expect(page.getByTestId(`capacity-${provider}`)).toBeVisible();
    }
    await shot(page, "console_11_capacity.png");

    await page.getByTestId("nav-github").click();
    await expect(page.getByTestId("github-status")).toContainText("not configured");
  });

  test("admin: accounts import/remove and API key lifecycle with scopes", async ({ page, browser }) => {
    await connect(page);
    await page.getByTestId("nav-accounts").click();
    await expect(page.getByTestId("accounts-table")).toContainText("codex-1");
    await page.getByTestId("import-account").click();
    await page.getByTestId("acct-provider").selectOption("grok");
    await page.getByTestId("acct-label").fill("e2e grok seat");
    await page.getByTestId("acct-file-0").setInputFiles({
      name: "auth.json",
      mimeType: "application/json",
      buffer: Buffer.from('{"token":"REDACTED"}'),
    });
    await page.getByTestId("acct-submit").click();
    await expect(page.getByTestId("accounts-table")).toContainText("e2e grok seat");
    await shot(page, "console_12_accounts.png");
    const row = page.getByTestId("account-row").filter({ hasText: "e2e grok seat" });
    await row.getByRole("button", { name: "Remove account" }).click();
    await page.getByTestId("confirm-ok").click();
    await expect(page.getByTestId("accounts-table")).not.toContainText("e2e grok seat");

    await page.getByTestId("nav-keys").click();
    await page.getByTestId("create-key").click();
    await page.getByTestId("key-label").fill("e2e automation");
    await page.getByTestId("key-submit").click();
    const plaintext = (await page.getByTestId("key-plaintext").innerText()).trim();
    expect(plaintext.startsWith("sbx_")).toBeTruthy();
    // This screenshot is published in the docs: show a placeholder, never a key value.
    await page.getByTestId("key-plaintext").evaluate((el) => (el.textContent = "sbx_REDACTED"));
    await shot(page, "console_13_key_created.png");
    await page.getByRole("button", { name: "Done" }).click();
    await expect(page.getByTestId("keys-table")).toContainText("e2e automation");

    // The agents-only key cannot open admin pages.
    const other = await browser.newContext({ colorScheme: "light", viewport: { width: 1280, height: 800 } });
    const scoped = await other.newPage();
    await connect(scoped, plaintext);
    await scoped.getByTestId("nav-accounts").click();
    await expect(scoped.getByTestId("admin-locked")).toBeVisible();

    // Revoking it bounces the other session back to Connect.
    const keyRow = page.getByTestId("key-row").filter({ hasText: "e2e automation" });
    await keyRow.getByTestId("key-revoke").click();
    await page.getByTestId("confirm-ok").click();
    await expect(keyRow).toContainText("revoked");
    await scoped.getByTestId("nav-agents").click();
    await expect(scoped.getByTestId("connect-view")).toBeVisible({ timeout: 20_000 });
    await other.close();
  });

  test("light theme and Chinese UI", async ({ browser }) => {
    const ctx = await browser.newContext({
      colorScheme: "light",
      locale: "zh-CN",
      viewport: { width: 1280, height: 800 },
    });
    const page = await ctx.newPage();
    await connect(page);
    await expect(page.getByTestId("nav-agents")).toContainText("Agent");
    await expect(page.getByTestId("nav-workflows")).toContainText("工作流");
    await page.getByTestId("agent-row").filter({ hasText: "hello agent" }).click();
    await waitRun(page, "run-3", "FINISHED");
    await expect(page.getByTestId("tab-conversation")).toContainText("对话");
    await shot(page, "console_14_zh_light.png");
    await ctx.close();
  });
});
