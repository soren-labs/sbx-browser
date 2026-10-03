import { mergeActivity } from "../prototype/session-state";
import { hostedMode } from "../hosted/api";
import { cacheScope, readSessionCache, writeSessionCache } from "../prototype/session-cache";
import { ApiError, type SessionApi, type SessionEventHandlers } from "./client";
import {
  endReasonOf,
  normalizeEvent,
  normalizePhase,
  normalizeTurnStatus,
  nextSeq,
  toErrorKind,
  toTurnError,
} from "./normalize";
import type {
  ActivityItem,
  DeliverInput,
  IntegrationStatus,
  ModelInfo,
  NewSessionInput,
  ProviderInfo,
  RepoRef,
  Session,
  SessionChange,
  SessionChangesDiff,
  SessionDeliverResult,
  SessionFileDiff,
  SessionStatus,
  Turn,
  Usage,
} from "./types";

/* eslint-disable @typescript-eslint/no-explicit-any */

/**
 * HttpSessionApi — the real wire client for the merged V2 Session API
 * (control/api_v2) plus the explicitly allowed V1 surfaces for providers,
 * models and GitHub App state. Every path lives in PATHS so integration
 * rewiring is a one-place change.
 */
const PATHS = {
  sessions: "/v2/sessions",
  session: (id: string) => `/v2/sessions/${encodeURIComponent(id)}`,
  messages: (id: string) =>
    `/v2/sessions/${encodeURIComponent(id)}/messages`,
  cancel: (id: string) => `/v2/sessions/${encodeURIComponent(id)}/cancel`,
  retry: (id: string) => `/v2/sessions/${encodeURIComponent(id)}/retry`,
  deliver: (id: string) => `/v2/sessions/${encodeURIComponent(id)}/deliver`,
  changes: (id: string) => `/v2/sessions/${encodeURIComponent(id)}/changes`,
  changesDiff: (id: string) =>
    `/v2/sessions/${encodeURIComponent(id)}/changes/diff`,
  fileDiff: (id: string, path: string) =>
    `/v2/sessions/${encodeURIComponent(id)}/changes/diff?path=${encodeURIComponent(path)}`,
  events: (id: string) => `/v2/sessions/${encodeURIComponent(id)}/events`,
  providers: "/v1/providers",
  models: "/v1/models",
  githubApp: "/v1/github/app",
  githubAuthorize: "/v1/github/install",
} as const;

const DEFAULT_BASE = (
  (import.meta.env.VITE_API_BASE as string | undefined) ?? ""
).replace(/\/+$/, "");

const SSE_BACKOFF_MS = [1000, 2000, 5000, 10000, 15000] as const;

const TOKEN_KEY = "sbx.console.token";

/** Bearer token for live mode — a manual Settings field until SOR-262
 * wires auth. Stored in localStorage only; never sent anywhere except the
 * configured control plane. */
export function getToken(): string {
  if (hostedMode) return "";
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode — ignore */
  }
}

function usageOf(raw: any): Usage | null {
  if (!raw || typeof raw !== "object") return null;
  return {
    inputTokens: Number(raw.input_tokens ?? 0),
    cachedInputTokens: Number(raw.cached_input_tokens ?? 0),
    outputTokens: Number(raw.output_tokens ?? 0),
    cacheWriteInputTokens:
      raw.cache_write_input_tokens != null
        ? Number(raw.cache_write_input_tokens)
        : undefined,
    reasoningOutputTokens:
      raw.reasoning_output_tokens != null
        ? Number(raw.reasoning_output_tokens)
        : undefined,
  };
}

function repoOf(raw: any): RepoRef | null {
  if (!raw || typeof raw !== "object") return null;
  const name = String(raw.repo ?? "");
  if (!name) return null;
  // Only github.com repos get a web URL — local paths / other hosts render
  // as plain text instead of a dead link.
  const gh = /^[\w.-]+\/[\w.-]+$/.test(name)
    ? name
    : /github\.com[:/]([\w.-]+\/[\w.-]+?)(?:\.git)?$/.exec(name)?.[1];
  return {
    name,
    url: gh ? `https://github.com/${gh}` : undefined,
    ref: raw.ref ?? undefined,
    baseSha: raw.base_sha ?? undefined,
  };
}

function deliveryOf(raw: any): Session["delivery"] {
  if (!raw || typeof raw !== "object") return null;
  const pr = raw.pull_request ?? null;
  const mode =
    pr != null ? (pr.draft ? "draft_pr" : "pr") : raw.branch != null ? "branch" : "none";
  return {
    mode,
    status: raw.status ?? undefined,
    branch: raw.branch ?? undefined,
    pushedHeadSha: raw.pushed_head_sha ?? undefined,
    prUrl: pr?.url ?? undefined,
    prNumber: pr?.number != null ? Number(pr.number) : undefined,
    prState: pr?.draft ? "draft" : pr?.state ?? undefined,
    prHeadSha: pr?.head_sha ?? undefined,
    prBase: pr?.base ?? undefined,
    merged: Boolean(raw.merge?.merged),
    error:
      raw.error != null
        ? typeof raw.error === "string"
          ? raw.error
          : {
              code: raw.error.code ?? undefined,
              message: raw.error.message ?? undefined,
            }
        : undefined,
  };
}

/** ChangesView → SessionChangeInfo (base/head shas for the Details rail). */
function changesOf(raw: any): Session["changes"] {
  if (!raw || typeof raw !== "object") return null;
  return {
    status: String(raw.status ?? "none"),
    baseSha: raw.base_sha ?? undefined,
    headSha: raw.head_sha ?? undefined,
    branch: raw.branch ?? undefined,
  };
}

/** RevisionView → SessionChange row (shared by listChanges + deliver). */
function mapRevisionChange(raw: any): SessionChange {
  const delivery = raw?.delivery ?? null;
  const pr = delivery?.pull_request ?? null;
  const err = raw?.error ?? delivery?.error ?? null;
  return {
    id: `rev-${raw?.n ?? 0}`,
    kind: "revision",
    n: raw?.n != null ? Number(raw.n) : undefined,
    status: String(raw?.status ?? ""),
    deliveryStatus: delivery?.status ?? undefined,
    merged: Boolean(delivery?.merge?.merged),
    summary: `revision ${raw?.n ?? "?"}`,
    ts: String(raw?.updated_at ?? raw?.created_at ?? ""),
    branch: delivery?.branch ?? undefined,
    headSha: raw?.head_sha ?? undefined,
    url: pr?.url ?? undefined,
    prNumber: pr?.number != null ? Number(pr.number) : undefined,
    error:
      err != null ? (typeof err === "string" ? err : (err.message ?? "")) : undefined,
  };
}

/** changes/diff response → SessionChangesDiff (stats only, no bodies). */
function mapChangesDiff(data: any): SessionChangesDiff {
  const files = Array.isArray(data?.files) ? data.files : [];
  return {
    n: Number(data?.n ?? 0),
    baseSha: data?.base_sha ?? undefined,
    headSha: data?.head_sha ?? undefined,
    filesChanged: Number(data?.files_changed ?? files.length),
    additions: Number(data?.additions ?? 0),
    deletions: Number(data?.deletions ?? 0),
    files: files.map((f: any) => ({
      path: String(f?.path ?? ""),
      status: f?.status ?? "modified",
      additions: Number(f?.additions ?? 0),
      deletions: Number(f?.deletions ?? 0),
      oldPath: f?.old_path ?? undefined,
    })),
  };
}

/** RunView → Turn. Wire fields: n, status, prompt, result, error, usage,
 * provider, model, reasoning_effort, queue_position, created/started/
 * finished_at. */
function mapRun(raw: any): Turn {
  const n = Number(raw?.n ?? 0);
  return {
    id: `turn-${n}`,
    index: n,
    prompt: String(raw?.prompt ?? ""),
    status: normalizeTurnStatus(raw?.status),
    createdAt: String(raw?.created_at ?? ""),
    startedAt: raw?.started_at ?? null,
    finishedAt: raw?.finished_at ?? null,
    result: raw?.result ?? null,
    error: toTurnError(raw?.error),
    usage: usageOf(raw?.usage),
    queuePosition: raw?.queue_position ?? null,
    provider: raw?.provider ?? null,
    model: raw?.model ?? null,
    effort: raw?.reasoning_effort ?? null,
    activity: [],
  };
}

/** SessionView → product Session. Runs are attached by the caller when a
 * detail envelope is available. */
function mapSession(raw: any, runs?: Turn[]): Session {
  const status = String(raw?.status ?? "queued") as SessionStatus;
  const exec = raw?.execution ?? null;
  const changes = raw?.changes ?? null;
  const turnsArr = runs ?? [];
  const lastDone = [...turnsArr].reverse().find((t) => t.result);
  return {
    id: String(raw?.id ?? ""),
    title: String(raw?.title ?? raw?.prompt ?? "").slice(0, 80),
    status,
    phase: normalizePhase(raw?.phase, status),
    endReason: endReasonOf(raw?.phase, status),
    prompt: String(raw?.prompt ?? ""),
    provider: exec?.provider ?? null,
    model: exec?.model ?? null,
    accountLabel: null, // raw account ids never cross the API boundary
    repo: repoOf(raw?.repository),
    effort: exec?.reasoning_effort ?? null,
    compute: null,
    idleTimeoutS: null,
    delivery: deliveryOf(raw?.delivery),
    changes: changesOf(changes),
    createdAt: String(raw?.created_at ?? ""),
    updatedAt: String(raw?.updated_at ?? ""),
    usage: usageOf(raw?.usage),
    costUsd:
      raw?.cost_estimate_usd != null ? Number(raw.cost_estimate_usd) : null,
    turnCount: Number(raw?.turns ?? turnsArr.length ?? 0),
    turns: turnsArr,
    lastActivityPreview: lastDone?.result
      ? lastDone.result.slice(0, 120)
      : null,
    hasChanges: changes?.status === "ready",
    error: toTurnError(raw?.error),
  };
}

/**
 * Build the real CreateSessionRequest body (POST /v2/sessions). Pure —
 * exported for contract tests. "auto" values are sent literally (the
 * backend resolves them); only set fields are emitted.
 */
export function toCreateSessionRequest(input: NewSessionInput): Record<string, any> {
  const req: Record<string, any> = { prompt: input.prompt };
  if (input.title?.trim()) req.title = input.title.trim();

  if (input.repo?.trim()) {
    req.repository = { repo: input.repo.trim() };
    if (input.repoRef?.trim()) req.repository.ref = input.repoRef.trim();
  }

  const execution: Record<string, any> = {};
  execution.provider =
    input.provider && input.provider !== "auto" ? input.provider : "auto";
  // Only real account ids (from /v1/models) or literal "auto" — the picker
  // can no longer fabricate ids, but we still guard at the boundary.
  if (input.account && input.account !== "auto") {
    execution.account_id = input.account;
  } else {
    execution.account_id = "auto";
  }
  if (input.model && input.model !== "auto") execution.model = input.model;
  else execution.model = "auto";
  if (input.effort && input.effort !== "auto") {
    execution.reasoning_effort = input.effort;
  } else {
    execution.reasoning_effort = "auto";
  }
  req.execution = execution;

  if (input.delivery && input.delivery !== "none") {
    const delivery: Record<string, any> = { auto_publish: true };
    if (input.delivery === "pr" || input.delivery === "draft_pr") {
      delivery.pull_request = {
        draft: input.delivery === "draft_pr",
        ...(input.deliveryTarget?.trim()
          ? { target: input.deliveryTarget.trim() }
          : {}),
      };
    }
    req.delivery = delivery;
  }

  const advanced: Record<string, any> = {};
  const resources: Record<string, any> = {};
  if (input.secrets?.length) resources.secrets = input.secrets;
  if (input.mcpServers?.length) resources.mcp = input.mcpServers;
  if (Object.keys(resources).length) advanced.resources = resources;
  if (input.compute) {
    const compute: Record<string, any> = {};
    if (typeof input.compute.cpu === "number") compute.cpu = input.compute.cpu;
    if (typeof input.compute.memoryMib === "number") {
      compute.memory_mib = input.compute.memoryMib;
    }
    if (Object.keys(compute).length) advanced.compute = compute;
  }
  if (typeof input.idleTimeoutS === "number" && input.idleTimeoutS > 0) {
    advanced.idle_timeout_s = input.idleTimeoutS;
  }
  if (Object.keys(advanced).length) req.advanced = advanced;

  return req;
}

export class HttpSessionApi implements SessionApi {
  private readonly base: string;

  constructor(base = DEFAULT_BASE) {
    this.base = base;
  }

  private headers(): HeadersInit {
    const h: Record<string, string> = { Accept: "application/json" };
    const token = getToken().trim();
    if (token) h.Authorization = `Bearer ${token}`;
    return h;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    extraHeaders?: HeadersInit,
  ): Promise<T> {
    let res: Response;
    try {
      res = await fetch(`${this.base}${path}`, {
        method,
        headers: {
          ...this.headers(),
          ...(body !== undefined
            ? { "Content-Type": "application/json" }
            : {}),
          ...extraHeaders,
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        credentials: hostedMode ? "include" : "omit",
      });
    } catch {
      throw new ApiError("network", "network request failed", {
        subcode: "network",
        retryable: true,
      });
    }

    if (!res.ok) {
      // Wire error body: {error: {code, message, retryable, action}}.
      let code = "internal";
      let message = `The request could not be completed (${res.status}). Please try again.`;
      let retryable = false;
      let retryAfter: number | undefined;
      try {
        const data = await res.json();
        const err = data?.error;
        if (err && typeof err === "object") {
          if (err.code) code = String(err.code);
          if (err.message) message = String(err.message);
          retryable = Boolean(err.retryable);
          if (typeof err.retry_after === "number") retryAfter = err.retry_after;
        }
      } catch {
        /* non-JSON error body — keep defaults */
      }
      const retryAfterHeader = res.headers.get("retry-after");
      if (retryAfter == null && retryAfterHeader) {
        const parsed = Number(retryAfterHeader);
        if (!Number.isNaN(parsed)) retryAfter = parsed;
      }
      throw new ApiError(toErrorKind(code, res.status), message, {
        httpStatus: res.status,
        subcode: code,
        retryable,
        retryAfter,
      });
    }
    return (await res.json()) as T;
  }

  async listSessions(): Promise<Session[]> {
    // Envelope: {sessions, total, limit, offset}
    const data = await this.request<any>("GET", `${PATHS.sessions}?limit=100`);
    const rows = Array.isArray(data?.sessions) ? data.sessions : [];
    return rows.map((s: any) => mapSession(s));
  }

  async getSession(id: string): Promise<Session> {
    // Detail envelope: {session, runs: RunView[], run_count, truncated}
    const data = await this.request<any>("GET", PATHS.session(id));
    const runs = Array.isArray(data?.runs) ? data.runs.slice(-10).map(mapRun) : [];
    return mapSession(data?.session ?? {}, runs);
  }

  async readSessionCache(id: string) {
    if (hostedMode) return null;
    return readSessionCache(await cacheScope(this.base, getToken()), id);
  }
  async writeSessionCache(session: Session) {
    if (hostedMode) return;
    writeSessionCache(await cacheScope(this.base, getToken()), session);
  }
  async getHistory(id: string, before: number) {
    const data = await this.request<any>("GET", `${PATHS.session(id)}/history?before_n=${before}&limit=10`);
    const turns: Turn[] = (data.runs ?? []).map(mapRun);
    for (const entry of data.events ?? []) {
      const item = normalizeEvent(entry.event, id);
      const turn = turns.find(t=>t.id===item?.turnId);
      if (item && turn) turn.activity = mergeActivity(turn.activity, [item]);
    }
    return {turns,hasMore:Boolean(data.has_more)};
  }

  async createSession(input: NewSessionInput): Promise<Session> {
    // Envelope: {session: SessionView} — the id lives under `session`, never
    // at the top level.
    const data = await this.request<any>(
      "POST",
      PATHS.sessions,
      toCreateSessionRequest(input),
      { "Idempotency-Key": crypto.randomUUID() },
    );
    return mapSession(data?.session ?? {});
  }

  async sendFollowUp(
    sessionId: string,
    text: string,
  ): Promise<{ session: Session; n: number | null }> {
    // POST /v2/sessions/{id}/messages {prompt, on_busy} → 202 {session, message}
    const data = await this.request<any>("POST", PATHS.messages(sessionId), {
      prompt: text,
      on_busy: "queue",
    });
    return {
      session: mapSession(data?.session ?? {}),
      n: data?.message?.n != null ? Number(data.message.n) : null,
    };
  }

  async stopSession(sessionId: string): Promise<Session> {
    const data = await this.request<any>("POST", PATHS.cancel(sessionId));
    return mapSession(data?.session ?? {});
  }

  async retrySession(sessionId: string, prompt?: string): Promise<Session> {
    // mode=delivery re-publishes a failed delivery; mode=run re-runs the
    // failed turn. The backend picks the right thing when mode is omitted.
    const data = await this.request<any>("POST", PATHS.retry(sessionId), {
      mode: null,
      ...(prompt?.trim() ? { prompt: prompt.trim() } : {}),
      on_busy: "queue",
    });
    const runs: Turn[] = [];
    if (data?.run) runs.push(mapRun(data.run));
    const session = mapSession(data?.session ?? {});
    if (runs.length) session.turns = runs;
    return session;
  }

  /**
   * Explicit deliver (POST /v2/sessions/{id}/deliver). The console's only
   * delivery action is pull-request creation, so the request always
   * carries the pull_request override ({title, draft}); the synchronous
   * response returns the refreshed session plus the delivered revision.
   */
  async deliverSession(
    sessionId: string,
    input?: DeliverInput,
  ): Promise<SessionDeliverResult> {
    const body: Record<string, any> = {
      n: input?.n,
      branch: input?.branch,
      pull_request: {
        title: input?.title ?? undefined,
        draft: input?.draft ?? false,
        target: input?.target ?? undefined,
      },
    };
    const data = await this.request<any>("POST", PATHS.deliver(sessionId), body);
    return {
      session: mapSession(data?.session ?? {}),
      revision: mapRevisionChange(data?.revision),
    };
  }

  async listChangesDiff(sessionId: string): Promise<SessionChangesDiff> {
    const data = await this.request<any>("GET", PATHS.changesDiff(sessionId));
    return mapChangesDiff(data);
  }

  async getFileDiff(
    sessionId: string,
    path: string,
    n?: number,
  ): Promise<SessionFileDiff> {
    const data = await this.request<any>(
      "GET",
      PATHS.fileDiff(sessionId, path) + (n === undefined ? "" : `&n=${n}`),
    );
    const file = (Array.isArray(data?.files) ? data.files : [])[0];
    if (!file || typeof file.diff !== "string") {
      throw new ApiError("not_found", `no diff for file ${path}`, {
        httpStatus: 404,
        subcode: "not_found",
      });
    }
    return {
      path: String(file.path),
      status: file.status ?? "modified",
      additions: Number(file.additions ?? 0),
      deletions: Number(file.deletions ?? 0),
      oldPath: file.old_path ?? undefined,
      diff: file.diff,
    };
  }

  async listProviders(): Promise<ProviderInfo[]> {
    // Envelope: {providers: Provider[]}; real fields: provider, status
    // (constant "available"), readiness, default_models, cli, runtime{…},
    // connection{status, accounts_total, accounts_available, detail}.
    const data = await this.request<any>("GET", PATHS.providers);
    const rows = Array.isArray(data?.providers) ? data.providers : [];
    return rows.map((p: any): ProviderInfo => {
      const conn = p?.connection ?? {};
      const runtime = p?.runtime ?? {};
      const readiness = String(p?.readiness ?? "disabled");
      const runtimeStatus = String(runtime.status ?? "unknown");
      return {
        id: String(p?.provider ?? ""),
        label: String(p?.provider ?? "").replace(/^\w/, (c) => c.toUpperCase()),
        support: p?.support ?? undefined,
        readiness,
        models: Array.isArray(p?.default_models)
          ? p.default_models.map(String)
          : [],
        runtimeStatus,
        runtimeEnabled: Boolean(runtime.enabled),
        connectionStatus: String(conn.status ?? "not_connected"),
        connectionDetail: conn.detail ?? undefined,
        accountsTotal: Number(conn.accounts_total ?? 0),
        accountsAvailable: Number(conn.accounts_available ?? 0),
        needsLogin: readiness === "needs_login",
      };
    });
  }

  async listModels(): Promise<ModelInfo[]> {
    // Envelope: {models: Model[]} — the only agents-scope surface exposing
    // real account ids (row.account).
    const data = await this.request<any>("GET", PATHS.models);
    const rows = Array.isArray(data?.models) ? data.models : [];
    return rows.map((m: any): ModelInfo => ({
      provider: String(m?.provider ?? ""),
      model: String(m?.model ?? ""),
      displayName: m?.display_name ?? undefined,
      account: m?.account != null ? String(m.account) : undefined,
      accountsAvailable: Number(m?.accounts_available ?? 0),
      availability: m?.availability ?? undefined,
      reasoningEfforts: Array.isArray(m?.reasoning_efforts)
        ? m.reasoning_efforts
        : [],
      defaultEffort: m?.default_effort ?? undefined,
    }));
  }

  async getIntegrations(): Promise<IntegrationStatus> {
    const [providers, gh] = await Promise.all([
      this.listProviders(),
      this.request<any>("GET", PATHS.githubApp).catch((e) => {
        // GitHub card should degrade gracefully, not 404 the page.
        if (e instanceof ApiError && e.kind === "github_required") {
          return { configured: false, installable: false, installations: [] };
        }
        throw e;
      }),
    ]);
    const installations = Array.isArray(gh?.installations) ? gh.installations : [];
    const accounts = installations
      .map((i: any) => i?.account_login)
      .filter((a: unknown): a is string => typeof a === "string" && !!a);
    return {
      providers,
      github: {
        configured: Boolean(gh?.configured),
        installable: Boolean(gh?.installable ?? gh?.configured),
        connected: accounts.length > 0 || Boolean(gh?.broker?.bound),
        bridgeToken: Boolean(gh?.bridge_token),
        brokerBound: Boolean(gh?.broker?.bound),
        brokerHealthy: Boolean(gh?.broker?.healthy),
        accounts,
        appSlug: gh?.app_slug ?? undefined,
        source: gh?.source ?? undefined,
      },
      runtime: {
        enabled: providers.some((p) => p.runtimeEnabled),
      },
    };
  }

  async beginGithubAuthorize(): Promise<{ url: string }> {
    // POST /v1/github/app/authorize → 201 {authorize_url, state, expires_at}
    const data = await this.request<any>("POST", PATHS.githubAuthorize, {});
    const url = data?.authorize_url;
    if (typeof url !== "string" || !url) {
      throw new ApiError("github_required", "no authorize_url in response", {
        subcode: "github_app_invalid",
      });
    }
    return { url };
  }

  async listChanges(sessionId: string): Promise<SessionChange[]> {
    // Envelope: {session, changes: ChangesView|null, revisions: RevisionView[]}
    const data = await this.request<any>("GET", PATHS.changes(sessionId));
    const rows: SessionChange[] = [];
    const changes = data?.changes ?? null;
    if (changes && typeof changes === "object" && changes.status !== "none") {
      const pr = changes.pull_request ?? null;
      rows.push({
        id: "workspace",
        kind: "workspace",
        status: String(changes.status ?? ""),
        summary:
          changes.status === "ready"
            ? "workspace changes ready"
            : "workspace unchanged",
        ts: String(changes.updated_at ?? ""),
        branch: changes.branch ?? undefined,
        headSha: changes.head_sha ?? undefined,
        url: pr?.url ?? undefined,
      });
    }
    const revisions = Array.isArray(data?.revisions) ? data.revisions : [];
    for (const r of revisions) {
      rows.push(mapRevisionChange(r));
    }
    return rows;
  }

  /**
   * Session-scoped SSE (GET /v2/sessions/{id}/events). The stream replays
   * history then follows live, covers every turn (not just the run active
   * at connect), and resumes with Last-Event-ID on the frame's ``id:``
   * line. Item rows dedupe via stable ``item-<id>`` ids — a completed frame
   * replaces its started placeholder.
   */

  subscribe(sessionId: string, handlers: SessionEventHandlers): () => void {
    let closed = false;
    let attempt = 0;
    let lastEventId: string | null = null;
    let abort: AbortController | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let sawDisconnect = false;
    let statusTimer: ReturnType<typeof setInterval> | null = null;

    const pending = new Map<string, ActivityItem>();
    let paint: number | null = null;
    let fallback: ReturnType<typeof setTimeout> | null = null;
    const flush = () => {
      if (paint !== null) cancelAnimationFrame(paint);
      if (fallback !== null) clearTimeout(fallback);
      paint = null; fallback = null;
      const items = [...pending.values()]; pending.clear();
      if (!items.length || closed) return;
      if (handlers.onActivities) handlers.onActivities(items);
      else items.forEach(item=>handlers.onActivity?.(item));
    };
    const emit = (item: ActivityItem | null) => {
      if (!item) return;
      const key = `${item.turnId}:${item.id}`;
      const prior = pending.get(key);
      if (prior && (prior.status === "finished" || prior.status === "failed") && item.status === "running") return;
      pending.set(key, prior ? {...item,seq:prior.seq,ts:prior.ts} : item);
      if (fallback === null) {
        fallback = setTimeout(flush,50);
        paint = requestAnimationFrame(flush);
      }
    };


    const handleFrame = (frame: any) => {
      const type = String(frame?.type ?? "");
      switch (type) {
        case "session.status": {
          flush();
          // Fresh on every connect (no id) — always apply.
          const phase = normalizePhase(frame.phase, frame.status);
          handlers.onPhase?.(phase);
          emit({
            id: `evt-status-${sessionId}-${String(frame.phase ?? frame.status ?? "x")}`,
            seq: nextSeq(),
            ts: new Date().toISOString(),
            turnId: null,
            kind: "status",
            status: String(frame.phase ?? frame.status ?? ""),
          });
          // Terminal states: pull the final SessionView so usage/cost/
          // delivery/error settle without a manual refresh.
          const s = String(frame.status ?? "").toLowerCase();
          if (s === "finished" || s === "failed" || s === "cancelled") {
            void this.getSession(sessionId)
              .then((sess) => handlers.onSession?.(sess))
              .catch(() => undefined);
          }
          return;
        }
        case "session.meta": {
          handlers.onMeta?.({
            provider: frame.provider ?? null,
            model: frame.model ?? null,
          });
          return;
        }
        case "turn.started":
        case "turn.finished":
        case "turn.completed":
        case "turn.failed": {
          flush();
          const n = typeof frame.n === "number" ? frame.n : undefined;
          if (n != null) {
            handlers.onTurn?.({
              id: `turn-${n}`,
              index: n,
              prompt: "",
              status:
                type === "turn.started"
                  ? "running"
                  : type === "turn.failed" ||
                      String(frame.status ?? "") === "failed"
                    ? "failed"
                    : type === "turn.completed" ||
                        String(frame.status ?? "") === "finished"
                      ? "finished"
                      : normalizeTurnStatus(frame.status),
              createdAt: "",
              startedAt: null,
              finishedAt: null,
              result: null,
              error: toTurnError(frame.error),
              usage: usageOf(frame.usage),
              activity: [],
            });
          }
          emit(normalizeEvent(frame, sessionId));
          // Terminal session states arrive via session.status; a finished
          // turn while session stays running keeps streaming.
          return;
        }
        default:
          emit(normalizeEvent(frame, sessionId));
      }
    };

    const open = async () => {
      if (closed) return;
      abort = new AbortController();
      try {
        let url = `${this.base}${PATHS.events(sessionId)}${handlers.historyAfterTurn ? `?after_n=${handlers.historyAfterTurn}` : ""}`;
        let directGrant: string | null = null;
        if (hostedMode) {
          try {
            let connection: any = null;
            const deadline = Date.now() + 5000;
            while (!closed && !connection) {
              try { connection = await this.request<any>("POST", `/hosted/sessions/${encodeURIComponent(sessionId)}/connect`, {}); }
              catch (error) {
                if (!(error instanceof ApiError) || error.httpStatus !== 409 || Date.now() >= deadline) throw error;
                await new Promise(resolve => setTimeout(resolve, 100));
              }
            }
            if (closed) return;
            url = connection.url; directGrant = connection.grant;
            if (statusTimer === null) statusTimer = setInterval(() => {
              void this.getSession(sessionId).then(session => {
                handlers.onSession?.(session); handlers.onPhase?.(session.phase);
              }).catch(() => {});
            }, 1000);
          } catch { /* Provisioning/unreachable runtime: use the relayed stream. */ }
        }
        const streamHeaders = directGrant ? {
          Authorization: `Bearer ${directGrant}`, Accept: "text/event-stream",
          ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
        } : {
            ...this.headers(),
            Accept: "text/event-stream",
            ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
        };
        let res: Response;
        try {
          res = await fetch(url, {
            headers: streamHeaders,
            credentials: directGrant ? "omit" : hostedMode ? "include" : "omit",
            signal: abort.signal,
          });
          if (directGrant && !res.ok) throw new Error("direct runtime unavailable");
        } catch (error) {
          if (!directGrant || abort.signal.aborted) throw error;
          res = await fetch(`${this.base}${PATHS.events(sessionId)}`, {
            headers: {...this.headers(), Accept: "text/event-stream", ...(lastEventId ? {"Last-Event-ID": lastEventId} : {})},
            credentials: "include",
            signal: abort.signal,
          });
        }
        if (!res.ok) {
          let code = "internal";
          try {
            const data = await res.clone().json();
            if (data?.error?.code) code = String(data.error.code);
          } catch {
            /* ignore */
          }
          throw new ApiError(toErrorKind(code, res.status), `events → ${res.status}`, {
            httpStatus: res.status,
            subcode: code,
            retryable: res.status >= 500,
          });
        }
        handlers.onOpen?.();
        if (sawDisconnect) {
          sawDisconnect = false;
          handlers.onReconnect?.();
        }
        const reader = res.body?.getReader();
        if (!reader) throw new ApiError("network", "no event stream body");
        const decoder = new TextDecoder();
        let buf = "";
        let frameLines: string[] = [];
        let frameId: string | null = null;

        const dispatch = () => {
          const data = frameLines.join("\n");
          frameLines = [];
          if (frameId != null) lastEventId = frameId;
          frameId = null;
          if (!data) return;
          try {
            handleFrame(JSON.parse(data));
            attempt = 0;
          } catch {
            /* malformed frame — skip */
          }
        };

        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let idx: number;
          while ((idx = buf.indexOf("\n")) >= 0) {
            const line = buf.slice(0, idx).replace(/\r$/, "");
            buf = buf.slice(idx + 1);
            if (line === "") {
              dispatch();
            } else if (line.startsWith(":")) {
              // keepalive — never surfaces as an activity row
              continue;
            } else if (line.startsWith("id:")) {
              frameId = line.slice(3).trim();
            } else if (line.startsWith("data:")) {
              frameLines.push(line.slice(5).replace(/^ /, ""));
            }
          }
        }
        flush();
        throw new ApiError("network", "event stream ended", {
          subcode: "eof",
          retryable: true,
        });
      } catch (e) {
        if (closed) return;
        flush();
        if (e instanceof ApiError && [401, 403, 404].includes(e.httpStatus)) {
          handlers.onError?.(e);
          return; // session gone — don't retry
        }
        sawDisconnect = true;
        const delay = SSE_BACKOFF_MS[Math.min(attempt, SSE_BACKOFF_MS.length - 1)];
        attempt += 1;
        handlers.onDisconnect?.(delay);
        retryTimer = setTimeout(open, delay);
      }
    };

    void open();
    return () => {
      closed = true;
      pending.clear();
      if (paint !== null) cancelAnimationFrame(paint);
      if (fallback !== null) clearTimeout(fallback);
      if (retryTimer) clearTimeout(retryTimer);
      if (statusTimer) clearInterval(statusTimer);
      abort?.abort();
    };
  }
}
