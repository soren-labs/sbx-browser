import { useEffect, useRef, useState } from "react";
import { useApi } from "../state/api";
import {
  connections,
  connectionTerminal,
  type AccountConnection,
} from "./domain";
import { demoMode, demoModels, providerNames } from "./demo";
import type { ProviderInfo } from "../api/types";
import { Icon } from "./Icon";
import { GithubConnection } from "./GithubConnection";

function commandArgument(value:string) { return "'" + value.replaceAll("'", "'\\''") + "'"; }
export function Integrations() {
  const api = useApi();
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [notice, setNotice] = useState("");
  const connectionGeneration=useRef(0);
  const focusOrigin = useRef<HTMLElement | null>(null);
  const [accounts, setAccounts] = useState<AccountConnection[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [modelRefresh, setModelRefresh] = useState("");
  const [provider, setProvider] = useState("codex");
  const [label, setLabel] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [target, setTarget] = useState<string>();
  const [pair, setPair] = useState<Awaited<
    ReturnType<typeof connections.begin>
  > | null>(null);
  const labelRef = useRef<HTMLInputElement>(null);
  const refresh = async () => {
    try {
      setAccounts(await connections.list());
    } catch (e) {
      setError(String((e as Error).message));
    }
  };
  useEffect(() => {
    void refresh();
    void api.listProviders().then(setProviders).catch(() => setProviders([]));
  }, [api]);
  useEffect(() => {
    if (connecting) labelRef.current?.focus();
  }, [connecting]);
  useEffect(() => {
    if (accounts.length) connections.rememberDemo(accounts);
  }, [accounts]);
  useEffect(() => {
    if (!pair || demoMode || connectionTerminal(pair.state)) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const p = await connections.poll(pair.id);
        if (!active) return;
        setPair((prev) => ({ ...prev, ...p }));
        if (["verified", "materialized"].includes(p.state)) {
          setNotice(
            p.state === "verified"
              ? "Account connected and verified."
              : "Account saved. Verify it before starting a session.",
          );
          setConnecting(false);
          setPair(null);
          void refresh();
          focusOrigin.current?.focus();
        } else if (!connectionTerminal(p.state)) {
          timer = setTimeout(poll, 2500);
        }
      } catch (e) {
        if (active) {
          setError((e as Error).message);
          timer = setTimeout(poll, 5000);
        }
      }
    };
    timer = setTimeout(poll, 1200);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [pair?.id, pair?.state]);
  const close = () => {
    connectionGeneration.current+=1;
    if (pair)
      void connections.cancel(pair.id).catch((e) => setError(e.message));
    setPair(null);
    setConnecting(false);
    focusOrigin.current?.focus();
  };
  const check = async (a: AccountConnection, action: "verify" | "refresh") => {
    setBusy(a.id + action);
    setError("");
    try {
      const result = await connections.check(a.id, action);
      setNotice(
        action === "verify"
          ? "Connection verified."
          : `Credential refresh: ${String(result?.result ?? "complete").replace("skipped:not_due", "healthy; refresh not due").replace("skipped:static_credential", "this account uses a static credential").replaceAll("_"," ")}.`,
      );
      if (demoMode)
        setAccounts((list) =>
          list.map((row) =>
            row.id === a.id
              ? {
                  ...row,
                  lastVerified: "Just now",
                  health:
                    row.health === "needs_login" ? "needs_login" : "ready",
                }
              : row,
          ),
        );
      else await refresh();
    } catch (e) {
      if(!demoMode)await refresh();
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  const openConnect = (a?: AccountConnection) => {
    connectionGeneration.current+=1;
    focusOrigin.current = document.activeElement as HTMLElement;
    setError("");
    setProvider(a?.provider ?? "codex");
    setLabel(a?.label ?? "");
    setTarget(a?.id);
    setPair(null);
    setConnecting(true);
  };
  const begin = async () => {
    const generation=connectionGeneration.current;
    setBusy("connect");setError("");
    try {
      const flow=await connections.begin(provider,label||`${providerNames[provider]} account`,target);
      if(generation!==connectionGeneration.current){await connections.cancel(flow.id);return;}
      setPair(flow);
    } catch(e){if(generation===connectionGeneration.current)setError((e as Error).message);}
    finally{setBusy("");}
  };
  const completeDemo = () => {
    if (!demoMode) return;
    setAccounts((list) =>
      target
        ? list.map((a) =>
            a.id === target
              ? { ...a, health: "ready", lastVerified: "Just now" }
              : a,
          )
        : [
            ...list,
            {
              id: `demo-${Date.now()}`,
              provider,
              label: label || `${providerNames[provider]} account`,
              health: "ready",
              lastVerified: "Just now",
              models: demoModels[provider],
            },
          ],
    );
    setConnecting(false);
    setPair(null);
  };
  return (
    <div className="page-scroll">
      <div className="integrations-content">
        <div className="page-eyebrow">WORKSPACE</div>
        <div className="page-title-row">
          <div>
            <h1>Connections</h1>
            <p className="page-subtitle">
              Manage provider accounts and your GitHub connection.
            </p>
          </div>
          <button className="button primary" onClick={() => openConnect()}>
            <Icon name="plus" size={15} />
            Connect account
          </button>
        </div>
        {notice && (
          <div className="notice" role="status">
            {notice}
          </div>
        )}
        {error && (
          <div className="error-banner" role="alert">
            {error}
            <button onClick={() => setError("")} aria-label="Dismiss error">
              <Icon name="x" />
            </button>
          </div>
        )}
        <div className="section-header">
          <h2>Source control</h2>
          <span>Repositories and pull requests</span>
        </div>
        <GithubConnection />
        <div className="section-header">
          <h2>AI providers</h2>
          <span>{accounts.length} subscription accounts</span>
        </div>
        <p className="section-description">
          Use your existing subscriptions. SBX picks a ready account when you
          start a session.
        </p>
        <div className="provider-list">
          {(demoMode
            ? Object.entries(providerNames)
            : providers.map((p) => [p.id, p.label] as [string, string])
          ).map(([id, name]) => {
            const rows = accounts.filter((a) => a.provider === id);
            const healthy = rows.some((a) => a.health === "ready");
            return (
              <section key={id} className="provider-card">
                <div className="provider-card-head">
                  <span className={`integration-logo provider-${id}`}>
                    {id === "codex" ? (
                      <Icon name="code" size={23} />
                    ) : id === "devin" ? (
                      "D"
                    ) : id === "antigravity" ? (
                      "A"
                    ) : id === "grok" ? (
                      "𝕏"
                    ) : (
                      <Icon name="terminal" size={22} />
                    )}
                  </span>
                  <div>
                    <h3>{name}</h3>
                    <p>
                      {id === "codex"
                        ? "OpenAI subscription"
                        : id === "devin"
                          ? "Autonomous software engineer"
                          : id === "antigravity"
                            ? "Google AI subscription"
                            : id === "grok"
                              ? "xAI subscription"
                              : "OpenCode Zen subscription"}
                    </p>
                  </div>
                  <span
                    className={`status ${healthy ? "status-idle" : "status-failed"}`}
                  >
                    <span className="status-dot" />
                    {healthy
                      ? "Ready"
                      : rows.length
                        ? "Needs attention"
                        : "Not connected"}
                  </span>
                  <button
                    className="icon-button"
                    aria-label={`Refresh ${name} models`}
                    title={
                      modelRefresh === id
                        ? "Models refreshed"
                        : "Refresh model catalog"
                    }
                    disabled={!!busy}
                    onClick={async () => {
                      setBusy(id + "models");
                      setError("");
                      try {
                        await connections.refreshModels(id);
                        setModelRefresh(id);
                      } catch (e) {
                        setError((e as Error).message);
                      } finally {
                        setBusy("");
                      }
                    }}
                  >
                    <Icon
                      name={modelRefresh === id ? "check" : "refresh"}
                      size={14}
                    />
                  </button>
                  <button
                    className="icon-button"
                    aria-label={`Add ${name} account`}
                    onClick={() => {
                      openConnect();
                      setProvider(id);
                    }}
                  >
                    <Icon name="plus" />
                  </button>
                </div>
                <div className="account-table">
                  {rows.map((a) => (
                    <div className="account-row" key={a.id}>
                      <span className="account-avatar">{a.label[0]}</span>
                      <div className="account-name">
                        <strong>{a.label}</strong>
                        <span>{a.models.join(" · ")}</span>
                      </div>
                      <div className="account-health">
                        <span
                          className={
                            a.health === "ready" ? "positive" : "attention"
                          }
                        >
                          <span className="status-dot" />
                          {a.health === "ready"
                            ? "Healthy"
                            : a.health === "disabled"
                              ? "Disabled"
                              : "Reconnect needed"}
                        </span>
                        <small>{a.lastVerified}</small>
                      </div>
                      <div className="account-actions">
                        <button className="button small" disabled={!!busy} onClick={()=>void check(a,"verify")}>{busy===a.id+"verify"?"Checking…":"Verify"}</button>
                        {a.health==="needs_login" ? <button className="button attention-button" onClick={()=>openConnect(a)}>Reconnect</button> : <button className="icon-button" disabled={!!busy} aria-label={`Refresh ${a.label}`} title="Refresh credentials" onClick={()=>void check(a,"refresh")}><Icon name="refresh" size={14}/></button>}
                      </div>
                      <AccountDetails
                        account={a}
                        onRemoved={() => {
                          setAccounts((rows) =>
                            rows.filter((row) => row.id !== a.id),
                          );
                        }}
                      />
                    </div>
                  ))}
                  {!rows.length && (
                    <button
                      className="text-button"
                      onClick={() => {
                        openConnect();
                        setProvider(id);
                      }}
                    >
                      Connect your first {name} account
                      <Icon name="plus" size={13} />
                    </button>
                  )}
                </div>
              </section>
            );
          })}
        </div>
        <div className="integration-note">
          <Icon name="shield" size={19} />
          <p>
            <strong>Connections stay private.</strong> Each session receives
            only the account it needs. Credential health and refresh are managed
            here.
            {demoMode && (
              <small>
                Prototype mode: account actions are simulated locally.
              </small>
            )}
          </p>
        </div>
      </div>
      {connecting && (
        <div
          className="modal-backdrop"
          onKeyDown={(e) => {
            if (e.key === "Escape") close();
            if (e.key === "Tab") {
              const elements = Array.from(
                e.currentTarget.querySelectorAll<HTMLElement>(
                  "button:not(:disabled), input, select, a[href]",
                ),
              );
              const first = elements[0],
                last = elements.at(-1);
              if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last?.focus();
              } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first?.focus();
              }
            }
          }}
        >
          <section
            className="connect-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="connect-title"
          >
            <button
              className="icon-button modal-close"
              onClick={close}
              aria-label="Close connection dialog"
            >
              <Icon name="x" />
            </button>
            <span className="connect-symbol">
              <Icon name="plug" size={25} />
            </span>
            <h2 id="connect-title">
              {pair
                ? "Authorize your account"
                : target
                  ? "Reconnect your account"
                  : "Connect an AI account"}
            </h2>
            <p>
              {pair
                ? demoMode
                  ? "This demonstrates the secure pairing step. No login or credentials are needed."
                  : "Complete the provider’s sign-in flow. SBX will check the connection automatically."
                : "Bring your subscription. Sign in securely through your provider."}
            </p>
            {!pair ? (
              <>
                <label className="form-label">
                  Provider
                  <select
                    value={provider}
                    onChange={(e) => setProvider(e.target.value)}
                    disabled={!!target}
                  >
                    {(demoMode
                      ? Object.entries(providerNames)
                      : providers.map(
                          (p) => [p.id, p.label] as [string, string],
                        )
                    ).map(([id, name]) => (
                      <option key={id} value={id}>
                        {name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="form-label">
                  Account name
                  <input
                    ref={labelRef}
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    placeholder="e.g. Engineering · work"
                  />
                </label>
                <button
                  className="button primary full"
                  disabled={!!busy}
                  onClick={() => void begin()}
                >
                  {busy ? "Connecting…" : "Continue securely"}
                  <Icon name="external" size={14} />
                </button>
              </>
            ) : (
              <>
                <div className="pair-code">
                  {pair.user_code ??
                    (pair.state === "authenticating"
                      ? "Awaiting sign-in"
                      : pair.state)}
                </div>
                {pair.error && (
                  <p className="negative" role="alert">
                    {pair.error.message ?? pair.error.code}
                  </p>
                )}
                {pair.expires_at && (
                  <p className="fine-print">
                    Expires {new Date(pair.expires_at).toLocaleTimeString()}
                  </p>
                )}
                {pair.browser_url && (
                  <a
                    className="button primary full"
                    target="_blank"
                    rel="noreferrer"
                    href={pair.browser_url}
                  >
                    Open provider sign-in
                    <Icon name="external" size={14} />
                  </a>
                )}
                {pair.pair_command && (
                  <pre className="pair-command">{pair.pair_command}{` --base-url ${commandArgument(String(import.meta.env.VITE_API_BASE || window.location.origin).replace(/\/+$/, ""))}`}</pre>
                )}
                {demoMode ? (
                  <button
                    className="button primary full"
                    onClick={completeDemo}
                  >
                    Complete demo connection
                    <Icon name="check" size={15} />
                  </button>
                ) : connectionTerminal(pair.state) ? (
                  <button
                    className="button primary full"
                    disabled={!!busy}
                    onClick={async () => {
                      setBusy("connect");
                      try {
                        setPair(await connections.retry(pair.id));
                        setError("");
                      } catch (e) {
                        setError((e as Error).message);
                      } finally {
                        setBusy("");
                      }
                    }}
                  >
                    Try connecting again
                  </button>
                ) : (
                  <p className="muted">
                    {pair.kind === "pair"
                      ? "Run this command in a terminal configured for this SBX deployment. The local CLI signs in and pairs securely."
                      : "Waiting for authorization…"}
                  </p>
                )}
              </>
            )}
            <small className="fine-print">
              {demoMode
                ? "Local demo · nothing is sent to a provider"
                : "Credentials are never pasted into the conversation."}
            </small>
          </section>
        </div>
      )}
    </div>
  );
}

function AccountDetails({
  account,
  onRemoved,
}: {
  account: AccountConnection;
  onRemoved: () => void;
}) {
  const [details, setDetails] = useState<{
    state?: string;
    refreshable?: boolean;
    verified_at?: number;
    last_error?: string;
    generation?: number;
  }>();
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  return (
    <details
      className="account-details"
      onToggle={(e) => {
        if (e.currentTarget.open && !demoMode)
          void connections
            .lifecycle(account.id)
            .then((d) => setDetails(d.credential_lifecycle))
            .catch((e) => setError(e.message));
      }}
    >
      <summary>Connection details</summary>
      <p>
        {account.authState ?? account.status ?? account.health}
        {details?.state && ` · ${details.state.replaceAll("_", " ")}`}
      </p>
      {details && (
        <p>
          {details.refreshable
            ? "Refresh supported"
            : "No OAuth refresh channel"}{" "}
          · Generation {details.generation ?? 0}
        </p>
      )}
      {(error || account.lastError || details?.last_error) && (
        <p className="negative" role="alert">
          {error || account.lastError || details?.last_error}
        </p>
      )}
      {confirm ? (
        <div>
          <p>
            Remove this account from SBX? Future sessions will use another ready
            account.
          </p>
          <button className="button small" onClick={() => setConfirm(false)}>
            Keep account
          </button>
          <button
            className="button small"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await connections.remove(account.id);
                onRemoved();
              } catch (e) {
                setError((e as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            Confirm removal
          </button>
        </div>
      ) : (
        <button className="text-button" onClick={() => setConfirm(true)}>
          Remove account
        </button>
      )}
    </details>
  );
}
