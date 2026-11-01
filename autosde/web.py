"""Local web app: a JSON API plus the built React frontend (autosde/static).

Binds 127.0.0.1 only. Every POST must be same-origin JSON carrying the
per-start token, so other websites cannot trigger paid reviews.
"""

import base64
import binascii
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from autosde import agents, chat, git, report
from autosde.knowledge import Knowledge, KnowledgeError
from autosde.llm import OpenAIModel
from autosde.review import ReviewError, approve, cancel, prepare
from autosde.scan import ScanUnavailable, scanner

STATIC = Path(__file__).parent / "static"
# Never listed or searched: system data and dependency folders.
SKIP = {"Library", "node_modules", "__pycache__", "venv", "Applications"}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, store, settings, *, model_factory=None, port=8765, browse_root=None):
        self.store, self.settings, self.port = store, settings, port
        self.token = secrets.token_urlsafe(24)
        self.model_factory = model_factory or (lambda: OpenAIModel.from_settings(settings))
        # The folder tree never leaves this root (your home folder by default).
        self.browse_root = Path(browse_root or Path.home()).resolve()
        # Plans awaiting approval; their file contents exist only here, in memory.
        self.pending, self._lock = {}, threading.Lock()
        self.planning = set()  # ids this process is still planning
        self._repos = None  # (found_at, [paths]) from the last repository walk
        self._models = None  # cached model list; fetched once per start

    def models(self):
        """Models for the Agents page: the key's text models, or what is configured."""
        if self._models is None:
            try:
                self._models = {"models": self.model_factory().models(), "source": "openai"}
            except Exception:
                return {"default": self.settings.model, "models": [], "source": "unavailable"}
        return {"default": self.settings.model, **self._models}

    # --- folders and refs ---------------------------------------------------

    def _inside(self, raw):
        """Resolve a path; None when it is outside the browse root or not a folder."""
        try:
            path = Path(raw).expanduser().resolve() if raw else self.browse_root
        except (OSError, RuntimeError, ValueError):
            return None
        if path != self.browse_root and self.browse_root not in path.parents:
            return None
        return path if path.is_dir() else None

    def _repo(self, raw):
        path = self._inside(raw)
        if path is None:
            raise ApiError(400, "Choose a folder inside your home folder.")
        try:
            root = git.repo_root(path)
        except git.GitError:
            raise ApiError(400, "That folder is not a git repository.") from None
        if self._inside(str(root)) is None:
            raise ApiError(400, "That repository is outside your home folder.")
        return root

    @staticmethod
    def _readable(path):
        """False for folders macOS has not approved for this app (or that we cannot list)."""
        try:
            with os.scandir(path):
                return True
        except OSError:
            return False

    def repositories(self):
        """(repositories, blocked folders) under the browse root, from a bounded walk.

        Blocked folders are ones macOS has not approved for this app. Cached briefly.
        """
        if self._repos and time.monotonic() - self._repos[0] < 60:
            return self._repos[1]
        found, blocked, deadline = [], [], time.monotonic() + 5
        stack = [(self.browse_root, 0)]
        while stack and time.monotonic() < deadline and len(found) < 1000:
            folder, depth = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    children = [e for e in entries if e.is_dir(follow_symlinks=False)]
            except PermissionError:
                blocked.append(str(folder))
                continue
            except OSError:
                continue
            if any(e.name == ".git" for e in children):
                found.append(str(folder))
                continue
            if depth < 5:
                stack += [(Path(e.path), depth + 1) for e in children
                          if not e.name.startswith(".") and e.name not in SKIP]
        result = (sorted(found, key=str.lower), sorted(blocked, key=str.lower))
        self._repos = (time.monotonic(), result)
        return result

    def workspaces(self):
        """Recent (reviewed) projects, other available repositories, and blocked folders."""
        found, blocked = self.repositories()
        recent, seen = [], set()
        for p in self.store.projects():
            if self._inside(p["path"]) is None:
                continue
            entry = {"name": p["name"], "path": p["path"], "project_id": p["id"],
                     "reviews": p["reviews"], "last_review": p["last_review"]}
            seen.add(str(Path(p["path"]).resolve()))
            if self._readable(p["path"]):
                recent.append(entry)
            else:
                blocked.append(p["path"])
        available = [{"name": Path(path).name, "path": path} for path in found if path not in seen]
        available.sort(key=lambda r: r["name"].lower())
        return {
            "recent": recent,
            "available": available,
            "blocked": [{"name": Path(path).name, "path": path}
                        for path in sorted(set(blocked), key=str.lower)],
        }

    def tree(self, raw):
        path = self._inside(raw)
        if path is None:
            raise ApiError(400, "Folder outside your home folder.")
        try:
            folders = sorted(
                (p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")
                 and p.name not in SKIP and self._readable(p)),
                key=lambda p: p.name.lower(),
            )[:500]
        except PermissionError:
            raise ApiError(403, "macOS blocked access to this folder. Allow it in System Settings → "
                                "Privacy & Security → Files and Folders.") from None
        return {
            "path": str(path),
            "root": str(self.browse_root),
            "entries": [{"name": p.name, "path": str(p), "repo": (p / ".git").exists()} for p in folders],
        }

    def repo(self, raw):
        """From choices: HEAD, branches and recent commits. Default HEAD."""
        root = self._repo(raw)
        branches, commits = git.refs(root)
        return {
            "path": str(root),
            "name": root.name,
            "project_id": (self.store.find_project(str(root)) or {}).get("id"),
            "from": self._options(["HEAD"], branches, commits),
            "default_from": "HEAD",
        }

    def older(self, raw, start):
        """To choices: only refs strictly older than From. Default main, else From's parent."""
        root = self._repo(raw)
        branches, _ = git.refs(root)
        if start not in {o["value"] for o in self.repo(raw)["from"]}:
            raise ApiError(400, "Pick From from the list.")
        names, commits = git.older(root, start, branches)
        options = self._options([], names, commits)
        base = git.default_base(names) or (commits[0].sha if commits else None)
        return {"to": options, "default_to": base}

    @staticmethod
    def _options(heads, branches, commits):
        return (
            [{"value": h, "label": h, "kind": "head"} for h in heads]
            + [{"value": b, "label": b, "kind": "branch"} for b in branches]
            + [{"value": c.sha, "label": f"{c.short} {c.subject[:80]}", "kind": "commit",
                "when": c.when} for c in commits]
        )

    # --- reviews ------------------------------------------------------------

    def review(self, review_id):
        r = self.store.review(review_id)
        if r is None:
            raise ApiError(404, "No such review.")
        with self._lock:
            ready = review_id in self.pending
            orphan = not ready and review_id not in self.planning
        if r["status"] == "awaiting_approval" and orphan:
            self.store.cancel_review(review_id, status="expired")
            r = self.store.review(review_id)
        if r["status"] == "awaiting_approval" and not ready:
            r["status"] = "planning"  # saved but not yet handed over
        if r["status"] == "awaiting_approval":
            pending = self.pending[review_id]
            selection = pending.result.default_selection()
            tokens_in, tokens_out, agents = pending.estimate(selection)
            r["proposed"] = sorted(selection)
            r["estimate"] = {
                "agents": agents, "input_tokens": tokens_in, "output_tokens": tokens_out,
                "max_cost_usd": round((tokens_in * self.settings.input_price
                                       + tokens_out * self.settings.output_price) / 1e6, 6),
            }
        return r

    def estimate(self, review_id, files):
        with self._lock:
            pending = self.pending.get(review_id)
        if pending is None:
            raise ApiError(409, "This plan is no longer waiting for approval.")
        tokens_in, tokens_out, agents = pending.estimate(set(files) & set(pending.result.files()))
        return {"agents": agents, "input_tokens": tokens_in, "output_tokens": tokens_out,
                "max_cost_usd": round((tokens_in * self.settings.input_price
                                       + tokens_out * self.settings.output_price) / 1e6, 6)}

    def submit(self, body):
        """Plan a review in the background; return its id once it starts."""
        root = self._repo(body.get("repo", ""))
        start, end = body.get("from", ""), body.get("to", "")
        if end not in {o["value"] for o in self.older(str(root), start)["to"]}:
            raise ApiError(400, "Pick To from the older commits.")
        try:
            model = self.model_factory()
        except RuntimeError as error:
            raise ApiError(400, str(error)) from None
        started, ids, failure = threading.Event(), {}, {}

        def on_start(review_id):
            with self._lock:
                self.planning.add(review_id)
            ids["id"] = review_id
            started.set()

        def work():
            try:
                pending = prepare(root, f"{end}..{start}", store=self.store, model=model,
                                  settings=self.settings, on_start=on_start)
                if pending.result.plan:
                    with self._lock:
                        self.pending[pending.review_id] = pending
                else:
                    self.store.finish_review(pending.review_id, pending.result,
                                             input_price=self.settings.input_price,
                                             output_price=self.settings.output_price)
            except ReviewError as error:
                failure["message"] = str(error)
            except Exception as error:
                failure["message"] = f"Review failed ({type(error).__name__})"
            finally:
                with self._lock:
                    self.planning.discard(ids.get("id"))
                started.set()

        threading.Thread(target=work, daemon=True).start()
        started.wait(60)
        if "id" not in ids:
            raise ApiError(400, failure.get("message", "Review could not start."))
        return {"id": ids["id"]}

    def decide(self, review_id, body, *, approved):
        with self._lock:
            pending = self.pending.pop(review_id, None)
        if pending is None:
            raise ApiError(409, "This plan is no longer waiting for approval.")
        if not approved:
            cancel(pending, store=self.store)
            return {"id": review_id, "status": "cancelled"}
        selection = set(body.get("files") or []) & set(pending.result.files())
        self.store.mark_running(review_id)

        def work():
            try:
                approve(pending, store=self.store, settings=self.settings, selection=selection)
            except Exception:
                pass  # approve() already recorded the failure

        threading.Thread(target=work, daemon=True).start()
        return {"id": review_id, "status": "running"}

    def project(self, project_id):
        project = self.store.find_project(project_id)
        if project is None:
            raise ApiError(404, "No such project.")
        return {**project, "reviews": self.store.reviews(project["id"]),
                "agents": self.store.agents(project["id"])}


    def _project_or_404(self, project_id):
        project = self.store.find_project(project_id)
        if project is None:
            raise ApiError(404, "No such project.")
        return project

    def machine(self):
        """Every readable repository with git metadata, for the Home chat."""
        spaces = self.workspaces()
        repos = spaces["recent"] + spaces["available"]
        for repo in repos:
            try:
                repo["git"] = git.summary(repo["path"])
            except Exception:
                repo["git"] = {"unavailable": True}
        return repos

    def chat(self, project_id, body):
        project = self._project_or_404(project_id) if project_id is not None else None
        try:
            scan = scanner(self.settings.gitleaks)
            repositories = None if project else self.machine()
            return {"messages": chat.ask(self.store, self.model_factory(), project,
                                         body.get("message"), scan=scan,
                                         repositories=repositories)}
        except (chat.ChatError, ScanUnavailable) as error:
            raise ApiError(400, str(error)) from None
        except RuntimeError:
            raise ApiError(503, "The model is not configured (OpenAI key missing).") from None


    # --- knowledge bases ---------------------------------------------------

    def _knowledge(self):
        """Knowledge store embedding through the configured model client."""
        def embed(texts, model, dimensions):
            try:
                client = self.model_factory()
            except RuntimeError:
                raise RuntimeError("OpenAI key missing") from None
            return client.embed(texts, model, dimensions)
        def scan(text):  # gitleaks is only needed when a document is embedded
            return scanner(self.settings.gitleaks)(text)
        return Knowledge(self.store.db, embed=embed, scan=scan)

    def knowledge(self, method, parts, body):
        kb = self._knowledge()
        try:
            if method == "GET" and not parts:
                return kb.bases()
            if method == "POST" and not parts:
                return kb.create(body.get("name"), body.get("description", ""), body.get("settings"))
            if method == "POST" and parts == ["search"]:
                ids = body.get("kb_ids") or []
                if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                    raise KnowledgeError("kb_ids must be a list of ids.")
                top_k = body.get("top_k")
                return {"results": kb.search(ids, str(body.get("query") or "")[:4000],
                                             top_k=top_k if isinstance(top_k, int) and 0 < top_k <= 20 else None)}
            if parts[:1] == ["documents"] and len(parts) >= 2 and parts[1].isdigit():
                doc_id = int(parts[1])
                if method == "GET" and parts[2:] == ["chunks"]:
                    return {"chunks": kb.chunks(doc_id)}
                if method == "POST" and parts[2:] == ["delete"]:
                    kb.delete_document(doc_id)
                    return {"ok": True}
            if parts and parts[0].isdigit():
                kb_id, rest = int(parts[0]), parts[1:]
                if method == "GET" and not rest:
                    return kb.base(kb_id)
                if method == "POST" and not rest:
                    return kb.update(kb_id, description=body.get("description"),
                                     settings=body.get("settings"))
                if method == "POST" and rest == ["delete"]:
                    kb.delete(kb_id)
                    return {"ok": True}
                if method == "POST" and rest == ["documents"]:
                    try:
                        data = base64.b64decode(body.get("data") or "", validate=True)
                    except (binascii.Error, ValueError):
                        raise KnowledgeError("Upload data must be base64.") from None
                    doc_id = kb.add_document(kb_id, body.get("name"), data)
                    threading.Thread(target=kb.ingest, args=(doc_id, data), daemon=True).start()
                    return {"id": doc_id, "status": "processing"}
        except KnowledgeError as error:
            raise ApiError(400, str(error)) from None
        except RuntimeError:
            raise ApiError(503, "Embedding needs an OpenAI key (OPENAI_API_KEY).") from None
        raise ApiError(404, "Not found.")


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AutoSDE"
        sys_version = ""

        def log_message(self, *args):
            pass

        def _send(self, status, data, content_type, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;"
                " font-src https://fonts.gstatic.com; img-src 'self' data:",
            )
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, value):
            self._send(status, json.dumps(value, default=str).encode(), "application/json")

        def _same_origin(self):
            hosts = {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}
            origin = self.headers.get("Origin")
            return self.headers.get("Host", "") in hosts and (
                origin is None or origin in {f"http://{h}" for h in hosts}
            )

        def _static(self, path):
            target = (STATIC / path.lstrip("/")).resolve()
            if not target.is_file() or STATIC.resolve() not in target.parents:
                target = STATIC / "index.html"  # client-side routes
            if not target.is_file():
                return self._send(503, b"Frontend not built: run `npm run build` in frontend/.",
                                  "text/plain")
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), kind)

        def do_GET(self):
            if not self._same_origin():
                return self._send(403, b"Forbidden", "text/plain")
            path, _, raw_query = self.path.partition("?")
            query = {k: v[0] for k, v in urllib.parse.parse_qs(raw_query).items()}
            if not path.startswith("/api/"):
                return self._static(path)
            try:
                if path == "/api/session":
                    return self._json(200, {"token": app.token, "model": app.settings.model,
                                            "root": str(app.browse_root)})
                if path == "/api/workspaces":
                    return self._json(200, app.workspaces())
                if path == "/api/tree":
                    return self._json(200, app.tree(query.get("path")))
                if path == "/api/repo":
                    return self._json(200, app.repo(query.get("path")))
                if path == "/api/older":
                    return self._json(200, app.older(query.get("path"), query.get("from", "")))
                if path == "/api/models":
                    return self._json(200, app.models())
                if path == "/api/agents":
                    return self._json(200, agents.snapshot())
                if path == "/api/chat":
                    return self._json(200, {"messages": app.store.chat(None)})
                if path == "/api/knowledge" or path.startswith("/api/knowledge/"):
                    parts = [p for p in path[len("/api/knowledge"):].split("/") if p]
                    return self._json(200, app.knowledge("GET", parts, {}))
                if path == "/api/agent-runs":
                    return self._json(200, app.store.agents())
                if path == "/api/projects":
                    return self._json(200, app.store.projects())
                if path == "/api/reviews":
                    return self._json(200, app.store.reviews(limit=50))
                if m := re.fullmatch(r"/api/projects/(\d+)/chat", path):
                    project = app._project_or_404(int(m[1]))
                    return self._json(200, {"messages": app.store.chat(project["id"])})
                if m := re.fullmatch(r"/api/projects/(\d+)", path):
                    return self._json(200, app.project(int(m[1])))
                if m := re.fullmatch(r"/api/reviews/(\d+)\.sarif", path):
                    review = app.review(int(m[1]))
                    return self._send(200, report.as_json(report.sarif(review)).encode(),
                                      "application/json",
                                      {"Content-Disposition": f'attachment; filename="review-{m[1]}.sarif"'})
                if m := re.fullmatch(r"/api/reviews/(\d+)", path):
                    return self._json(200, app.review(int(m[1])))
                raise ApiError(404, "Not found.")
            except ApiError as error:
                self._json(error.status, {"error": str(error)})

        def do_POST(self):
            if not self._same_origin():
                return self._send(403, b"Forbidden", "text/plain")
            if not secrets.compare_digest(self.headers.get("X-AutoSDE-Token", ""), app.token):
                return self._json(403, {"error": "Session expired; reload the page."})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json(415, {"error": "JSON required."})
            length = int(self.headers.get("Content-Length") or 0)
            limit = 8_000_000 if self.path.endswith("/documents") else 100_000  # uploads
            if length > limit:
                return self._json(413, {"error": "Too large."})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "Invalid JSON."})
            try:
                if self.path == "/api/reviews":
                    return self._json(202, app.submit(body))
                if self.path == "/api/chat":
                    return self._json(200, app.chat(None, body))
                if self.path == "/api/knowledge" or self.path.startswith("/api/knowledge/"):
                    parts = [p for p in self.path[len("/api/knowledge"):].split("/") if p]
                    return self._json(200, app.knowledge("POST", parts, body))
                if self.path == "/api/chat/clear":
                    app.store.clear_chat(None)
                    return self._json(200, {"messages": []})
                if m := re.fullmatch(r"/api/projects/(\d+)/chat", self.path):
                    return self._json(200, app.chat(int(m[1]), body))
                if m := re.fullmatch(r"/api/projects/(\d+)/chat/clear", self.path):
                    app.store.clear_chat(app._project_or_404(int(m[1]))["id"])
                    return self._json(200, {"messages": []})
                if self.path == "/api/agents":
                    try:
                        return self._json(200, agents.update(body))
                    except agents.ConfigError as error:
                        raise ApiError(400, str(error)) from None
                if m := re.fullmatch(r"/api/reviews/(\d+)/(approve|cancel)", self.path):
                    return self._json(200, app.decide(int(m[1]), body, approved=m[2] == "approve"))
                if m := re.fullmatch(r"/api/reviews/(\d+)/estimate", self.path):
                    return self._json(200, app.estimate(int(m[1]), body.get("files") or []))
                raise ApiError(404, "Not found.")
            except ApiError as error:
                self._json(error.status, {"error": str(error)})

    return Handler


def serve(store, settings, *, port=8765):
    app = App(store, settings, port=port)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(app))
    print(json.dumps({"status": "listening", "url": f"http://127.0.0.1:{port}/"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
