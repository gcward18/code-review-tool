import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import Agents from "./Agents.jsx";
import Knowledge from "./Knowledge.jsx";
import Markdown from "./Markdown.jsx";
import Workspace, { Panel } from "./Workspace.jsx";

const THEMES = ["light", "dark", "dracula"];
const ago = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "–");
const money = (n) => `$${(n || 0).toFixed(4)}`;

function useTheme() {
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem("autosde-theme") || "light";
    } catch {
      return "light";
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

// --- projects sidebar -----------------------------------------------------------

// Per-browser preferences (Settings page).
function usePrefs() {
  const [prefs, setPrefs] = useState(() => {
    try {
      return { showBlocked: false, ...JSON.parse(localStorage.getItem("autosde-prefs") || "{}") };
    } catch {
      return { showBlocked: false };
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem("autosde-prefs", JSON.stringify(prefs));
    } catch {
      /* private mode */
    }
  }, [prefs]);
  return [prefs, (change) => setPrefs((p) => ({ ...p, ...change }))];
}

function ProjectList({ title, items, selected, onSelect, blocked }) {
  if (!items.length) return null;
  return (
    <section className="group">
      <h3>{title} <span className="count">{items.length}</span></h3>
      <ul className="tree">
        {items.map((r) => (
          <li key={r.path}>
            <div className={`node ${selected === r.path ? "selected" : ""} ${blocked ? "blocked" : ""}`}
              title={blocked ? `${r.path} — macOS has not approved access` : r.path}
              onClick={blocked ? undefined : () => onSelect(r.path)}>
              <span className="name">{blocked ? "🔒" : "◆"} {r.name}
                <span className="muted small"> {r.path.split("/").slice(-2, -1)[0]}</span>
              </span>
              {r.reviews > 0 && <span className="count">{r.reviews}</span>}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Workspaces({ selected, onSelect, showBlocked, refresh }) {
  const [query, setQuery] = useState("");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setData(null);
    api.workspaces().then(setData).catch((e) => setError(e.message));
  }, [refresh]);
  const q = query.trim().toLowerCase();
  const match = (r) => !q || r.name.toLowerCase().includes(q) || r.path.toLowerCase().includes(q);
  const pick = (list) => (data?.[list] || []).filter(match);
  const shown = data && pick("recent").length + pick("available").length
    + (showBlocked ? pick("blocked").length : 0);
  return (
    <>
      <input className="search" type="search" value={query} placeholder="Search projects"
        onChange={(e) => setQuery(e.target.value)} aria-label="Search projects" />
      {error && <p className="error small">{error}</p>}
      {!data && !error && <p className="muted small">Finding repositories…</p>}
      {data && (
        <>
          <ProjectList title="Recent" items={pick("recent")} selected={selected} onSelect={onSelect} />
          <ProjectList title="Available" items={pick("available")} selected={selected} onSelect={onSelect} />
          {showBlocked && <ProjectList title="Blocked" items={pick("blocked")} blocked />}
          {!shown && <p className="muted small">{q ? "No projects match." : "No git repositories found."}</p>}
          {!showBlocked && data.blocked.length > 0 && (
            <p className="muted small hint">{data.blocked.length} blocked folder(s) hidden · Settings</p>
          )}
        </>
      )}
    </>
  );
}

function Settings({ theme, setTheme, prefs, setPrefs, onRescan }) {
  return (
    <div className="card">
      <h2>Settings</h2>
      <label className="field setting">
        <span>Theme</span>
        <select value={theme} onChange={(e) => setTheme(e.target.value)}>
          {THEMES.map((t) => <option key={t} value={t}>{t[0].toUpperCase() + t.slice(1)}</option>)}
        </select>
      </label>
      <label className="toggle setting">
        <input type="checkbox" checked={!!prefs.showAgents}
          onChange={(e) => setPrefs({ showAgents: e.target.checked })} />
        Show agent runs on project and review pages
      </label>
      <p className="small muted">Off: agent details appear only on the Agents page.</p>
      <label className="toggle setting">
        <input type="checkbox" checked={prefs.showBlocked}
          onChange={(e) => setPrefs({ showBlocked: e.target.checked })} />
        Show blocked folders and projects
      </label>
      <p className="small muted">
        Blocked means macOS has not approved AutoSDE (your terminal) for that folder. To review
        a blocked project, allow access in System Settings → Privacy &amp; Security → Files and
        Folders, then rescan.
      </p>
      <button className="secondary" onClick={onRescan}>Rescan repositories</button>
    </div>
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

function RangePicker({ repoPath, onStarted, onOpen, showAgents }) {
  const [repo, setRepo] = useState(null);
  const [project, setProject] = useState(null);
  useEffect(() => {
    if (repo?.project_id) api.project(repo.project_id).then(setProject);
  }, [repo?.project_id]);
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
  const picker = (
    <>
      <code className="muted small path">{repo.path}</code>
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
    </>
  );
  if (!repo.project_id) {
    return <div className="card"><h2>{repo.name}</h2>{picker}</div>;
  }
  return (
    <Workspace
      topLeft={<Panel title={`New review · ${repo.name}`}>{picker}</Panel>}
      bottomLeft={<Chat projectId={repo.project_id} />}
      topRight={<Panel title={`Reviews${project ? ` (${project.reviews.length})` : ""}`}>
        {project ? <ReviewTable reviews={project.reviews} onOpen={onOpen} /> : <p className="muted">Loading…</p>}
      </Panel>}
      bottomRight={showAgents && <Panel title="Agents that ran">
        {project ? <AgentsTable agents={project.agents} /> : <p className="muted">Loading…</p>}
      </Panel>}
    />
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

function Review({ id, onProject, onContext, showAgents }) {
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
      {showAgents && review.agent_runs.length > 0 && (
        <details className="card">
          <summary>Agents ({review.agent_runs.length})</summary>
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
        </details>
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
  const projectTable = projects.length ? (
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
  ) : <p className="muted">None yet. Pick a project in the sidebar to start a review.</p>;
  return (
    <Workspace
      topLeft={<Chat />}
      topRight={<Panel title={`Projects (${projects.length})`}>{projectTable}</Panel>}
      bottomRight={<Panel title="Recent reviews"><ReviewTable reviews={reviews} onOpen={onOpen} /></Panel>}
    />
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

function Message({ role, text }) {
  return role === "user" ? <p className="said">{text}</p> : <Markdown text={text} />;
}

// projectId omitted: the Home chat about every project on this machine.
function Chat({ projectId }) {
  const home = projectId === undefined;
  const [messages, setMessages] = useState(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const end = useRef(null);
  useEffect(() => {
    (home ? api.homeChat() : api.chat(projectId))
      .then((d) => setMessages(d.messages)).catch((e) => setError(e.message));
  }, [projectId]);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest" });
  }, [messages, busy]);

  const send = async (e) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    setError("");
    setMessages((m) => [...(m || []), { role: "user", content: text, pending: true }]);
    setDraft("");
    try {
      setMessages((await (home ? api.askHome(text) : api.ask(projectId, text))).messages);
    } catch (err) {
      setError(err.message);
      setMessages((m) => m.filter((x) => !x.pending));
      setDraft(text);
    } finally {
      setBusy(false);
    }
  };

  const clear = messages?.length > 0 && (
    <button className="secondary"
      onClick={() => (home ? api.clearHomeChat() : api.clearChat(projectId)).then((d) => setMessages(d.messages))}>
      Clear
    </button>
  );
  return (
    <Panel title={home ? "Chat about all your projects" : "Chat about this project's history"}
      actions={clear} className="chat">
      <div className="messages">
        {messages?.length === 0 && (
          <p className="muted small">{home
            ? "Ask about any repository on this machine, e.g. \"Which projects have open critical findings?\" or \"Which repos changed this week?\""
            : "Ask about past reviews, e.g. \"Which files keep getting findings?\" or \"What did review #10 find?\""}</p>
        )}
        {messages?.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <Message role={m.role} text={m.content} />
            {m.role === "assistant" && (
              <span className="muted small">{m.model} · {m.input_tokens}/{m.output_tokens} tokens</span>
            )}
          </div>
        ))}
        {busy && <div className="msg assistant muted">Thinking…</div>}
        <div ref={end} />
      </div>
      {error && <p className="error small">{error}</p>}
      <form className="actions" onSubmit={send}>
        <textarea className="grow" rows={2} value={draft} placeholder="Ask the chat agent…"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send(e)} />
        <button type="submit" disabled={busy || !draft.trim()}>Send</button>
      </form>
    </Panel>
  );
}

function AgentsTable({ agents }) {
  return (
    <table>
      <thead><tr><th>Role</th><th>Agent</th><th>Runs</th><th>OK</th><th>Failed</th><th>Kept</th></tr></thead>
      <tbody>
        {agents.map((a) => (
          <tr key={a.role + a.agent}>
            <td>{a.role}</td><td>{a.agent}</td><td>{a.runs}</td><td>{a.succeeded}</td><td>{a.failed}</td><td>{a.findings_kept}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// Projects open on their repository page, which holds history and chat.
function ProjectRedirect({ id, go }) {
  useEffect(() => {
    api.project(id).then((p) => go({ kind: "repo", repo: p.path }, { replace: true }));
  }, [id]);
  return <div className="card muted">Loading…</div>;
}

// Where you are, with a way back: Home › project or repository › review.
function Crumbs({ view, canGoBack, onBack, go }) {
  if (view.kind === "home") return null;
  const trail = [];
  if (view.repo) trail.push({ label: view.repo.split("/").pop(), to: { kind: "repo", repo: view.repo } });
  if (view.kind === "review") trail.push({ label: `Review #${view.id}` });
  if (view.kind === "agents") trail.push({ label: "Agents" });
  if (view.kind === "settings") trail.push({ label: "Settings" });
  if (view.kind === "knowledge") trail.push({ label: "Knowledge" });
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
  const [prefs, setPrefs] = usePrefs();
  const [scan, setScan] = useState(0);
  const [session, setSession] = useState(null);
  const [error, setError] = useState("");
  const [view, setView] = useState({ kind: "home" });
  const [drawer, setDrawer] = useState(false);

  useEffect(() => {
    api.session().then(setSession).catch((e) => setError(e.message));
  }, []);

  const [history, setHistory] = useState([]);
  const go = (next, { replace = false } = {}) => {
    if (!replace) setHistory((h) => [...h.slice(-49), view]);
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
    <div className={`shell ${prefs.collapsed ? "collapsed" : ""}`}>
      <header>
        <button className="menu" aria-label="Toggle projects menu"
          onClick={() => (window.matchMedia("(max-width: 760px)").matches
            ? setDrawer(!drawer) : setPrefs({ collapsed: !prefs.collapsed }))}>☰</button>
        <a className="brand" href="#" onClick={(e) => (e.preventDefault(), go({ kind: "home" }))}><span className="brand-mark">A</span>AutoSDE</a>
        <Crumbs view={view} canGoBack={history.length > 0} onBack={back} go={go} />
        <span className="grow" />
        <a href="#" className={view.kind === "agents" ? "active" : ""}
          onClick={(e) => (e.preventDefault(), go({ kind: "agents" }))}>Agents</a>
        <a href="#" className={view.kind === "knowledge" ? "active" : ""}
          onClick={(e) => (e.preventDefault(), go({ kind: "knowledge" }))}>Knowledge</a>
        <a href="#" className={view.kind === "settings" ? "active" : ""}
          onClick={(e) => (e.preventDefault(), go({ kind: "settings" }))}>Settings</a>
      </header>
      <aside className={drawer ? "open" : ""}>
        {session && <Workspaces selected={view.repo} showBlocked={prefs.showBlocked} refresh={scan}
          onSelect={(repo) => go({ kind: "repo", repo })} />}
      </aside>
      {drawer && <div className="scrim" onClick={() => setDrawer(false)} />}
      <main>
        {error && <div className="card error">{error}</div>}
        {session && view.kind === "home" && (
          <Home onOpen={(id) => go({ kind: "review", id })} onProject={(id) => go({ kind: "project", id })} />
        )}
        {view.kind === "repo" && (
          <RangePicker key={view.repo} repoPath={view.repo}
            onStarted={(id) => go({ kind: "review", id, repo: view.repo })}
            onOpen={(id) => go({ kind: "review", id, repo: view.repo })} showAgents={prefs.showAgents} />
        )}
        {view.kind === "review" && (
          <Review key={view.id} id={view.id} onContext={context} showAgents={prefs.showAgents} onProject={(id) => go({ kind: "project", id, repo: view.repo })} />
        )}
        {session && view.kind === "knowledge" && <Knowledge />}
        {view.kind === "settings" && (
          <Settings theme={theme} setTheme={setTheme} prefs={prefs} setPrefs={setPrefs}
            onRescan={() => setScan((n) => n + 1)} />
        )}
        {session && view.kind === "agents" && <Agents defaultModel={session.model} />}
        {view.kind === "project" && <ProjectRedirect id={view.id} go={go} />}
      </main>
    </div>
  );
}
