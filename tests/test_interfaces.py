import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from autosde import report
from autosde.cli import main
from autosde.db import Store
from autosde.llm import ScriptedModel
from autosde.review import run_review
from autosde.web import App, handler_for
from helpers import FINDING, PLAN, clean_scan, findings, repository, settings, verdict

XSS = dict(FINDING, title="<script>alert(1)</script>")


class Seeded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = repository(self.tmp.name)
        self.settings = settings(self.tmp.name)
        self.store = Store(self.settings.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def scripted(self, finding=FINDING):
        return ScriptedModel({"coordinator": [PLAN], "python": [findings(finding)],
                              "aggregator": [verdict()]})

    def seed(self, finding=FINDING):
        return run_review(self.repo, "HEAD~1..HEAD", store=self.store, model=self.scripted(finding),
                          settings=self.settings, scan=clean_scan)


class CliTests(Seeded):
    def run_cli(self, *args):
        out = io.StringIO()
        with patch("autosde.cli.load_settings", return_value=self.settings), redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_queries(self):
        review_id = self.seed()
        code, out = self.run_cli("projects")
        self.assertEqual(code, 0)
        self.assertIn("calculator", out)
        _, out = self.run_cli("reviews", "calculator")
        self.assertIn("completed", out)
        self.assertIn("python", out)
        _, out = self.run_cli("agents", "calculator")
        self.assertIn("coordinator", out)
        self.assertIn("aggregator", out)
        _, out = self.run_cli("show", str(review_id))
        self.assertIn("calculator.py:4: MEDIUM Total is overwritten  [python]", out)
        _, out = self.run_cli("show", str(review_id), "--format", "sarif")
        result = json.loads(out)["runs"][0]["results"][0]
        self.assertEqual(result["locations"][0]["physicalLocation"]["region"]["startLine"], 4)

    def test_review_command_uses_the_model_and_prints_the_report(self):
        with patch("autosde.cli.OpenAIModel", return_value=self.scripted()), \
                patch("autosde.review.scanner", return_value=clean_scan):
            code, out = self.run_cli("review", str(self.repo), "HEAD~1..HEAD")
        self.assertEqual(code, 0)
        self.assertIn("Total is overwritten", out)

    def test_unknown_project(self):
        with self.assertRaises(SystemExit):
            self.run_cli("reviews", "nope")


class WebTests(Seeded):
    def start(self):
        app = App(self.store, self.settings, model_factory=self.scripted, port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(app))
        app.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return app, f"http://127.0.0.1:{app.port}"

    def post(self, url, data, origin=None):
        request = urllib.request.Request(url + "/reviews", data=urllib.parse.urlencode(data).encode(),
                                         method="POST")
        if origin:
            request.add_header("Origin", origin)
        return urllib.request.urlopen(request, timeout=30)

    def test_submit_runs_a_review_and_pages_render_escaped(self):
        app, url = self.start()
        with patch("autosde.review.scanner", return_value=clean_scan):
            app.model_factory = lambda: self.scripted(XSS)
            response = self.post(url, {"token": app.token, "repo": str(self.repo),
                                       "range": "HEAD~1..HEAD"}, origin=url)
        self.assertRegex(response.url, r"/reviews/\d+$")
        for _ in range(50):
            page = urllib.request.urlopen(response.url, timeout=5).read().decode()
            if "completed" in page:
                break
            threading.Event().wait(0.1)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>", page)
        home = urllib.request.urlopen(url + "/", timeout=5).read().decode()
        self.assertIn("calculator", home)
        project = urllib.request.urlopen(url + "/projects/1", timeout=5).read().decode()
        self.assertIn("Agents that ran", project)
        sarif = json.loads(urllib.request.urlopen(response.url + ".sarif", timeout=5).read())
        self.assertEqual(sarif["version"], "2.1.0")

    def test_submission_needs_the_token_and_same_origin(self):
        app, url = self.start()
        good = {"token": app.token, "repo": str(self.repo), "range": "HEAD~1..HEAD"}
        with self.assertRaises(urllib.error.HTTPError) as bad_token:
            self.post(url, good | {"token": "wrong"}, origin=url)
        self.assertEqual(bad_token.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as cross_site:
            self.post(url, good, origin="https://evil.example")
        self.assertEqual(cross_site.exception.code, 403)
        self.assertEqual(self.store.reviews(), [])


class ReportTests(Seeded):
    def test_text_and_sarif(self):
        review = self.store.review(self.seed())
        self.assertIn("Fix: Use total += item", report.text(review))
        location = report.sarif(review)["runs"][0]["results"][0]["locations"][0]
        self.assertEqual(location["physicalLocation"]["artifactLocation"]["uri"], "calculator.py")


if __name__ == "__main__":
    unittest.main()
