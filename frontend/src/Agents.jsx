import { useEffect, useState } from "react";
import { api } from "./api.js";

const CORE = ["coordinator", "aggregator"];
const FIXED = [...CORE, "general"];
const PROMPTS = [
  ["coordinator", "Coordinator", "Plans which specialist reviews which file and flags low-value files."],
  ["specialist", "Specialist", "Shared by every specialist. Keep {agent} and {focus}."],
  ["aggregator", "Aggregator", "Removes duplicates and weak findings."],
];

const clone = (value) => JSON.parse(JSON.stringify(value));

export default function Agents({ defaultModel }) {
  const [saved, setSaved] = useState(null);
  const [draft, setDraft] = useState(null);
  const [status, setStatus] = useState({ kind: "", text: "" });
  const [newName, setNewName] = useState("");

  useEffect(() => {
    api.agents().then((data) => (setSaved(data), setDraft(clone(data))))
      .catch((e) => setStatus({ kind: "error", text: e.message }));
  }, []);

  if (!draft) return <div className={`card ${status.kind || "muted"}`}>{status.text || "Loading…"}</div>;

  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const edit = (fn) => setDraft((d) => { const next = clone(d); fn(next); return next; });
  const agent = (name, key, value) => edit((d) => { d.config.agents[name][key] = value; });
  const specialists = Object.keys(draft.config.agents).filter((n) => !CORE.includes(n));

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
  };

  const modelField = (name) => (
    <label className="field">
      <span className="small muted">Model</span>
      <input value={draft.config.agents[name].model || ""} placeholder={defaultModel || "default"}
        onChange={(e) => agent(name, "model", e.target.value || null)} />
    </label>
  );

  return (
    <>
      <div className="card sticky">
        <div className="actions">
          <h2 className="grow">Agents</h2>
          <button className="secondary" disabled={!dirty} onClick={() => (setDraft(clone(saved)), setStatus({}))}>Discard</button>
          <button disabled={!dirty} onClick={save}>Save</button>
        </div>
        <p className={`small ${status.kind || "muted"}`}>
          {status.text || (dirty ? "Unsaved changes." : "Saved in autosde/prompts/. Changes apply to the next review.")}
        </p>
      </div>

      <div className="card">
        <h3>Team</h3>
        <div className="agent-grid">
          {CORE.map((name) => (
            <div key={name} className="agent">
              <strong>{name}</strong>
              {modelField(name)}
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <h3>Specialists</h3>
        <p className="small muted">Files go to the specialist whose extension matches; everything else goes to general.</p>
        <div className="agent-grid">
          {specialists.map((name) => {
            const item = draft.config.agents[name];
            return (
              <div key={name} className={`agent ${item.enabled ? "" : "off"}`}>
                <label className="toggle">
                  <input type="checkbox" checked={item.enabled} disabled={FIXED.includes(name)}
                    onChange={(e) => agent(name, "enabled", e.target.checked)} />
                  <strong>{name}</strong>
                  {name === "general" && <span className="badge">catch-all</span>}
                </label>
                {name !== "general" && (
                  <label className="field">
                    <span className="small muted">Extensions</span>
                    <input value={item.extensions.join(", ")} placeholder=".rs, .toml"
                      onChange={(e) => agent(name, "extensions",
                        e.target.value.split(/[\s,]+/).filter(Boolean))} />
                  </label>
                )}
                {modelField(name)}
                <label className="field">
                  <span className="small muted">Focus</span>
                  <textarea rows={4} value={draft.focus[name] || ""}
                    placeholder="What this specialist should look for."
                    onChange={(e) => edit((d) => { d.focus[name] = e.target.value; })} />
                </label>
              </div>
            );
          })}
        </div>
        <form className="actions add" onSubmit={add}>
          <input value={newName} placeholder="new specialist, e.g. rust" onChange={(e) => setNewName(e.target.value)} />
          <button className="secondary" type="submit">Add specialist</button>
        </form>
      </div>

      <div className="card">
        <h3>System prompts</h3>
        {PROMPTS.map(([kind, title, hint]) => (
          <details key={kind} className="prompt">
            <summary>{title} <span className="small muted">— {hint}</span></summary>
            <textarea rows={14} className="mono" value={draft.prompts[kind]}
              onChange={(e) => edit((d) => { d.prompts[kind] = e.target.value; })} />
          </details>
        ))}
      </div>
    </>
  );
}
