import { useCallback, useEffect, useRef, useState } from "react";
import type { IntegrationStatus } from "../api/types";
import {
  GITHUB_PENDING_KEY,
  GITHUB_REQUEST_MS,
  GITHUB_RETURN_KEY,
  GITHUB_WAIT_MS,
  githubReturnError,
} from "../api/github-return";
import { useApi } from "../state/api";
import { connections } from "./domain";
import { demoMode } from "./demo";
import { Icon } from "./Icon";

const NOT_CONFIRMED =
  "GitHub installation has not been confirmed. Check status or connect GitHub again.";

function initialPendingUntil() {
  if (new URLSearchParams(location.search).has("broker_error")) return 0;
  try {
    const saved = Number(sessionStorage.getItem(GITHUB_PENDING_KEY));
    if (saved > Date.now()) return saved;
  } catch {
    /* Storage is optional. */
  }
  const returned = new URLSearchParams(location.search).get("broker");
  return ["connected", "returned"].includes(returned ?? "")
    ? Date.now() + GITHUB_WAIT_MS
    : 0;
}

/** GitHub has a separate lifecycle from provider account pairing. All live
 * connection state comes from GET /v1/github/app, including after callbacks. */
export function GithubConnection() {
  const api = useApi();
  const [status, setStatus] = useState<IntegrationStatus["github"]>();
  const [refreshing, setRefreshing] = useState(true);
  const [statusError, setStatusError] = useState("");
  const [actionError, setActionError] = useState(() => {
    const code = new URLSearchParams(location.search).get("broker_error");
    return code !== null ? githubReturnError(code) : "";
  });
  const [notice, setNotice] = useState("");
  const [action, setAction] = useState<"connect" | "sync" | "">("");
  const [pendingUntil, setPendingUntil] = useState(initialPendingUntil);
  const pending = useRef(pendingUntil);
  const mounted = useRef(false);
  const generation = useRef(0);
  const request = useRef<{
    controller: AbortController;
    queued: boolean;
  } | null>(null);
  const actionController = useRef<AbortController | null>(null);

  const waitUntil = useCallback((until: number) => {
    pending.current = until;
    setPendingUntil(until);
    try {
      if (until) sessionStorage.setItem(GITHUB_PENDING_KEY, String(until));
      else sessionStorage.removeItem(GITHUB_PENDING_KEY);
    } catch {
      /* Polling still works without storage. */
    }
  }, []);

  const invalidate = useCallback(() => {
    generation.current += 1;
    request.current?.controller.abort();
    request.current = null;
  }, []);

  const refresh = useCallback(async (): Promise<void> => {
    if (!mounted.current || actionController.current) return;
    if (request.current) {
      request.current.queued = true;
      return;
    }
    const id = ++generation.current;
    const current = { controller: new AbortController(), queued: false };
    request.current = current;
    const timeout = setTimeout(
      () => current.controller.abort(),
      GITHUB_REQUEST_MS,
    );
    setRefreshing(true);
    try {
      const next = await api.getGithubStatus(current.controller.signal);
      if (!mounted.current || id !== generation.current) return;
      setStatus(next);
      setStatusError("");
      if (!next.connected) setNotice("");
      if (next.connected)
        setActionError((previous) =>
          previous === NOT_CONFIRMED ? "" : previous,
        );
      if (next.connected && pending.current) {
        waitUntil(0);
        setNotice("GitHub installation connected.");
        setActionError("");
      }
    } catch (error) {
      if (mounted.current && id === generation.current) {
        setNotice("");
        setStatusError(
          current.controller.signal.aborted
            ? "GitHub status check timed out. Try checking again."
            : `Could not check GitHub status: ${(error as Error).message}`,
        );
      }
    } finally {
      clearTimeout(timeout);
      if (mounted.current && id === generation.current) {
        request.current = null;
        setRefreshing(false);
        // Coalesce focus/return events during a slow request into one fresh read.
        if (current.queued) void refresh();
      }
    }
  }, [api, waitUntil]);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const onVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const onFocus = () => void refresh();
    const onReturn = (event: StorageEvent) => {
      if (event.key !== GITHUB_RETURN_KEY || !event.newValue) return;
      try {
        const result = JSON.parse(event.newValue) as { error?: string };
        if (result.error) {
          waitUntil(0);
          setNotice("");
          setActionError(githubReturnError(result.error));
        }
      } catch {
        /* The notification is only a hint to refetch backend truth. */
      }
      void refresh();
    };
    window.addEventListener("focus", onFocus);
    window.addEventListener("pageshow", onFocus);
    window.addEventListener("sbx-connection-change", onFocus);
    window.addEventListener("storage", onReturn);
    document.addEventListener("visibilitychange", onVisible);
    const query = new URLSearchParams(location.search);
    if (query.has("broker") || query.has("broker_error")) {
      if (query.has("broker_error")) waitUntil(0);
      query.delete("broker");
      query.delete("broker_error");
      history.replaceState(
        null,
        "",
        location.pathname + (query.size ? `?${query}` : "") + location.hash,
      );
    }
    return () => {
      mounted.current = false;
      invalidate();
      actionController.current?.abort();
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("pageshow", onFocus);
      window.removeEventListener("sbx-connection-change", onFocus);
      window.removeEventListener("storage", onReturn);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh, invalidate, waitUntil]);

  useEffect(() => {
    if (!pendingUntil || demoMode) return;
    const timer = setInterval(() => {
      if (Date.now() >= pendingUntil) {
        waitUntil(0);
        setActionError(NOT_CONFIRMED);
      } else if (document.visibilityState === "visible") {
        // Polling never queues another poll behind an already-running read.
        if (!request.current) void refresh();
      }
    }, 2500);
    return () => clearInterval(timer);
  }, [pendingUntil, refresh, waitUntil]);

  const perform = async (kind: "connect" | "sync") => {
    if (actionController.current) return;
    // Open synchronously with the click to avoid popup blockers. A blocked
    // popup falls back to same-tab navigation, with pending state persisted.
    const popup =
      kind === "connect" && !demoMode
        ? window.open("about:blank", "_blank")
        : null;
    if (popup) popup.opener = null;
    const controller = new AbortController();
    actionController.current = controller;
    const timeout = setTimeout(() => controller.abort(), GITHUB_REQUEST_MS);
    invalidate();
    setAction(kind);
    setActionError("");
    setNotice("");
    try {
      if (kind === "sync") {
        await connections.syncGithub(controller.signal);
      } else if (demoMode) {
        setStatus((previous) =>
          previous ? { ...previous, connected: true } : previous,
        );
      } else {
        const { url } = await api.beginGithubAuthorize(controller.signal);
        if (!mounted.current || controller.signal.aborted) {
          popup?.close();
          return;
        }
        waitUntil(Date.now() + GITHUB_WAIT_MS);
        if (popup) popup.location.href = url;
        else window.location.assign(url);
      }
    } catch (error) {
      popup?.close();
      if (mounted.current)
        setActionError(
          controller.signal.aborted
            ? "GitHub request timed out. Please try again."
            : (error as Error).message,
        );
    } finally {
      clearTimeout(timeout);
      if (actionController.current === controller)
        actionController.current = null;
      if (mounted.current) {
        setAction("");
        // Even a failed sync may have removed an upstream installation.
        void refresh();
      }
    }
  };

  const connected = Boolean(status?.connected);
  const error = actionError || statusError;
  return (
    <>
      {notice && (
        <div className="notice" role="status">
          {notice}
        </div>
      )}
      {error && (
        <div className="error-banner" role="alert">
          {error}
          <button
            onClick={() => {
              setActionError("");
              void refresh();
            }}
            disabled={!!action || refreshing}
          >
            Check status
          </button>
          {actionError && (
            <button
              onClick={() => setActionError("")}
              aria-label="Dismiss GitHub error"
            >
              <Icon name="x" />
            </button>
          )}
        </div>
      )}
      <section
        className="github-card"
        aria-label="GitHub connection"
        aria-busy={refreshing || !!action}
      >
        <span className="integration-logo github-logo">
          <Icon name="github" size={25} />
        </span>
        <div>
          <h3>
            GitHub{" "}
            <span
              className={`status ${connected && !statusError ? "status-idle" : "status-failed"}`}
            >
              <span className="status-dot" />
              {statusError
                ? "Status unavailable"
                : !status
                  ? "Checking status…"
                  : connected
                    ? "Connected"
                    : status.bridgeToken
                      ? "Credential available"
                      : "Not connected"}
            </span>
          </h3>
          <p>
            {connected
              ? status?.accounts.join(" · ") || "GitHub App connected"
              : status?.bridgeToken
                ? "Repository access uses the deployment’s GitHub credential. Connect an App installation to manage repository access here."
                : "Connect your repositories to create and review pull requests."}
          </p>
          <small role="status">
            {pendingUntil
              ? "Complete installation in GitHub. SBX will confirm the connection automatically."
              : refreshing
                ? "Checking GitHub status…"
                : statusError
                  ? "Last status could not be verified"
                  : "Installation status checked"}
          </small>
        </div>
        <button
          className="button"
          disabled={!!action || (!status && refreshing)}
          onClick={() => void perform(connected ? "sync" : "connect")}
        >
          <Icon name="refresh" size={14} />
          {action === "connect"
            ? "Opening GitHub…"
            : action === "sync"
              ? "Syncing…"
              : connected
                ? "Sync repositories"
                : "Connect GitHub"}
        </button>
        <button
          className="button small"
          disabled={!!action || refreshing}
          onClick={() => void refresh()}
        >
          {refreshing ? "Checking…" : "Refresh status"}
        </button>
      </section>
    </>
  );
}
