import { ApiError } from "./client";
import { getToken, HttpSessionApi } from "./http";

// Only an outcome travels between tabs, never callback credentials or API keys.
export const GITHUB_RETURN_KEY = "sbx.console.github-return";
export const GITHUB_PENDING_KEY = "sbx.console.github-pending-until";
export const GITHUB_WAIT_MS = 10 * 60_000;
export const GITHUB_REQUEST_MS = 15_000;

export function githubReturnError(code: string): string {
  if (code === "connection_key_required")
    return "GitHub authorization needs a workspace connection key. Save it in Settings, then connect GitHub again.";
  if (code === "authorization_expired")
    return "GitHub authorization expired or was already used. Connect GitHub again.";
  if (code === "connection_failed")
    return "GitHub authorization could not be confirmed. Check your connection, then connect GitHub again.";
  return "GitHub authorization did not complete. Connect GitHub again.";
}

function finish(error?: string) {
  history.replaceState(
    null,
    "",
    `/integrations?${error ? `broker_error=${error}` : "broker=returned"}`,
  );
  try {
    localStorage.setItem(
      GITHUB_RETURN_KEY,
      JSON.stringify({ error, at: Date.now(), nonce: crypto.randomUUID() }),
    );
  } catch {
    /* Focus refresh and polling also work when storage is unavailable. */
  }
}

/** Run before the router mounts. A return marker requests verification; it is
 * never evidence that an installation is connected. */
export async function handleGithubReturn() {
  const query = new URLSearchParams(location.search);
  const legacy = location.hash.match(
    /^#\/(?:admin\/github|integrations\/github|integrations)\/?(?:\?(.*))?$/,
  );
  const hashQuery = new URLSearchParams(legacy?.[1]);
  const value = (key: string) => query.get(key) ?? hashQuery.get(key);
  const installation = value("installation_id"),
    state = value("state");

  // GitHub appends query parameters before the old Setup URL fragment. This
  // callback must take precedence over adapting that fragment to a new route.
  if (
    installation !== null ||
    state !== null ||
    value("setup_action") !== null
  ) {
    history.replaceState(null, "", "/integrations");
    if (
      !installation ||
      !/^[1-9]\d*$/.test(installation) ||
      !Number.isSafeInteger(Number(installation)) ||
      !state
    ) {
      finish("authorization_failed");
      return;
    }
    if (!getToken().trim()) {
      finish("connection_key_required");
      return;
    }
    try {
      await new HttpSessionApi().completeGithubAuthorize(
        Number(installation),
        state,
        AbortSignal.timeout(GITHUB_REQUEST_MS),
      );
      finish();
    } catch (error) {
      finish(
        error instanceof ApiError && error.httpStatus === 401
          ? "connection_key_required"
          : error instanceof ApiError && error.subcode === "github_app_state"
            ? "authorization_expired"
            : error instanceof ApiError && error.kind === "network"
              ? "connection_failed"
              : "authorization_failed",
      );
    }
    return;
  }
  if (value("broker_error") !== null) {
    finish("authorization_failed");
  } else if (["connected", "returned"].includes(value("broker") ?? "")) {
    finish();
  } else if (legacy) {
    // A bare legacy route is navigation, not a successful authorization.
    history.replaceState(null, "", "/integrations");
  }
}
