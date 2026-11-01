"""Optional LangSmith tracing of agent calls, for testing and validating the team.

Off unless `langsmith.enabled` is set in prompts/agents.json and LANGSMITH_API_KEY is
in the environment. Each review becomes one trace: a parent run with a child `llm`
run per agent call carrying the full prompt and reply. Every payload goes through
the gitleaks scan first; a hit replaces that text with a marker. Delivery happens on
a background thread and failures never affect a review.
"""

import json
import os
import ssl
import sys
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from autosde.agents import load_config

ENDPOINTS = ("https://api.smith.langchain.com", "https://eu.api.smith.langchain.com")
WITHHELD = "[withheld: gitleaks flagged possible secret]"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _stamp(seconds):
    return datetime.fromtimestamp(seconds, UTC)


def _order(seconds, run_id):
    return _stamp(seconds).strftime("%Y%m%dT%H%M%S%fZ") + run_id


_WORKSPACES = {}  # endpoint -> workspace id, looked up once per process


def _opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )


def workspace(api_key, endpoint, opener=None):
    """LangSmith refuses runs without a workspace header; use the key's only workspace."""
    if endpoint not in _WORKSPACES:
        request = urllib.request.Request(endpoint + "/api/v1/workspaces",
                                         headers={"x-api-key": api_key})
        try:
            with (opener or _opener()).open(request, timeout=5) as response:
                found = json.loads(response.read(1_000_000))
        except Exception:
            raise RuntimeError("LangSmith workspaces could not be listed") from None
        if len(found) != 1:
            raise RuntimeError("Key has several LangSmith workspaces; set LANGSMITH_WORKSPACE_ID")
        _WORKSPACES[endpoint] = found[0]["id"]
    return _WORKSPACES[endpoint]


def _warn(message):
    print(f"autosde: LangSmith {message}", file=sys.stderr, flush=True)


class Tracer:
    """Posts runs to LangSmith. The key only ever appears in request headers."""

    def __init__(self, api_key, *, project, endpoint=ENDPOINTS[0], workspace_id=None,
                 scan=None, opener=None, timeout=5):
        if endpoint not in ENDPOINTS:
            raise ValueError("Unknown LangSmith endpoint")
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        if workspace_id:
            self._headers["x-tenant-id"] = workspace_id
        self.project, self.endpoint, self.timeout = project, endpoint, timeout
        self.scan = scan or (lambda text: [])
        self.errors = []  # error class names / HTTP codes, never messages
        self._opener = opener or _opener()
        self._queue = ThreadPoolExecutor(max_workers=1)  # keeps parent before children

    def __repr__(self):
        return f"Tracer(project={self.project!r}, api_key=<redacted>)"

    def _clean(self, text):
        try:
            return WITHHELD if self.scan(text) else text
        except Exception:
            return WITHHELD

    def _send(self, method, path, body):
        def post():
            request = urllib.request.Request(self.endpoint + path, method=method,
                                             data=json.dumps(body).encode(), headers=self._headers)
            try:
                with self._opener.open(request, timeout=self.timeout):
                    pass
            except urllib.error.HTTPError as error:
                self.errors.append(f"HTTP {error.code}")
            except Exception as error:
                self.errors.append(type(error).__name__)

        self._queue.submit(post)

    def start(self, name, metadata):
        run_id, now = str(uuid.uuid4()), time.time()
        root = {"id": run_id, "order": _order(now, run_id)}
        self._send("POST", "/runs", {
            "id": run_id, "trace_id": run_id, "dotted_order": root["order"], "name": name,
            "run_type": "chain", "start_time": _stamp(now).isoformat(), "inputs": metadata,
            "session_name": self.project, "tags": ["autosde"], "extra": {"metadata": metadata},
        })
        return root

    def call(self, root, *, agent, model, system, prompt, reply, started, ended):
        run_id = str(uuid.uuid4())
        usage = {"input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                 "total_tokens": reply.input_tokens + reply.output_tokens}
        self._send("POST", "/runs", {
            "id": run_id, "trace_id": root["id"], "parent_run_id": root["id"],
            "dotted_order": root["order"] + "." + _order(started, run_id),
            "name": agent, "run_type": "llm",
            "start_time": _stamp(started).isoformat(), "end_time": _stamp(ended).isoformat(),
            "inputs": {"messages": [{"role": "system", "content": self._clean(system)},
                                    {"role": "user", "content": self._clean(prompt)}]},
            "outputs": {"content": self._clean(reply.text or ""), "usage_metadata": usage},
            "error": None if reply.ok else reply.error,
            "session_name": self.project, "tags": ["autosde", agent],
            "extra": {"metadata": {"ls_provider": "openai", "ls_model_name": model,
                                   "agent": agent}},
        })

    def finish(self, root, outputs):
        self._send("PATCH", f"/runs/{root['id']}",
                   {"end_time": _stamp(time.time()).isoformat(), "outputs": outputs})

    def flush(self):
        self._queue.shutdown(wait=True)


class TracedModel:
    """Wraps a model client; traces each call of an agent whose `trace` is on."""

    def __init__(self, inner, tracer, root, config):
        self.inner, self.tracer, self.root, self.config = inner, tracer, root, config
        self.model = inner.model

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete(self, *, role, system, prompt, schema, max_output_tokens, model=None):
        started = time.time()
        reply = self.inner.complete(role=role, system=system, prompt=prompt, schema=schema,
                                    max_output_tokens=max_output_tokens, model=model)
        if self.config["agents"].get(role, {}).get("trace", True):
            try:
                self.tracer.call(self.root, agent=role, model=model or self.model, system=system,
                                 prompt=prompt, reply=reply, started=started, ended=time.time())
            except Exception:
                pass
        return reply

    def finish(self, outputs):
        try:
            self.tracer.finish(self.root, outputs)
            self.tracer.flush()
        except Exception:
            pass
        if self.tracer.errors:
            _warn("trace incomplete: " + ", ".join(sorted(set(self.tracer.errors))))


def traced(model, *, review_id, name, scan, config=None, environ=os.environ, opener=None):
    """Return `model` wrapped for LangSmith when enabled and a key is set, else unchanged."""
    config = config or load_config()
    settings = config.get("langsmith") or {}
    key = environ.get("LANGSMITH_API_KEY")
    if not settings.get("enabled") or not key:
        return model
    try:
        workspace_id = environ.get("LANGSMITH_WORKSPACE_ID") or workspace(
            key, settings["endpoint"], opener)
    except RuntimeError as error:
        _warn(f"tracing skipped: {error}")
        return model
    tracer = Tracer(key, project=settings["project"], endpoint=settings["endpoint"],
                    workspace_id=workspace_id, scan=scan, opener=opener)
    root = tracer.start(name, {"review_id": review_id, "model": model.model})
    return TracedModel(model, tracer, root, config)


def finish(model, outputs):
    if isinstance(model, TracedModel):
        model.finish(outputs)
