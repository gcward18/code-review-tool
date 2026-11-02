"""The JSON API's routes, shared by the local server (web.py) and AWS Lambda (aws.py).

`dispatch(app, method, path, query, body, user=None)` returns a Response. Authentication
and request checks happen before it: the per-start token and same-origin checks locally,
the Cognito JWT authorizer in AWS.
"""

import json
import re
from dataclasses import dataclass, field

from autosde import agents, report
from autosde.app import ApiError

POST_LIMIT, UPLOAD_LIMIT = 100_000, 28_000_000


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: dict = field(default_factory=dict)


def json_response(status, value):
    return Response(status, json.dumps(value, default=str).encode())


def body_limit(path):
    return UPLOAD_LIMIT if path.endswith("/documents") else POST_LIMIT


def _prefixed(path, prefix):
    if path == prefix or path.startswith(prefix + "/"):
        return [p for p in path[len(prefix):].split("/") if p]
    return None


def dispatch(app, method, path, query=None, body=None, user=None):
    query, body = query or {}, body or {}
    try:
        if method == "GET":
            return _get(app, path, query, user)
        if method == "POST":
            return _post(app, path, body)
        raise ApiError(405, "Method not allowed.")
    except ApiError as error:
        return json_response(error.status, {"error": str(error)})


def _get(app, path, query, user):
    ok = lambda value: json_response(200, value)
    if path == "/api/session":
        session = {"model": app.settings.model, "cloud": app.cloud}
        if app.cloud:
            session["user"] = {"email": (user or {}).get("email"), "sub": (user or {}).get("sub")}
        else:
            session.update(token=app.token, root=str(app.browse_root))
        return ok(session)
    if path == "/api/workspaces":
        return ok(app.workspaces())
    if path == "/api/tree":
        return ok(app.tree(query.get("path")))
    if path == "/api/repo":
        return ok(app.repo(query.get("path")))
    if path == "/api/older":
        return ok(app.older(query.get("path"), query.get("from", "")))
    if path == "/api/models":
        return ok(app.models())
    if path == "/api/agents":
        return ok(agents.snapshot())
    if path == "/api/chat":
        return ok({"messages": app.store.chat(None)})
    if (parts := _prefixed(path, "/api/knowledge")) is not None:
        return ok(app.knowledge("GET", parts, {}))
    if (parts := _prefixed(path, "/api/remotes")) is not None:
        return ok(app.remotes("GET", parts, {}))
    if (parts := _prefixed(path, "/api/batches")) is not None:
        return ok(app.batches("GET", parts, {}, query))
    if path == "/api/agent-runs":
        return ok(app.store.agents())
    if path == "/api/projects":
        return ok(app.store.projects())
    if path == "/api/reviews":
        return ok(app.store.reviews(limit=50))
    if m := re.fullmatch(r"/api/projects/(\d+)/chat", path):
        return ok({"messages": app.store.chat(app._project_or_404(int(m[1]))["id"])})
    if m := re.fullmatch(r"/api/projects/(\d+)", path):
        return ok(app.project(int(m[1])))
    if m := re.fullmatch(r"/api/reviews/(\d+)\.sarif", path):
        review = app.review(int(m[1]))
        name = re.sub(r"[^A-Za-z0-9._-]", "_", review["project"])
        return Response(200, report.as_json(report.sarif(review)).encode(), "application/json",
                        {"Content-Disposition": f'attachment; filename="{name}-review-{review["number"]}.sarif"'})
    if m := re.fullmatch(r"/api/reviews/(\d+)/files", path):
        return ok(app.review_files(int(m[1]), query.get("path")))
    if m := re.fullmatch(r"/api/reviews/(\d+)", path):
        return ok(app.review(int(m[1])))
    raise ApiError(404, "Not found.")


def _post(app, path, body):
    ok = lambda value: json_response(200, value)
    if path == "/api/reviews":
        return json_response(202, app.submit(body))
    if path == "/api/chat":
        return ok(app.chat(None, body))
    if path == "/api/chat/clear":
        app.store.clear_chat(None)
        return ok({"messages": []})
    if (parts := _prefixed(path, "/api/knowledge")) is not None:
        return ok(app.knowledge("POST", parts, body))
    if (parts := _prefixed(path, "/api/remotes")) is not None:
        return ok(app.remotes("POST", parts, body))
    if (parts := _prefixed(path, "/api/batches")) is not None:
        return ok(app.batches("POST", parts, body))
    if m := re.fullmatch(r"/api/projects/(\d+)/chat", path):
        return ok(app.chat(int(m[1]), body))
    if m := re.fullmatch(r"/api/projects/(\d+)/chat/clear", path):
        app.store.clear_chat(app._project_or_404(int(m[1]))["id"])
        return ok({"messages": []})
    if path == "/api/agents":
        try:
            return ok(agents.update(body))
        except agents.ConfigError as error:
            raise ApiError(400, str(error)) from None
    if m := re.fullmatch(r"/api/reviews/(\d+)/(approve|cancel)", path):
        return ok(app.decide(int(m[1]), body, approved=m[2] == "approve"))
    if m := re.fullmatch(r"/api/reviews/(\d+)/estimate", path):
        return ok(app.estimate(int(m[1]), body.get("files") or []))
    raise ApiError(404, "Not found.")
