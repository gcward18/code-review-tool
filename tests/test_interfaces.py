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
from pathlib import Path
from unittest.mock import patch

from autosde import report
from autosde.cli import main
from autosde.db import Store
from autosde.llm import ScriptedModel
from autosde.review import run_review
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

    @property
    def base(self):
        from helpers import git

        return git(self.repo, "rev-parse", "HEAD~1")

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
        with patch("autosde.cli.OpenAIModel.from_settings", return_value=self.scripted()), \
                patch("autosde.review.scanner", return_value=clean_scan):
            code, out = self.run_cli("review", str(self.repo), "HEAD~1..HEAD", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Total is overwritten", out)

    def test_unknown_project(self):
        with self.assertRaises(SystemExit):
            self.run_cli("reviews", "nope")


class ReportTests(Seeded):
    def test_text_and_sarif(self):
        review = self.store.review(self.seed())
        self.assertIn('      Fix:\n        Use total += item["price"].', report.text(review))
        review["findings"][0]["recommendation"] = "@@ lines 4-4 @@\n-         total = x\n+         total += x"
        self.assertIn("        @@ lines 4-4 @@\n        -         total = x\n        +         total += x",
                      report.text(review))
        location = report.sarif(review)["runs"][0]["results"][0]["locations"][0]
        self.assertEqual(location["physicalLocation"]["artifactLocation"]["uri"], "calculator.py")




if __name__ == "__main__":
    unittest.main()
