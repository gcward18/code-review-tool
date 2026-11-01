"""Chat agent: answers questions about a project's review history.

It sees reviews and findings from the database (no source code beyond what findings
quote) plus the recent conversation. The whole outbound prompt is gitleaks-scanned.
"""

import json

from autosde.agents import load_config, read_prompt

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


def ask(store, model, project, question, *, scan, config=None):
    question = (question or "").strip()
    if not question or len(question) > 4000:
        raise ChatError("Ask a question under 4,000 characters.")
    config = config or load_config()
    turns = store.chat(project["id"])[-MAX_TURNS:]
    payload = json.dumps({
        "project": {"name": project["name"]},
        "reviews": _history(store, project["id"]),
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
    store.add_chat(project["id"], "user", question)
    store.add_chat(project["id"], "assistant", answer, model=used,
                   input_tokens=reply.input_tokens, output_tokens=reply.output_tokens)
    return store.chat(project["id"])
