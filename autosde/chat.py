"""Chat agent: answers questions about a project's review history.

It sees reviews and findings from the database (no source code beyond what findings
quote) plus the recent conversation. The whole outbound prompt is gitleaks-scanned.
"""

import json

from autosde.agents import load_config, read_prompt
from autosde.knowledge import Knowledge

ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}
MAX_HISTORY_CHARS = 60_000
MAX_TURNS = 20


class ChatError(RuntimeError):
    pass


def _history(store, project_id):
    """Newest reviews first, dropping the oldest until the history fits."""
    reviews = store.history(project_id)
    while reviews and len(json.dumps(reviews)) > MAX_HISTORY_CHARS:
        reviews.pop()
    return reviews


def _all_projects(store, repositories):
    """Every repository on the machine: git metadata, plus review history where reviewed."""
    reviewed = {p["path"]: p for p in store.projects()}
    projects, budget = [], MAX_HISTORY_CHARS
    for repo in repositories:
        entry = {"name": repo["name"], "path": repo["path"], **repo.get("git", {})}
        known = reviewed.get(repo["path"])
        if known:
            entry["reviews"] = [
                {"id": r["id"], "range": r["range_spec"], "status": r["status"],
                 "summary": r["summary"],
                 "kept_findings": [f"{f['file']}:{f['start_line']} {f['severity']} {f['title']}"
                                   for f in r["findings"] if f["kept"]]}
                for r in store.history(known["id"], limit=10)
            ]
        size = len(json.dumps(entry))
        if size > budget:
            entry = {"name": repo["name"], "path": repo["path"], "omitted": "history too large"}
            size = len(json.dumps(entry))
        budget -= size
        projects.append(entry)
    return projects


def ask(store, model, project, question, *, scan, config=None, repositories=None):
    """`project` None asks about every repository in `repositories` (the Home chat)."""
    question = (question or "").strip()
    if not question or len(question) > 4000:
        raise ChatError("Ask a question under 4,000 characters.")
    config = config or load_config()
    key = project["id"] if project else None
    turns = store.chat(key)[-MAX_TURNS:]
    context = (
        {"scope": "project", "project": {"name": project["name"]},
         "reviews": _history(store, project["id"])}
        if project else
        {"scope": "all projects", "projects": _all_projects(store, repositories or [])}
    )
    kb_ids = config["agents"].get("chat", {}).get("knowledge") or []
    if kb_ids and hasattr(model, "embed"):
        try:
            found = Knowledge(store.db, embed=model.embed, scan=scan).search(kb_ids, question)
            context["reference"] = [
                {"n": n, "source": f"{r['knowledge_base']}/{r['document']}",
                 "heading": r["heading"], "text": r["text"]} for n, r in enumerate(found, 1)]
        except Exception:
            pass  # answer without reference material
    payload = json.dumps({
        **context,
        "conversation": [{"role": t["role"], "content": t["content"]} for t in turns],
        "question": question,
    })
    if scan(payload):
        raise ChatError("Gitleaks flagged a possible secret in the question or history; not sent.")
    override = config["agents"].get("chat", {}).get("model")
    reply = model.complete(role="chat", system=read_prompt("chat"), prompt=payload,
                           schema=ANSWER_SCHEMA, max_output_tokens=2000, model=override)
    if not reply.ok:
        raise ChatError(f"The chat agent failed ({reply.error}).")
    try:
        answer = json.loads(reply.text)["answer"]
    except (ValueError, KeyError, TypeError):
        raise ChatError("The chat agent replied in an unexpected format.") from None
    used = override or getattr(model, "model", None)
    store.add_chat(key, "user", question)
    store.add_chat(key, "assistant", answer, model=used,
                   input_tokens=reply.input_tokens, output_tokens=reply.output_tokens)
    return store.chat(key)
