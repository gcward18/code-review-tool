import { useEffect, useState } from "react";
import { api } from "./api.js";
import Workspace, { Panel } from "./Workspace.jsx";

const CORE = ["coordinator", "aggregator", "chat"];
const FIXED = [...CORE, "general"];
const PROMPTS = [
  ["coordinator", "Coordinator", "Plans which specialist reviews which file and flags low-value files."],
  ["specialist", "Specialist", "Shared by every specialist. Keep {agent} and {focus}."],
  ["aggregator", "Aggregator", "Removes duplicates and weak findings."],
  ["chat", "Chat", "Answers questions about a project's review history."],
  ["chunk_summary", "Chunk summaries", "Writes the one-line summary for each knowledge base chunk."],
];

const clone = (value) => JSON.parse(JSON.stringify(value));

export default function Agents({ defaultModel, selected = "python", onSelect }) {
  const [saved, setSaved] = useState(null);
  const [draft, setDraft] = useState(null);
  const [status, setStatus] = useState({ kind: "", text: "" });
  const [newName, setNewName] = useState("");
  const [promptKind, setPromptKind] = useState("coordinator");
  const [models, setModels] = useState({});
  const [custom, setCustom] = useState({});
  const [runs, setRuns] = useState(null);
  const [bases, setBases] = useState([]);

  useEffect(() => {
    api.agents().then((data) => (setSaved(data), setDraft(clone(data))))
      .catch((e) => setStatus({ kind: "error", text: e.message }));
    api.models().then(setModels).catch(() => setModels({ source: "unavailable" }));
    api.agentRuns().then(setRuns).catch(() => setRuns([]));
    api.knowledge().then(setBases).catch(() => setBases([]));
  }, []);

  if (!draft) return <div className={`card ${status.kind || "muted"}`}>{status.text || "Loading…"}</div>;

  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const edit = (fn) => setDraft((d) => { const next = clone(d); fn(next); return next; });
  const agent = (name, key, value) => edit((d) => { d.config.agents[name][key] = value; });
  const specialists = Object.keys(draft.config.agents).filter((n) => !CORE.includes(n));
  const current = draft.config.agents[selected] ? selected : "general";

  const save = async () => {
    setStatus({ kind: "muted", text: "Saving…" });
    try {
      const data = await api.saveAgents(draft);
      setSaved(data);
      setDraft(clone(data));
      setStatus({ kind: "ok", text: "Saved. The next review uses these settings." });
    } catch (e) {
      setStatus({ kind: "error", text: e.message });
    }
  };

  const add = (e) => {
    e.preventDefault();
    const name = newName.trim().toLowerCase();
    if (!/^[a-z][a-z0-9_]{0,31}$/.test(name) || draft.config.agents[name]) {
      return setStatus({ kind: "error", text: "Use a new lowercase name (letters, digits, _)." });
    }
    edit((d) => {
      d.config.agents[name] = { enabled: true, extensions: [], model: null };
      d.focus[name] = "";
    });
    setNewName("");
    onSelect(name);
  };

  const modelField = (name) => {
    const value = draft.config.agents[name].model || "";
    const options = [...new Set([...(models.models || []), ...(value ? [value] : [])])];
    const isCustom = custom[name] || (value && !options.includes(value));
    return (
      <label className="field">
        <span className="small muted">Model</span>
        <select value={isCustom ? "__custom" : value}
          onChange={(e) => {
            const next = e.target.value;
            setCustom((c) => ({ ...c, [name]: next === "__custom" }));
            if (next !== "__custom") agent(name, "model", next || null);
          }}>
          <option value="">Default ({defaultModel || "settings"})</option>
          {options.map((m) => <option key={m} value={m}>{m}</option>)}
          <option value="__custom">Other…</option>
        </select>
        {isCustom && (
          <input value={value} placeholder="model id" autoFocus
            onChange={(e) => agent(name, "model", e.target.value || null)} />
        )}
        {models.source === "unavailable" && (
          <span className="small muted">Couldn't list models from OpenAI; choose Other… to type one.</span>
        )}
      </label>
    );
  };

  const actions = (
    <>
      <span className={`small save-note ${status.kind || "muted"}`}>
        {status.text || (dirty ? "Unsaved changes" : "Saved")}
      </span>
      <button className="secondary" disabled={!dirty} onClick={() => (setDraft(clone(saved)), setStatus({}))}>Discard</button>
      <button disabled={!dirty} onClick={save}>Save</button>
    </>
  );
  const prompt = PROMPTS.find(([kind]) => kind === promptKind) || PROMPTS[0];
  return (
    <Workspace
      topLeft={<Panel title="Agent" actions={actions}>
        {!draft.config.agents[selected] && <p className="error small">Agent “{selected}” was not found. Choose an agent below.</p>}
        <div className="actions picker">
          <label className="field grow">
            <span className="small muted">Agent</span>
            <select value={current} onChange={(e) => onSelect(e.target.value)}>
              <optgroup label="Team">
                {CORE.map((name) => <option key={name} value={name}>{name}</option>)}
              </optgroup>
              <optgroup label="Specialists">
                {specialists.map((name) => (
                  <option key={name} value={name}>
                    {name}{draft.config.agents[name].enabled ? "" : " (off)"}
                  </option>
                ))}
              </optgroup>
            </select>
          </label>
          <form className="actions add" onSubmit={add}>
            <input value={newName} placeholder="new specialist, e.g. rust" onChange={(e) => setNewName(e.target.value)} />
            <button className="secondary" type="submit">Add</button>
          </form>
        </div>
        {(() => {
          const name = current;
          const item = draft.config.agents[name];
          const core = CORE.includes(name);
          return (
            <div className={`agent ${item.enabled ? "" : "off"}`}>
              {core ? (
                <strong>{name}</strong>
              ) : (
                <label className="toggle">
                  <input type="checkbox" checked={item.enabled} disabled={FIXED.includes(name)}
                    onChange={(e) => agent(name, "enabled", e.target.checked)} />
                  <strong>{name}</strong>
                  {name === "general" && <span className="badge">catch-all, always on</span>}
                </label>
              )}
              {!core && name !== "general" && (
                <label className="field">
                  <span className="small muted">Extensions</span>
                  <input value={item.extensions.join(", ")} placeholder=".rs, .toml"
                    onChange={(e) => agent(name, "extensions",
                      e.target.value.split(/[\s,]+/).filter(Boolean))} />
                </label>
              )}
              {modelField(name)}
              <label className="toggle small">
                <input type="checkbox" checked={item.trace !== false}
                  onChange={(e) => agent(name, "trace", e.target.checked)} />
                Trace this agent to LangSmith
              </label>
              {(!core || name === "chat") && (
                <div className="field">
                  <span className="small muted">Knowledge bases</span>
                  {bases.length === 0 ? (
                    <span className="small muted">None yet; create them on the Knowledge page.</span>
                  ) : (
                    <div className="kb-scope">
                      {bases.map((b) => (
                        <label key={b.id} className="toggle small">
                          <input type="checkbox" checked={(item.knowledge || []).includes(b.id)}
                            onChange={(e) => agent(name, "knowledge", e.target.checked
                              ? [...(item.knowledge || []), b.id]
                              : (item.knowledge || []).filter((i) => i !== b.id))} />
                          {b.name}
                        </label>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {!core && (
                <label className="field">
                  <span className="small muted">Focus</span>
                  <textarea rows={6} value={draft.focus[name] || ""}
                    placeholder="What this specialist should look for."
                    onChange={(e) => edit((d) => { d.focus[name] = e.target.value; })} />
                </label>
              )}
              {core && <p className="small muted">Edit its prompt in the System prompts panel.</p>}
            </div>
          );
        })()}
      </Panel>}
      bottomLeft={<Panel title="System prompts" className="prompt-panel">
        <label className="field">
          <span className="small muted">Prompt · {prompt[2]}</span>
          <select value={prompt[0]} onChange={(e) => setPromptKind(e.target.value)}>
            {PROMPTS.map(([kind, title]) => <option key={kind} value={kind}>{title}</option>)}
          </select>
        </label>
        <textarea className="mono prompt-editor" value={draft.prompts[prompt[0]] || ""}
          onChange={(e) => edit((d) => { d.prompts[prompt[0]] = e.target.value; })} />
        <p className="small muted">Saved with Save above, in autosde/prompts/. Changes apply to the next review.</p>
      </Panel>}
      topRight={<Panel title="LangSmith tracing">
        <label className="toggle">
          <input type="checkbox" checked={draft.config.langsmith.enabled}
            onChange={(e) => edit((d) => { d.config.langsmith.enabled = e.target.checked; })} />
          Send each review to LangSmith as a trace
        </label>
        <div className="range">
          <label className="field">
            <span className="small muted">Project</span>
            <input value={draft.config.langsmith.project}
              onChange={(e) => edit((d) => { d.config.langsmith.project = e.target.value; })} />
          </label>
          <label className="field">
            <span className="small muted">Region</span>
            <select value={draft.config.langsmith.endpoint}
              onChange={(e) => edit((d) => { d.config.langsmith.endpoint = e.target.value; })}>
              <option value="https://api.smith.langchain.com">US</option>
              <option value="https://eu.api.smith.langchain.com">EU</option>
            </select>
          </label>
        </div>
        <p className="small">
          {draft.langsmith_key
            ? <span className="ok">LANGSMITH_API_KEY is set.</span>
            : <span className="warn-text">LANGSMITH_API_KEY is not set. Export it before starting <code>autosde serve</code>.</span>}
        </p>
        <p className="small muted">
          Traces include each traced agent's full prompt, the reviewed code, and its reply.
          Text that gitleaks flags is withheld.
        </p>
      </Panel>}
      bottomRight={<Panel title={`Agent runs${runs ? ` (${runs.reduce((n, r) => n + r.runs, 0)})` : ""}`}>
        {runs?.length ? (
          <table>
            <thead><tr><th>Project</th><th>Role</th><th>Agent</th><th>Runs</th><th>OK</th><th>Failed</th><th>Tokens</th><th>Kept</th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.project + r.role + r.agent}>
                  <td>{r.project}</td><td>{r.role}</td><td>{r.agent}</td><td>{r.runs}</td>
                  <td>{r.succeeded}</td><td className={r.failed ? "error" : ""}>{r.failed}</td>
                  <td>{r.input_tokens}/{r.output_tokens}</td><td>{r.findings_kept}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted small">{runs ? "No agent runs yet." : "Loading…"}</p>}
      </Panel>}
    />
  );
}
