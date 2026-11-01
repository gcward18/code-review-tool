import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from autosde import git as capture_git
from autosde.db import Store
from autosde.llm import ScriptedModel
from autosde.review import ReviewError, run_review
from autosde.team import Team, numbered
from helpers import FINDING, PLAN, clean_scan, findings, git, repository, settings, verdict


class GitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = repository(self.tmp.name, {"img.bin": b"\0\1\2", "notes/big.txt": "x" * 70_000})

    def tearDown(self):
        self.tmp.cleanup()

    def test_ranges(self):
        head = git(self.repo, "rev-parse", "HEAD")
        base = git(self.repo, "rev-parse", "HEAD~1")
        for spec in ("HEAD~1..HEAD", "HEAD~1..", "HEAD", "main~1..main"):
            self.assertEqual(capture_git.resolve_range(self.repo, spec), (base, head), spec)
        with self.assertRaises(capture_git.GitError):
            capture_git.resolve_range(self.repo, "nope..HEAD")
        with self.assertRaises(capture_git.GitError):
            capture_git.capture(self.repo, "HEAD..HEAD")

    def test_capture_skips_binary_and_large_files_and_ignores_dirty_edits(self):
        (self.repo / "calculator.py").write_text("dirty, uncommitted\n")
        changes = capture_git.capture(self.repo, "HEAD~1..HEAD")
        by_path = {f.path: f for f in changes.files}
        self.assertEqual(by_path["calculator.py"].head.splitlines()[3].strip(), 'total = item["price"]')
        self.assertIn("+        total = item", by_path["calculator.py"].diff)
        self.assertEqual(by_path["img.bin"].skipped, "binary")
        self.assertIn("larger than", by_path["notes/big.txt"].skipped)
        self.assertEqual(changes.name, "calculator")


class TeamTests(unittest.TestCase):
    def files(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return capture_git.capture(repository(tmp.name), "HEAD~1..HEAD").files

    def test_coordinator_specialist_aggregator_with_numbered_lines(self):
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)],
                               "aggregator": [verdict()]})
        result = Team(model).run(self.files())
        self.assertEqual([c.agent for c in result.calls], ["coordinator", "python", "aggregator"])
        self.assertEqual([(f.id, f.file, f.start_line, f.kept) for f in result.findings],
                         [("python-1", "calculator.py", 4, True)])
        coordinator, specialist, aggregator = model.calls
        self.assertNotIn("total = item", coordinator["prompt"])  # paths only
        self.assertIn('    4|         total = item["price"]', specialist["prompt"])
        self.assertIn("loop totals", specialist["system"])
        self.assertNotIn("total = 0", aggregator["prompt"])  # findings only, not code
        self.assertEqual(result.summary, "One real defect.")

    def test_fallbacks_and_scope(self):
        stray = dict(FINDING, file="other.py")
        wide = dict(FINDING, start_line=0, end_line=999)
        model = ScriptedModel({"python": [findings(stray, wide)]})  # no coordinator, no aggregator
        result = Team(model).run(self.files())
        self.assertIn("Coordinator unavailable; files were routed by type.", result.limitations)
        self.assertIn("Aggregator unavailable; all findings were kept.", result.limitations)
        self.assertEqual([(f.file, f.start_line, f.end_line) for f in result.findings],
                         [("calculator.py", 1, 5)])

    def test_aggregator_rejections_are_recorded(self):
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING, FINDING)],
                               "aggregator": [verdict(rejected=[("python-2", "duplicate")])]})
        result = Team(model).run(self.files())
        self.assertEqual([(f.id, f.kept, f.rejection) for f in result.findings],
                         [("python-1", True, None), ("python-2", False, "duplicate")])

    def test_numbered(self):
        self.assertEqual(numbered("a\nb"), "    1| a\n    2| b")


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = repository(self.tmp.name)
        self.settings = settings(self.tmp.name)
        self.store = Store(self.settings.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def review(self, model, scan=clean_scan, spec="HEAD~1..HEAD"):
        return run_review(self.repo, spec, store=self.store, model=model,
                          settings=self.settings, scan=scan)

    def test_review_is_stored_and_queryable_by_project_and_agent(self):
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)],
                               "aggregator": [verdict()]})
        review_id = self.review(model)
        review = self.store.review(review_id)
        self.assertEqual(review["status"], "completed")
        self.assertEqual(review["project"], "calculator")
        self.assertEqual([r["agent"] for r in review["agent_runs"]], ["coordinator", "python", "aggregator"])
        self.assertEqual(review["findings"][0]["title"], "Total is overwritten")
        tokens_in = sum(r["input_tokens"] for r in review["agent_runs"])
        tokens_out = sum(r["output_tokens"] for r in review["agent_runs"])
        self.assertAlmostEqual(review["cost_usd"], (tokens_in * 0.10 + tokens_out * 0.50) / 1e6, places=6)
        project = self.store.find_project("calculator")
        self.assertEqual(self.store.find_project(str(self.repo))["id"], project["id"])
        self.assertEqual([r["id"] for r in self.store.reviews(project["id"])], [review_id])
        self.assertEqual(self.store.reviews(project["id"])[0]["agents"], "python")
        agents = {(a["role"], a["agent"]): a for a in self.store.agents(project["id"])}
        self.assertEqual(agents[("specialist", "python")]["findings_kept"], 1)
        self.assertEqual(self.store.projects()[0]["reviews"], 1)
        # No source code is stored anywhere in the database.
        dump = "\n".join(self.store.db.iterdump())
        self.assertNotIn('total = item["price"]', dump)
        self.assertEqual(stat.S_IMODE(os.stat(self.settings.db_path).st_mode), 0o600)

    def test_possible_secret_blocks_the_file_before_any_model_call(self):
        model = ScriptedModel({})
        review_id = self.review(model, scan=lambda text: ["github-pat"])
        review = self.store.review(review_id)
        self.assertEqual(model.calls, [])
        self.assertIn("possible secret (github-pat); not sent", review["limitations"])

    def test_missing_gitleaks_refuses_to_start(self):
        with self.assertRaises(ReviewError):
            run_review(self.repo, "HEAD~1..HEAD", store=self.store, model=ScriptedModel({}),
                       settings=self.settings)
        self.assertEqual(self.store.reviews(), [])

    def test_failed_specialists_mark_the_review_failed(self):
        review_id = self.review(ScriptedModel({"coordinator": [PLAN]}))
        review = self.store.review(review_id)
        self.assertEqual(review["status"], "failed")
        self.assertIn("python specialist failed", review["limitations"])

    def test_real_gitleaks_scan_when_available(self):
        from autosde.config import gitleaks_path
        from autosde.scan import scanner

        executable = gitleaks_path()
        if not executable:
            self.skipTest("gitleaks not installed (set AUTOSDE_GITLEAKS)")
        scan = scanner(executable)
        self.assertEqual(scan("x = 1\n"), [])
        token = "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6"
        self.assertEqual(scan(f'token = "{token}"\n'), ["github-pat"])


class OpenAIRequestTests(unittest.TestCase):
    def test_request_shape_and_parsing(self):
        from unittest.mock import MagicMock

        from autosde.llm import OpenAIModel
        from autosde.team import FINDINGS_SCHEMA

        model = OpenAIModel("gpt-6-luna", api_key="sk-test", reasoning_effort="low")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "status": "completed",
            "output": [{"type": "reasoning"}, {"type": "message", "content": [
                {"type": "output_text", "text": '{"findings": [], "summary": "ok"}'}]}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }).encode()
        model._opener = MagicMock(open=MagicMock(return_value=response))
        reply = model.complete(role="python", system="s", prompt="p", schema=FINDINGS_SCHEMA,
                               max_output_tokens=4000)
        self.assertTrue(reply.ok)
        self.assertEqual((reply.input_tokens, reply.output_tokens), (10, 5))
        body = json.loads(model._opener.open.call_args.args[0].data)
        self.assertEqual(body["store"], False)
        self.assertEqual(body["text"]["format"]["strict"], True)
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertNotIn("sk-test", repr(model))


if __name__ == "__main__":
    unittest.main()
