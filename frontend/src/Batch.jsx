import { useEffect, useState } from "react";
import { api } from "./api.js";

const money = (n) => `$${(n || 0).toFixed(4)}`;
const LIMITS = [5, 10, 20, 30, 50, 100];
const EVERY = [2, 3, 5, 10, 20];
const ACTIVE = ["running", "cancelling"];

// Plan, approve and follow a history batch: many commits reviewed in one approved run.
export default function BatchPanel({ repoPath, from, to, onProgress }) {
  const [modes, setModes] = useState({});
  const [mode, setMode] = useState("every");
  const [every, setEvery] = useState(5);
  const [limit, setLimit] = useState(10);
  const [batch, setBatch] = useState(null);
  const [cap, setCap] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [history, setHistory] = useState([]);

  useEffect(() => {
    api.batchModes().then((d) => setModes(d.modes)).catch(() => {});
    api.batches(repoPath).then((list) => {
      setHistory(list);
      // Show a running batch, else the latest one that ran (its card survives page re-layouts).
      const shown = list.find((b) => ACTIVE.includes(b.status)) || list.find((b) => b.status !== "planned");
      if (shown) setBatch(shown);
    }).catch(() => {});
  }, [repoPath]);

  useEffect(() => {  // follow a running batch
    if (!batch || !ACTIVE.includes(batch.status)) return undefined;
    const timer = setTimeout(async () => {
      const next = await api.batch(batch.id);
      if (next.done + next.failed !== batch.done + batch.failed || next.status !== batch.status) onProgress?.();
      setBatch(next);
      if (!ACTIVE.includes(next.status)) setHistory(await api.batches(repoPath));
    }, 2000);
    return () => clearTimeout(timer);
  }, [batch]);

  const plan = async () => {
    setBusy(true);
    setError("");
    try {
      const planned = await api.planBatch({ repo: repoPath, from, to, mode, every, limit });
      setBatch(planned);
      setCap(planned.max_cost.toFixed(4));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  const start = async () => {
    setBusy(true);
    setError("");
    try {
      setBatch(await api.startBatch(batch.id, Number(cap)));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  const cancel = async () => setBatch(await api.cancelBatch(batch.id));

  const planned = batch?.status === "planned";
  const total = batch?.commits.length || 0;
  return (
    <div className="batch">
      <div className="kb-grid">
        <label className="field">
          <span className="small">Which commits</span>
          <select value={mode} onChange={(e) => setMode(e.target.value)} disabled={busy}>
            {Object.entries(modes).map(([key, text]) => <option key={key} value={key}>{text}</option>)}
          </select>
        </label>
        {mode === "nth" && (
          <label className="field">
            <span className="small">N</span>
            <select value={every} onChange={(e) => setEvery(Number(e.target.value))}>
              {EVERY.map((n) => <option key={n} value={n}>every {n}th commit</option>)}
            </select>
          </label>
        )}
        <label className="field">
          <span className="small">At most</span>
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {LIMITS.map((n) => <option key={n} value={n}>{n} commits (newest)</option>)}
          </select>
        </label>
      </div>
      <div className="actions lab-actions">
        <button onClick={plan} disabled={busy || !from || !to || ACTIVE.includes(batch?.status)}>
          {busy && !batch ? "Planning…" : "Plan batch"}
        </button>
        <span className="small muted">Uses the From and To range above. Planning is local and free.</span>
      </div>
      {error && <p className="error small">{error}</p>}

      {batch && (
        <div className="card-inset batch-card">
          <div className="actions">
            <b className="grow">{total} commit{total === 1 ? "" : "s"} · {batch.range_spec.split("..").map((r) => r.slice(0, 12)).join(" → ")}</b>
            <span className={`badge status-${batch.status === "completed" ? "completed" : planned ? "awaiting_approval" : batch.status}`}>{batch.status}</span>
          </div>
          {planned ? (
            <>
              <p className="small">
                Estimated <b>{money(batch.est_cost)}</b>, at most <b>{money(batch.max_cost)}</b> if every agent uses its full output.
                Each commit is reviewed like a normal review: gitleaks scan, flagged files skipped, one review per commit.
              </p>
              <div className="actions">
                <label className="field cap">
                  <span className="small">Stop when spending reaches ($)</span>
                  <input type="number" min="0.0001" step="0.01" value={cap} onChange={(e) => setCap(e.target.value)} />
                </label>
                <span className="grow" />
                <button className="secondary" onClick={cancel}>Discard</button>
                <button onClick={start} disabled={busy || !(Number(cap) > 0)}>Approve and run {total} review{total === 1 ? "" : "s"}</button>
              </div>
            </>
          ) : (
            <>
              <div className="progress" aria-label="Batch progress">
                <span style={{ width: `${total ? ((batch.done + batch.failed) / total) * 100 : 0}%` }} />
              </div>
              <div className="actions small">
                <span className="grow">{batch.done} reviewed{batch.failed ? `, ${batch.failed} failed` : ""} of {total} · spent {money(batch.spent)} of {money(batch.max_cost)}
                  {batch.status === "budget" && " · stopped at the cost cap"}</span>
                {ACTIVE.includes(batch.status) && (
                  <button className="secondary danger small-btn" onClick={cancel} disabled={batch.status === "cancelling"}>
                    {batch.status === "cancelling" ? "Stopping after this commit…" : "Cancel"}
                  </button>
                )}
              </div>
            </>
          )}
          <table className="batch-commits">
            <thead><tr><th>Commit</th><th>Message</th><th>Files</th><th>Est.</th></tr></thead>
            <tbody>
              {batch.commits.map((c, i) => {
                const state = planned ? "" : i < batch.done + batch.failed ? "✓" : ACTIVE.includes(batch.status) && i === batch.done + batch.failed ? "…" : "";
                return (
                  <tr key={c.sha}>
                    <td><code>{c.short}</code> <span className="muted small">{c.date}</span> {state}</td>
                    <td className="small">{c.subject}</td>
                    <td className="small">{c.reviewable}/{c.files}</td>
                    <td className="small">{money(c.estimate)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {history.filter((b) => b.id !== batch?.id && b.status !== "planned").length > 0 && (
        <details className="batch-history">
          <summary className="small">Earlier batches</summary>
          {history.filter((b) => b.id !== batch?.id && b.status !== "planned").map((b) => (
            <p key={b.id} className="small">
              {new Date(b.created_at * 1000).toLocaleString()} · {b.mode} · {b.done}/{b.commits.length} reviewed · {money(b.spent)} · {b.status}
            </p>
          ))}
        </details>
      )}
    </div>
  );
}
