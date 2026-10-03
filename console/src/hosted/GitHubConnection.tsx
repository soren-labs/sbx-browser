import { useEffect, useState } from "react";
import { hostedRequest } from "./api";

export function GitHubConnection() {
  const [installations, setInstallations] = useState<any[]>([]);
  const [configured, setConfigured] = useState(false);
  const [mock, setMock] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const refresh = async () => {
    const status = await hostedRequest("/hosted/connections/github");
    setInstallations(status.installations); setConfigured(status.configured); setMock(status.mock);
  };
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const state = params.get("state"), installation = params.get("installation_id");
    const load = async () => {
      if (state && installation) {
        window.history.replaceState(null, "", "/integrations");
        await hostedRequest("/hosted/connections/github/callback", {state, installation_id: Number(installation)});
      }
      await refresh();
    };
    void load().catch(e => setError(e.message));
  }, []);
  const act = async (work: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await work(); await refresh(); } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const connect = () => void act(async () => {
    const authorization = await hostedRequest("/hosted/connections/github/authorize", {});
    if (authorization.mock) await hostedRequest("/hosted/connections/github/mock-approve", {state: authorization.state});
    else window.location.assign(authorization.authorize_url);
  });
  return <section className="settings-section" aria-label="GitHub connection">
    <h2>GitHub</h2>
    <p>Authorize repositories for your Sessions. GitHub access is independent of your compute workspace.</p>
    {error && <p role="alert">{error}</p>}
    {mock && <p>Mock GitHub installations for Alpha development.</p>}
    {!configured && <p>GitHub installation is not configured for this deployment.</p>}
    {installations.map(installation => <div key={installation.installation_id}>
      <p>Connected: {installation.account_login}</p>
      <ul>{installation.repositories.map((repo: string) => <li key={repo}>{repo}</li>)}</ul>
      <button disabled={busy} onClick={() => void act(() => hostedRequest(`/hosted/connections/github/installations/${installation.installation_id}`, {}, "DELETE"))}>Disconnect {installation.account_login}</button>
    </div>)}
    <button disabled={busy || !configured} onClick={connect}>Connect GitHub</button>
  </section>;
}
