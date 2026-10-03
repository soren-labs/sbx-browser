import { hostedMode } from "../hosted/api";
import { getToken } from "../api/http";
import { demoMode } from "./demo";
import type { ActivityItem } from "../api/types";

export type AccountHealth = "ready" | "needs_login" | "disabled" | "refreshing";
export interface AccountConnection {
  id: string;
  provider: string;
  label: string;
  health: AccountHealth;
  lastVerified: string;
  models: string[];
  status?: string;
  authState?: string;
  lastError?: string;
}
export const initialAccounts: AccountConnection[] = [
  {
    id: "demo-codex-work",
    provider: "codex",
    label: "Soren · work",
    health: "ready",
    lastVerified: "2 minutes ago",
    models: ["GPT-6.1 Sol", "GPT-6 Astra", "GPT-6 Luna"],
  },
  {
    id: "demo-codex-personal",
    provider: "codex",
    label: "Soren · personal",
    health: "ready",
    lastVerified: "18 minutes ago",
    models: ["GPT-6.1 Sol", "GPT-6 Astra"],
  },
  {
    id: "demo-devin",
    provider: "devin",
    label: "Sorenforge team",
    health: "ready",
    lastVerified: "5 minutes ago",
    models: ["SWE-2"],
  },
  {
    id: "demo-agy",
    provider: "antigravity",
    label: "Engineering",
    health: "ready",
    lastVerified: "32 minutes ago",
    models: ["Gemini 3.8 Pro"],
  },
  {
    id: "demo-grok",
    provider: "grok",
    label: "Soren · work",
    health: "ready",
    lastVerified: "1 hour ago",
    models: ["Grok 4.7"],
  },
  {
    id: "demo-opencode",
    provider: "opencode",
    label: "OpenCode Zen",
    health: "needs_login",
    lastVerified: "Connection expired",
    models: ["Muse Spark 1.3"],
  },
];
let demoAccounts = structuredClone(initialAccounts);

/** Product facade: transport versions never appear in the workspace. */
async function managementRequest(
  path: string,
  body?: unknown,
  method?: string,
) {
  const base = String(import.meta.env.VITE_API_BASE ?? "").replace(/\/+$/, "");
  const response = await fetch(base + "/v1" + path, {
    method: method ?? (body === undefined ? "GET" : "POST"),
    credentials: hostedMode ? "include" : "omit",
    headers: {
      "Content-Type": "application/json",
      ...(hostedMode ? {} : {Authorization: `Bearer ${getToken()}`}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    const failure = await response.json().catch(() => ({}));
    throw new Error(
      failure.error?.message ??
        (response.status === 403
          ? "Account management requires an administrator connection."
          : `The connection could not be updated (${response.status}). Please try again.`),
    );
  }
  return response.status === 204 ? null : response.json();
}
export interface ConnectFlow {
  id: string;
  state: string;
  kind?: "pair" | "hosted";
  account_id?: string;
  browser_url?: string;
  pair_command?: string;
  user_code?: string;
  expires_at?: string;
  error?: { code?: string; message?: string };
}
export const connectionTerminal = (state: string) =>
  ["verified", "materialized", "failed", "cancelled", "expired"].includes(
    state,
  );
export const connections = {
  rememberDemo(accounts: AccountConnection[]) {
    if (demoMode) demoAccounts = structuredClone(accounts);
  },
  async list(): Promise<AccountConnection[]> {
    if (demoMode) return structuredClone(demoAccounts);
    const data = (await managementRequest("/accounts")) as {
      accounts: {
        id: string;
        provider: string;
        label: string;
        status: string;
        models: string[];
        auth_state?: string;
        last_error?: string;
      }[];
    };
    return data.accounts.map((a) => ({
      id: a.id,
      provider: a.provider,
      label: a.label,
      models: a.models ?? [],
      status: a.status,
      authState: a.auth_state,
      lastError: a.last_error,
      lastVerified:
        a.auth_state === "verified" ? "Verified connection" : "Not verified",
      health:
        a.status === "active"
          ? "ready"
          : a.status === "disabled"
            ? "disabled"
            : "needs_login",
    }));
  },
  async check(id: string, action: "verify" | "refresh") {
    if (demoMode) {
      await new Promise((resolve) => setTimeout(resolve, 650));
      return;
    }
    const result = await managementRequest(
      `/accounts/${encodeURIComponent(id)}/${action === "verify" ? "verify" : "lifecycle/refresh"}`,
      {},
    );
    if (action === "verify" && (result.status !== "active" || result.last_error))
      throw new Error(
        result.last_error ??
          "This account could not be verified. Reconnect it and try again.",
      );
    if (
      action === "refresh" &&
      ["auth_invalid", "revoked"].includes(result.result)
    )
      throw new Error(
        "Refresh requires a new sign-in. Reconnect this account.",
      );
    return result;
  },
  async lifecycle(id: string) {
    return managementRequest(`/accounts/${encodeURIComponent(id)}/lifecycle`);
  },
  async remove(id: string) {
    if (demoMode) {
      demoAccounts = demoAccounts.filter((a) => a.id !== id);
      return;
    }
    await managementRequest(
      `/accounts/${encodeURIComponent(id)}`,
      undefined,
      "DELETE",
    );
  },
  async retry(id: string): Promise<ConnectFlow> {
    return managementRequest(
      `/auth/connect/${encodeURIComponent(id)}/retry`,
      {},
    );
  },
  async begin(
    provider: string,
    label: string,
    accountId?: string,
  ): Promise<ConnectFlow> {
    if (demoMode)
      return { id: "demo-pair", state: "awaiting_user", user_code: "SBX-DEMO" };
    return managementRequest("/auth/connect", {
      provider,
      label,
      ...(accountId ? { account_id: accountId } : {}),
    });
  },
  async poll(id: string) {
    return managementRequest(`/auth/connect/${encodeURIComponent(id)}`);
  },
  async cancel(id: string) {
    if (!demoMode)
      await managementRequest(
        `/auth/connect/${encodeURIComponent(id)}/cancel`,
        {},
      );
  },
  async refreshModels(provider: string) {
    if (demoMode) {
      await new Promise((resolve) => setTimeout(resolve, 400));
      return;
    }
    await managementRequest(
      `/models/refresh?provider=${encodeURIComponent(provider)}`,
      {},
    );
  },
  async syncGithub() {
    if (demoMode) {
      await new Promise((resolve) => setTimeout(resolve, 650));
      return;
    }
    await managementRequest("/github/app/sync", {});
  },
};

export interface WorkGroup {
  id: string;
  title: string;
  items: ActivityItem[];
  category: "changes" | "work";
  running: boolean;
}
export type WorkEntry =
  | { type: "message"; item: ActivityItem }
  | { type: "group"; group: WorkGroup };
/** Presentation grouping over the canonical normalized event kinds, never new SSE types. */
export function groupActivity(activity: ActivityItem[]): WorkEntry[] {
  const entries: WorkEntry[] = [];
  const unique = [...new Map(activity.map((a) => [a.id, a])).values()].sort(
    (a, b) => a.seq - b.seq,
  );
  let group: WorkGroup | null = null;
  for (const item of unique) {
    if (item.kind === "message") {
      group = null;
      entries.push({ type: "message", item });
      continue;
    }
    if (item.kind === "status") continue;
    if (!group) {
      group = {
        id: item.id,
        title: item.text ?? "Working through the task",
        items: [],
        category: "work",
        running: false,
      };
      entries.push({ type: "group", group });
    }
    group.items.push(item);
    if (item.kind === "file_change") group.category = "changes";
    group.running = group.items.some((i) => i.status === "running");
  }
  return entries;
}

export interface ReviewRecord {
  id: string;
  verdict: "approve" | "request_changes" | "comment";
  reviewed_head_sha?: string;
  independent?: boolean;
  stale?: boolean;
  created_at?: string;
}
const localReviews = new Map<string, ReviewRecord[]>();
/** Session IDs are durable task IDs in the current Session facade. */
export const reviews = {
  async list(sessionId: string, revision?: number): Promise<ReviewRecord[]> {
    if (demoMode) return structuredClone(localReviews.get(sessionId) ?? []);
    const data = await managementRequest(
      `/tasks/${encodeURIComponent(sessionId)}/reviews${revision === undefined ? "" : `?revision=${revision}`}`,
    );
    return (data.reviews ?? []).sort((a: ReviewRecord, b: ReviewRecord) =>
      (a.created_at ?? "").localeCompare(b.created_at ?? ""),
    );
  },
  async record(
    sessionId: string,
    revision: number,
    verdict: ReviewRecord["verdict"],
  ) {
    if (demoMode) {
      const record = {
        id: `demo-review-${Date.now()}`,
        verdict,
        independent: true,
        stale: false,
      };
      localReviews.set(sessionId, [
        ...(localReviews.get(sessionId) ?? []),
        record,
      ]);
      return record;
    }
    const data = await managementRequest(
      `/tasks/${encodeURIComponent(sessionId)}/reviews`,
      { revision: String(revision), verdict },
    );
    return data.review as ReviewRecord;
  },
  async merge(sessionId: string, revision: number) {
    if (demoMode) return;
    await managementRequest(`/tasks/${encodeURIComponent(sessionId)}/merge`, {
      revision: String(revision),
    });
  },
};
