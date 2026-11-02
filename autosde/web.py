"""Local web app: a JSON API plus the built React frontend (autosde/static).

Binds 127.0.0.1 only. Every POST must be same-origin JSON carrying the
per-start token, so other websites cannot trigger paid reviews.
"""

import json
import mimetypes
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from autosde.api import body_limit, dispatch, json_response
from autosde.app import SKIP, ApiError, App  # noqa: F401  (re-exported for callers and tests)

STATIC = Path(__file__).parent / "static"


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
            self._reply(json_response(status, value))

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

        def _reply(self, response):
            self._send(response.status, response.body, response.content_type, response.headers)

        def do_GET(self):
            if not self._same_origin():
                return self._send(403, b"Forbidden", "text/plain")
            path, _, raw_query = self.path.partition("?")
            if not path.startswith("/api/"):
                return self._static(path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(raw_query).items()}
            self._reply(dispatch(app, "GET", path, query))

        def do_POST(self):
            if not self._same_origin():
                return self._send(403, b"Forbidden", "text/plain")
            if not secrets.compare_digest(self.headers.get("X-AutoSDE-Token", ""), app.token):
                return self._json(403, {"error": "Session expired; reload the page."})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json(415, {"error": "JSON required."})
            length = int(self.headers.get("Content-Length") or 0)
            if length > body_limit(self.path):
                return self._json(413, {"error": "Too large."})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "Invalid JSON."})
            self._reply(dispatch(app, "POST", self.path, None, body))

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
