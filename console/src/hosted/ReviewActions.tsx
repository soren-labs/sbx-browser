import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { Session } from "../api/types";
import { reviews } from "../prototype/domain";
import { hostedRequest } from "./api";

export function HostedReviewActions({ session, onChanged }: {session: Session; onChanged?: () => void}) {
  const [items, setItems] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [merged, setMerged] = useState(false);
  const load = async () => {
    const data = await hostedRequest(`/hosted/sessions/${session.id}/review-sessions`);
    setItems(data.sessions);
  };
  useEffect(() => {
    let active = true;
    const update = () => { if (active) void load().catch(e => setError(e.message)); };
    update();
    const timer = window.setInterval(update, 1000);
    return () => {active = false; window.clearInterval(timer);};
  }, [session.id, session.changes?.headSha]);
  const latest = items.filter(i => i.reviewed_head_sha === session.changes?.headSha).at(-1);
  const approved = latest?.review?.verdict === "approve" && latest.review.independent && !latest.review.stale;
  const act = async (merge = false) => {
    setBusy(true); setError("");
    try {
      if (merge) {
        await reviews.merge(session.id, latest.revision_n);
        setMerged(true); setConfirm(false); onChanged?.();
      } else {
        await hostedRequest(`/hosted/sessions/${session.id}/review-sessions`, {});
        await load();
      }
    } catch(e) {setError((e as Error).message);} finally {setBusy(false);}
  };
  return <section className="review-section">
    <h3>Independent review</h3>
    {error && <p role="alert">{error}</p>}
    {items.map(item => <div key={item.reviewer_session_id}>
      <Link to={`/sessions/${item.reviewer_session_id}`}>Open review Session</Link>
      <p>{item.review?.verdict === "request_changes" ? "Changes requested" : item.review?.verdict === "approve" ? "Review passed" : item.status === "failed" ? "Review failed — retry with a new review Session" : "Review running"}{item.review?.stale ? " · Outdated revision" : ""}</p>
      {item.review?.findings?.map((finding: any, i: number) => <p key={i}>{finding.message}</p>)}
    </div>)}
    {!merged && !session.delivery?.merged && <button className="button" disabled={busy} onClick={() => void act()}>Launch independent review</button>}
    {latest?.review?.verdict === "request_changes" && <p>Send the requested fixes as a follow-up, update the pull request, then launch a new review.</p>}
    {approved && !merged && !session.delivery?.merged && (session.delivery?.prState === "draft" ? <p>Update this pull request to mark it ready before merging.</p> : <button className="button primary" disabled={busy} onClick={() => setConfirm(true)}>Merge pull request</button>)}
    {confirm && <div className="merge-confirm"><p>Merge this reviewed commit into its target branch?</p><button className="button" onClick={() => setConfirm(false)}>Cancel</button><button className="button primary" disabled={busy} onClick={() => void act(true)}>Confirm merge</button></div>}
    {(merged || session.delivery?.merged) && <p>Pull request merged.</p>}
  </section>;
}
