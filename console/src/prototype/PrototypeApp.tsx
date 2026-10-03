import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  Link,
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import { hostedMode, hostedRequest } from "../hosted/api";
import { AccountSettings } from "../hosted/AccountSettings";
import { useApi } from "../state/api";
import type {
  DeliveryMode,
  ModelInfo,
  NewSessionInput,
  ProviderInfo,
  Session,
  Turn,
} from "../api/types";
import { toErrorKind } from "../api/normalize";
import { getToken, setToken } from "../api/http";
import { Icon, Mark } from "./Icon";
import { demoMode, providerNames } from "./demo";
import { Status, Worklog } from "./Worklog";
import { Changes, Progress, Review, DeliveryDialog } from "./Panels";
import { Integrations } from "./Integrations";
import "./prototype.css";
import { mergeSession, mergeActivity, mergeTurn } from "./session-state";

function age(date: string) {
  const mins = Math.max(
    0,
    Math.floor((Date.now() - new Date(date).getTime()) / 60_000),
  );
  return mins < 1
    ? "Just now"
    : mins < 60
      ? `${mins}m ago`
      : mins < 1440
        ? `${Math.floor(mins / 60)}h ago`
        : `${Math.floor(mins / 1440)}d ago`;
}
const shortcutModifier = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";
const isActive = (s: Session) =>
  ["running", "starting", "queued"].includes(s.phase);

export function PrototypeApp() {
  const api = useApi();
  const location = useLocation();
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [search, setSearch] = useState("");
  const [listError, setListError] = useState("");
  const [listLoading, setListLoading] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [pinned, setPinned] = useState<string[]>(() => {
    try {
      return JSON.parse(
        localStorage.getItem("sbx.prototype.pinned") ?? '["stream-reconnect"]',
      );
    } catch {
      return ["stream-reconnect"];
    }
  });
  const searchRef = useRef<HTMLInputElement>(null);
  const listRequest = useRef<Promise<void> | null>(null);
  useEffect(() => {
    if (sidebarOpen) requestAnimationFrame(() => searchRef.current?.focus());
  }, [sidebarOpen]);
  const refresh = useCallback(() => {
    if (listRequest.current) return listRequest.current;
    const request = api.listSessions().then(rows=>{setSessions(rows);setListError("");}).catch(e=>setListError(e.message)).finally(()=>{setListLoading(false);listRequest.current=null;});
    listRequest.current=request;return request;
  }, [api]);
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 6000);
    window.addEventListener("sbx-connection-change", refresh);
    return () => {
      clearInterval(timer);
      window.removeEventListener("sbx-connection-change", refresh);
    };
  }, [refresh]);
  useEffect(() => {
    setSidebarOpen(false);
    if (!location.pathname.startsWith("/sessions/"))
      document.title = "SBX Browser · Sessions";
    document.getElementById("workspace-main")?.focus();
  }, [location.pathname]);
  useEffect(() => {
    localStorage.setItem("sbx.prototype.pinned", JSON.stringify(pinned));
  }, [pinned]);
  useEffect(() => {
    const keydown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setCollapsed(false);
        setSidebarOpen(true);
        searchRef.current?.focus();
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "o") {
        e.preventDefault();
        navigate("/");
      }
      if (e.key === "Escape") setSidebarOpen(false);
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, []);
  const selected = sessions.find(
    (s) => location.pathname === `/sessions/${s.id}`,
  );
  const section = location.pathname.startsWith("/integrations")
    ? "Connections"
    : location.pathname === "/settings"
      ? "Settings"
      : "Sessions";
  const togglePin = (id: string) =>
    setPinned((ids) =>
      ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id],
    );
  const visibleSessions = sessions.filter((s) =>
    s.title.toLowerCase().includes(search.toLowerCase()),
  );
  const sessionLink = (s: Session) => (
    <div className="sidebar-session-row" key={s.id}>
      <NavLink
        key={s.id}
        to={`/sessions/${s.id}`}
        className={({ isActive }) =>
          `sidebar-session ${isActive ? "selected" : ""}`
        }
      >
        <span
          className={`sidebar-session-state ${isActive(s) ? "live" : s.phase === "failed" ? "attention" : ""}`}
        >
          <Icon
            name={isActive(s) ? "clock" : s.phase === "failed" ? "x" : "pr"}
            size={14}
          />
        </span>
        <span>
          {s.title}
          <small>
            {isActive(s)
              ? "Working"
              : s.phase === "failed"
                ? "Needs attention"
                : s.endReason === "cancelled"
                  ? "Stopped"
                  : s.delivery?.merged
                    ? "Merged"
                    : s.delivery?.status === "delivered"
                    ? "PR ready"
                    : "Finished"}{" "}
            · {age(s.updatedAt)}
          </small>
        </span>
        {isActive(s) && <span className="unread-dot" />}
      </NavLink>
      <button
        className="row-pin icon-button"
        aria-label={`${pinned.includes(s.id) ? "Unpin" : "Pin"} ${s.title} locally`}
        title="Local sidebar preference"
        aria-pressed={pinned.includes(s.id)}
        onClick={() => togglePin(s.id)}
      >
        <Icon name="pin" size={12} />
      </button>
    </div>
  );
  return (
    <div className={`prototype ${collapsed ? "sidebar-collapsed" : ""}`}>
      <a className="skip-link" href="#workspace-main">
        Skip to workspace
      </a>
      {sidebarOpen && (
        <button
          className="sidebar-scrim"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside
        className={`workspace-sidebar ${sidebarOpen ? "mobile-open" : ""}`}
      >
        <div className="sidebar-org-row">
          <Link className="workspace-brand" to="/">
            <Mark small />
            <span>SBX Browser</span>
          </Link>
          <button
            className="icon-button"
            aria-label="Collapse sidebar"
            onClick={() => setCollapsed(true)}
          >
            <Icon name="menu" size={15} />
          </button>
        </div>
        <Link to="/" className="new-session-button">
          <Icon name="plus" size={17} />
          New session<kbd>{shortcutModifier} O</kbd>
        </Link>
        <label className="sidebar-search">
          <Icon name="search" size={14} />
          <input
            ref={searchRef}
            aria-label="Search sessions"
            placeholder="Search sessions…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <kbd>{shortcutModifier} K</kbd>
        </label>
        <NavLink to="/review" className="sessions-nav">
          <Icon name="pr" />
          Review
          <span>
            {sessions.filter((s) => s.delivery?.status === "delivered").length}
          </span>
        </NavLink>
        <div className="sidebar-section-label sessions-section-title">
          <Link to="/sessions">Sessions</Link>
          <Link to="/" aria-label="New session">
            <Icon name="plus" size={14} />
          </Link>
        </div>
        <div className="sidebar-session-scroll">
          {pinned.length > 0 && (
            <>
              <div className="sidebar-section-label">
                <Icon name="pin" size={12} />
                Pinned
                <span>
                  {visibleSessions.filter((s) => pinned.includes(s.id)).length}
                </span>
              </div>
              {visibleSessions
                .filter((s) => pinned.includes(s.id))
                .map(sessionLink)}
            </>
          )}
          <div className="sidebar-section-label">
            Recent
            <span>
              {visibleSessions.filter((s) => !pinned.includes(s.id)).length}
            </span>
          </div>
          {visibleSessions
            .filter((s) => !pinned.includes(s.id))
            .map(sessionLink)}
          {visibleSessions.length === 0 && (
            <p className="sidebar-empty">
              {listLoading
                ? "Loading sessions…"
                : listError
                  ? "Sessions unavailable"
                  : "No sessions found."}
            </p>
          )}
        </div>
        <div className="sidebar-bottom">
          <NavLink to="/integrations">
            <Icon name="plug" />
            Connections
            <span className="integration-alert" />
          </NavLink>
          <NavLink to="/settings">
            <Icon name="settings" />
            Settings
          </NavLink>
          <div className="sidebar-runtime">
            <span className="status-dot" />
            <span>
              {demoMode
                ? "Prototype workspace"
                : listError
                  ? "Connection needs attention"
                  : listLoading
                    ? "Connecting…"
                    : "Connected workspace"}
            </span>
            <span className="small-tag">{demoMode ? "DEMO" : "LIVE"}</span>
          </div>
          <div className="sidebar-user">
            <span className="user-avatar avatar">S</span>
            <span>
              {demoMode ? "Soren" : "SBX"}<small>{demoMode ? "Sorenforge workspace" : "Session workspace"}</small>
            </span>
            <button
              className="icon-button"
              aria-label="Open settings"
              onClick={() => navigate("/settings")}
            >
              <Icon name="settings" size={15} />
            </button>
          </div>
        </div>
      </aside>
      <div className="workspace-body">
        <header
          className={`workspace-topbar ${selected ? "session-route" : ""} ${location.pathname === "/" ? "home-route" : ""}`}
        >
          <button
            className="icon-button desktop-toggle"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            onClick={() => setCollapsed(!collapsed)}
          >
            <Icon name="menu" size={17} />
          </button>
          <button
            className="icon-button mobile-toggle"
            aria-label="Open navigation"
            onClick={() => setSidebarOpen(true)}
          >
            <Icon name="menu" size={18} />
          </button>
          <Link to="/sessions">{section}</Link>
          {selected && (
            <>
              <Icon name="chevron" size={12} />
              <span className="breadcrumb-title">{selected.title}</span>
            </>
          )}
          <div className="topbar-right">
            <span className="demo-indicator">
              <span />
              {demoMode ? "Prototype mode" : "Live workspace"}
            </span>
            <span className="small-tag">
              {demoMode ? "Local data" : listError ? "Needs attention" : listLoading ? "Connecting" : "Connected"}
            </span>
          </div>
        </header>
        <main id="workspace-main" tabIndex={-1}>
          {listError && (
            <div className="error-banner" role="alert">
              Could not load sessions. {listError}{" "}
              <button className="button small" onClick={refresh}>
                Try again
              </button>{" "}
              <Link to="/settings">Connection settings</Link>
            </div>
          )}
          <Routes>
            <Route
              path="/"
              element={
                <Home
                  onCreated={refresh}
                  recentRepos={[
                    ...new Set(
                      sessions
                        .map((s) => s.repo?.name)
                        .filter((r): r is string => Boolean(r)),
                    ),
                  ]}
                />
              }
            />
            <Route
              path="/sessions"
              element={<SessionList sessions={sessions} />}
            />
            <Route
              path="/sessions/:id"
              element={
                <SessionWorkspace
                  key={location.pathname}
                  summary={selected}
                  onChanged={refresh}
                  pinned={selected ? pinned.includes(selected.id) : false}
                  onPin={togglePin}
                />
              }
            />
            <Route
              path="/review"
              element={
                <SessionList
                  sessions={sessions.filter(
                    (s) => s.delivery?.status === "delivered",
                  )}
                  review
                />
              }
            />
            <Route path="/integrations/*" element={<Integrations />} />
            <Route path="/settings" element={<Settings />} />
            <Route
              path="*"
              element={
                <div className="panel-empty">
                  <h1>Page not found</h1>
                  <Link className="button" to="/">
                    Start a session
                  </Link>
                </div>
              }
            />
          </Routes>
        </main>
      </div>
    </div>
  );
}
function SessionRows({ sessions }: { sessions: Session[] }) {
  return (
    <div className="session-rows">
      {sessions.map((s) => (
        <Link key={s.id} to={`/sessions/${s.id}`} className="session-row">
          <span
            className={`session-row-icon ${isActive(s) ? "live" : s.phase === "failed" ? "attention" : ""}`}
          >
            <Icon
              name={isActive(s) ? "clock" : s.phase === "failed" ? "x" : "pr"}
              size={17}
            />
          </span>
          <span className="session-row-name">
            <strong>{s.title}</strong>
            <small>
              <Icon name="github" size={11} />
              {s.repo?.name ?? "No repository"}
              <span>·</span>
              {providerNames[s.provider ?? ""] ?? "Auto"}
            </small>
          </span>
          <Status phase={s.phase} />
          <span className="session-row-time">{age(s.updatedAt)}</span>
          <Icon name="chevron" size={14} />
        </Link>
      ))}
    </div>
  );
}
function Home({
  onCreated,
  recentRepos,
}: {
  onCreated: () => void;
  recentRepos: string[];
}) {
  const api = useApi();
  const navigate = useNavigate();
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [prompt, setPrompt] = useState("");
  const [ownedRepos, setOwnedRepos] = useState<string[]>([]);
  const [repo, setRepo] = useState(demoMode ? "soren-labs/sbx-browser" : "");
  const [provider, setProvider] = useState(demoMode ? "codex" : "auto");
  const [model, setModel] = useState(demoMode ? "gpt-6.1-sol" : "auto");
  const [effort, setEffort] = useState(demoMode ? "high" : "auto");
  const [delivery, setDelivery] = useState<DeliveryMode>(
    demoMode ? "draft_pr" : "none",
  );
  const [branch, setBranch] = useState("main");
  const [account, setAccount] = useState("auto");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const formRef = useRef<HTMLFormElement>(null);
  useEffect(() => {
    if (hostedMode) void hostedRequest("/hosted/repositories").then(data => setOwnedRepos(data.repositories.map((r: any) => r.name))).catch(e => setError(e.message));
    void Promise.all([api.listProviders(), api.listModels()])
      .then(([p, m]) => {
        setProviders(p);
        setModels(m);
      })
      .catch((e) => setError(e.message))
      .finally(() => setCatalogLoading(false));
  }, [api]);
  useEffect(() => {
    const close = (event: Event) => {
      if (event instanceof KeyboardEvent && event.key !== "Escape") return;
      formRef.current?.querySelectorAll("details[open]").forEach((d) => {
        if (
          event instanceof KeyboardEvent ||
          !d.contains(event.target as Node)
        ) {
          d.removeAttribute("open");
          if (event instanceof KeyboardEvent)
            (d.querySelector("summary") as HTMLElement)?.focus();
        }
      });
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", close);
    };
  }, []);
  const modelRows = models.filter((m) => m.provider === provider);
  const selectedModel = modelRows.find((m) => m.model === model);
  const efforts = selectedModel?.reasoningEfforts ?? [];
  const closePickers = () =>
    formRef.current?.querySelectorAll(".composer-picker[open]").forEach((d) => {
      d.removeAttribute("open");
      (d.querySelector("summary") as HTMLElement)?.focus();
    });
  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!prompt.trim() || busy || catalogLoading) return;
    setBusy(true);
    setError("");
    try {
      const input: NewSessionInput = {
        prompt: prompt.trim(),
        provider,
        model,
        effort,
        delivery,
        ...(repo ? { repo, repoRef: branch } : {}),
        ...(account !== "auto" ? { account } : {}),
      };
      const session = await api.createSession(input);
      onCreated();
      navigate(`/sessions/${session.id}`, { state: { session } });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="page-scroll home-page">
      <div className="home-content">
        <div className="home-composer-title">
          <h1>
            <Mark />
            SBX <span>Browser</span>
          </h1>
          <span className="agent-mode">
            <Icon name="code" size={13} />
            Agent session
          </span>
        </div>
        <form
          ref={formRef}
          className="session-composer"
          onSubmit={(e) => void submit(e)}
        >
          <textarea
            aria-label="Session task"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Describe the work you want to hand off…"
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                void submit();
              }
            }}
          />
          <div className="composer-mention">
            <Icon name="github" size={12} />
            {repo || "No repository selected"}
          </div>
          <div className="composer-tools">
            <details className="composer-picker">
              <summary title="Select repository">
                <Icon name="plus" size={17} />
                <span className="sr-only">Repository</span>
              </summary>
              <div className="picker-popover">
                <h2>Repository</h2>
                <label className="form-label">
                  Repository name or URL
                  <input
                    aria-label="Repository"
                    value={repo}
                    onChange={(e) => setRepo(e.target.value)}
                    placeholder="owner/repository"
                  />
                </label>
                {(demoMode
                  ? [
                      "soren-labs/sbx-browser",
                      "soren-labs/docs",
                      "soren-labs/website",
                    ]
                  : hostedMode ? ownedRepos : recentRepos
                ).map((r) => (
                  <button
                    type="button"
                    className="picker-option"
                    key={r}
                    onClick={() => {
                      setRepo(r);
                      closePickers();
                    }}
                  >
                    <Icon name="github" size={14} />
                    {r}
                    {repo === r && <Icon name="check" size={13} />}
                  </button>
                ))}
                <p className="fine-print">
                  Use a repository available to your GitHub connection.
                </p>
                <button
                  type="button"
                  className="button small"
                  onClick={closePickers}
                >
                  Done
                </button>
              </div>
            </details>
            <details className="composer-picker">
              <summary title="Session configuration">
                <Icon name="settings" size={17} />
                <span className="sr-only">Session configuration</span>
              </summary>
              <div className="picker-popover config-picker">
                <h2>Session configuration</h2>
                <label className="form-label">
                  Reasoning effort
                  <select
                    aria-label="Reasoning effort"
                    value={effort}
                    onChange={(e) => setEffort(e.target.value)}
                  >
                    <option value="auto">Automatic</option>
                    {efforts.map((e) => (
                      <option key={e} value={e}>
                        {e}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="form-label">
                  Delivery intent
                  <select
                    aria-label="Delivery intent"
                    value={delivery}
                    onChange={(e) =>
                      setDelivery(e.target.value as DeliveryMode)
                    }
                  >
                    <option value="draft_pr">Draft pull request</option>
                    <option value="pr">Open pull request</option>
                    <option value="branch">Push a branch</option>
                    <option value="none">No delivery</option>
                  </select>
                </label>
                <details className="advanced-config">
                  <summary>Advanced</summary>
                  <label className="form-label">
                    Base branch
                    <input aria-label="Base branch"
                      value={branch}
                      onChange={(e) => setBranch(e.target.value)}
                    />
                  </label>
                  <label className="form-label">
                    Account
                    <select
                      value={account}
                      onChange={(e) => setAccount(e.target.value)}
                    >
                      <option value="auto">
                        Automatic · healthy connection
                      </option>
                      {[
                        ...new Set(
                          modelRows.map((m) => m.account).filter(Boolean),
                        ),
                      ].map((a) => (
                        <option key={a} value={a}>
                          {a}
                        </option>
                      ))}
                    </select>
                  </label>
                  <Link to="/integrations">Manage account connections</Link>
                </details>
              </div>
            </details>
            <span className="composer-configuration-label">
              {effort} ·{" "}
              {delivery === "draft_pr"
                ? "Draft PR"
                : delivery === "pr"
                  ? "PR"
                  : delivery === "branch"
                    ? "Branch"
                    : "No delivery"}
            </span>
            <details className="composer-picker model-picker">
              <summary>
                {selectedModel?.displayName ??
                  (model === "auto"
                    ? "Automatic model"
                    : model.replace("gpt-6.1-sol", "GPT-6.1 Sol"))}
                <Icon name="down" size={12} />
              </summary>
              <div className="picker-popover model-popover">
                <h2>Agent & model</h2>
                <label className="form-label">
                  Provider
                  <select
                    aria-label="Provider"
                    value={provider}
                    onChange={(e) => {
                      setProvider(e.target.value);
                      setModel("auto");
                      setAccount("auto");
                      setEffort("auto");
                    }}
                  >
                    <option value="auto">Automatic</option>
                    {providers.map((p) => (
                      <option
                        key={p.id}
                        value={p.id}
                        disabled={!p.runtimeEnabled}
                      >
                        {p.label} · {p.readiness?.replaceAll("_", " ")}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  className="picker-option"
                  onClick={() => {
                    setModel("auto");
                    closePickers();
                  }}
                >
                  Automatic model
                  {model === "auto" && <Icon name="check" size={13} />}
                </button>
                {[...new Map(modelRows.map((m) => [m.model, m])).values()].map(
                  (m) => (
                    <button
                      key={m.model}
                      type="button"
                      className={`picker-option ${m.model === model ? "selected" : ""}`}
                      disabled={m.availability === "unavailable"}
                      onClick={() => {
                        setModel(m.model);
                        setEffort(m.defaultEffort ?? "auto");
                        closePickers();
                      }}
                    >
                      {m.displayName ??
                        m.model.replace("gpt-6.1-sol", "GPT-6.1 Sol")}
                      {m.model === model && <Icon name="check" size={13} />}
                    </button>
                  ),
                )}
                <p className="fine-print">
                  Models come from your connected providers.
                </p>
              </div>
            </details>
            <button
              className="start-session-button"
              aria-label={busy ? "Starting session" : "Start session"}
              title="Start session · Ctrl/⌘ Enter"
              disabled={!prompt.trim() || busy || catalogLoading}
              type="submit"
            >
              <Icon name={busy ? "clock" : "arrow"} size={17} />
            </button>
          </div>
        </form>
        {error && (
          <div role="alert" className="error-banner">
            {error}
            <Link to="/integrations">Check connections</Link>
          </div>
        )}
        {demoMode && (
          <div className="home-demo-links">
            <span className="small-tag">DEMO</span>
            <Link to="/sessions/stream-reconnect">Follow active work</Link>
            <span>·</span>
            <Link to="/sessions/event-replay">Review a draft PR</Link>
          </div>
        )}
      </div>
    </div>
  );
}
function SessionList({
  sessions,
  review = false,
}: {
  sessions: Session[];
  review?: boolean;
}) {
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const shown = sessions.filter(
    (s) =>
      s.title.toLowerCase().includes(query.toLowerCase()) &&
      (filter === "all" ||
        (filter === "active" && isActive(s)) ||
        (filter === "finished" && s.phase === "idle") ||
        (filter === "attention" && s.phase === "failed")),
  );
  return (
    <div className="page-scroll">
      <div className="list-content">
        <div className="page-eyebrow">YOUR WORKSPACE</div>
        <div className="page-title-row">
          <div>
            <h1>{review ? "Review" : "Sessions"}</h1>
            <p className="page-subtitle">
              {review
                ? "Delivered work ready for a closer look."
                : "Work you’ve handed off, in one place."}
            </p>
          </div>
          <Link className="button primary" to="/">
            <Icon name="plus" size={15} />
            New session
          </Link>
        </div>
        <div className="list-toolbar">
          <div className="filter-tabs">
            {[
              ["all", "All sessions"],
              ["active", "Active"],
              ["finished", "Finished"],
              ["attention", "Needs attention"],
            ].map(([id, label]) => (
              <button
                className={filter === id ? "selected" : ""}
                key={id}
                onClick={() => setFilter(id)}
                aria-pressed={filter === id}
              >
                {label}
                {id === "active" && (
                  <span>{sessions.filter(isActive).length}</span>
                )}
              </button>
            ))}
          </div>
          <label className="list-search">
            <Icon name="search" size={14} />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search sessions"
              aria-label="Filter sessions"
            />
          </label>
        </div>
        <SessionRows sessions={shown} />
        {!shown.length && (
          <div className="panel-empty">No sessions match this view.</div>
        )}
      </div>
    </div>
  );
}
function SessionWorkspace({
  summary,
  onChanged,
  pinned,
  onPin,
}: {
  summary?: Session;
  onChanged: () => void;
  pinned: boolean;
  onPin: (id: string) => void;
}) {
  const api = useApi();
  const { id = "" } = useParams();
  const location = useLocation();
  const [session, setSession] = useState<Session | null>(
    (location.state as { session?: Session } | null)?.session ?? null,
  );
  useEffect(()=>{if(summary)setSession(current=>mergeSession(current,summary));},[summary]);
  const [historyFloor, setHistoryFloor] = useState<number | null>(null);
  const [hasMoreHistory, setHasMoreHistory] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [deliveryOpen, setDeliveryOpen] = useState(false);
  const [context, setContext] = useState("changes");
  const [contextHidden, setContextHidden] = useState(false);
  const [mobilePane, setMobilePane] = useState("conversation");
  const showContext = (tab: string) => {
    setContext(tab);
    setContextHidden(false);
    setMobilePane("context");
  };

  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stream, setStream] = useState("connecting");
  const [connectionEpoch,setConnectionEpoch]=useState(0);
  useEffect(()=>{const reconnect=()=>{setSession(null);setHistoryFloor(null);setConnectionEpoch(n=>n+1);setError("");};window.addEventListener("sbx-connection-change",reconnect);return()=>window.removeEventListener("sbx-connection-change",reconnect);},[]);
  const logRef = useRef<HTMLDivElement>(null);
  const followLog = useRef(true);
  useLayoutEffect(() => {
    const el = logRef.current;
    if (el && followLog.current) el.scrollTop = el.scrollHeight;
  }, [session]);
  useEffect(() => {
    let alive = true;
    let networkReady = false;
    void api.readSessionCache?.(id).then(cached=>{
      if (alive && !networkReady && cached) setSession(prev=>mergeSession(prev,cached));
    }).catch(()=>undefined);
    void api
      .getSession(id)
      .then((s) => {
        if (alive) {
          networkReady = true;
          setError("");
          setHistoryFloor(s.turns.length ? Math.max(0, Math.min(...s.turns.map(t=>t.index))-1) : 0);
          setSession((prev) => mergeSession(prev, s));
          if (s.delivery?.status === "delivered") setContext("review");
          else if (!s.hasChanges) setContext("progress");
        }
      })
      .catch((e) => {
        if (alive) {setError(e.message);setStream("unavailable");}
      });
    return () => {
      alive = false;
    };
  }, [api, id, connectionEpoch]);
  useEffect(() => {
    if (historyFloor === null) return;
    return api.subscribe(id, {
        historyAfterTurn: historyFloor,
        onSession: (next) => setSession((prev) => mergeSession(prev, next)),
        onPhase: (phase) => setSession((s) => (s ? { ...s, phase } : s)),
        onMeta: (meta) =>
          setSession((s) =>
            s
              ? {
                  ...s,
                  provider: meta.provider ?? s.provider,
                  model: meta.model ?? s.model,
                }
              : s,
          ),
        onTurn: (turn) =>
          setSession((s) => {
            if (!s) return s;
            const exists = s.turns.some((t) => t.id === turn.id);
            return {
              ...s,
              turns: exists
                ? s.turns.map((t) =>
                    t.id === turn.id ? mergeTurn(t, turn) : t,
                  )
                : [...s.turns, turn],
            };
          }),
        onActivities: (items) => setSession(s=>{
          if (!s) return s;
          const turns = new Map(s.turns.map(t=>[t.id,t]));
          const grouped = new Map<string, typeof items>();
          for (const item of items) {
            if (item.kind === "status" && !item.turnId) continue;
            const turnId=item.turnId ?? `turn-${Math.max(1,item.n ?? s.turnCount ?? 1)}`;
            grouped.set(turnId,[...(grouped.get(turnId) ?? []),item]);
          }
          for (const [turnId,rows] of grouped) {
            const first=rows[0], prior=turns.get(turnId);
            const placeholder: Turn = {id:turnId,index:Math.max(1,first.n ?? s.turnCount ?? 1),prompt:(first.n ?? 1)===1 ? s.prompt : "",status:"running",createdAt:first.ts,startedAt:first.ts,finishedAt:null,result:null,error:null,activity:[]};
            const turn=prior ?? placeholder;
            turns.set(turnId,{...turn,activity:mergeActivity(turn.activity,rows)});
          }
          return {...s,turns:[...turns.values()].sort((a,b)=>a.index-b.index)};
        }),
        // Fixture transports may emit single rows rather than batches.
        onActivity: (item) => setSession(s=>{
          if (!s || !item.turnId) return s;
          return {...s,turns:s.turns.map(t=>t.id===item.turnId ? {...t,activity:mergeActivity(t.activity,[item])} : t)};
        }),
        onOpen: () => setStream("connected"),
        onError: (e) => {
          setError(e.message);
          setStream("unavailable");
        },
        onDisconnect: () => setStream("reconnecting"),
        onReconnect: () => {
          setStream("connected");
          setError("");
          void api
            .getSession(id)
            .then((s) => setSession((prev) => mergeSession(prev, s)))
            .catch((e) => setError(e.message));
        },
      });
  }, [api, id, connectionEpoch, historyFloor]);
  useEffect(()=>{
    if (!session || session.id!==id) return;
    const timer=setTimeout(()=>{void api.writeSessionCache?.(session).catch(()=>undefined);},500);
    return ()=>clearTimeout(timer);
  },[api,id,session]);
  const loadHistory=async()=>{
    if (!session || !api.getHistory || historyLoading) return;
    const before=Math.min(...session.turns.map(t=>t.index));
    const el=logRef.current, height=el?.scrollHeight ?? 0, top=el?.scrollTop ?? 0;
    followLog.current = false;
    setHistoryLoading(true);setHistoryError("");
    try {
      const page=await api.getHistory(id,before);
      setHasMoreHistory(page.hasMore);
      setSession(current=>current ? {...current,turns:[...page.turns,...current.turns.filter(t=>!page.turns.some(p=>p.id===t.id))]} : current);
      requestAnimationFrame(()=>{if(el)el.scrollTop=top+el.scrollHeight-height;});
    } catch(e) {setHistoryError((e as Error).message);}
    finally {setHistoryLoading(false);}
  };
  useEffect(() => {
    document.title = session ? `${session.title} · SBX` : "Session · SBX";
  }, [session?.title]);
  const act = async (action: "stop" | "retry") => {
    setBusy(true);
    setError("");
    try {
      const next = action === "stop" ? await api.stopSession(id) : await api.retrySession(id);
      setSession((prev) => mergeSession(prev, next));
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const send = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!message.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const next = await api.sendFollowUp(id, message.trim());
      setSession((prev) => mergeSession(prev, next.session));
      setMessage("");
      onChanged();
      setTimeout(
        () =>
          logRef.current?.scrollTo({
            top: logRef.current.scrollHeight,
            behavior: "smooth",
          }),
        100,
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (!session)
    return (
      <div className="panel-empty" role={error ? "alert" : "status"}>
        {error || <SessionLoading />} 
        {error && <button className="button" onClick={()=>{setError("");setStream("connecting");setConnectionEpoch(n=>n+1);}}>Retry opening session</button>}
        {error && (
          <Link to="/sessions" className="button">
            Back to sessions
          </Link>
        )}
      </div>
    );
  const active = isActive(session);
  return (
    <div
      className={`session-workspace ${contextHidden ? "context-hidden" : ""} ${mobilePane === "context" ? "mobile-show-context" : ""}`}
    >
      <header className="session-header">
        <div className="session-title">
          <div>
            <h1>{session.title}</h1>
          </div>
        </div>
        <div className="session-header-actions">
          <Status phase={session.phase} />
          <button
            className="icon-button"
            title="Show progress and session details"
            aria-label="Show session details"
            onClick={() => showContext("progress")}
          >
            <Icon name="settings" size={15} />
          </button>
          <button
            className={`icon-button ${pinned ? "is-pinned" : ""}`}
            onClick={() => onPin(id)}
            title={pinned ? "Unpin session" : "Pin session"}
            aria-label={pinned ? "Unpin session" : "Pin session"}
            aria-pressed={pinned}
          >
            <Icon name="pin" size={15} />
          </button>
          {active ? (
            <button
              className="button small"
              disabled={busy}
              onClick={() => void act("stop")}
            >
              <Icon name="stop" size={12} />
              Stop
            </button>
          ) : session.phase === "failed" ||
            session.endReason === "cancelled" ? (
            <button
              className="button small"
              disabled={busy}
              onClick={() => void act("retry")}
            >
              <Icon name="refresh" size={13} />
              Retry
            </button>
          ) : (
            <button
              className="button small"
              onClick={() => showContext("review")}
            >
              <Icon name="pr" size={13} />
              {session.delivery?.status === "delivered"
                ? "Review PR"
                : "Deliver"}
            </button>
          )}
        </div>
      </header>
      {deliveryOpen && (
        <DeliveryDialog
          session={session}
          busy={busy}
          error={error}
          onClose={() => {
            setDeliveryOpen(false);
            setError("");
          }}
          onSubmit={async (input) => {
            setBusy(true);
            setError("");
            try {
              const result = await api.deliverSession(id, input);
              setSession((prev) => mergeSession(prev, result.session));
              setDeliveryOpen(false);
              showContext("review");
              onChanged();
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        />
      )}
      {error && !deliveryOpen && (
        <div className="error-banner" role="alert">
          {error}
          {stream === "unavailable" && <button className="button small" onClick={()=>{setStream("connecting");setError("");setConnectionEpoch(n=>n+1);}}>Retry connection</button>}
          <button
            className="icon-button"
            aria-label="Dismiss error"
            onClick={() => setError("")}
          >
            <Icon name="x" />
          </button>
        </div>
      )}
      {session.phase === "failed" && (
        <div className="error-banner" role="alert">
          {session.error?.message ?? "This session needs attention."}
          {toErrorKind(session.error?.code ?? "") === "provider_login" ? (
            <Link to="/integrations">
              Reconnect account <Icon name="external" size={12} />
            </Link>
          ) : (
            <Link to="/">Start a new session</Link>
          )}
        </div>
      )}
      {(stream === "reconnecting" || stream === "connecting") && (
        <div className="notice">
          <Icon name="refresh" size={14} />
          {stream === "connecting"
            ? "Connecting to live activity…"
            : "Reconnecting to the session. Your work is preserved."}
        </div>
      )}
      <nav className="mobile-workspace-tabs" aria-label="Workspace panels">
        <button
          className={mobilePane === "conversation" ? "selected" : ""}
          onClick={() => setMobilePane("conversation")}
        >
          <Icon name="sessions" size={14} />
          Worklog
        </button>
        {[
          ["progress", "clock", "Progress"],
          ["changes", "code", "Changes"],
          ["review", "pr", "Review"],
        ].map(([tab, icon, label]) => (
          <button
            key={tab}
            className={
              mobilePane === "context" && context === tab ? "selected" : ""
            }
            onClick={() => showContext(tab)}
          >
            <Icon name={icon} size={14} />
            {label}
          </button>
        ))}
      </nav>
      <div className="session-split">
        <section className="conversation-pane" aria-label="Session worklog">
          <div className="worklog-scroll" ref={logRef} onScroll={event => {
            const el = event.currentTarget;
            followLog.current = el.scrollHeight - el.clientHeight - el.scrollTop < 64;
          }}>
            {api.getHistory && hasMoreHistory && session.turns.length>0 && Math.min(...session.turns.map(t=>t.index))>1 && <div className="history-controls">
              <button className="button small" disabled={historyLoading} onClick={loadHistory}>{historyLoading ? "Loading earlier messages…" : "Load earlier messages"}</button>
              {historyError && <p role="alert">{historyError}</p>}
            </div>}
            <Worklog session={session} onContext={showContext} />
          </div>
          <div className="followup-container">
            <form className="followup-composer" onSubmit={(e) => void send(e)}>
              <textarea
                aria-label="Follow-up message"
                placeholder={
                  session.phase === "ended"
                    ? "This session has stopped"
                    : "Send a follow-up or steer the work…"
                }
                value={message}
                disabled={
                  session.phase === "ended" || session.phase === "failed"
                }
                onChange={(e) => setMessage(e.target.value)}
                onKeyDown={(e) => {
                  if (
                    e.key === "Enter" &&
                    !e.shiftKey &&
                    !e.nativeEvent.isComposing
                  ) {
                    e.preventDefault();
                    void send();
                  }
                }}
              />
              <div className="followup-tools">
                <span>
                  <Icon name="code" size={13} />
                  {session.model ??
                    providerNames[session.provider ?? ""] ??
                    "Auto"}
                  <span className="meta-dot">·</span>
                  {session.effort ?? "Auto"} effort
                </span>
                <button
                  className="send-followup"
                  type={active && !message.trim() ? "button" : "submit"}
                  onClick={
                    active && !message.trim()
                      ? () => void act("stop")
                      : undefined
                  }
                  aria-label={
                    active && !message.trim()
                      ? "Stop session"
                      : "Send follow-up"
                  }
                  disabled={
                    busy ||
                    (!active && !message.trim()) ||
                    session.phase === "failed" ||
                    session.phase === "ended"
                  }
                >
                  <Icon
                    name={active && !message.trim() ? "stop" : "arrow"}
                    size={15}
                  />
                </button>
              </div>
            </form>
            <div className="followup-caption">
              <span>
                {active
                  ? "Follow-ups are added to this session’s work."
                  : "Continue with a follow-up in the same workspace."}
              </span>
              <span>Enter ↵</span>
            </div>
          </div>
        </section>
        <section className="context-pane" aria-label="Session workspace tools">
          <div className="pane-toolbar context-toolbar">
            <div className="pane-tabs">
              {[
                ["progress", "clock", "Progress"],
                ["changes", "code", "Changes"],
                ["review", "pr", "Review"],
              ].map(([tab, icon, label]) => (
                <button
                  key={tab}
                  onClick={() => setContext(tab)}
                  className={context === tab ? "selected" : ""}
                  aria-pressed={context === tab}
                >
                  <Icon name={icon} size={14} />
                  {label}
                  {tab === "changes" && session.hasChanges && (
                    <span className="tab-count">{demoMode ? "4" : "•"}</span>
                  )}
                  {tab === "review" &&
                    session.delivery?.status === "delivered" && (
                      <span className="unread-dot" />
                    )}
                </button>
              ))}
            </div>
            <button
              className="icon-button"
              aria-label="Hide context panel"
              onClick={() => setContextHidden(true)}
            >
              <Icon name="x" size={15} />
            </button>
          </div>
          <div className="context-scroll">
            {context === "progress" ? (
              <Progress session={session} />
            ) : context === "changes" ? (
              <Changes
                session={session}
                busy={busy}
                onDeliver={() => {
                  setError("");
                  setDeliveryOpen(true);
                }}
                onReview={() => showContext("review")}
              />
            ) : (
              <Review
                key={session.id}
                session={session}
                busy={busy}
                onDeliver={() => {
                  setError("");
                  setDeliveryOpen(true);
                }}
                onChanged={()=>{void api.getSession(id).then(next=>setSession(prev=>mergeSession(prev,next))).catch(e=>setError(e.message));onChanged();}}
                onChanges={() => showContext("changes")}
              />
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
function Settings() {
  const [theme, setTheme] = useState(
    document.documentElement.dataset.theme ?? "light",
  );
  const [saved, setSaved] = useState(false);
  const [token, setApiToken] = useState("");
  return (
    <div className="page-scroll">
      <div className="settings-content">
        <div className="page-eyebrow">WORKSPACE</div>
        <h1>Settings</h1>
        <p className="page-subtitle">Make this workspace yours.</p>
        <section className="settings-section">
          <h2>Appearance</h2>
          <p>Choose how SBX looks on your screen.</p>
          <div className="theme-options">
            {["light", "dark"].map((t) => (
              <button
                className={theme === t ? "selected" : ""}
                key={t}
                onClick={() => {
                  setTheme(t);
                  document.documentElement.dataset.theme = t;
                  localStorage.setItem("sbx.console.theme", t);
                }}
                aria-pressed={theme === t}
              >
                <Icon name={t === "light" ? "sun" : "moon"} />
                {t[0].toUpperCase() + t.slice(1)}
              </button>
            ))}
          </div>
        </section>
        {hostedMode && <AccountSettings />}
        {!hostedMode && <section className="settings-section">
          <h2>Workspace connection</h2>
          <p>
            {demoMode
              ? "You’re exploring the local prototype with demo sessions and accounts."
              : "This workspace uses your configured control plane."}
          </p>
          <span className="small-tag">
            {demoMode ? "PROTOTYPE MODE" : "LIVE MODE"}
          </span>
          <p className="fine-print">
            Session activity, changes, and delivery share one product interface
            with provider accounts and GitHub integrations.
          </p>
          {!demoMode && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                setToken(token.trim());
                setApiToken("");
                setSaved(true);
                window.dispatchEvent(new Event("sbx-connection-change"));
              }}
            >
              <label className="form-label">
                API connection key
                <input
                  type="password"
                  autoComplete="off"
                  value={token}
                  onChange={(e) => setApiToken(e.target.value)}
                  placeholder={
                    getToken()
                      ? "Connection key is configured"
                      : "Enter connection key"
                  }
                />
              </label>
              <button className="button primary">Save connection</button>
              {saved && <span role="status">Connection saved.</span>}
            </form>
          )}
        </section>}
        <section className="settings-section">
          <h2>Keyboard shortcuts</h2>
          <div className="shortcut-row">
            <span>Find a session</span>
            <kbd>⌘ / Ctrl K</kbd>
          </div>
          <div className="shortcut-row">
            <span>Start a new session</span>
            <kbd>⌘ / Ctrl Enter</kbd>
          </div>
          <div className="shortcut-row">
            <span>Send a follow-up</span>
            <kbd>Enter</kbd>
          </div>
          <div className="shortcut-row">
            <span>New line in a follow-up</span>
            <kbd>Shift Enter</kbd>
          </div>
        </section>
      </div>
    </div>
  );
}

function SessionLoading() {
  return <div className="session-loading" role="status" aria-label="Opening session">
    <span className="loading-orbit" aria-hidden="true"><i/><i/><i/></span>
    <span>Opening session…</span>
  </div>;
}
