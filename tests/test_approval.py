import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from autosde import git as capture_git
from autosde.cli import main
from autosde.db import Store
from autosde.llm import ScriptedModel
from autosde.noise import flag
from autosde.review import approve, cancel, prepare
from autosde.team import Team
from autosde.web import App
from helpers import FINDING, clean_scan, findings, repository, settings, verdict

NOISE = {
    ".idea/workspace.xml": "<project/>\n",
    "package-lock.json": '{"lockfileVersion": 3}\n',
    "fixtures/data.csv": "a,b\n1,2\n",
    "generated_client.py": "# generated\nX = 1\n",
}
PLAN = {
    "assignments": [{"agent": "python", "files": ["calculator.py", "generated_client.py"],
                     "focus": "totals"},
                    {"agent": "general", "files": [".idea/workspace.xml", "package-lock.json",
                                                   "fixtures/data.csv"], "focus": ""}],
    "flagged": [{"file": "generated_client.py", "reason": "generated code"},
                {"file": "package-lock.json", "reason": "lockfile again"}],
    "rationale": "python plus noise",
}


class NoiseRuleTests(unittest.TestCase):
    def test_rules(self):
        expected = {
            ".idea/workspace.xml": "IDE settings (.idea/)",
            "app/node_modules/x/index.js": "vendored dependencies (node_modules/)",
            "web/package-lock.json": "lockfile",
            "poetry.lock": "lockfile",
            "static/app.min.js": "minified code",
            "static/app.js.map": "source map",
            "reports/out.csv": "data file",
            ".DS_Store": "OS metadata",
            "autosde.iml": "IDE settings",
            "src/app.py": None,
            "build.gradle": None,
            "tests/test_build.py": None,
        }
        for path, reason in expected.items():
            self.assertEqual(flag(path), reason, path)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = repository(self.tmp.name, NOISE)
        self.settings = settings(self.tmp.name)
        self.store = Store(self.settings.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def model(self, **extra):
        return ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)],
                              "aggregator": [verdict()], **extra})

    def prepare(self, model):
        return prepare(self.repo, "HEAD~1..HEAD", store=self.store, model=model,
                       settings=self.settings, scan=clean_scan)


class PlanTests(Fixture):
    def test_rules_and_coordinator_flags_are_excluded_by_default(self):
        files = capture_git.capture(self.repo, "HEAD~1..HEAD").files
        team = Team(self.model())
        result = team.plan(files)
        self.assertEqual(result.flagged, {
            ".idea/workspace.xml": "IDE settings (.idea/)",
            "package-lock.json": "lockfile",  # the rule wins over the coordinator
            "fixtures/data.csv": "data file",
            "generated_client.py": "coordinator: generated code",
        })
        self.assertEqual(result.default_selection(), {"calculator.py"})
        tokens_in, tokens_out, agents = team.estimate(result, files, result.default_selection())
        self.assertGreater(tokens_in, 0)
        self.assertEqual(agents, 1)
        everything = team.estimate(result, files, set(result.files()))
        self.assertGreater(everything[0], tokens_in)
        self.assertEqual(team.estimate(result, files, set()), (0, 0, 0))

    def test_only_approved_files_reach_the_specialists(self):
        model = self.model()
        pending = self.prepare(model)
        self.assertEqual([c["role"] for c in model.calls], ["coordinator"])  # nothing else yet
        review = self.store.review(pending.review_id)
        self.assertEqual(review["status"], "awaiting_approval")
        self.assertEqual({f["path"]: f["included"] for f in review["files"]},
                         {p: None for p in ["calculator.py", *NOISE]})
        approve(pending, store=self.store, settings=self.settings,
                selection={"calculator.py", "generated_client.py"})
        specialist = next(c for c in model.calls if c["role"] == "python")
        self.assertIn("generated_client.py", specialist["prompt"])
        self.assertNotIn("lockfileVersion", "".join(c["prompt"] for c in model.calls))
        review = self.store.review(pending.review_id)
        self.assertEqual(review["status"], "completed")
        included = {f["path"]: f["included"] for f in review["files"]}
        self.assertEqual(included["calculator.py"], 1)
        self.assertEqual(included["generated_client.py"], 1)
        self.assertEqual(included["package-lock.json"], 0)
        self.assertIn("package-lock.json: not reviewed (lockfile; not approved).", review["limitations"])
        self.assertEqual([r["agent"] for r in review["agent_runs"]],
                         ["coordinator", "python", "aggregator"])

    def test_cancel_spends_nothing_more(self):
        model = self.model()
        pending = self.prepare(model)
        cancel(pending, store=self.store)
        review = self.store.review(pending.review_id)
        self.assertEqual(review["status"], "cancelled")
        self.assertEqual([c["role"] for c in model.calls], ["coordinator"])
        self.assertEqual({f["included"] for f in review["files"] if f["agent"]}, {0})


class CliApprovalTests(Fixture):
    def run_cli(self, *args, answer=None, tty=False):
        out, err = io.StringIO(), io.StringIO()
        model = self.model()
        with patch("autosde.cli.load_settings", return_value=self.settings), \
                patch("autosde.cli.OpenAIModel", return_value=model), \
                patch("autosde.review.scanner", return_value=clean_scan), \
                patch("sys.stdin.isatty", return_value=tty), \
                patch("builtins.input", return_value=answer or ""), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(["review", str(self.repo), "HEAD~1..HEAD", *args])
        return code, out.getvalue(), err.getvalue(), model

    def test_noninteractive_without_yes_cancels_after_planning(self):
        code, out, err, model = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("[ ] package-lock.json -> general  flagged: lockfile", err)
        self.assertIn("[x] calculator.py -> python", err)
        self.assertIn("Approval needed", err)
        self.assertEqual([c["role"] for c in model.calls], ["coordinator"])
        self.assertEqual(self.store.reviews()[0]["status"], "cancelled")

    def test_yes_reviews_the_proposed_plan(self):
        code, out, err, model = self.run_cli("--yes")
        self.assertEqual(code, 0)
        self.assertIn("Total is overwritten", out)
        prompt = next(c for c in model.calls if c["role"] == "python")["prompt"]
        self.assertNotIn("generated_client.py", prompt)

    def test_include_flags_and_interactive_answers(self):
        _, _, _, model = self.run_cli("--yes", "--include", "generated_client.py")
        self.assertIn("generated_client.py", next(c for c in model.calls if c["role"] == "python")["prompt"])
        code, _, _, model = self.run_cli(tty=True, answer="n")
        self.assertEqual(code, 1)
        code, _, _, model = self.run_cli(tty=True, answer="a")
        self.assertEqual(code, 0)
        roles = sorted({c["role"] for c in model.calls})
        self.assertIn("general", roles)  # flagged files were approved too


class WebApprovalTests(Fixture):
    def test_plan_page_checkboxes_decide_and_expire(self):
        app = App(self.store, self.settings, model_factory=self.model, port=0,
                  browse_root=self.tmp.name)
        pending = self.prepare(self.model())
        app.pending[pending.review_id] = pending
        page = app.review(pending.review_id)
        self.assertIn('value="calculator.py" checked', page)
        self.assertIn('value="package-lock.json">', page)  # flagged: unchecked
        self.assertIn("flagged: lockfile", page)
        self.assertEqual(app.decide(pending.review_id, {"token": ["wrong"]}, approved=True),
                         "This form has expired; reload the page.")
        self.assertIsNone(app.decide(pending.review_id, {"token": [app.token]}, approved=False))
        self.assertEqual(self.store.review(pending.review_id)["status"], "cancelled")
        handing_over = self.prepare(self.model())  # saved, not yet registered by this process
        app.planning.add(handing_over.review_id)
        self.assertIn("Waiting for the team", app.review(handing_over.review_id))
        self.assertEqual(self.store.review(handing_over.review_id)["status"], "awaiting_approval")
        orphan = self.prepare(self.model())  # never registered: e.g. after a restart
        app.review(orphan.review_id)
        self.assertEqual(self.store.review(orphan.review_id)["status"], "expired")


if __name__ == "__main__":
    unittest.main()
