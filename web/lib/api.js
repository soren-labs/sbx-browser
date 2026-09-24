import { getConnection } from "./config.js";

/** Error from the /v1 API: `{error: {code, message, retry_after?}}`. */
export class ApiError extends Error {
  constructor({ status, code, message, retryAfter = null, body = null }) {
    super(message || code || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code || (status === 0 ? "network_error" : `http_${status}`);
    this.retryAfter = retryAfter;
    this.body = body;
  }
}

export function apiUrl(path, query) {
  const base = getConnection().baseUrl || window.location.origin;
  const url = new URL(path, base.endsWith("/") ? base : `${base}/`);
  // `new URL("/v1/x", "https://h/prefix/")` drops the prefix; keep it.
  if (getConnection().baseUrl) {
    const prefix = new URL(base).pathname.replace(/\/+$/, "");
    url.pathname = `${prefix}${path}`;
  }
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value != null && value !== "") url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

export function authHeaders(key = getConnection().apiKey) {
  return key ? { Authorization: `Bearer ${key}` } : {};
}

async function parseError(res) {
  let body = null;
  const text = await res.text().catch(() => "");
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { raw: text };
  }
  const err = body?.error;
  if (err && typeof err === "object") {
    return new ApiError({
      status: res.status,
      code: err.code,
      message: err.message,
      retryAfter: err.retry_after ?? null,
      body,
    });
  }
  // FastAPI validation errors: {detail: [...]}
  if (Array.isArray(body?.detail)) {
    const first = body.detail[0] || {};
    const where = Array.isArray(first.loc) ? first.loc.slice(1).join(".") : "";
    return new ApiError({
      status: res.status,
      code: "invalid_request",
      message: where ? `${where}: ${first.msg}` : first.msg,
      body,
    });
  }
  return new ApiError({
    status: res.status,
    code: typeof body?.detail === "string" ? body.detail : undefined,
    body,
  });
}

export async function request(
  method,
  path,
  { query, body, key, raw = false, signal, headers: extra } = {},
) {
  const headers = { Accept: "application/json", ...authHeaders(key), ...(extra || {}) };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(apiUrl(path, query), {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
      cache: "no-store",
    });
  } catch (err) {
    if (err?.name === "AbortError") throw err;
    throw new ApiError({ status: 0, code: "network_error", message: String(err?.message || err) });
  }
  if (!res.ok) {
    const error = await parseError(res);
    if (res.status === 401 && key === undefined) {
      window.dispatchEvent(new CustomEvent("sbx:unauthorized", { detail: error }));
    }
    throw error;
  }
  if (raw) return res;
  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

const enc = encodeURIComponent;

export const api = {
  me: (key) => request("GET", "/v1/me", { key }),
  models: () => request("GET", "/v1/models"),
  capabilities: () => request("GET", "/v1/capabilities"),
  refreshCapabilities: () => request("POST", "/v1/capabilities/refresh"),
  refreshAccountCapabilities: (id) =>
    request("POST", `/v1/accounts/${enc(id)}/capabilities/refresh`),

  listAgents: (query) => request("GET", "/v1/agents", { query }),
  getAgent: (id) => request("GET", `/v1/agents/${enc(id)}`),
  createAgent: (body, idempotencyKey) =>
    request("POST", "/v1/agents", {
      body,
      headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
    }),
  closeAgent: (id) => request("DELETE", `/v1/agents/${enc(id)}`),
  usage: (id) => request("GET", `/v1/agents/${enc(id)}/usage`),

  listRuns: (id) => request("GET", `/v1/agents/${enc(id)}/runs`),
  getRun: (id, runId) => request("GET", `/v1/agents/${enc(id)}/runs/${enc(runId)}`),
  createRun: (id, body) => request("POST", `/v1/agents/${enc(id)}/runs`, { body }),
  cancelRun: (id, runId) => request("POST", `/v1/agents/${enc(id)}/runs/${enc(runId)}/cancel`),
  streamPath: (id, runId) => `/v1/agents/${enc(id)}/runs/${enc(runId)}/stream`,

  workspace: (id) => request("GET", `/v1/agents/${enc(id)}/workspace`),
  review: (id, body) => request("POST", `/v1/agents/${enc(id)}/workspace/review`, { body }),
  publish: (id) => request("POST", `/v1/agents/${enc(id)}/git/publish`),
  merge: (id) => request("POST", `/v1/agents/${enc(id)}/git/merge`),
  handoff: (id, body) => request("POST", `/v1/agents/${enc(id)}/handoff`, { body }),

  createArtifact: (id, body) => request("POST", `/v1/agents/${enc(id)}/artifacts`, { body }),
  listArtifacts: (query) => request("GET", "/v1/artifacts", { query }),
  getArtifact: (artifactId) => request("GET", `/v1/artifacts/${enc(artifactId)}`),
  downloadArtifact: (artifactId, member) =>
    request("GET", `/v1/artifacts/${enc(artifactId)}/download`, { query: { member }, raw: true }),

  workflow: (workflowId) => request("GET", `/v1/workflows/${enc(workflowId)}`),
  closeWorkflow: (workflowId) => request("DELETE", `/v1/workflows/${enc(workflowId)}`),

  listAccounts: (query) => request("GET", "/v1/accounts", { query }),
  createAccount: (body) => request("POST", "/v1/accounts", { body }),
  deleteAccount: (id) => request("DELETE", `/v1/accounts/${enc(id)}`),
  verifyAccount: (id) => request("POST", `/v1/accounts/${enc(id)}/verify`),

  listKeys: () => request("GET", "/v1/api-keys"),
  createKey: (body) => request("POST", "/v1/api-keys", { body }),
  revokeKey: (id) => request("DELETE", `/v1/api-keys/${enc(id)}`),

  githubStatus: () => request("GET", "/v1/github/app"),
  githubAuthorize: () => request("POST", "/v1/github/app/authorize"),
  githubCallback: (body) => request("POST", "/v1/github/app/authorize/callback", { body }),
  githubSync: () => request("POST", "/v1/github/app/sync"),
  githubRevoke: (installationId) =>
    request("DELETE", `/v1/github/app/installations/${enc(installationId)}`),
};

/** Unwrap `{artifact}` (create) vs bare manifest (get) responses. */
export function unwrapArtifact(body) {
  return body?.artifact ?? body;
}
