import { Fragment, useEffect, useMemo, useState } from "react";
import { api } from "./api.js";

import { CONTEXT, segments } from "./diffSegments.js";

const STEP = 20;
const STATUS = { A: "added", M: "modified", D: "deleted", R: "renamed", C: "copied", T: "type changed" };

function FileDiff({ reviewId, path }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [revealed, setRevealed] = useState(new Set());
  useEffect(() => {
    let live = true;
    api.reviewFileDiff(reviewId, path).then((d) => live && setData(d)).catch((e) => live && setError(e.message));
    return () => { live = false; };
  }, [reviewId, path]);
  const parts = useMemo(() => (data ? segments(data.rows, revealed) : []), [data, revealed]);
  if (error) return <p className="error small">{error}</p>;
  if (!data) return <p className="muted small">Loading diff…</p>;
  if (data.binary) return <p className="muted small">Binary file; no text diff.</p>;
  const reveal = (from, to) => setRevealed((r) => {
    const next = new Set(r);
    for (let i = from; i < to; i++) next.add(i);
    return next;
  });
  return (
    <div className="diff">
      <table>
        <tbody>
          {parts.map((part, index) => {
            if (!part.visible) {
              const count = part.end - part.start;
              const first = index === 0;
              const last = index === parts.length - 1;
              return (
                <tr key={`f${part.start}`} className="fold">
                  <td colSpan={4}>
                    <span className="muted">⋯ {count} unchanged line{count === 1 ? "" : "s"}</span>
                    {!first && count > STEP && (
                      <button className="linkish" onClick={() => reveal(part.start, part.start + STEP)}>↓ {STEP} more</button>
                    )}
                    {!last && count > STEP && (
                      <button className="linkish" onClick={() => reveal(part.end - STEP, part.end)}>↑ {STEP} more</button>
                    )}
                    <button className="linkish" onClick={() => reveal(part.start, part.end)}>Show all</button>
                  </td>
                </tr>
              );
            }
            return (
              <Fragment key={`v${part.start}`}>
                {data.rows.slice(part.start, part.end).map((row, k) => (
                  <tr key={part.start + k} className={row.t === "+" ? "add" : row.t === "-" ? "del" : ""}>
                    <td className="ln">{row.o ?? ""}</td>
                    <td className="ln">{row.n ?? ""}</td>
                    <td className="sign">{row.t === " " ? "" : row.t}</td>
                    <td className="code">{row.s || " "}</td>
                  </tr>
                ))}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      {data.truncated && <p className="muted small">Long file: showing the first 20,000 lines.</p>}
    </div>
  );
}

// The files a review covered, each expandable to its diff.
export default function ChangedFiles({ reviewId, highlight = [] }) {
  const [files, setFiles] = useState(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState([]);
  useEffect(() => {
    api.reviewFiles(reviewId).then((d) => setFiles(d.files)).catch((e) => setError(e.message));
  }, [reviewId]);
  if (error) return <div className="card"><h3>Changed files</h3><p className="muted small">{error}</p></div>;
  if (!files) return <div className="card muted">Loading changed files…</div>;
  const added = files.reduce((n, f) => n + (f.added || 0), 0);
  const removed = files.reduce((n, f) => n + (f.removed || 0), 0);
  const toggle = (path) => setOpen((o) => (o.includes(path) ? o.filter((p) => p !== path) : [...o, path]));
  return (
    <div className="card changed-files">
      <div className="actions title">
        <h3 className="grow">Changed files ({files.length}) <span className="diffstat"><span className="plus">+{added}</span> <span className="minus">−{removed}</span></span></h3>
        <button className="secondary small-btn" onClick={() => setOpen(files.map((f) => f.path))}>Expand all</button>
        <button className="secondary small-btn" onClick={() => setOpen([])}>Collapse all</button>
      </div>
      {files.map((f) => {
        const isOpen = open.includes(f.path);
        return (
          <div key={f.path} className={`file ${isOpen ? "open" : ""}`}>
            <button className="file-head" aria-expanded={isOpen} onClick={() => toggle(f.path)}>
              <span className="twisty-mark">{isOpen ? "▾" : "▸"}</span>
              <code className="grow">{f.old_path ? `${f.old_path} → ` : ""}{f.path}</code>
              {highlight.includes(f.path) && <span className="badge warn">finding</span>}
              <span className="badge">{STATUS[f.status] || f.status}</span>
              <span className="diffstat">{f.binary ? "binary" : <><span className="plus">+{f.added}</span> <span className="minus">−{f.removed}</span></>}</span>
            </button>
            {isOpen && <FileDiff reviewId={reviewId} path={f.path} />}
          </div>
        );
      })}
    </div>
  );
}
