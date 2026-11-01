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
from pathlib import Path

from autosde import git, report
from autosde.llm import OpenAIModel
from autosde.review import ReviewError, approve, cancel, prepare

STYLE = """
body{font:15px/1.45 -apple-system,system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d2433}
main{max-width:980px;margin:0 auto;padding:24px 16px}
a{color:#2457c5;text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:22px;margin:0 0 16px}h2{font-size:16px;margin:28px 0 8px}
.card{background:#fff;border:1px solid #e3e6eb;border-radius:8px;padding:16px;margin-bottom:16px}
form{display:flex;gap:8px;flex-wrap:wrap}input{padding:8px;border:1px solid #c9ced6;border-radius:6px;font:inherit}
form{align-items:center}select{padding:7px;border:1px solid #c9ced6;border-radius:6px;font:inherit;max-width:420px}label{display:flex;gap:6px;align-items:center}
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


def link(**query):
    return "/?" + urllib.parse.urlencode(query)


class App:
    def __init__(self, store, settings, *, model_factory=None, port=8765, browse_root=None):
        self.store, self.settings, self.port = store, settings, port
        self.token = secrets.token_urlsafe(24)
        self.model_factory = model_factory or (
            lambda: OpenAIModel(settings.model, reasoning_effort=settings.reasoning_effort)
        )
        # The folder browser never leaves this root (your home folder by default).
        self.browse_root = Path(browse_root or Path.home()).resolve()
        # Plans awaiting approval; their file contents exist only here, in memory.
        self.pending, self._lock = {}, threading.Lock()
        self.planning = set()  # ids this process is still planning

    def _inside(self, raw):
        """Resolve a browse path; None when it is outside the browse root."""
        try:
            path = Path(raw).expanduser().resolve() if raw else self.browse_root
        except (OSError, RuntimeError, ValueError):
            return None
        if path != self.browse_root and self.browse_root not in path.parents:
            return None
        return path if path.is_dir() else None

    def _choices(self, repo):
        """(value, label) options for the from/to pickers, and the defaults."""
        branches, commits = git.refs(repo)
        options = [("HEAD", "HEAD")]
        options += [(name, f"branch: {name}") for name in branches]
        options += [(c.sha, f"{c.short}  {c.subject[:70]}  · {c.when}") for c in commits]
        base = git.default_base(branches)
        if base is not None and commits and git.resolve_ref(repo, base) == commits[0].sha:
            base = None  # main is HEAD: main..HEAD would be empty
        if base is None:  # default to the commit before HEAD
            base = commits[1].sha if len(commits) > 1 else "HEAD"
        return options, base, "HEAD"

    def browser(self, path):
        parent = (
            f'<a href="{esc(link(browse=path.parent))}">⬑ {esc(path.parent)}</a>'
            if path != self.browse_root
            else '<span class="muted">Home</span>'
        )
        try:
            folders = sorted(
                (p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")),
                key=lambda p: p.name.lower(),
            )[:500]
        except PermissionError:
            return (
                f'<div class="card"><p>{parent}</p><p class="error">macOS blocked access to '
                f"<code>{esc(path)}</code>. Allow it in System Settings → Privacy &amp; Security"
                " → Files and Folders, then reload.</p></div>"
            )
        listing = "".join(
            "<tr><td>"
            + f'<a href="{esc(link(browse=p))}">📁 {esc(p.name)}</a></td><td>'
            + (
                f'<a href="{esc(link(repo=p))}"><b>Select</b></a> <span class="muted">git</span>'
                if (p / ".git").exists()
                else ""
            )
            + "</td></tr>"
            for p in folders
        ) or '<tr><td class="muted">No folders here.</td><td></td></tr>'
        here = (
            f' · <a href="{esc(link(repo=path))}"><b>Select this repository</b></a>'
            if (path / ".git").exists()
            else ""
        )
        return (
            f'<div class="card"><p>{parent} · <code>{esc(path)}</code>{here}</p>'
            f"<table><tr><th>Folder</th><th></th></tr>{listing}</table></div>"
        )

    def picker(self, repo, message=""):
        options, base, head = self._choices(repo)

        def select(name, default):
            items = "".join(
                f'<option value="{esc(value)}"{" selected" if value == default else ""}>{esc(label)}</option>'
                for value, label in options
            )
            return f'<select name="{name}">{items}</select>'

        return (
            f'<div class="card"><p><b>{esc(repo.name)}</b> <code>{esc(repo)}</code> · '
            f'<a href="{esc(link(browse=repo.parent))}">change repository</a></p>'
            '<form method="post" action="/reviews">'
            f'<input type="hidden" name="token" value="{esc(self.token)}">'
            f'<input type="hidden" name="repo" value="{esc(repo)}">'
            f"<label>From {select('from', base)}</label>"
            f"<label>To {select('to', head)}</label>"
            "<button>Review</button></form>"
            '<p class="muted">Reviews every commit after <i>From</i> up to and including <i>To</i>. '
            f"Model: {esc(self.settings.model)}. Code is sent to the model after a gitleaks scan.</p>"
            f'{f"<p class=error>{esc(message)}</p>" if message else ""}</div>'
        )

    def home(self, message="", *, browse=None, repo=None):
        selected = self._inside(repo) if repo else None
        if selected is not None:
            try:
                selected = git.repo_root(selected)
            except git.GitError:
                selected, message = None, message or "That folder is not a git repository."
        if selected is not None and self._inside(str(selected)) is not None:
            chooser = self.picker(selected, message)
        else:
            path = self._inside(browse) or self.browse_root
            note = f'<p class="error">{esc(message)}</p>' if message else ""
            chooser = note + self.browser(path)
        projects = rows(self.store.projects(), [
            ("Project", lambda p: f'<a href="/projects/{p["id"]}">{esc(p["name"])}</a>'),
            ("Reviews", lambda p: esc(p["reviews"])),
            ("Last review", lambda p: esc(report.when(p["last_review"]))),
            ("Path", lambda p: f'<code>{esc(p["path"])}</code>'),
        ])
        recent = rows(self.store.reviews(limit=20), REVIEW_COLUMNS)
        return page("AutoSDE", f"<h1>Review a commit range</h1>{chooser}<h2>Projects</h2>"
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
        with self._lock:
            orphan = review_id not in self.pending and review_id not in self.planning
        if r["status"] == "awaiting_approval" and orphan:
            self.store.cancel_review(review_id, status="expired")
            r = self.store.review(review_id)
        running = r["status"] in ("planning", "running")
        approval = self.approval(r) if r["status"] == "awaiting_approval" else ""
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
            f'{approval}<h2>Findings</h2>{findings}<h2>Agents</h2><div class="card">{runs}</div>'
            f'{f"<h2>Limits</h2><div class=card><ul>{limits}</ul></div>" if limits else ""}'
        )
        return page(f"Review #{r['id']}", body, refresh=running)

    def approval(self, r):
        """Plan page: one checkbox per file; flagged files start unchecked."""
        pending = self.pending[r["id"]]
        plan = pending.result
        selection = plan.default_selection()
        tokens_in, tokens_out, agents = pending.estimate(selection)
        cost = (tokens_in * self.settings.input_price + tokens_out * self.settings.output_price) / 1e6
        rows_ = []
        for f in r["files"]:
            if not f["agent"]:
                rows_.append(f'<tr><td></td><td><code>{esc(f["path"])}</code></td><td></td>'
                             f'<td class="muted">not sendable: {esc(f["skipped"])}</td></tr>')
                continue
            checked = " checked" if f["path"] in selection else ""
            flag = f'<span class="error">flagged: {esc(f["flag"])}</span>' if f["flag"] else ""
            rows_.append(
                f'<tr><td><input type="checkbox" name="file" value="{esc(f["path"])}"{checked}></td>'
                f'<td><code>{esc(f["path"])}</code></td><td>{esc(f["agent"])}</td><td>{flag}</td></tr>'
            )
        return (
            '<h2>Approve the plan</h2><div class="card">'
            f'<form method="post" action="/reviews/{r["id"]}/approve" style="display:block">'
            f'<input type="hidden" name="token" value="{esc(self.token)}">'
            "<table><tr><th>Review</th><th>File</th><th>Specialist</th><th></th></tr>"
            f'{"".join(rows_)}</table>'
            f'<p class="muted">Estimate for the proposed selection: {agents} specialist(s), '
            f"~{tokens_in:,} input tokens, up to {tokens_out:,} output tokens, at most ${cost:.4f}. "
            "Flagged files are unchecked: they cost tokens and rarely contain defects.</p>"
            '<p><button>Approve and review</button></p></form>'
            f'<form method="post" action="/reviews/{r["id"]}/cancel">'
            f'<input type="hidden" name="token" value="{esc(self.token)}">'
            '<button style="background:#5b6474">Cancel</button></form></div>'
        )

    def decide(self, review_id, form, *, approved):
        """Approve (with the checked files) or cancel a pending plan."""
        if not secrets.compare_digest((form.get("token") or [""])[0], self.token):
            return "This form has expired; reload the page."
        with self._lock:
            pending = self.pending.pop(review_id, None)
        if pending is None:
            return "This plan is no longer waiting for approval."
        if not approved:
            cancel(pending, store=self.store)
            return None
        selection = set(form.get("file", [])) & set(pending.result.files())
        self.store.mark_running(review_id)

        def work():
            try:
                approve(pending, store=self.store, settings=self.settings, selection=selection)
            except Exception:
                pass  # approve() already recorded the failure

        threading.Thread(target=work, daemon=True).start()
        return None

    def submit(self, form):
        """Start a review in the background; return its id or an error message."""
        if not secrets.compare_digest(form.get("token", ""), self.token):
            return None, "This form has expired; reload the page."
        repo = self._inside(form.get("repo", "").strip())
        if repo is None:
            return None, "Choose a repository inside your home folder."
        try:
            options, _, _ = self._choices(git.repo_root(repo))
        except git.GitError:
            return None, "That folder is not a git repository."
        # Only refs the page offered are accepted, never free text.
        offered = {value for value, _ in options}
        start, end = form.get("from", ""), form.get("to", "")
        if start not in offered or end not in offered:
            return None, "Pick From and To from the lists."
        if start == end:
            return None, "From and To are the same; pick an older From."
        range_spec = f"{start}..{end}"
        try:
            model = self.model_factory()
        except RuntimeError as error:
            return None, str(error)
        started, failure = threading.Event(), {}
        ids = {}

        def work():
            try:
                pending = prepare(repo, range_spec, store=self.store, model=model,
                                  settings=self.settings,
                                  on_start=lambda review_id: (
                                      self.planning.add(review_id), ids.update(id=review_id),
                                      started.set()))
                if pending.result.plan:
                    with self._lock:
                        self.pending[pending.review_id] = pending
                        self.planning.discard(pending.review_id)
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
                query = urllib.parse.parse_qs(self.path.partition("?")[2])
                return self._send(200, app.home(browse=(query.get("browse") or [None])[0],
                                                repo=(query.get("repo") or [None])[0]))
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

        def _redirect(self, location):
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            decision = re.fullmatch(r"/reviews/(\d+)/(approve|cancel)", self.path)
            if not (self.path == "/reviews" or decision) or not self._same_origin():
                return self._send(403, "Forbidden", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 100_000:
                return self._send(413, "Too large", "text/plain")
            fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
            if decision:
                review_id = int(decision[1])
                message = app.decide(review_id, fields, approved=decision[2] == "approve")
                if message:
                    return self._send(400, page("Review", f'<p class="error">{esc(message)}</p>'
                                                         f'<p><a href="/reviews/{review_id}">Back</a></p>'))
                return self._redirect(f"/reviews/{review_id}")
            form = {k: v[0] for k, v in fields.items()}
            review_id, message = app.submit(form)
            if review_id is None:
                return self._send(400, app.home(message or "Review could not start.",
                                                repo=form.get("repo")))
            self._redirect(f"/reviews/{review_id}")

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
