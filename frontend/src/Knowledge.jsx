import { Fragment, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import Workspace, { Panel } from "./Workspace.jsx";

const ACCEPT = ".txt,.md,.markdown,.rst,.adoc,.json,.csv,.tsv,.yaml,.yml,.toml,.ini,.cfg,.xml,.html,.htm,"
  + ".docx,.pdf,.log,.sql,.py,.js,.jsx,.ts,.tsx,.java,.go,.cs,.rb,.rs,.c,.h,.cpp,.hpp,.kt,.swift,.php,.sh,.tf,.proto,.graphql";
const size = (n) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n > 1e3 ? `${Math.round(n / 1e3)} KB` : `${n} B`);

function toBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error("Could not read the file."));
    reader.readAsDataURL(file);
  });
}

function NewBase({ onCreated, onCancel }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const create = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const base = await api.createKnowledge(name.trim(), description.trim());
      onCreated(base.id);
    } catch (err) {
      setError(err.message === "Not found."
        ? "The server is running an older AutoSDE. Restart `autosde serve` and try again."
        : err.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="kb-create card-inset" onSubmit={create}>
      <label className="field">
        <span className="small">Name</span>
        <input autoFocus value={name} placeholder="team-conventions" onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field">
        <span className="small">Description <span className="muted">· optional</span></span>
        <input value={description} placeholder="Coding standards and review rules" onChange={(e) => setDescription(e.target.value)} />
      </label>
      {error && <p className="error small">{error}</p>}
      <div className="actions">
        <button type="submit" disabled={!name.trim() || busy}>{busy ? "Creating…" : "Create knowledge base"}</button>
        {onCancel && <button type="button" className="secondary" onClick={onCancel}>Cancel</button>}
      </div>
    </form>
  );
}

function Bases({ bases, selected, onSelect, creating, setCreating, onCreated }) {
  return (
    <>
      {creating && <NewBase onCreated={(id) => (setCreating(false), onCreated(id))}
        onCancel={bases.length ? () => setCreating(false) : null} />}
      <ul className="tree kb-list">
        {bases.map((b) => (
          <li key={b.id}>
            <div className={`node ${selected === b.id ? "selected" : ""}`} onClick={() => onSelect(b.id)}>
              <span className="name">📚 {b.name}</span>
              <span className="count">{b.documents} docs · {b.chunks} chunks</span>
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

function StrategyDetail({ info }) {
  if (!info) return null;
  return (
    <div className="strategy-detail">
      <p className="small"><b>{info.summary}</b></p>
      <dl className="small">
        <dt>How it works</dt><dd>{info.how}</dd>
        <dt>Best for</dt><dd>{info.best}</dd>
        <dt>Trade-off</dt><dd>{info.tradeoff}</dd>
      </dl>
    </div>
  );
}

function Select({ label, hint, value, onChange, options, disabled }) {
  return (
    <label className="field">
      <span className="small">{label}{hint && <span className="muted"> · {hint}</span>}</span>
      <select value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, text]) => <option key={v} value={v}>{text}</option>)}
      </select>
    </label>
  );
}

const SIZES = [100, 200, 300, 400, 500, 600, 800, 1000, 1500, 2000];
const OVERLAPS = [0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5];
const PERCENTILES = [70, 75, 80, 85, 90, 95, 98];
const TOP_KS = [1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20];
const BUDGETS = [500, 1000, 1500, 2000, 3000, 4000, 6000, 8000];
const withValue = (list, value) => [...new Set([...list, value])].sort((x, y) => x - y);

function Strategy({ base, options, onSaved, onDeleted }) {
  const [draft, setDraft] = useState(base.settings);
  const [status, setStatus] = useState("");
  useEffect(() => setDraft(base.settings), [base.id]);
  const set = (key, value) => setDraft((d) => ({ ...d, [key]: value }));
  const strategies = options?.strategies || {};
  const models = options?.models || {};
  const info = strategies[draft.strategy];
  const uses = info?.uses || ["chunk_tokens", "overlap"];
  const locked = base.chunks > 0;
  const stale = base.documents.filter((d) => d.status === "ready" && d.strategy && d.strategy !== base.settings.strategy);

  const save = async () => {
    try {
      const saved = await api.updateKnowledge(base.id, { settings: draft });
      onSaved(saved);
      const chunking = ["strategy", "chunk_tokens", "overlap", "semantic_percentile"]
        .some((k) => draft[k] !== base.settings[k]);
      setStatus(chunking && base.documents.length ? "Saved. Re-chunk to apply it to existing documents." : "Saved.");
    } catch (e) {
      setStatus(e.message);
    }
  };
  const rechunk = async () => {
    const result = await api.rechunkKnowledge(base.id);
    setStatus(`Re-chunking ${result.rechunking} document(s)`
      + (result.skipped.length ? `; re-upload ${result.skipped.join(", ")} (stored before text was kept).` : "."));
    onSaved(await api.knowledgeBase(base.id));
  };
  const dirty = JSON.stringify(draft) !== JSON.stringify(base.settings);
  const modelDims = models[draft.embed_model]?.dimensions || [draft.dimensions];

  return (
    <>
      {base.description && <p className="small muted">{base.description}</p>}
      <h4 className="kb-h">Chunking</h4>
      <Select label="Strategy" value={draft.strategy} onChange={(v) => set("strategy", v)}
        options={Object.entries(strategies).map(([key, s]) => [key, s.name])} />
      <StrategyDetail info={info} />
      <div className="kb-grid">
        {uses.includes("chunk_tokens") && <Select label="Chunk size" hint="tokens" value={draft.chunk_tokens}
          onChange={(v) => set("chunk_tokens", Number(v))} options={withValue(SIZES, draft.chunk_tokens).map((n) => [n, `${n} tokens`])} />}
        {uses.includes("overlap") && <Select label="Overlap" hint="carried into the next chunk" value={draft.overlap}
          onChange={(v) => set("overlap", Number(v))} options={withValue(OVERLAPS, draft.overlap).map((n) => [n, `${Math.round(n * 100)}%`])} />}
        {uses.includes("semantic_percentile") && <Select label="Breakpoint percentile" hint="higher = fewer, larger chunks"
          value={draft.semantic_percentile} onChange={(v) => set("semantic_percentile", Number(v))}
          options={withValue(PERCENTILES, draft.semantic_percentile).map((n) => [n, `${n}th`])} />}
      </div>
      <h4 className="kb-h">Embedding</h4>
      <div className="kb-grid">
        <Select label="Model" hint={locked ? "fixed once documents exist" : ""} value={draft.embed_model} disabled={locked}
          onChange={(v) => setDraft((d) => {
            const dims = models[v]?.dimensions || [d.dimensions];
            return { ...d, embed_model: v, dimensions: dims.includes(d.dimensions) ? d.dimensions : dims[Math.min(1, dims.length - 1)] };
          })}
          options={Object.entries(models).map(([m, o]) => [m, `${m} · $${o.price}/1M tokens`])} />
        <Select label="Dimensions" hint="smaller is faster" value={draft.dimensions} disabled={locked || modelDims.length < 2}
          onChange={(v) => set("dimensions", Number(v))} options={modelDims.map((n) => [n, String(n)])} />
      </div>
      <h4 className="kb-h">Retrieval</h4>
      <div className="kb-grid">
        <Select label="Top K" hint="chunks given to an agent" value={draft.top_k}
          onChange={(v) => set("top_k", Number(v))} options={withValue(TOP_KS, draft.top_k).map((n) => [n, String(n)])} />
        <Select label="Context budget" hint="max reference tokens" value={draft.context_tokens}
          onChange={(v) => set("context_tokens", Number(v))} options={withValue(BUDGETS, draft.context_tokens).map((n) => [n, `${n} tokens`])} />
      </div>
      <p className="small muted">
        Retrieval is hybrid: vector similarity and BM25 keyword ranks fused with reciprocal rank fusion, at most 3 chunks per document.
      </p>
      <div className="actions">
        <button disabled={!dirty} onClick={save}>Save strategy</button>
        {stale.length > 0 && !dirty && (
          <button className="secondary" onClick={rechunk}>Re-chunk {stale.length} document{stale.length > 1 ? "s" : ""}</button>
        )}
        <span className="grow small muted">{status}</span>
        <button className="secondary danger" onClick={async () => {
          if (window.confirm(`Delete "${base.name}" and all its documents and embeddings?`)) {
            await api.deleteKnowledge(base.id);
            onDeleted();
          }
        }}>Delete base</button>
      </div>
    </>
  );
}

const textToBase64 = (text) => btoa(String.fromCharCode(...new TextEncoder().encode(text)));

function PasteText({ onAdd, onCancel }) {
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    const file = /\.[a-z0-9]+$/i.test(name.trim()) ? name.trim() : `${name.trim() || "note"}.md`;
    await onAdd(file, text);
    setBusy(false);
  };
  return (
    <form className="card-inset kb-paste" onSubmit={submit}>
      <label className="field">
        <span className="small">Document name</span>
        <input autoFocus value={name} placeholder="error-handling-guidelines.md" onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field">
        <span className="small">Content <span className="muted">· Markdown or plain text</span></span>
        <textarea rows={8} value={text} placeholder={"# Error handling\n\nNever swallow exceptions…"} onChange={(e) => setText(e.target.value)} />
      </label>
      <div className="actions">
        <button type="submit" disabled={busy || !text.trim()}>{busy ? "Adding…" : "Add document"}</button>
        <button type="button" className="secondary" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function Documents({ base, onChanged }) {
  const [uploading, setUploading] = useState([]);
  const [errors, setErrors] = useState([]);
  const [open, setOpen] = useState(null);
  const [chunks, setChunks] = useState([]);
  const [drag, setDrag] = useState(false);
  const [pasting, setPasting] = useState(false);
  const [picked, setPicked] = useState([]);
  const input = useRef(null);
  const docs = base.documents;

  const send = async (name, data) => {
    setUploading((u) => [...u, name]);
    try {
      await api.uploadDocument(base.id, name, data);
      return true;
    } catch (e) {
      setErrors((list) => [...list, `${name}: ${e.message}`]);
      return false;
    } finally {
      setUploading((u) => u.filter((n) => n !== name));
      onChanged();
    }
  };
  const upload = async (files) => {
    setErrors([]);
    for (const file of files) {
      if (file.size > 20e6) setErrors((list) => [...list, `${file.name}: over 20 MB.`]);
      else await send(file.name, await toBase64(file));
    }
  };
  const addText = async (name, text) => {
    setErrors([]);
    if (await send(name, textToBase64(text))) setPasting(false);
  };
  const remove = async (ids) => {
    const names = docs.filter((d) => ids.includes(d.id)).map((d) => d.name);
    const label = names.length === 1 ? `"${names[0]}"` : `${names.length} documents`;
    if (!window.confirm(`Remove ${label} and their embeddings from ${base.name}?`)) return;
    setErrors([]);
    for (const id of ids) await api.deleteDocument(id);
    setPicked([]);
    if (ids.includes(open)) setOpen(null);
    onChanged();
  };
  const show = async (doc) => {
    if (open === doc.id) return setOpen(null);
    setOpen(doc.id);
    setChunks((await api.documentChunks(doc.id)).chunks);
  };
  const toggle = (id) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));

  return (
    <div className={`kb-docs-wrap ${drag ? "over" : ""}`}
      onDragOver={(e) => (e.preventDefault(), setDrag(true))} onDragLeave={(e) => e.currentTarget === e.target && setDrag(false)}
      onDrop={(e) => (e.preventDefault(), setDrag(false), upload([...e.dataTransfer.files]))}>
      <div className="actions kb-toolbar">
        <button onClick={() => input.current.click()}>⬆ Upload files</button>
        <button className="secondary" onClick={() => setPasting(!pasting)}>✎ Paste text</button>
        <span className="grow" />
        {picked.length > 0 && (
          <button className="secondary danger" onClick={() => remove(picked)}>Remove selected ({picked.length})</button>
        )}
        <input ref={input} type="file" multiple accept={ACCEPT} hidden
          onChange={(e) => (upload([...e.target.files]), (e.target.value = ""))} />
      </div>
      {pasting && <PasteText onAdd={addText} onCancel={() => setPasting(false)} />}
      {errors.map((m) => <p key={m} className="error small">{m}</p>)}
      {uploading.map((n) => <p key={n} className="small muted">Uploading {n}…</p>)}
      {docs.length === 0 && !pasting ? (
        <div className="dropzone" onClick={() => input.current.click()}>
          <div className="empty-icon">📄</div>
          <b>Drop documents here, or click to choose files</b>
          <span className="small muted">PDF, Markdown, text, code, HTML, JSON, CSV or .docx · up to 20 MB each · scanned by gitleaks before embedding</span>
        </div>
      ) : docs.length > 0 && (
        <table className="kb-docs">
          <thead><tr>
            <th><input type="checkbox" aria-label="Select all documents" checked={picked.length === docs.length}
              onChange={(e) => setPicked(e.target.checked ? docs.map((d) => d.id) : [])} /></th>
            <th>Document</th><th>Status</th><th>Size</th><th>Chunks</th><th /></tr></thead>
          <tbody>
            {docs.map((d) => (
              <Fragment key={d.id}>
                <tr className={picked.includes(d.id) ? "picked" : ""}>
                  <td><input type="checkbox" aria-label={`Select ${d.name}`} checked={picked.includes(d.id)} onChange={() => toggle(d.id)} /></td>
                  <td>
                    {d.status === "ready"
                      ? <button className="linkish" onClick={() => show(d)} title="Show chunks">{open === d.id ? "▾" : "▸"} {d.name}</button>
                      : d.name}
                    {d.error && <div className="small error">{d.error}</div>}
                  </td>
                  <td><span className={`badge ${d.status === "ready" ? "status-completed" : d.status === "failed" ? "status-failed" : "status-awaiting_approval"}`}>{d.status}</span></td>
                  <td className="small">{size(d.bytes)}</td>
                  <td className="small">{d.chunks} · {d.tokens} tok</td>
                  <td><button className="secondary danger small-btn" onClick={() => remove([d.id])}>Remove</button></td>
                </tr>
                {open === d.id && (
                  <tr><td colSpan={6}>
                    <div className="chunks">
                      {chunks.map((c) => (
                        <div key={c.position} className="chunk">
                          <div className="small muted">#{c.position + 1} · {c.heading} · {c.tokens} tokens</div>
                          <pre>{c.text}</pre>
                        </div>
                      ))}
                    </div>
                  </td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
      {docs.length > 0 && <p className="small muted kb-hint">Drop more files anywhere in this panel to add them.</p>}
    </div>
  );
}

function Highlight({ text, query }) {
  const terms = [...new Set((query.toLowerCase().match(/[a-z0-9_]{3,}/g) || []))];
  if (!terms.length) return text;
  const pattern = new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return text.split(pattern).map((part, i) => (i % 2 ? <mark key={i}>{part}</mark> : part));
}

function ChunkPreview({ base, options }) {
  const docs = base.documents.filter((d) => d.has_text);
  const [source, setSource] = useState(docs[0] ? String(docs[0].id) : "paste");
  const [text, setText] = useState("");
  const [strategy, setStrategy] = useState(base.settings.strategy);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => setStrategy(base.settings.strategy), [base.settings.strategy]);
  const strategies = options?.strategies || {};
  const run = async () => {
    setBusy(true);
    setError("");
    try {
      setResult(await api.previewChunks(base.id, {
        ...(source === "paste" ? { text } : { doc_id: Number(source) }),
        settings: { strategy },
      }));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="kb-grid">
        <Select label="Source" value={source} onChange={setSource}
          options={[...docs.map((d) => [String(d.id), d.name]), ["paste", "Paste text…"]]} />
        <Select label="Strategy" value={strategy} onChange={setStrategy}
          options={Object.entries(strategies).map(([key, s]) => [key, s.name])} />
      </div>
      {source === "paste" && (
        <textarea rows={5} className="kb-paste-area" value={text} placeholder="Paste text to see how it would be chunked…"
          onChange={(e) => setText(e.target.value)} />
      )}
      <div className="actions lab-actions">
        <button onClick={run} disabled={busy || (source === "paste" && !text.trim())}>{busy ? "Chunking…" : "Preview chunks"}</button>
        <span className="small muted">Uses this base's size and overlap; nothing is saved{strategy === "semantic" ? " (semantic embeds each sentence)" : ""}.</span>
      </div>
      {error && <p className="error small">{error}</p>}
      {result && (
        <>
          <div className="lab-stats small">
            <span><b>{result.stats.chunks}</b> chunks</span>
            <span>avg <b>{result.stats.avg_tokens}</b> tok</span>
            <span>min {result.stats.min_tokens}</span>
            <span>max {result.stats.max_tokens}</span>
            <span>total {result.stats.total_tokens}</span>
          </div>
          {result.chunks.map((c) => (
            <div key={c.position} className={`chunk lab-chunk tone-${c.position % 2}`}>
              <div className="small muted">#{c.position + 1} · {c.heading} · {c.tokens} tokens</div>
              <pre>{c.text}</pre>
            </div>
          ))}
        </>
      )}
    </>
  );
}

function ChunkLab({ base, bases, selected, options }) {
  const [tab, setTab] = useState("chunks");
  return (
    <>
      <div className="tabs" role="tablist">
        {[["chunks", "Chunks"], ["retrieval", "Retrieval"]].map(([key, label]) => (
          <button key={key} role="tab" aria-selected={tab === key} className={tab === key ? "tab active" : "tab"}
            onClick={() => setTab(key)}>{label}</button>
        ))}
      </div>
      {tab === "chunks"
        ? (base ? <ChunkPreview key={base.id} base={base} options={options} /> : <p className="muted small">Select a knowledge base.</p>)
        : <Tester bases={bases} selected={selected} />}
    </>
  );
}

function Tester({ bases, selected }) {
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState([]);
  const [results, setResults] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => setScope(selected ? [selected] : []), [selected]);
  const run = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      setResults((await api.searchKnowledge(scope, query)).results);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <form onSubmit={run}>
        <div className="kb-scope">
          {bases.map((b) => (
            <label key={b.id} className="toggle small">
              <input type="checkbox" checked={scope.includes(b.id)}
                onChange={(e) => setScope(e.target.checked ? [...scope, b.id] : scope.filter((i) => i !== b.id))} />
              {b.name}
            </label>
          ))}
        </div>
        <div className="actions">
          <input className="grow" value={query} placeholder="Ask what an agent would look up…" onChange={(e) => setQuery(e.target.value)} />
          <button type="submit" disabled={busy || !query.trim() || !scope.length}>{busy ? "Searching…" : "Search"}</button>
        </div>
      </form>
      {error && <p className="error small">{error}</p>}
      {results?.length === 0 && <p className="muted small">No matching chunks.</p>}
      {results?.map((r, n) => (
        <div key={r.id} className="chunk">
          <div className="small">
            <b>[{n + 1}]</b> {r.heading} <span className="muted">· {r.knowledge_base}/{r.document}</span>
          </div>
          <div className="small muted">
            score {r.score} · {r.vector !== null ? `vector ${r.vector}` : "no vector match"} · {r.keyword ? "keyword match" : "no keyword match"} · {r.tokens} tokens
          </div>
          <pre><Highlight text={r.text} query={query} /></pre>
        </div>
      ))}
    </>
  );
}

function EmptyState({ onNew }) {
  return (
    <div className="empty">
      <div className="empty-icon">📚</div>
      <h3>Give your agents reference material</h3>
      <p className="small muted">Create a knowledge base, upload your conventions, API docs or runbooks, and attach it to agents on the Agents page.</p>
      <button onClick={onNew}>+ New knowledge base</button>
    </div>
  );
}

export default function Knowledge({ selected, onSelect: setSelected }) {
  const [bases, setBases] = useState([]);
  const [base, setBase] = useState(null);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [options, setOptions] = useState(null);
  useEffect(() => {
    api.knowledgeOptions().then(setOptions).catch(() => setOptions(null));
  }, []);

  const loadBases = () => api.knowledge().then((list) => {
    setBases(list);
    if (!list.length) setCreating(true);
  }).catch((e) => setError(e.message === "Not found."
    ? "The server is running an older AutoSDE without knowledge bases. Restart `autosde serve` and reload this page."
    : e.message));
  const selection = useRef(selected);
  selection.current = selected;
  const loadBase = (id) => (id ? api.knowledgeBase(id)
    .then((data) => { if (selection.current === id) setBase(data); })
    .catch((e) => { if (selection.current === id) setError(e.message); }) : setBase(null));
  useEffect(() => {
    loadBases();
  }, []);
  useEffect(() => {
    if (!selected && bases.length) setSelected(bases[0].id, { replace: true });
  }, [bases, selected]);
  useEffect(() => {
    let active = true;
    setBase(null);
    setError("");
    if (selected) api.knowledgeBase(selected)
      .then((data) => { if (active) setBase(data); })
      .catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [selected]);
  useEffect(() => {  // poll while documents are embedding
    if (!base?.documents.some((d) => d.status === "processing")) return undefined;
    const timer = setTimeout(() => (loadBase(base.id), loadBases()), 1500);
    return () => clearTimeout(timer);
  }, [base]);

  const refresh = () => (loadBase(selected), loadBases());

  return (
    <Workspace
      topLeft={<Panel title={`Knowledge bases (${bases.length})`}
        actions={!creating && <button onClick={() => setCreating(true)}>+ New</button>}>
        {error && <p className="error small">{error}</p>}
        <Bases bases={bases} selected={selected} onSelect={setSelected} creating={creating}
          setCreating={setCreating} onCreated={(id) => (loadBases(), setSelected(id))} />
      </Panel>}
      bottomLeft={<Panel title={base ? `Strategy · ${base.name}` : "Strategy"}>
        {base ? <Strategy key={base.id} base={base} options={options} onSaved={(b) => (setBase(b), loadBases())}
          onDeleted={() => {
            setBases((items) => items.filter((item) => item.id !== base.id));
            setSelected(null);
            setBase(null);
            loadBases();
          }} />
          : <EmptyState onNew={() => setCreating(true)} />}
      </Panel>}
      topRight={<Panel title={base ? `Documents · ${base.name}` : "Documents"}>
        {base ? <Documents key={base.id} base={base} onChanged={refresh} /> : <p className="muted small">Select a knowledge base to upload documents.</p>}
      </Panel>}
      bottomRight={<Panel title="Chunk lab">
        {bases.length ? <ChunkLab base={base} bases={bases} selected={selected} options={options} /> : <p className="muted small">Create a knowledge base first.</p>}
      </Panel>}
    />
  );
}
