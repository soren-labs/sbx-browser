import { api } from "../lib/api.js";
import { getConnection, hasScope } from "../lib/config.js";
import { debounce, h, mount } from "../lib/dom.js";
import { explainApiError, PROVIDER_META, PROVIDERS, providerLabel } from "../lib/domain.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { navigate } from "../lib/router.js";
import { prompts } from "../lib/store.js";
import { banner, button, card, codeBlock, field, pageHeader, segmented, toast, toggle } from "../lib/ui.js";

const SHA = /^[0-9a-f]{40}$/;
// Canonical reasoning-effort ladder (SOR-204). A level is offered only
// when the resolved model's capability report exposes it.
const CANONICAL_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];
const DEFAULT_SCHEMA = `{
  "type": "object",
  "properties": {
    "summary": { "type": "string" },
    "files_changed": { "type": "array", "items": { "type": "string" } }
  },
  "required": ["summary"]
}`;

function uuid() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function num(value) {
  if (value === "" || value == null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : NaN;
}

function rangeValue(min, max) {
  const a = num(min);
  const b = num(max);
  if (a == null && b == null) return undefined;
  if (a != null && b != null) return a === b ? a : [a, b];
  return a ?? b;
}

/** Comma/Enter separated token input. */
function chipInput(values, onChange, { placeholder, testid }) {
  const root = h("div", { class: "chip-input", "data-testid": testid });
  const input = h("input", {
    placeholder,
    onKeydown: (ev) => {
      if ((ev.key === "Enter" || ev.key === ",") && input.value.trim()) {
        ev.preventDefault();
        add(input.value);
      } else if (ev.key === "Backspace" && !input.value && values.length) {
        values.pop();
        onChange(values);
        render();
      }
    },
    onBlur: () => input.value.trim() && add(input.value),
  });
  function add(raw) {
    for (const part of raw.split(",")) {
      const v = part.trim();
      if (v && !values.includes(v)) values.push(v);
    }
    input.value = "";
    onChange(values);
    render();
    input.focus();
  }
  function render() {
    mount(
      root,
      values.map((v) =>
        h(
          "span",
          { class: "chip" },
          v,
          h("button", { type: "button", "aria-label": t("Remove"), onClick: () => {
            values.splice(values.indexOf(v), 1);
            onChange(values);
            render();
          } }, icon("x", { size: 12 })),
        ),
      ),
      input,
    );
  }
  render();
  root.addEventListener("click", () => input.focus());
  return root;
}

export function renderNewAgent({ route }) {
  const q = route.query;
  const f = {
    prompt: q.prompt || "",
    name: "",
    provider: PROVIDERS.includes(q.provider) ? q.provider : "codex",
    model: "",
    effort: "",
    account: "auto",
    useRepo: Boolean(q.repo || q.handoff_artifact || q.handoff_head),
    repo: q.repo || "",
    baseRef: q.base_ref || "main",
    baseSha: q.base_sha || "",
    handoff: q.handoff_artifact ? "artifact" : q.handoff_head ? "head" : "none",
    handoffArtifact: q.handoff_artifact || "",
    handoffHead: q.handoff_head || "",
    prRef: "",
    prSha: "",
    useGit: false,
    git: { branch: "", push: true, auto_create_pr: false, auto_publish: false, merge: false, target: "", draft: false, title: "", body: "" },
    useContract: false,
    schema: DEFAULT_SCHEMA,
    enforcement: "strict",
    useWorkflow: Boolean(q.workflow_id),
    workflowId: q.workflow_id || "",
    taskId: "",
    role: q.role || "worker",
    parentTaskId: q.parent_task_id || "",
    cpuMin: "",
    cpuMax: "",
    memMin: "",
    memMax: "",
    secrets: [],
    mcp: [],
    idleTimeout: "",
  };
  const idempotencyKey = uuid();
  let models = [];
  let accounts = [];
  // SOR-204: per-account capability catalog keyed by provider —
  // {provider: [{id, label, status, running, max_concurrent, source,
  // stale, plan, families, models[]}]}. Agents-scope, so non-admin users
  // get the same Provider→Account→Model→Effort linking.
  let capAccounts = {};
  let errors = {};
  let submitting = false;

  // ------------------------------------------------------------ body
  function build() {
    const body = { prompt: { text: f.prompt }, agent: { provider: f.provider } };
    if (f.account && f.account !== "auto") body.agent.account_id = f.account;
    if (f.model) body.agent.model = f.model;
    if (f.effort) body.agent.reasoning_effort = f.effort;
    if (f.name.trim()) body.name = f.name.trim();
    if (f.useRepo) {
      body.workspace = { repo: f.repo.trim(), base_ref: f.baseRef.trim(), base_sha: f.baseSha.trim() };
      if (f.handoff === "artifact") body.handoff = { artifact_id: f.handoffArtifact.trim() };
      if (f.handoff === "head") body.handoff = { head_sha: f.handoffHead.trim() };
      if (f.handoff === "pr") body.handoff = { pull_request: { ref: f.prRef.trim(), head_sha: f.prSha.trim() } };
      if (f.useGit) {
        const g = {};
        for (const [k, v] of Object.entries(f.git)) {
          if (typeof v === "boolean") {
            if (v) g[k] = true;
          } else if (v.trim()) g[k] = v.trim();
        }
        body.git = g;
      }
    }
    if (f.useContract) {
      let schema;
      try {
        schema = JSON.parse(f.schema);
      } catch {
        schema = "<invalid JSON>";
      }
      body.output_contract = { schema, enforcement: f.enforcement };
    }
    if (f.useWorkflow) {
      body.metadata = { workflow_id: f.workflowId.trim(), task_id: f.taskId.trim(), role: f.role.trim() };
      if (f.parentTaskId.trim()) body.metadata.parent_task_id = f.parentTaskId.trim();
    }
    const cpu = rangeValue(f.cpuMin, f.cpuMax);
    const mem = rangeValue(f.memMin, f.memMax);
    if (cpu !== undefined || mem !== undefined) {
      body.compute = {};
      if (cpu !== undefined) body.compute.cpu = cpu;
      if (mem !== undefined) body.compute.memory_mib = mem;
    }
    if (f.secrets.length || f.mcp.length) {
      body.resources = {};
      if (f.secrets.length) body.resources.secrets = [...f.secrets];
      if (f.mcp.length) body.resources.mcp = [...f.mcp];
    }
    const idle = num(f.idleTimeout);
    if (idle != null) body.idle_timeout_s = idle;
    return body;
  }

  function validate() {
    const e = {};
    if (!f.prompt.trim()) e.prompt = t("Describe the task for the agent.");
    if (f.useRepo) {
      if (!f.repo.trim()) e.repo = t("Repository URL or path is required.");
      if (!f.baseRef.trim()) e.baseRef = t("Base ref is required.");
      if (!SHA.test(f.baseSha.trim())) e.baseSha = t("Must be a full 40-character lowercase commit sha.");
      if (f.handoff === "artifact" && !f.handoffArtifact.trim()) e.handoff = t("Artifact id is required.");
      if (f.handoff === "head" && !SHA.test(f.handoffHead.trim())) e.handoff = t("Must be a full 40-character lowercase commit sha.");
      if (f.handoff === "pr" && (!f.prRef.trim() || !SHA.test(f.prSha.trim()))) e.handoff = t("Ref and a full 40-character head sha are required.");
    }
    if (f.useContract) {
      try {
        const parsed = JSON.parse(f.schema);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) e.schema = t("The schema must be a JSON object.");
      } catch (err) {
        e.schema = `${t("Invalid JSON")}: ${err.message}`;
      }
    }
    if (f.useWorkflow && (!f.workflowId.trim() || !f.taskId.trim() || !f.role.trim())) {
      e.workflow = t("Workflow id, task id and role are all required.");
    }
    for (const [key, a, b] of [["cpu", f.cpuMin, f.cpuMax], ["memory", f.memMin, f.memMax]]) {
      const x = num(a);
      const y = num(b);
      if (Number.isNaN(x) || Number.isNaN(y)) e.compute = t("Compute values must be numbers.");
      else if (x != null && y != null && x > y) e.compute = t("The {key} request must not exceed the limit.", { key });
    }
    const idle = num(f.idleTimeout);
    if (Number.isNaN(idle) || (idle != null && idle < 1)) e.idle = t("Must be a positive number of seconds.");
    // SOR-204: reject combinations the capability catalog does not
    // expose — the API refuses them too, but fail before submit.
    const entries = modelEntries();
    if (f.model) {
      const sel = entries.find((m) => m.model === f.model);
      if (entries.length && !sel) e.model = t("Model is not available on the selected account.");
      else if (sel && !sel.available) e.model = t("Model is currently unavailable.");
    }
    if (f.effort && !effortLevels().includes(f.effort)) {
      e.effort = t("Reasoning effort is not supported by this model.");
    }
    return e;
  }

  // --------------------------------------------------------- preview
  const previewTabs = { current: "json" };
  const previewBody = h("div");
  function renderPreview() {
    const body = build();
    const json = JSON.stringify(body, null, 2);
    const base = getConnection().baseUrl || window.location.origin;
    let text = json;
    if (previewTabs.current === "curl") {
      text = `curl -X POST "${base}/v1/agents" \\\n  -H "Authorization: Bearer $SBX_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '${json.replace(/'/g, "'\\''")}'`;
    } else if (previewTabs.current === "python") {
      text = `import os, httpx\n\nbody = ${json.replace(/\btrue\b/g, "True").replace(/\bfalse\b/g, "False").replace(/\bnull\b/g, "None")}\n\nres = httpx.post(\n    f"{os.environ['SBX_BASE_URL']}/v1/agents",\n    headers={"Authorization": f"Bearer {os.environ['SBX_API_KEY']}"},\n    json=body,\n)\nres.raise_for_status()\nagent, run = res.json()["agent"], res.json()["run"]`;
    }
    mount(previewBody, codeBlock(text, { testid: "request-preview" }));
  }
  const refreshPreview = debounce(renderPreview, 80);

  // ------------------------------------------------------------ inputs
  const input = (key, attrs = {}) =>
    h("input", {
      class: ["input", attrs.mono && "mono"],
      value: f[key],
      id: `f-${key}`,
      "data-testid": `f-${key}`,
      ...attrs,
      mono: null,
      onInput: (ev) => {
        f[key] = ev.target.value;
        refreshPreview();
      },
    });
  const gitInput = (key, attrs = {}) =>
    h("input", {
      class: "input",
      value: f.git[key],
      "data-testid": `git-${key}`,
      ...attrs,
      onInput: (ev) => {
        f.git[key] = ev.target.value;
        refreshPreview();
      },
    });

  const errorSlot = h("div");
  const sections = h("div", { class: "stack" });

  const promptArea = h("textarea", {
    class: "textarea",
    id: "f-prompt",
    rows: 6,
    placeholder: t("e.g. Add a /health endpoint with a test, then run the test suite."),
    "data-testid": "f-prompt",
    onInput: (ev) => {
      f.prompt = ev.target.value;
      refreshPreview();
    },
    onKeydown: (ev) => {
      if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
        ev.preventDefault();
        void submit();
      }
    },
  });
  promptArea.value = f.prompt;

  // The task card is built once: re-rendering it would steal focus from
  // the prompt while the user types (models/accounts load asynchronously).
  const promptHint = h("p", { class: "field-hint" }, t("The first run starts as soon as the agent is created. ⌘/Ctrl + Enter to submit."));
  const promptError = h("p", { class: "field-error", hidden: true });
  const taskCard = card({
    title: t("Task"),
    iconName: "message",
    body: h(
      "div",
      { class: "fields" },
      h(
        "div",
        { class: "field" },
        h("label", { class: "field-label", for: "f-prompt" }, t("Prompt"), h("span", { class: "req", "aria-hidden": "true" }, " *")),
        promptArea,
        promptError,
        promptHint,
      ),
      field(t("Name"), input("name", { placeholder: t("Optional — defaults to a generated title") }), { htmlFor: "f-name" }),
    ),
  });
  const dynamicSections = h("div", { class: "stack" });
  mount(sections, errorSlot, taskCard, dynamicSections);

  function providerModels(provider) {
    return models.filter((m) => m.provider === provider);
  }

  // ---- SOR-204: Provider → Account → Model → Effort chaining ----------

  /** Accounts for a provider: capability catalog rows merged with the
   * admin account list (same ids; caps already carry status/slots). */
  function accountsFor(provider) {
    const byId = new Map();
    for (const a of capAccounts[provider] || []) byId.set(a.id, a);
    for (const a of accounts.filter((x) => x.provider === provider)) {
      const prior = byId.get(a.id) || {};
      const merged = { ...prior, ...a };
      // The admin list carries models as bare id strings; keep the
      // capability row's normalized entries (efforts/display/availability)
      // whenever both describe the same account.
      if (Array.isArray(prior.models) && typeof prior.models[0] === "object") {
        merged.models = prior.models;
      }
      byId.set(a.id, merged);
    }
    return [...byId.values()];
  }

  /** The pinned account's capability row, or null on Auto/unknown. */
  function selectedAccountReport() {
    if (!f.account || f.account === "auto") return null;
    return accountsFor(f.provider).find((a) => a.id === f.account) || null;
  }

  /** Model entries for the current Provider+Account selection:
   * {model, display, efforts, default_effort, available, free, stale}.
   * A pinned account's own report is the truth; Auto falls back to the
   * provider-wide /v1/models union. */
  function modelEntries() {
    const pinned = selectedAccountReport();
    if (pinned) {
      const free = pinned.status === "active" && (pinned.running ?? 0) < (pinned.max_concurrent ?? 1);
      return (pinned.models || []).map((m) => {
        // Capability rows carry dicts; a bare account row only has ids.
        const entry = typeof m === "string" ? { model: m } : m || {};
        return {
          model: entry.model,
          display: entry.display || entry.model,
          efforts: entry.reasoning_efforts || [],
          default_effort: entry.default_effort || null,
          available: entry.availability !== "unavailable",
          free: free && entry.availability !== "unavailable" ? 1 : 0,
          stale: Boolean(pinned.stale),
        };
      });
    }
    return providerModels(f.provider).map((m) => ({
      model: m.model,
      display: m.display || m.model,
      efforts: m.reasoning_efforts || [],
      default_effort: m.default_effort || null,
      available: (m.accounts_available || 0) > 0,
      free: m.accounts_available || 0,
      stale: Boolean(m.stale),
    }));
  }

  /** Effort levels the resolved model exposes. No explicit model → the
   * entry the server would pick (first available = report default). */
  function effortLevels() {
    const entries = modelEntries();
    const sel = f.model
      ? entries.find((m) => m.model === f.model)
      : entries.find((m) => m.available) || entries[0];
    return sel ? sel.efforts : [];
  }

  function renderSections() {
    const entries = modelEntries();
    const efforts = effortLevels();
    if (f.effort && !efforts.includes(f.effort)) f.effort = "";
    if (f.model && entries.length && !entries.some((m) => m.model === f.model)) f.model = "";
    const provAccounts = accountsFor(f.provider);

    const providerPicker = h(
      "div",
      { class: "provider-picker", role: "radiogroup", "data-testid": "provider-picker" },
      PROVIDERS.map((p) => {
        const pm = providerModels(p);
        const free = pm.reduce((n, m) => Math.max(n, m.accounts_available || 0), 0);
        const configured = pm.length > 0;
        return h(
          "button",
          {
            type: "button",
            role: "radio",
            class: ["provider-option", f.provider === p && "is-active"],
            "aria-checked": String(f.provider === p),
            "data-provider": p,
            disabled: models.length > 0 && !configured,
            title: configured ? null : t("Not enabled on this deployment"),
            onClick: () => {
              f.provider = p;
              f.model = "";
              f.effort = "";
              f.account = "auto";
              renderSections();
              refreshPreview();
            },
          },
          h("span", { class: `provider provider-${p}` }, h("span", { class: "provider-dot" }), providerLabel(p)),
          h(
            "span",
            { class: "cell-sub" },
            !models.length ? t(PROVIDER_META[p].tier === "stable" ? "Stable" : "Experimental") : configured ? t("{n} free account(s)", { n: free }) : t("Not enabled"),
          ),
        );
      }),
    );

    const modelSelect = h(
      "select",
      {
        class: "select",
        id: "f-model",
        "data-testid": "f-model",
        onChange: (ev) => {
          f.model = ev.target.value;
          refreshPreview();
        },
      },
      h("option", { value: "" }, entries.length ? t("Default ({model})", { model: (entries.find((m) => m.available) || entries[0]).model }) : t("Provider default")),
      entries.map((m) =>
        h(
          "option",
          {
            value: m.model,
            selected: f.model === m.model,
            disabled: !m.available,
            title: m.available ? (m.stale ? t("Reported by a stale capability snapshot") : null) : t("Unavailable on the selected account"),
          },
          `${m.display !== m.model ? `${m.display} (${m.model})` : m.model} — ${m.available ? t("{n} free", { n: m.free }) : t("unavailable")}`,
        ),
      ),
    );

    const effortControl = segmented(
      [
        { value: "", label: t("Default") },
        ...CANONICAL_EFFORTS.map((v) => ({
          value: v,
          label: t(v),
          disabled: !efforts.includes(v),
          title: efforts.includes(v) ? null : t("Not exposed by this model"),
        })),
      ],
      f.effort,
      (v) => {
        f.effort = v;
        refreshPreview();
      },
      { testid: "f-effort" },
    );

    const accountControl = provAccounts.length
      ? h(
          "select",
          {
            class: "select",
            "data-testid": "f-account",
            onChange: (ev) => {
              f.account = ev.target.value;
              // Account change re-scopes the model list and effort surface.
              if (f.model) {
                const next = accountsFor(f.provider).find((a) => a.id === f.account);
                const ok = (next?.models || []).some((m) => m.model === f.model && m.availability !== "unavailable");
                if (!ok) f.model = "";
              }
              renderSections();
              refreshPreview();
            },
          },
          h("option", { value: "auto" }, t("Auto — scheduler picks a free account")),
          provAccounts.map((a) =>
            h(
              "option",
              { value: a.id, selected: f.account === a.id, disabled: a.status !== "active" },
              `${a.label} (${a.id}) — ${a.running}/${a.max_concurrent} · ${a.status}${a.stale ? ` · ${t("stale")}` : ""}${a.plan ? ` · ${a.plan}` : ""}`,
            ),
          ),
        )
      : h("input", {
          class: "input mono",
          value: f.account,
          "data-testid": "f-account",
          onInput: (ev) => {
            f.account = ev.target.value.trim() || "auto";
            refreshPreview();
          },
        });

    const agentCard = card({
      title: t("Agent"),
      iconName: "bot",
      body: h(
        "div",
        { class: "fields" },
        field(t("Provider"), providerPicker),
        h(
          "div",
          { class: "fields-2" },
            field(t("Model"), modelSelect, { htmlFor: "f-model", error: errors.model }),
          field(t("Account"), accountControl, { hint: t("Pinning an account fails fast when it is busy instead of waiting.") }),
        ),
        field(t("Reasoning effort"), effortControl, {
          error: errors.effort,
          hint: efforts.length ? t("Applies to every run of this agent.") : t("{provider} has no native effort setting.", { provider: providerLabel(f.provider) }),
        }),
      ),
    });

    // Repository + handoff + git
    const handoffFields = {
      none: null,
      artifact: field(t("Artifact id"), input("handoffArtifact", { mono: true, placeholder: "art-…" }), { error: errors.handoff, hint: t("The workspace must stand on the artifact's base sha.") }),
      head: field(t("Commit sha"), input("handoffHead", { mono: true, placeholder: t("40-hex sha that descends from the base") }), { error: errors.handoff }),
      pr: h(
        "div",
        { class: "fields-2" },
        field(t("Pull request ref"), input("prRef", { mono: true, placeholder: "refs/pull/42/head" }), { error: errors.handoff }),
        field(t("Pinned head sha"), input("prSha", { mono: true, placeholder: t("40-hex sha") })),
      ),
    }[f.handoff];

    const g = f.git;
    const gitBody = f.useGit
      ? h(
          "div",
          { class: "fields" },
          h(
            "div",
            { class: "fields-2" },
            field(t("Work branch"), gitInput("branch", { placeholder: "sbx/my-change" }), { hint: t("Created on the base sha when the sandbox prepares.") }),
            field(t("PR target"), gitInput("target", { placeholder: f.baseRef || "main" }), { hint: t("Defaults to the base ref.") }),
          ),
          h(
            "div",
            { class: "fields-2" },
            toggle(t("Push the work branch"), g.push, (v) => {
              g.push = v;
              if (!v) Object.assign(g, { auto_create_pr: false, auto_publish: false, merge: false });
              renderSections();
              refreshPreview();
            }, { hint: t("Enables Publish on the workspace tab."), testid: "git-push" }),
            toggle(t("Publish automatically"), g.auto_publish, (v) => {
              g.auto_publish = v;
              refreshPreview();
            }, { disabled: !g.push, hint: t("Publishes when a run finishes successfully."), testid: "git-auto_publish" }),
            toggle(t("Open a pull request"), g.auto_create_pr, (v) => {
              g.auto_create_pr = v;
              if (!v) g.merge = false;
              renderSections();
              refreshPreview();
            }, { disabled: !g.push, hint: t("Needs the GitHub bridge on the control plane."), testid: "git-auto_create_pr" }),
            toggle(t("Allow review-gated merge"), g.merge, (v) => {
              g.merge = v;
              refreshPreview();
            }, { disabled: !g.auto_create_pr, hint: t("Merge only after an exact-sha review pin."), testid: "git-merge" }),
          ),
          g.auto_create_pr
            ? h(
                "div",
                { class: "fields" },
                h(
                  "div",
                  { class: "fields-2" },
                  field(t("PR title"), gitInput("title", { placeholder: "sbx/<agent id>" })),
                  h("div", { style: "align-self:end" }, toggle(t("Open as draft"), g.draft, (v) => {
                    g.draft = v;
                    refreshPreview();
                  })),
                ),
                field(t("PR body"), h("textarea", { class: "textarea", rows: 3, value: g.body, onInput: (ev) => {
                  g.body = ev.target.value;
                  refreshPreview();
                } })),
              )
            : null,
        )
      : null;

    const repoCard = card({
      title: t("Repository"),
      subtitle: t("Clone a repo into the sandbox at an exact commit."),
      iconName: "branch",
      actions: toggle("", f.useRepo, (v) => {
        f.useRepo = v;
        renderSections();
        refreshPreview();
      }, { testid: "toggle-repo" }),
      body: f.useRepo
        ? h(
            "div",
            { class: "fields" },
            field(t("Repository"), input("repo", { mono: true, placeholder: "https://github.com/owner/repo" }), { required: true, error: errors.repo, hint: t("Any URL the sandbox can reach, or a path. Private GitHub repos need the GitHub integration.") }),
            h(
              "div",
              { class: "fields-2" },
              field(t("Base ref"), input("baseRef", { mono: true }), { required: true, error: errors.baseRef }),
              field(t("Base sha"), input("baseSha", { mono: true, placeholder: t("40-hex commit the run must start on") }), { required: true, error: errors.baseSha }),
            ),
            field(
              t("Start from"),
              segmented(
                [
                  { value: "none", label: t("Base commit") },
                  { value: "artifact", label: t("Artifact"), iconName: "package" },
                  { value: "head", label: t("Commit"), iconName: "commit" },
                  { value: "pr", label: t("Pull request"), iconName: "pullRequest" },
                ],
                f.handoff,
                (v) => {
                  f.handoff = v;
                  renderSections();
                  refreshPreview();
                },
                { testid: "f-handoff" },
              ),
              { hint: t("Hand off work from another agent instead of starting on the bare base.") },
            ),
            handoffFields,
            h("div", { class: "section-toggle" }, h("div", null, h("strong", null, t("Git policy")), h("p", { class: "field-hint" }, t("Branch, push, pull request and merge rules for this agent."))), toggle("", f.useGit, (v) => {
              f.useGit = v;
              renderSections();
              refreshPreview();
            }, { testid: "toggle-git" })),
            gitBody,
          )
        : null,
    });

    const schemaArea = h("textarea", {
      class: "textarea mono",
      rows: 9,
      "data-testid": "f-schema",
      "aria-invalid": errors.schema ? "true" : null,
      onInput: (ev) => {
        f.schema = ev.target.value;
        refreshPreview();
      },
    });
    schemaArea.value = f.schema;
    const contractCard = card({
      title: t("Structured output"),
      subtitle: t("Require the final message to be JSON matching a schema."),
      iconName: "braces",
      actions: toggle("", f.useContract, (v) => {
        f.useContract = v;
        renderSections();
        refreshPreview();
      }, { testid: "toggle-contract" }),
      body: f.useContract
        ? h(
            "div",
            { class: "fields" },
            field(t("JSON Schema"), schemaArea, { error: errors.schema, hint: t("Assertion keywords only; $ref, pattern and conditionals are refused.") }),
            field(
              t("Enforcement"),
              segmented(
                [
                  { value: "strict", label: t("Strict — invalid output fails the run") },
                  { value: "warn", label: t("Warn — keep the run, attach a diagnostic") },
                ],
                f.enforcement,
                (v) => {
                  f.enforcement = v;
                  refreshPreview();
                },
              ),
            ),
          )
        : null,
    });

    const workflowCard = card({
      title: t("Workflow"),
      subtitle: t("Group agents so any client can recover or clean them up by id."),
      iconName: "workflow",
      actions: toggle("", f.useWorkflow, (v) => {
        f.useWorkflow = v;
        renderSections();
        refreshPreview();
      }, { testid: "toggle-workflow" }),
      body: f.useWorkflow
        ? h(
            "div",
            { class: "fields" },
            errors.workflow ? h("p", { class: "field-error" }, errors.workflow) : null,
            h("div", { class: "fields-2" }, field(t("Workflow id"), input("workflowId", { mono: true, placeholder: "release-42" }), { required: true }), field(t("Task id"), input("taskId", { mono: true, placeholder: "implement" }), { required: true })),
            h("div", { class: "fields-2" }, field(t("Role"), input("role", { placeholder: "worker / reviewer" }), { required: true }), field(t("Parent task id"), input("parentTaskId", { mono: true }))),
          )
        : null,
    });

    const advancedCard = h(
      "details",
      { class: "card", "data-testid": "advanced", open: Boolean(errors.compute || errors.idle) || null },
      h("summary", { class: "card-header", style: "cursor:pointer;list-style:none" }, h("div", { class: "card-title" }, icon("cpu"), h("div", null, h("h2", null, t("Advanced")), h("p", { class: "muted" }, t("Compute, secrets, MCP servers and idle timeout.")))), icon("chevronDown", { className: "muted-icon" })),
      h(
        "div",
        { class: "card-body fields" },
        errors.compute ? h("p", { class: "field-error" }, errors.compute) : null,
        h(
          "div",
          { class: "fields-2" },
          field(t("CPU cores (request – limit)"), h("div", { class: "input-group" }, input("cpuMin", { type: "number", step: "0.125", min: "0.125", placeholder: "1" }), h("span", { class: "subtle" }, "–"), input("cpuMax", { type: "number", step: "0.125", min: "0.125", placeholder: "2" })), { hint: t("Default 1 – 2 cores.") }),
          field(t("Memory MiB (request – limit)"), h("div", { class: "input-group" }, input("memMin", { type: "number", step: "128", min: "128", placeholder: "1024" }), h("span", { class: "subtle" }, "–"), input("memMax", { type: "number", step: "128", min: "128", placeholder: "8192" })), { hint: t("Default 1024 – 8192 MiB. Cost is estimated at the request.") }),
        ),
        field(t("Secrets"), chipInput(f.secrets, () => refreshPreview(), { placeholder: t("Modal Secret names, Enter to add"), testid: "f-secrets" }), { hint: t("Only names allow-listed by the operator (SBX_RESOURCE_SECRETS).") }),
        field(t("MCP servers"), chipInput(f.mcp, () => refreshPreview(), { placeholder: t("Registry names, Enter to add"), testid: "f-mcp" }), { hint: t("From the deployment's MCP registry. Only Devin supports MCP today.") }),
        field(t("Idle timeout (seconds)"), input("idleTimeout", { type: "number", min: "1", placeholder: "300" }), { error: errors.idle, hint: t("How long an idle agent is kept before it is reclaimed.") }),
      ),
    );

    promptHint.hidden = Boolean(errors.prompt);
    promptError.hidden = !errors.prompt;
    promptError.textContent = errors.prompt || "";
    promptArea.setAttribute("aria-invalid", errors.prompt ? "true" : "false");
    mount(dynamicSections, agentCard, repoCard, contractCard, workflowCard, advancedCard);
  }

  async function submit() {
    if (submitting) return;
    errors = validate();
    if (Object.keys(errors).length) {
      renderSections();
      errorSlot.replaceChildren(banner({ tone: "danger", title: t("Fix the highlighted fields."), testid: "form-error" }));
      sections.querySelector(".field-error")?.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    errorSlot.replaceChildren();
    submitting = true;
    submitBtn.disabled = true;
    submitBtn.classList.add("is-busy");
    try {
      const res = await api.createAgent(build(), idempotencyKey);
      prompts.set(res.agent.id, res.run.id, f.prompt);
      toast(t("Agent created — first run queued"), { tone: "success" });
      navigate(`/agents/${encodeURIComponent(res.agent.id)}`);
    } catch (err) {
      const info = explainApiError(err);
      errorSlot.replaceChildren(
        banner({ tone: "danger", title: info.title, body: h("span", { class: "muted" }, `${info.code}${info.detail && info.detail !== info.title ? ` · ${info.detail}` : ""}`), testid: "form-error" }),
      );
      errorSlot.scrollIntoView({ block: "center", behavior: "smooth" });
    } finally {
      submitting = false;
      submitBtn.disabled = false;
      submitBtn.classList.remove("is-busy");
    }
  }

  const submitBtn = button(t("Create agent"), { variant: "primary", iconName: "zap", testid: "create-agent", onClick: () => void submit() });

  const previewCard = card({
    title: t("API request"),
    subtitle: t("Exactly what the console will send."),
    iconName: "code",
    actions: segmented(
      [
        { value: "json", label: "JSON" },
        { value: "curl", label: "cURL" },
        { value: "python", label: "Python" },
      ],
      previewTabs.current,
      (v) => {
        previewTabs.current = v;
        renderPreview();
      },
      { size: "sm", testid: "preview-tabs" },
    ),
    body: previewBody,
  });

  const el = h(
    "div",
    { class: "page page-wide" },
    pageHeader({
      title: t("New agent"),
      subtitle: t("Creates an isolated sandbox, starts the provider CLI and queues the first run."),
      back: { href: "#/agents", label: t("Agents") },
      testid: "page-title",
    }),
    h(
      "div",
      { class: "compose" },
      h("div", { class: "stack" }, sections, h("div", { class: "form-footer" }, h("a", { class: "btn", href: "#/agents" }, t("Cancel")), submitBtn)),
      h("aside", { class: "compose-preview" }, previewCard),
    ),
  );

  renderSections();
  renderPreview();
  (async () => {
    try {
      models = (await api.models()).models || [];
      if (models.length && !providerModels(f.provider).length) f.provider = models[0].provider;
    } catch {
      models = [];
    }
    // SOR-204: capability catalog is agents-scoped, so every user gets
    // account→model→effort linking (admin listAccounts only adds labels).
    try {
      const caps = await api.capabilities();
      for (const p of caps.providers || []) capAccounts[p.provider] = p.accounts || [];
    } catch {
      capAccounts = {};
    }
    if (hasScope("admin")) {
      try {
        accounts = (await api.listAccounts()).accounts || [];
      } catch {
        accounts = [];
      }
    }
    if (el.isConnected || document.contains(el)) {
      renderSections();
      renderPreview();
    }
  })();
  queueMicrotask(() => promptArea.focus());
  return { el, title: t("New agent") };
}
