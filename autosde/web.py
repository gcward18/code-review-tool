"""Tiny local web page: submit a review for a commit range and browse results.

Binds 127.0.0.1 only. Submissions need the page's per-start token and a
same-origin request, so other websites cannot trigger paid reviews.
"""

import html
import json
import re
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from autosde import report
from autosde.llm import OpenAIModel
from autosde.review import ReviewError, run_review

STYLE = """
body{font:15px/1.45 -apple-system,system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d2433}
main{max-width:980px;margin:0 auto;padding:24px 16px}
a{color:#2457c5;text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:22px;margin:0 0 16px}h2{font-size:16px;margin:28px 0 8px}
.card{background:#fff;border:1px solid #e3e6eb;border-radius:8px;padding:16px;margin-bottom:16px}
form{display:flex;gap:8px;flex-wrap:wrap}input{padding:8px;border:1px solid #c9ced6;border-radius:6px;font:inherit}
input[name=repo]{flex:2 1 320px}input[name=range]{flex:1 1 160px}
button{padding:8px 16px;border:0;border-radius:6px;background:#2457c5;color:#fff;font:inherit;cursor:pointer}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eceef2;vertical-align:top}
th{font-weight:600;color:#5b6474;font-size:13px}code{font:13px ui-monospace,Menlo,monospace}
.sev{font-weight:600;font-size:12px;padding:2px 6px;border-radius:4px;color:#fff}
.critical,.high{background:#c2313a}.medium{background:#c77a00}.low{background:#5b6474}
.muted{color:#6b7380}.error{color:#c2313a}.ok{color:#1f7a3d}
"""


def esc(value):
    return html.escape(str(value if value is not None else ""))


def page(title, body, *, refresh=False):
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width,initial-scale=1">{meta}<title>{esc(title)}</title>'
        f"<style>{STYLE}</style></head><body><main>"
        f'<p><a href="/">AutoSDE</a></p>{body}</main></body></html>'
    )


def rows(items, columns):
    head = "".join(f"<th>{esc(label)}</th>" for label, _ in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell(item)}</td>" for _, cell in columns) + "</tr>"
        for item in items
    )
    return f"<table><tr>{head}</tr>{body}</table>" if items else '<p class="muted">None yet.</p>'


REVIEW_COLUMNS = [
    ("#", lambda r: f'<a href="/reviews/{r["id"]}">{r["id"]}</a>'),
    ("Project", lambda r: f'<a href="/projects/{r["project_id"]}">{esc(r["project"])}</a>'),
    ("Range", lambda r: f'<code>{esc(r["range_spec"])}</code>'),
    ("Started", lambda r: esc(report.when(r["started_at"]))),
    ("Status", lambda r: f'<span class="{"ok" if r["status"] == "completed" else "error" if r["status"] == "failed" else "muted"}">{esc(r["status"])}</span>'),
    ("Findings", lambda r: esc(r["findings"])),
    ("Agents", lambda r: esc((r["agents"] or "").replace(",", ", "))),
    ("Cost", lambda r: f'${r["cost_usd"]:.4f}'),
]


class App:
    def __init__(self, store, settings, *, model_factory=None, port=8765):
        self.store, self.settings, self.port = store, settings, port
        self.token = secrets.token_urlsafe(24)
        self.model_factory = model_factory or (
            lambda: OpenAIModel(settings.model, reasoning_effort=settings.reasoning_effort)
        )

    def home(self, message=""):
        form = (
            '<div class="card"><form method="post" action="/reviews">'
            f'<input type="hidden" name="token" value="{esc(self.token)}">'
            '<input name="repo" placeholder="/path/to/repo" required>'
            '<input name="range" placeholder="main..HEAD or a commit" required>'
            "<button>Review</button></form>"
            f'<p class="muted">Model: {esc(self.settings.model)}. Code in the range is sent to the model after a gitleaks scan.</p>'
            f'{f"<p class=error>{esc(message)}</p>" if message else ""}</div>'
        )
        projects = rows(self.store.projects(), [
            ("Project", lambda p: f'<a href="/projects/{p["id"]}">{esc(p["name"])}</a>'),
            ("Reviews", lambda p: esc(p["reviews"])),
            ("Last review", lambda p: esc(report.when(p["last_review"]))),
            ("Path", lambda p: f'<code>{esc(p["path"])}</code>'),
        ])
        recent = rows(self.store.reviews(limit=20), REVIEW_COLUMNS)
        return page("AutoSDE", f"<h1>Review a commit range</h1>{form}<h2>Projects</h2>"
                               f'<div class="card">{projects}</div><h2>Recent reviews</h2><div class="card">{recent}</div>')

    def project(self, project_id):
        project = self.store.find_project(project_id)
        if project is None:
            return None
        reviews = rows(self.store.reviews(project["id"]), REVIEW_COLUMNS)
        agents = rows(self.store.agents(project["id"]), [
            ("Role", lambda a: esc(a["role"])), ("Agent", lambda a: esc(a["agent"])),
            ("Runs", lambda a: esc(a["runs"])), ("OK", lambda a: esc(a["succeeded"])),
            ("Failed", lambda a: esc(a["failed"])), ("Findings kept", lambda a: esc(a["findings_kept"])),
            ("Last run", lambda a: esc(report.when(a["last_run"]))),
        ])
        return page(project["name"], f'<h1>{esc(project["name"])}</h1><p class="muted"><code>{esc(project["path"])}</code></p>'
                                     f'<h2>Reviews</h2><div class="card">{reviews}</div>'
                                     f'<h2>Agents that ran</h2><div class="card">{agents}</div>')

    def review(self, review_id):
        r = self.store.review(review_id)
        if r is None:
            return None
        running = r["status"] == "running"
        runs = rows(r["agent_runs"], [
            ("Role", lambda a: esc(a["role"])), ("Agent", lambda a: esc(a["agent"])),
            ("Status", lambda a: f'<span class="{"ok" if a["status"] == "ok" else "error"}">{esc(a["status"])}</span>'
                                 + (f' <span class="muted">{esc(a["error"])}</span>' if a["error"] else "")),
            ("Files", lambda a: "<br>".join(f"<code>{esc(p)}</code>" for p in a["files"].splitlines())),
            ("Time", lambda a: f'{a["seconds"]:.1f}s'),
            ("Tokens", lambda a: f'{a["input_tokens"]}/{a["output_tokens"]}'),
        ])
        findings = "".join(
            f'<div class="card"><p><span class="sev {esc(f["severity"])}">{esc(f["severity"])}</span> '
            f'<b>{esc(f["title"])}</b> <span class="muted">· {esc(f["agent"])}</span></p>'
            f'<p><code>{esc(f["file"])}:{f["start_line"]}</code>{"" if f["kept"] else " <span class=error>rejected: " + esc(f["rejection"]) + "</span>"}</p>'
            f'<p>{esc(f["description"])}</p><p><b>Fix:</b> {esc(f["recommendation"])}</p></div>'
            for f in r["findings"]
        ) or ('<p class="muted">Waiting for the team…</p>' if running else '<p class="muted">No findings.</p>')
        limits = "".join(f"<li>{esc(item)}</li>" for item in (r["limitations"] or "").splitlines())
        body = (
            f'<h1>Review #{r["id"]} · <a href="/projects/{r["project_id"]}">{esc(r["project"])}</a></h1>'
            f'<div class="card"><p><code>{esc(r["range_spec"])}</code> '
            f'<span class="muted">({esc(r["base_sha"][:7])}..{esc(r["head_sha"][:7])})</span></p>'
            f'<p>Status: <b>{esc(r["status"])}</b> · model {esc(r["model"])} · {r["files_changed"]} files · '
            f'{r["input_tokens"]}/{r["output_tokens"]} tokens · ${r["cost_usd"]:.4f}'
            f' · <a href="/reviews/{r["id"]}.sarif">SARIF</a></p>'
            f'{f"<p class=error>{esc(r["error"])}</p>" if r["error"] else ""}'
            f'{f"<p>{esc(r["summary"])}</p>" if r["summary"] else ""}</div>'
            f'<h2>Findings</h2>{findings}<h2>Agents</h2><div class="card">{runs}</div>'
            f'{f"<h2>Limits</h2><div class=card><ul>{limits}</ul></div>" if limits else ""}'
        )
        return page(f"Review #{r['id']}", body, refresh=running)

    def submit(self, form):
        """Start a review in the background; return its id or an error message."""
        if not secrets.compare_digest(form.get("token", ""), self.token):
            return None, "This form has expired; reload the page."
        repo, range_spec = form.get("repo", "").strip(), form.get("range", "").strip()
        if not repo or not range_spec:
            return None, "Enter a repository path and a commit range."
        try:
            model = self.model_factory()
        except RuntimeError as error:
            return None, str(error)
        started, failure = threading.Event(), {}
        ids = {}

        def work():
            try:
                run_review(repo, range_spec, store=self.store, model=model, settings=self.settings,
                           on_start=lambda review_id: (ids.update(id=review_id), started.set()))
            except ReviewError as error:
                failure["message"] = str(error)
            except Exception as error:
                failure["message"] = f"Review failed ({type(error).__name__})"
            finally:
                started.set()

        threading.Thread(target=work, daemon=True).start()
        started.wait(60)
        return ids.get("id"), failure.get("message")


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AutoSDE"
        sys_version = ""

        def log_message(self, *args):
            pass

        def _send(self, status, body, content_type="text/html; charset=utf-8", extra=None):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _same_origin(self):
            allowed = {f"http://127.0.0.1:{app.port}", f"http://localhost:{app.port}"}
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            return (origin is None or origin in allowed) and host in {
                f"127.0.0.1:{app.port}", f"localhost:{app.port}"
            }

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if not self._same_origin():
                return self._send(403, "Forbidden", "text/plain")
            if path == "/":
                return self._send(200, app.home())
            if match := re.fullmatch(r"/reviews/(\d+)\.sarif", path):
                review = app.store.review(int(match[1]))
                if review:
                    return self._send(200, report.as_json(report.sarif(review)), "application/json",
                                      {"Content-Disposition": f'attachment; filename="review-{match[1]}.sarif"'})
            elif match := re.fullmatch(r"/reviews/(\d+)", path):
                if body := app.review(int(match[1])):
                    return self._send(200, body)
            elif match := re.fullmatch(r"/projects/(\d+)", path):
                if body := app.project(int(match[1])):
                    return self._send(200, body)
            self._send(404, page("Not found", "<h1>Not found</h1>"))

        def do_POST(self):
            if self.path != "/reviews" or not self._same_origin():
                return self._send(403, "Forbidden", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 10_000:
                return self._send(413, "Too large", "text/plain")
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
            review_id, message = app.submit(form)
            if review_id is None:
                return self._send(400, app.home(message or "Review could not start."))
            self.send_response(303)
            self.send_header("Location", f"/reviews/{review_id}")
            self.send_header("Content-Length", "0")
            self.end_headers()

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
