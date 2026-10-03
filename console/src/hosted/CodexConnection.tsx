import { useEffect, useState } from "react";
import { hostedRequest, type HostedConnection } from "./api";

const labels: Record<string, string> = {connected: "Connected", refreshing: "Refreshing", reauth_required: "Reauth required", disabled: "Disabled"};
export function CodexConnection() {
  const [connection, setConnection] = useState<HostedConnection | null>(null);
  const [configured, setConfigured] = useState(false);
  const [mock, setMock] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    const status = await hostedRequest("/hosted/connections/codex");
    setConnection(status.connection); setConfigured(status.configured); setMock(status.mock);
  };
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const state = params.get("codex_state"), code = params.get("codex_code");
    const load = async () => {
      if (state && code) {
        window.history.replaceState(null, "", "/integrations");
        await hostedRequest("/hosted/connections/codex/callback", {state, code});
      }
      await refresh();
    };
    void load().catch(e => setError(e.message));
    const timer = setInterval(() => void refresh().catch(() => {}), 5000);
    return () => clearInterval(timer);
  }, []);
  const act = async (work: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await work(); } catch(e) { setError((e as Error).message); }
    finally { setBusy(false); await refresh().catch(() => {}); }
  };
  const connect = () => void act(async () => {
    const authorization = await hostedRequest("/hosted/connections/codex/authorize", {});
    if (authorization.mock) await hostedRequest("/hosted/connections/codex/mock-approve", {state: authorization.state});
    else window.location.assign(authorization.authorization_url);
  });
  return <section className="settings-section" aria-label="Codex connection">
    <h2>Codex / ChatGPT plan</h2>
    <p>{connection ? labels[connection.state] ?? connection.state : "Not connected"}</p>
    <p>Up to three concurrent Sessions share your connection. Credentials refresh on the control plane.</p>
    {mock && <p>Mock Codex authorization and rotation for Alpha development.</p>}
    {!configured && <p>Codex authorization is not configured for this deployment.</p>}
    {error && <p role="alert">{error}</p>}
    <button disabled={busy || !configured} onClick={connect}>{connection?.state === "reauth_required" ? "Reconnect Codex" : "Connect Codex"}</button>
    {connection && <><button disabled={busy} onClick={() => void act(() => hostedRequest("/hosted/connections/codex/refresh", {}))}>Check Codex connection</button>
      <button disabled={busy} onClick={() => void act(() => hostedRequest("/hosted/connections/codex", {}, "DELETE"))}>Disable Codex</button></>}
  </section>;
}
