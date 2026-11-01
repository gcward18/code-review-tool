import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import Agents from "./Agents.jsx";

const THEMES = ["light", "dark", "dracula"];
const ago = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "–");
const money = (n) => `$${(n || 0).toFixed(4)}`;

function useTheme() {
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem("autosde-theme") || "dracula";
    } catch {
      return "dracula";
    }
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("autosde-theme", theme);
    } catch {
      /* private mode */
    }
  }, [theme]);
  return [theme, setTheme];
}

// --- tree -------------------------------------------------------------------

function TreeNode({ entry, depth, selected, onSelect }) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState(null);
  const [error, setError] = useState("");

  const toggle = async () => {
    if (!open && children === null) {
      try {
        setChildren((await api.tree(entry.path)).entries);
      } catch (e) {
        setError(e.message);
        setChildren([]);
      }
    }
    setOpen(!open);
  };

  return (
    <li>
      <div
        className={`node ${selected === entry.path ? "selected" : ""}`}
        style={{ paddingLeft: 8 + depth * 14 }}
      >
        <button className="twisty" onClick={toggle} aria-label={open ? "Collapse" : "Expand"}>
          {open ? "▾" : "▸"}
        </button>
        <span className="name" onClick={entry.repo ? () => onSelect(entry.path) : toggle}>
          {entry.repo ? "◆" : "📁"} {entry.name}
        </span>
        {entry.repo && (
          <button className="pick" onClick={() => onSelect(entry.path)}>
            Select
          </button>
        )}
      </div>
      {open && (
        <ul>
          {error && <li className="muted small" style={{ paddingLeft: 22 + depth * 14 }}>{error}</li>}
          {children?.map((child) => (
            <TreeNode
              key={child.path}
              entry={child}
              depth={depth + 1}
              selected={selected}
              onSelect={onSelect}
            />
          ))}
          {children?.length === 0 && !error && (
            <li className="muted small" style={{ paddingLeft: 22 + depth * 14 }}>empty</li>
          )}
        </ul>
      )}
    </li>
  );
}

function Tree({ selected, onSelect }) {
  const [root, setRoot] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api.tree().then(setRoot).catch((e) => setError(e.message));
  }, []);
  if (error) return <p className="error">{error}</p>;
  if (!root) return <p className="muted">Loading…</p>;
  return (
    <ul className="tree">
      {root.entries.map((entry) => (
        <TreeNode key={entry.path} entry={entry} depth={0} selected={selected} onSelect={onSelect} />
      ))}
    </ul>
  );
}

// Search across every repository under your home folder; the tree when empty.
function Workspaces({ selected, onSelect }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState(null);
  useEffect(() => {
    if (!query.trim()) return setResults(null);
    let live = true;
    const timer = setTimeout(() => {
      api.search(query).then((data) => live && setResults(data.results))
        .catch(() => live && setResults([]));
    }, 200);
    return () => { live = false; clearTimeout(timer); };
  }, [query]);
  return (
    <>
      <input className="search" type="search" value={query} placeholder="Search repositories"
        onChange={(e) => setQuery(e.target.value)} aria-label="Search repositories" />
      {results === null ? (
        <Tree selected={selected} onSelect={onSelect} />
      ) : results.length === 0 ? (
        <p className="muted small">No repositories match.</p>
      ) : (
        <ul className="tree">
          {results.map((r) => (
            <li key={r.path}>
              <div className={`node ${selected === r.path ? "selected" : ""}`} title={r.path}
                onClick={() => onSelect(r.path)}>
                <span className="name">◆ {r.name}
                  <span className="muted small"> {r.path.split("/").slice(-2, -1)[0]}</span>
                </span>
                {r.project_id && <span className="badge">reviewed</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

// --- range picker -------------------------------------------------------------

function Options({ items }) {
  return items.map((o) => (
    <option key={o.value} value={o.value}>
      {o.kind === "branch" ? `branch: ${o.label}` : o.label}
      {o.when ? `  · ${o.when}` : ""}
    </option>
  ));
}

function RangePicker({ repoPath, onStarted, onProject }) {
  const [repo, setRepo] = useState(null);
  const [from, setFrom] = useState("");
  const [older, setOlder] = useState(null);
  const [to, setTo] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setRepo(null);
    setFrom("");
    setOlder(null);
    setTo("");
    setError("");
    api
      .repo(repoPath)
      .then((data) => {
        setRepo(data);
        setFrom(data.default_from);
      })
      .catch((e) => setError(e.message));
  }, [repoPath]);

  useEffect(() => {
    setOlder(null);
    setTo("");
    if (!from) return;
    api
      .older(repoPath, from)
      .then((data) => {
        setOlder(data.to);
        setTo(data.default_to || "");
      })
      .catch((e) => setError(e.message));
  }, [repoPath, from]);

  const start = async () => {
    setBusy(true);
    setError("");
    try {
      onStarted((await api.submit(repoPath, from, to)).id);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (error && !repo) return <div className="card error">{error}</div>;
  if (!repo) return <div className="card muted">Loading repository…</div>;
  return (
    <div className="card">
      <div className="actions title">
        <h2 className="grow">
          {repo.name} <code className="muted small">{repo.path}</code>
        </h2>
        {repo.project_id && (
          <button className="secondary" onClick={() => onProject(repo.project_id)}>Past reviews</button>
        )}
      </div>
      <div className="range">
        <label>
          From <span className="muted small">(newer)</span>
          <select value={from} onChange={(e) => setFrom(e.target.value)}>
            <option value="">Choose…</option>
            <Options items={repo.from} />
          </select>
        </label>
        <span className="arrow">→</span>
        <label>
          To <span className="muted small">(older)</span>
          <select value={to} onChange={(e) => setTo(e.target.value)} disabled={!from || !older?.length}>
            {!from && <option value="">Pick From first</option>}
            {from && !older && <option value="">Loading…</option>}
            {older?.length === 0 && <option value="">No older commits</option>}
            {older && older.length > 0 && <Options items={older} />}
          </select>
        </label>
        <button onClick={start} disabled={!from || !to || busy}>
          {busy ? "Planning…" : "Plan review"}
        </button>
      </div>
      <p className="muted small">
        Reviews every commit after <b>To</b> up to and including <b>From</b>. The coordinator plans
        first (paths only); you approve before any code is sent.
      </p>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

// --- review -------------------------------------------------------------------

function Plan({ review, onDecided }) {
  const reviewable = review.files.filter((f) => f.agent);
  const [chosen, setChosen] = useState(() => new Set(review.proposed));
  const [estimate, setEstimate] = useState(review.estimate);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const toggle = async (path) => {
    const next = new Set(chosen);
    next.has(path) ? next.delete(path) : next.add(path);
    setChosen(next);
    try {
      setEstimate(await api.estimate(review.id, [...next]));
    } catch (e) {
      setError(e.message);
    }
  };
  const decide = async (approved) => {
    setBusy(true);
    try {
      approved ? await api.approve(review.id, [...chosen]) : await api.cancel(review.id);
      onDecided();
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="card plan">
      <h2>Approve the plan</h2>
      <table>
        <thead>
          <tr>
            <th></th>
            <th>File</th>
            <th>Specialist</th>
            <th>Note</th>
          </tr>
        </thead>
        <tbody>
          {reviewable.map((f) => (
            <tr key={f.path} className={f.flag ? "flagged" : ""}>
              <td>
                <input type="checkbox" checked={chosen.has(f.path)} onChange={() => toggle(f.path)} />
              </td>
              <td><code>{f.path}</code></td>
              <td>{f.agent}</td>
              <td className="small">{f.flag && <span className="badge warn">flagged: {f.flag}</span>}</td>
            </tr>
          ))}
          {review.files
            .filter((f) => !f.agent)
            .map((f) => (
              <tr key={f.path} className="muted">
                <td></td>
                <td><code>{f.path}</code></td>
                <td>–</td>
                <td className="small">not sendable: {f.skipped}</td>
              </tr>
            ))}
        </tbody>
      </table>
      {estimate && (
        <p className="muted small">
          {chosen.size} file(s), {estimate.agents} specialist(s), ~{estimate.input_tokens.toLocaleString()}{" "}
          input tokens, up to {estimate.output_tokens.toLocaleString()} output tokens, at most{" "}
          {money(estimate.max_cost_usd)}.
        </p>
      )}
      <div className="actions">
        <button onClick={() => decide(true)} disabled={busy || chosen.size === 0}>
          Approve and review
        </button>
        <button className="secondary" onClick={() => decide(false)} disabled={busy}>
          Cancel
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function Review({ id, onProject, onContext }) {
  const [review, setReview] = useState(null);
  const [error, setError] = useState("");
  const load = useCallback(() => api.review(id).then((r) => {
    setReview(r);
    onContext({ repo: r.project_path });
  }).catch((e) => setError(e.message)), [id]);

  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (!review || !["planning", "running"].includes(review.status)) return;
    const timer = setTimeout(load, 1500);
    return () => clearTimeout(timer);
  }, [review, load]);

  if (error) return <div className="card error">{error}</div>;
  if (!review) return <div className="card muted">Loading…</div>;
  const kept = review.findings.filter((f) => f.kept);
  const rejected = review.findings.filter((f) => !f.kept);
  return (
    <>
      <div className="card">
        <h2>
          Review #{review.id} ·{" "}
          <a href="#" onClick={(e) => (e.preventDefault(), onProject(review.project_id))}>
            {review.project}
          </a>
        </h2>
        <p>
          <span className={`badge status-${review.status}`}>{review.status.replace("_", " ")}</span>{" "}
          <code>{review.base_sha.slice(0, 7)}..{review.head_sha.slice(0, 7)}</code> · {review.model} ·{" "}
          {review.files_changed} files · {review.input_tokens}/{review.output_tokens} tokens ·{" "}
          {money(review.cost_usd)} · <a href={`/api/reviews/${review.id}.sarif`}>SARIF</a>
        </p>
        {review.summary && <p>{review.summary}</p>}
        {review.error && <p className="error">{review.error}</p>}
        {["planning", "running"].includes(review.status) && (
          <p className="muted">{review.status === "planning" ? "Planning…" : "The team is reviewing…"}</p>
        )}
      </div>
      {review.status === "awaiting_approval" && <Plan review={review} onDecided={load} />}
      {kept.length > 0 && <h3>Findings ({kept.length})</h3>}
      {kept.map((f) => (
        <div className="card finding" key={f.id}>
          <p>
            <span className={`badge sev-${f.severity}`}>{f.severity}</span> <b>{f.title}</b>{" "}
            <span className="muted">· {f.agent}</span>
          </p>
          <p><code>{f.file}:{f.start_line}{f.end_line !== f.start_line ? `–${f.end_line}` : ""}</code></p>
          <p>{f.description}</p>
          <p><b>Fix</b></p>
          <Fix text={f.recommendation} />
        </div>
      ))}
      {rejected.length > 0 && (
        <details className="card">
          <summary>Rejected by the aggregator ({rejected.length})</summary>
          {rejected.map((f) => (
            <p key={f.id} className="small">
              <code>{f.file}:{f.start_line}</code> {f.title} — <span className="muted">{f.rejection}</span>
            </p>
          ))}
        </details>
      )}
      {review.agent_runs.length > 0 && (
        <div className="card">
          <h3>Agents</h3>
          <table>
            <thead>
              <tr><th>Role</th><th>Agent</th><th>Model</th><th>Status</th><th>Files</th><th>Time</th><th>Tokens</th></tr>
            </thead>
            <tbody>
              {review.agent_runs.map((a) => (
                <tr key={a.id}>
                  <td>{a.role}</td>
                  <td>{a.agent}</td>
                  <td className="small">{a.model || "–"}</td>
                  <td className={a.status === "ok" ? "ok" : "error"}>{a.status}{a.error ? ` (${a.error})` : ""}</td>
                  <td className="small">{a.files.split("\n").join(", ")}</td>
                  <td>{a.seconds.toFixed(1)}s</td>
                  <td>{a.input_tokens}/{a.output_tokens}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {review.limitations && (
        <details className="card">
          <summary>Limits</summary>
          <ul className="small">
            {review.limitations.split("\n").map((l) => <li key={l}>{l}</li>)}
          </ul>
        </details>
      )}
    </>
  );
}

// --- lists --------------------------------------------------------------------

function ReviewTable({ reviews, onOpen }) {
  if (!reviews.length) return <p className="muted">No reviews yet.</p>;
  return (
    <table>
      <thead>
        <tr><th>#</th><th>Project</th><th>Range</th><th>Started</th><th>Status</th><th>Findings</th><th>Agents</th><th>Cost</th></tr>
      </thead>
      <tbody>
        {reviews.map((r) => (
          <tr key={r.id} className="clickable" onClick={() => onOpen(r.id)}>
            <td>{r.id}</td>
            <td>{r.project}</td>
            <td><code>{r.base_sha.slice(0, 7)}..{r.head_sha.slice(0, 7)}</code></td>
            <td className="small">{ago(r.started_at)}</td>
            <td><span className={`badge status-${r.status}`}>{r.status.replace("_", " ")}</span></td>
            <td>{r.findings}</td>
            <td className="small">{(r.agents || "").replaceAll(",", ", ")}</td>
            <td>{money(r.cost_usd)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Home({ onOpen, onProject }) {
  const [projects, setProjects] = useState([]);
  const [reviews, setReviews] = useState([]);
  useEffect(() => {
    api.projects().then(setProjects);
    api.reviews().then(setReviews);
  }, []);
  return (
    <>
      <div className="card intro">
        <h2>Pick a repository</h2>
        <p className="muted">Expand folders in the tree and select a git repository (◆) to choose a commit range.</p>
      </div>
      <div className="card">
        <h3>Projects</h3>
        {projects.length ? (
          <table>
            <thead><tr><th>Project</th><th>Reviews</th><th>Last review</th></tr></thead>
            <tbody>
              {projects.map((p) => (
                <tr key={p.id} className="clickable" onClick={() => onProject(p.id)}>
                  <td>{p.name}</td><td>{p.reviews}</td><td className="small">{ago(p.last_review)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">None yet.</p>
        )}
      </div>
      <div className="card">
        <h3>Recent reviews</h3>
        <ReviewTable reviews={reviews} onOpen={onOpen} />
      </div>
    </>
  );
}

// Diff-style recommendations: "@@ lines a-b @@", "- old", "+ new", "  context".
function Fix({ text }) {
  const lines = (text || "").split("\n");
  if (!lines.some((l) => /^(@@|[-+] )/.test(l))) return <p>{text}</p>;
  return (
    <pre className="fix">
      {lines.map((line, i) => {
        const kind = line.startsWith("@@") ? "hunk" : line.startsWith("+ ") ? "add"
          : line.startsWith("- ") ? "del" : "";
        return <div key={i} className={kind}>{line || " "}</div>;
      })}
    </pre>
  );
}

function Project({ id, onOpen, onReview, onContext }) {
  const [project, setProject] = useState(null);
  useEffect(() => {
    api.project(id).then((p) => (setProject(p), onContext({ repo: p.path, name: p.name })));
  }, [id]);
  if (!project) return <div className="card muted">Loading…</div>;
  return (
    <>
      <div className="card">
        <div className="actions title">
          <h2 className="grow">{project.name}</h2>
          <button onClick={() => onReview(project.path)}>New review</button>
        </div>
        <code className="muted small">{project.path}</code>
      </div>
      <div className="card">
        <h3>Reviews</h3>
        <ReviewTable reviews={project.reviews} onOpen={onOpen} />
      </div>
      <div className="card">
        <h3>Agents that ran</h3>
        <table>
          <thead><tr><th>Role</th><th>Agent</th><th>Runs</th><th>OK</th><th>Failed</th><th>Findings kept</th></tr></thead>
          <tbody>
            {project.agents.map((a) => (
              <tr key={a.role + a.agent}>
                <td>{a.role}</td><td>{a.agent}</td><td>{a.runs}</td><td>{a.succeeded}</td><td>{a.failed}</td><td>{a.findings_kept}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

// Where you are, with a way back: Home › project or repository › review.
function Crumbs({ view, canGoBack, onBack, go }) {
  if (view.kind === "home") return null;
  const trail = [];
  if (view.repo) trail.push({ label: view.repo.split("/").pop(), to: { kind: "repo", repo: view.repo } });
  if (view.kind === "project") trail.push({ label: "Past reviews" });
  if (view.kind === "review") trail.push({ label: `Review #${view.id}` });
  if (view.kind === "agents") trail.push({ label: "Agents" });
  return (
    <nav className="crumbs">
      <button className="secondary" onClick={onBack} disabled={!canGoBack} aria-label="Back">←</button>
      <a href="#" onClick={(e) => (e.preventDefault(), go({ kind: "home" }))}>Home</a>
      {trail.map((item, i) => (
        <span key={i}>
          <span className="muted"> › </span>
          {item.to && i < trail.length - 1
            ? <a href="#" onClick={(e) => (e.preventDefault(), go(item.to))}>{item.label}</a>
            : <b>{item.label}</b>}
        </span>
      ))}
    </nav>
  );
}

// --- shell ----------------------------------------------------------------------

export default function App() {
  const [theme, setTheme] = useTheme();
  const [session, setSession] = useState(null);
  const [error, setError] = useState("");
  const [view, setView] = useState({ kind: "home" });
  const [drawer, setDrawer] = useState(false);

  useEffect(() => {
    api.session().then(setSession).catch((e) => setError(e.message));
  }, []);

  const [history, setHistory] = useState([]);
  const go = (next) => {
    setHistory((h) => [...h.slice(-49), view]);
    setView(next);
    setDrawer(false);
  };
  // Pages fill in the repository they belong to, for the breadcrumb; not a navigation.
  const context = (extra) => setView((v) => ({ ...v, ...extra }));
  const back = () => {
    setHistory((h) => {
      if (h.length) setView(h[h.length - 1]);
      return h.slice(0, -1);
    });
  };

  return (
    <div className="shell">
      <header>
        <button className="menu" onClick={() => setDrawer(!drawer)} aria-label="Folders">☰</button>
        <a className="brand" href="#" onClick={(e) => (e.preventDefault(), go({ kind: "home" }))}>AutoSDE</a>
        <span className="grow" />
        <a href="#" className={view.kind === "agents" ? "active" : ""}
          onClick={(e) => (e.preventDefault(), go({ kind: "agents" }))}>Agents</a>
        <select value={theme} onChange={(e) => setTheme(e.target.value)} aria-label="Theme">
          {THEMES.map((t) => <option key={t} value={t}>{t[0].toUpperCase() + t.slice(1)}</option>)}
        </select>
      </header>
      <aside className={drawer ? "open" : ""}>
        <h3>Workspaces</h3>
        {session && <Workspaces selected={view.repo} onSelect={(repo) => go({ kind: "repo", repo })} />}
      </aside>
      {drawer && <div className="scrim" onClick={() => setDrawer(false)} />}
      <main>
        <Crumbs view={view} canGoBack={history.length > 0} onBack={back} go={go} />
        {error && <div className="card error">{error}</div>}
        {session && view.kind === "home" && (
          <Home onOpen={(id) => go({ kind: "review", id })} onProject={(id) => go({ kind: "project", id })} />
        )}
        {view.kind === "repo" && (
          <RangePicker repoPath={view.repo} onStarted={(id) => go({ kind: "review", id, repo: view.repo })}
            onProject={(id) => go({ kind: "project", id, repo: view.repo })} />
        )}
        {view.kind === "review" && (
          <Review key={view.id} id={view.id} onContext={context} onProject={(id) => go({ kind: "project", id, repo: view.repo })} />
        )}
        {session && view.kind === "agents" && <Agents defaultModel={session.model} />}
        {view.kind === "project" && <Project key={view.id} id={view.id} onOpen={(id) => go({ kind: "review", id, repo: view.repo })}
          onReview={(repo) => go({ kind: "repo", repo })} onContext={context} />}
      </main>
    </div>
  );
}
