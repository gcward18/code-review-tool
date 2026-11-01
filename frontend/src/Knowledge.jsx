import { Fragment, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import Workspace, { Panel } from "./Workspace.jsx";

const ACCEPT = ".txt,.md,.markdown,.rst,.adoc,.json,.csv,.tsv,.yaml,.yml,.toml,.ini,.cfg,.xml,.html,.htm,"
  + ".docx,.log,.sql,.py,.js,.jsx,.ts,.tsx,.java,.go,.cs,.rb,.rs,.c,.h,.cpp,.hpp,.kt,.swift,.php,.sh,.tf,.proto,.graphql";
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

function Strategy({ base, onSaved, onDeleted }) {
  const [draft, setDraft] = useState(base.settings);
  const [status, setStatus] = useState("");
  useEffect(() => setDraft(base.settings), [base.id]);
  const field = (key, label, hint, props) => (
    <label className="field">
      <span className="small">{label} <span className="muted">· {hint}</span></span>
      <input type="number" value={draft[key]} {...props}
        onChange={(e) => setDraft({ ...draft, [key]: props.step ? parseFloat(e.target.value) : parseInt(e.target.value, 10) })} />
    </label>
  );
  const save = async () => {
    try {
      onSaved(await api.updateKnowledge(base.id, { settings: draft }));
      setStatus("Saved. New settings apply to documents uploaded from now on.");
    } catch (e) {
      setStatus(e.message);
    }
  };
  const dirty = JSON.stringify(draft) !== JSON.stringify(base.settings);
  return (
    <>
      {base.description && <p className="small muted">{base.description}</p>}
      <h4 className="kb-h">Chunking</h4>
      <div className="kb-grid">
        {field("chunk_tokens", "Chunk size", "tokens per chunk", { min: 100, max: 2000, step: undefined })}
        {field("overlap", "Overlap", "share carried into the next chunk", { min: 0, max: 0.5, step: 0.05 })}
      </div>
      <h4 className="kb-h">Embedding</h4>
      <div className="kb-grid">
        <label className="field">
          <span className="small">Model <span className="muted">· fixed once documents exist</span></span>
          <input value={draft.embed_model} disabled={base.chunks > 0}
            onChange={(e) => setDraft({ ...draft, embed_model: e.target.value })} />
        </label>
        {field("dimensions", "Dimensions", "smaller is faster", { min: 64, max: 3072, disabled: base.chunks > 0 })}
      </div>
      <h4 className="kb-h">Retrieval</h4>
      <div className="kb-grid">
        {field("top_k", "Top K", "chunks given to an agent", { min: 1, max: 20 })}
        {field("context_tokens", "Context budget", "max tokens of reference", { min: 200, max: 8000 })}
      </div>
      <p className="small muted">
        Hybrid search: vector similarity and BM25 keyword ranks fused with reciprocal rank fusion, at most 3 chunks per document.
      </p>
      <div className="actions">
        <button disabled={!dirty} onClick={save}>Save strategy</button>
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
      if (file.size > 5e6) setErrors((list) => [...list, `${file.name}: over 5 MB.`]);
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
          <span className="small muted">Markdown, text, code, HTML, JSON, CSV or .docx · up to 5 MB each · scanned by gitleaks before embedding</span>
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
          <pre>{r.text}</pre>
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
        {base ? <Strategy key={base.id} base={base} onSaved={(b) => (setBase(b), loadBases())}
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
      bottomRight={<Panel title="Retrieval tester">
        {bases.length ? <Tester bases={bases} selected={selected} /> : <p className="muted small">Create a knowledge base first.</p>}
      </Panel>}
    />
  );
}
