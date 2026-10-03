import { useEffect, useRef, useState } from "react";
import type {
  Session,
  SessionChangesDiff,
  SessionFileDiff,
} from "../api/types";
import { hostedMode } from "../hosted/api";
import { HostedReviewActions } from "../hosted/ReviewActions";
import { useApi } from "../state/api";
import { demoMode, providerNames } from "./demo";
import { reviews, type ReviewRecord } from "./domain";
import { Icon } from "./Icon";
import { PRCard, Status, Worklog } from "./Worklog";

export function Changes({
  session,
  onDeliver,
  busy,
  onReview,
}: {
  session: Session;
  onDeliver: () => void;
  busy: boolean;
  onReview: () => void;
}) {
  const api = useApi();
  const [summary, setSummary] = useState<SessionChangesDiff | null>(null);
  const [summaryLoading,setSummaryLoading]=useState(true);
  const [path, setPath] = useState("");
  const [diff, setDiff] = useState<SessionFileDiff | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let alive = true;
    setSummary(null);
    setSummaryLoading(true);
    setError("");
    setPath("");
    void api
      .listChangesDiff(session.id)
      .then((s) => {
        if (alive) {
          setSummaryLoading(false);
          setSummary(s);
          setPath(s.files[0]?.path ?? "");
        }
      })
      .catch((e) => {
        if (alive) setSummaryLoading(false);
        if (alive)
          setError(
            e.subcode === "revision_not_found"
              ? "Changes will appear here once a snapshot is ready."
              : e.message,
          );
      });
    return () => {
      alive = false;
    };
  }, [
    api,
    session.id,
    session.hasChanges,
    session.changes?.headSha,
    session.turnCount,
    refresh,
  ]);
  useEffect(() => {
    if (!path) return;
    let alive = true;
    setDiff(null);
    setLoading(true);
    void api
      .getFileDiff(session.id, path, summary?.n)
      .then((d) => {
        if (alive) {
          setDiff(d);
          setError("");
        }
      })
      .catch(() => {
        if (alive)
          setError("Could not load this file. Retry to load the diff.");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [api, session.id, path, summary?.n, refresh]);
  return (
    <div className="changes-panel">
      <div className="panel-heading">
        <div>
          <h2>Changes</h2>
          <p>
            {summary
              ? `${summary.filesChanged} files changed`
              : "Workspace changes"}{" "}
            <span className="positive">
              {summary && `+${summary.additions}`}
            </span>{" "}
            <span className="negative">
              {summary && `−${summary.deletions}`}
            </span>
          </p>
        </div>
        <span className="small-tag">
          {demoMode ? "Demo diff" : "Latest snapshot"}
        </span>
      </div>
      <div className="branch-line">
        <Icon name="branch" size={14} />
        <span>{session.repo?.ref ?? "Default base"}</span>
        <Icon name="chevron" size={12} />
        <code>{session.delivery?.branch ?? "Session branch"}</code>
      </div>
      {summary && summary.files.length > 0 && (
        <div className="changes-content">
          <div className="file-list" aria-label="Changed files">
            {summary.files.map((file) => (
              <button
                key={file.path}
                className={
                  path === file.path ? "file-row selected" : "file-row"
                }
                onClick={() => setPath(file.path)}
                aria-pressed={path === file.path}
              >
                <Icon name="file" size={14} />
                <span title={file.path}>
                  {file.path.split("/").at(-1)}
                  <small>{file.path.split("/").slice(0, -1).join("/")}/</small>
                </span>
                <span className="file-stats">
                  <b className="positive">+{file.additions}</b>
                  <b className="negative">−{file.deletions}</b>
                  <i>{file.status === "added" ? "A" : "M"}</i>
                </span>
              </button>
            ))}
          </div>
          <div className="diff-view">
            <div className="diff-header">
              <Icon name="code" size={14} />
              <strong>{path.split("/").at(-1)}</strong>
              <span>Unified diff</span>
            </div>
            {loading ? (
              <div className="panel-empty">Loading file diff…</div>
            ) : (
              diff && (
                <div className="diff-lines" aria-label={`Diff for ${path}`}>
                  {diff.diff.split("\n").map((line, i) => (
                    <div
                      key={i}
                      className={`diff-line ${line.startsWith("+") ? "addition" : line.startsWith("-") ? "deletion" : line.startsWith("@@") ? "hunk" : ""}`}
                    >
                      <span className="line-number">
                        {line.startsWith("@@") ? "" : i}
                      </span>
                      <code>{line || " "}</code>
                    </div>
                  ))}
                </div>
              )
            )}
          </div>
        </div>
      )}
      {summaryLoading ? <div className="panel-empty" role="status">Loading changes…</div> : (!summary?.files.length && !error) && (
        <div className="panel-empty">
          <Icon name="file" size={24} />
          <p>No file changes yet.</p>
          <small>Available snapshots will appear here.</small>
        </div>
      )}
      {error && (
        <div className="panel-empty" role="alert">
          {error}
          <button className="button" onClick={() => setRefresh((x) => x + 1)}>
            Retry changes
          </button>
        </div>
      )}
      <div className="changes-footer">
        <Icon name="shield" size={15} />
        <span>
          {session.phase === "running"
            ? "Work is in progress. Changes may update."
            : "Changes are ready to review."}
        </span>
        {!!summary?.files.length && (
          <button
            className="button primary"
            disabled={busy}
            onClick={
              session.delivery?.status === "delivered" ? onReview : onDeliver
            }
          >
            {session.delivery?.status === "delivered"
              ? "Review pull request"
              : busy
                ? "Creating…"
                : "Create draft PR"}
            <Icon name="pr" size={14} />
          </button>
        )}
      </div>
    </div>
  );
}

export function Progress({ session }: { session: Session }) {
  return (
    <div className="progress-panel">
      <div className="panel-heading">
        <div>
          <h2>Session details</h2>
          <p>Execution and work evidence</p>
        </div>
        <Status phase={session.phase} />
      </div>
      <dl className="session-facts">
        <dt>Repository</dt>
        <dd>{session.repo?.name ?? "No repository"}</dd>
        <dt>Base branch</dt>
        <dd>{session.repo?.ref ?? "Default"}</dd>
        <dt>Provider</dt>
        <dd>{providerNames[session.provider ?? ""] ?? "Automatic"}</dd>
        <dt>Model</dt>
        <dd>{session.model ?? "Automatic"}</dd>
        <dt>Reasoning</dt>
        <dd>{session.effort ?? "Automatic"}</dd>
        <dt>Turns</dt>
        <dd>{session.turnCount}</dd>
        <dt>Delivery</dt>
        <dd>{session.delivery?.mode?.replaceAll("_", " ") ?? "None"}</dd>
      </dl>
      <h3 className="progress-heading">Work performed</h3>
      <Worklog session={session} onContext={() => {}} progressOnly />
    </div>
  );
}

export function Review({
  session,
  onDeliver,
  onChanges,
  onChanged,
  busy,
}: {
  session: Session;
  onDeliver: () => void;
  onChanges: () => void;
  onChanged?: () => void;
  busy: boolean;
}) {
  const delivered = session.delivery?.status === "delivered";
  return (
    <div className="review-panel">
      <div className="panel-heading">
        <div>
          <h2>{session.delivery?.merged ? "Pull request merged" : delivered ? "Ready for your review" : "Deliver the work"}</h2>
          <p>
            {delivered
              ? "From a session to a pull request."
              : "Review the changes, then open a draft PR."}
          </p>
        </div>
        <Icon name="pr" size={23} />
      </div>
      {delivered ? (
        <>
          <PRCard session={session} onReview={onChanges} />
          {!session.delivery?.merged && <button className="button" disabled={busy} onClick={onDeliver}>
            Update pull request
          </button>}
          <div className="review-section">
            <h3>What changed</h3>
            <p>
              {!demoMode
                ? (session.turns.at(-1)?.result ??
                  "Review the changes produced by this session.")
                : session.id === "stream-reconnect" ||
                    session.id === "event-replay"
                  ? "Deduplicates replayed session events, keeps conversation ordering stable, and restores the stream after a disconnect."
                  : "Implements the requested changes with regression coverage and verification."}
            </p>
            {demoMode && (
              <div className="review-stats">
                <span>
                  <Icon name="file" />4 files
                </span>
                <span className="positive">+34</span>
                <span className="negative">−5</span>
              </div>
            )}
          </div>
          <div className="review-section">
            <h3>Verification</h3>
            {demoMode ? (
              [
                "12 regression tests passed",
                "TypeScript and production build passed",
                "No duplicate activity after replay",
              ].map((s) => (
                <p className="review-check" key={s}>
                  <Icon name="check" size={14} />
                  {s}
                </p>
              ))
            ) : (
              <p>
                Inspect the session’s command evidence and repository checks
                before approving.
              </p>
            )}
          </div>
          {hostedMode ? <HostedReviewActions session={session} onChanged={onChanged} /> : <ReviewActions session={session} onChanged={onChanged} />}
        </>
      ) : (
        <div className="delivery-empty">
          <span className="delivery-symbol">
            <Icon name="pr" size={35} />
          </span>
          <h3>Review changes before delivery.</h3>
          <p>
            Your session’s changes can be delivered as a draft pull request,
            ready for human review.
          </p>
          <button className="button" onClick={onChanges}>
            Review changes
            <Icon name="code" size={14} />
          </button>
          <button
            className="button primary"
            disabled={busy || !session.hasChanges}
            onClick={onDeliver}
          >
            {busy ? "Creating draft PR…" : "Create draft pull request"}
            <Icon name="pr" size={14} />
          </button>
          <span className="fine-print">
            {demoMode
              ? "Local demo · no GitHub changes"
              : "Uses the repository connected to this session"}
          </span>
        </div>
      )}
    </div>
  );
}

function ReviewActions({ session, onChanged }: { session: Session; onChanged?:()=>void }) {
  const api = useApi();
  const [records, setRecords] = useState<ReviewRecord[]>([]);
  const [revision, setRevision] = useState<number>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmMerge, setConfirmMerge] = useState(false);
  const [merged, setMerged] = useState(false);
  useEffect(() => {
    let alive = true;
    void api
      .listChangesDiff(session.id)
      .then(async (d) => {
        const [r, changes] = await Promise.all([
          reviews.list(session.id, d.n),
          api.listChanges(session.id),
        ]);
        if (alive) {
          setRecords(r);
          setRevision(d.n);
          setMerged(Boolean(changes.find((c) => c.n === d.n)?.merged));
        }
      })
      .catch((e) => {
        if (alive) setError(e.message);
      });
    return () => {
      alive = false;
    };
  }, [api, session.id, session.changes?.headSha, session.delivery?.prState]);
  const latestReview = records.filter((r) => !r.stale).at(-1);
  const approved =
    latestReview?.verdict === "approve" &&
    latestReview.independent === true &&
    !latestReview.stale;
  const act = async (verdict: ReviewRecord["verdict"] | "merge") => {
    if (revision === undefined) return;
    setBusy(true);
    setError("");
    try {
      if (verdict === "merge") {
        await reviews.merge(session.id, revision);
        setMerged(true);
        setConfirmMerge(false);
        onChanged?.();
      } else {
        await reviews.record(session.id, revision, verdict);
        setRecords(await reviews.list(session.id, revision));
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="review-section">
      <h3>Revision review</h3>
      <p>
        {merged
          ? demoMode
            ? "Demo merge recorded locally."
            : "Pull request merged."
          : approved
            ? "An approval is recorded for this revision."
            : latestReview?.verdict === "request_changes"
              ? "Changes requested on this revision."
              : "Review the diff before recording a verdict."}
      </p>
      {error && (
        <p role="alert" className="negative">
          {error}
        </p>
      )}
      {!merged && (
        <div className="review-action-row">
          <button
            className="button"
            disabled={busy || revision === undefined}
            onClick={() => void act("request_changes")}
          >
            Request changes
          </button>
          <button
            className="button primary"
            disabled={busy || revision === undefined}
            onClick={() => void act("approve")}
          >
            {demoMode ? "Record demo approval" : "Record approval"}
          </button>
        </div>
      )}
      {approved && !merged && session.delivery?.prState !== "draft" && (
        <button
          className="button full"
          disabled={busy}
          onClick={() => setConfirmMerge(true)}
        >
          Merge pull request
          <Icon name="pr" size={14} />
        </button>
      )}
      {approved && session.delivery?.prState === "draft" && (
        <p className="fine-print">
          Update this pull request to mark it ready before merging.
        </p>
      )}
      {confirmMerge && (
        <div className="merge-confirm">
          <p>
            {demoMode
              ? "Simulate merging this reviewed revision in the demo?"
              : "Merge this reviewed revision into its target branch? The server checks approval and commit identity."}
          </p>
          <button className="button" onClick={() => setConfirmMerge(false)}>
            Cancel
          </button>
          <button
            className="button primary"
            disabled={busy}
            onClick={() => void act("merge")}
          >
            {demoMode ? "Simulate merge" : "Confirm merge"}
          </button>
        </div>
      )}
      {session.delivery?.prUrl && !demoMode && (
        <a
          className="button full"
          href={session.delivery.prUrl}
          target="_blank"
          rel="noreferrer"
        >
          Open on GitHub
          <Icon name="external" size={14} />
        </a>
      )}
      <p className="fine-print">
        {demoMode
          ? "Demo records only · no GitHub mutation."
          : "Revision approval is pinned to the reviewed commit. Merge requires an independent, current review."}
      </p>
    </div>
  );
}

export function DeliveryDialog({
  session,
  onSubmit,
  onClose,
  busy,
  error,
}: {
  session: Session;
  onSubmit: (input: import("../api/types").DeliverInput) => Promise<void>;
  onClose: () => void;
  busy: boolean;
  error: string;
}) {
  const api = useApi();
  const dialog = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState(session.title);
  const [target, setTarget] = useState(
    session.delivery?.prBase ?? session.repo?.ref ?? "",
  );
  const [draft, setDraft] = useState(session.delivery?.prState !== "open");
  const [n, setN] = useState<number>();
  const [loadError, setLoadError] = useState("");
  useEffect(() => {
    const focus = document.activeElement as HTMLElement;
    dialog.current?.showModal();
    return () => focus?.focus();
  }, []);
  useEffect(() => {
    let alive = true;
    void api
      .listChangesDiff(session.id)
      .then((s) => {
        if (alive) setN(s.n);
      })
      .catch((e) => {
        if (alive) setLoadError(e.message);
      });
    return () => {
      alive = false;
    };
  }, [api, session.id]);
  return (
    <dialog
      ref={dialog}
      className="connect-modal delivery-dialog"
      aria-labelledby="delivery-title"
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onClose();
      }}
    >
      <h2 id="delivery-title">
        {session.delivery?.prNumber
          ? "Update pull request"
          : "Create pull request"}
      </h2>
      <p>
        {n ? `Revision ${n}` : "Loading revision…"} · {session.repo?.name}
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void onSubmit({
            n,
            title: title.trim(),
            draft,
            ...(target.trim() ? { target: target.trim() } : {}),
          });
        }}
      >
        <label className="form-label">
          Title
          <input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
          />
        </label>
        <label className="form-label">
          Target branch
          <input
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            required
          />
        </label>
        <label className="form-label">
          <span>
            <input
              type="checkbox"
              checked={draft}
              onChange={(e) => setDraft(e.target.checked)}
            />{" "}
            Draft pull request
          </span>
        </label>
        {(error || loadError) && (
          <p role="alert" className="negative">
            {error || loadError}
          </p>
        )}
        <div className="review-action-row">
          <button
            type="button"
            className="button"
            disabled={busy}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            className="button primary"
            disabled={
              busy || n === undefined || !title.trim() || !target.trim()
            }
          >
            {busy
              ? "Delivering…"
              : session.delivery?.prNumber
                ? "Update pull request"
                : "Create pull request"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
