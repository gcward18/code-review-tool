import json
import json
import os
import stat
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from autosde import agents
from autosde import git as capture_git
from autosde.db import Store
from autosde.llm import ScriptedModel
from autosde.review import ReviewError, context_reader, run_review
from autosde.team import (AGGREGATOR_INPUT, AGGREGATOR_OUTPUT, BUDGET_REACHED, MAX_CONTEXT_LINES, Budget, MAX_CONTEXT_READS, MAX_ROUNDS, ContextUnavailable, Team,
                          TeamResult, changed_lines, numbered)
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


class ChangedLinesTests(unittest.TestCase):
    def test_head_side_ranges_of_added_lines(self):
        diff = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,5 @@\n def f():\n-    return 1\n"
                "+    a = 1\n+    b = 2\n+    return a\n\\ No newline at end of file\n@@ -9 +12,2 @@\n"
                " ctx\n+new\n")
        self.assertEqual(changed_lines(diff), [[2, 4], [13, 13]])


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
        sent = json.loads(next(c for c in model.calls if c["role"] == "aggregator")["prompt"])
        self.assertEqual(sent["changed_lines"], {"calculator.py": [[2, 5]]})
        self.assertEqual([(f.id, f.file, f.start_line, f.kept) for f in result.findings],
                         [("python-1", "calculator.py", 4, True)])
        coordinator, specialist, aggregator = model.calls
        self.assertNotIn("total = item", coordinator["prompt"])  # paths only
        self.assertIn('    4|         total = item["price"]', specialist["prompt"])
        self.assertIn("loop totals", specialist["system"])
        # The aggregator judges against the numbered code around each finding.
        listing = json.loads(aggregator["prompt"])["findings"][0]
        self.assertIn('    4|         total = item["price"]', listing["code"])
        self.assertEqual(result.summary, "One real defect.")

    def test_coordinator_plan_is_validated_as_a_contract(self):
        broken = [{"assignments": "python", "flagged": [], "rationale": "x"},
                  {"assignments": [{"agent": "python", "files": [4], "focus": ""}], "flagged": [],
                   "rationale": "x"},
                  {"assignments": [], "rationale": "no flagged list"}]
        for plan in broken:
            model = ScriptedModel({"coordinator": [plan], "python": [findings()]})
            result = Team(model).run(self.files())
            self.assertEqual((result.calls[0].status, result.calls[0].error),
                             ("failed", "plan did not match the contract"))
            self.assertIn("Coordinator unavailable; files were routed by type.", result.limitations)
            self.assertEqual(result.plan, {"python": {"files": ["calculator.py"], "focus": ""}})
        stray = dict(PLAN, assignments=[{"agent": "cobol", "files": ["calculator.py"], "focus": "x"},
                                        {"agent": "python", "files": ["missing.py"], "focus": "y"}],
                     flagged=[{"file": "elsewhere.py", "reason": "not changed"}])
        result = Team(ScriptedModel({"coordinator": [stray], "python": [findings()]})).run(self.files())
        self.assertEqual(result.calls[0].status, "ok")  # valid shape; unknown names are dropped
        self.assertEqual(result.flagged, {})
        self.assertIn("Coordinator left 1 file(s) unassigned; routed by type.", result.limitations)

    def ask_for(self, *ranges):
        return dict(findings(), requested_context=[
            {"path": path, "start_line": a, "end_line": b, "reason": "callers"} for path, a, b in ranges])

    def test_specialist_reads_requested_context_once_and_then_answers(self):
        reads = []

        def reader(path, start, end):
            reads.append((path, start, end))
            if path == "secret.py":
                raise ContextUnavailable("possible secret; not sent")
            return f"    {start}| def helper(): ..."

        model = ScriptedModel({"coordinator": [PLAN],
                               "python": [self.ask_for(("helpers.py", 1, 900), ("calculator.py", 1, 5),
                                                       ("secret.py", 1, 2)),
                                          self.ask_for(("more.py", 1, 2)) | findings(FINDING)],
                               "aggregator": [verdict()]})
        result = Team(model, reader=reader).run(self.files())
        self.assertEqual(reads, [("helpers.py", 1, MAX_CONTEXT_LINES), ("secret.py", 1, 2)])
        second = [c for c in model.calls if c["role"] == "python"][1]["prompt"]
        self.assertIn("[helpers.py lines 1-200]\n    1| def helper(): ...", second)
        self.assertIn("[calculator.py lines 1-5] not available: it is a changed file", second)
        self.assertIn("[secret.py lines 1-2] not available: possible secret; not sent", second)
        self.assertEqual([c.focus for c in result.calls if c.agent == "python"],
                         ["loop totals", "loop totals (with context)"])
        self.assertIn("Context: python read helpers.py lines 1-200.", result.limitations)
        self.assertEqual([(f.id, f.kept) for f in result.findings], [("python-1", True)])  # no 2nd round

    def test_context_reads_stop_at_the_review_quota(self):
        many = [(f"lib{i}.py", 1, 2) for i in range(MAX_CONTEXT_READS + 2)]
        model = ScriptedModel({"coordinator": [PLAN], "python": [self.ask_for(*many), findings()]})
        result = Team(model, reader=lambda path, a, b: "    1| x").run(self.files())
        reads = [l for l in result.limitations if l.startswith("Context:")]
        self.assertEqual(len(reads), MAX_CONTEXT_READS)
        second = [c for c in model.calls if c["role"] == "python"][1]["prompt"]
        self.assertEqual(second.count("context quota is used up"), 2)

    def test_without_a_reader_requests_are_ignored(self):
        model = ScriptedModel({"coordinator": [PLAN], "python": [self.ask_for(("x.py", 1, 2))]})
        result = Team(model).run(self.files())
        self.assertEqual([c.agent for c in result.calls], ["coordinator", "python"])

    def test_context_reader_serves_unchanged_committed_files_only(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = repository(tmp.name, {"helpers.py": "def helper():\n    return 1\n",
                                     "package-lock.json": "{}", "blob.bin": b"\0\1"})
        # helpers.py and friends are part of the head commit but unchanged in a later commit.
        git(repo, "commit", "--allow-empty", "-qm", "later")
        (repo / "calculator.py").write_text("x = 1\n")
        git(repo, "commit", "-qam", "change")
        (repo / "helpers.py").write_text("dirty working tree edit\n")  # never read
        changes = capture_git.capture(repo, "HEAD~1..HEAD")
        scan = lambda text: ["generic-api-key"] if "SECRET" in text else []  # noqa: E731
        read = context_reader(changes, scan, settings=settings(tmp.name))
        self.assertEqual(read("helpers.py", 1, 5), "    1| def helper():\n    2|     return 1")
        for path, why in (("calculator.py", "changed file"), ("package-lock.json", "file rules"),
                          ("blob.bin", "binary"), ("missing.py", "not found"),
                          ("../etc/passwd", "repository path"), (".git/config", "repository path")):
            with self.assertRaises(ContextUnavailable) as refused:
                read(path, 1, 2)
            self.assertIn(why, str(refused.exception))
        (repo / "keys.py").write_text("TOKEN = 'SECRET'\n")
        git(repo, "add", "keys.py")
        git(repo, "commit", "-qm", "keys")
        git(repo, "commit", "--allow-empty", "-qm", "after keys")
        changes = capture_git.capture(repo, "HEAD~1..HEAD")
        with self.assertRaises(ContextUnavailable) as secret:
            context_reader(changes, scan, settings=settings(tmp.name))("keys.py", 1, 1)
        self.assertIn("possible secret", str(secret.exception))

    def budgeted(self, script, budget):
        model = ScriptedModel(script)
        team = Team(model)
        result = team.plan(self.files())
        return model, team.execute(result, self.files(), {"calculator.py"}, budget=budget)

    def test_the_approved_budget_stops_calls_but_keeps_the_aggregator(self):
        tiny = Budget(input_tokens=AGGREGATOR_INPUT + 300, output_tokens=AGGREGATOR_OUTPUT + 100,
                      input_price=2.5, output_price=10.0)
        model, result = self.budgeted({"coordinator": [PLAN], "python": [findings(FINDING)]}, tiny)
        python = [c for c in result.calls if c.agent == "python"]
        self.assertEqual([(c.status, c.error) for c in python], [("failed", BUDGET_REACHED)])  # no retry
        self.assertNotIn("python", [c["role"] for c in model.calls])  # nothing was sent
        self.assertTrue(any(l.startswith("Review budget reached (at most $") and "partial" in l
                            for l in result.limitations))

    def test_a_follow_up_round_stops_at_the_budget_and_the_aggregator_still_judges(self):
        class Full(ScriptedModel):  # every call uses its whole output allowance
            def complete(self, **kwargs):
                return replace(super().complete(**kwargs), output_tokens=kwargs["max_output_tokens"])

        ask = verdict(follow_ups=[("python", ["calculator.py"], "Check again?")])
        model = Full({"coordinator": [PLAN], "python": [findings(FINDING)] * 2,
                      "aggregator": [ask, verdict()]})
        team = Team(model)
        result = team.plan(self.files())
        # Output room for one specialist call and two aggregator calls, not a second specialist.
        budget = Budget(input_tokens=10**6, output_tokens=4000 + 2 * AGGREGATOR_OUTPUT,
                        input_price=2.5, output_price=10.0)
        result = team.execute(result, self.files(), {"calculator.py"}, budget=budget)
        self.assertEqual([(c.agent, c.status) for c in result.calls if c.agent != "coordinator"],
                         [("python", "ok"), ("aggregator", "ok"), ("python", "failed"),
                          ("aggregator", "ok")])
        self.assertEqual(result.calls[3].error, BUDGET_REACHED)
        self.assertEqual([(f.id, f.kept) for f in result.findings], [("python-1", True)])
        self.assertTrue(any("partial" in l for l in result.limitations))

    def test_a_failed_call_keeps_its_hold_against_the_budget(self):
        config = agents.defaults()
        config["agents"]["python"]["retries"] = 1
        model = ScriptedModel({"coordinator": [PLAN]})  # every python call fails, reporting nothing
        team = Team(model, config=config)
        result = team.plan(self.files())
        # Output room for one specialist hold plus the aggregator's share, not two holds.
        budget = Budget(input_tokens=10**6, output_tokens=4000 + AGGREGATOR_OUTPUT + 3000,
                        input_price=2.5, output_price=10.0)
        result = team.execute(result, self.files(), {"calculator.py"}, budget=budget)
        self.assertEqual([c.error for c in result.calls if c.agent == "python"],
                         ["no scripted reply", BUDGET_REACHED])

    def test_no_budget_means_no_limit(self):
        model, result = self.budgeted({"coordinator": [PLAN], "python": [findings(FINDING)],
                                       "aggregator": [verdict()]}, None)
        self.assertFalse(any("budget" in l for l in result.limitations))

    def test_fallbacks_and_scope(self):
        stray = dict(FINDING, file="other.py")
        wide = dict(FINDING, start_line=0, end_line=999)
        model = ScriptedModel({"python": [findings(stray, wide)]})  # no coordinator, no aggregator
        result = Team(model).run(self.files())
        self.assertIn("Coordinator unavailable; files were routed by type.", result.limitations)
        self.assertIn("Aggregator unavailable; 1 unvalidated finding(s) withheld.", result.limitations)
        self.assertEqual([(f.file, f.start_line, f.end_line, f.kept, f.rejection) for f in result.findings],
                         [("calculator.py", 1, 5, False, "aggregator verdict unavailable")])

    def test_aggregator_rejections_are_recorded(self):
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING, FINDING)],
                               "aggregator": [verdict(rejected=[("python-2", "duplicate")])]})
        result = Team(model).run(self.files())
        self.assertEqual([(f.id, f.kept, f.rejection) for f in result.findings],
                         [("python-1", True, None), ("python-2", False, "duplicate")])

    def test_continue_reruns_the_named_specialist_and_judges_again(self):
        second = dict(FINDING, title="Empty items return 0 only by accident", start_line=2, end_line=2)
        ask = verdict(follow_ups=[("python", ["calculator.py"], "Does the loop handle empty items?")])
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING), findings(FINDING, second)],
                               "aggregator": [ask, verdict(rejected=[("python-3", "speculative")])]})
        result = Team(model).run(self.files())
        self.assertEqual([c.agent for c in result.calls],
                         ["coordinator", "python", "aggregator", "python", "aggregator"])
        follow_up = [c for c in model.calls if c["role"] == "python"][1]
        self.assertIn("Does the loop handle empty items?", follow_up["prompt"])
        self.assertIn('"id": "python-1"', follow_up["prompt"])  # its current findings
        self.assertEqual(result.calls[3].focus, "Follow-up: Does the loop handle empty items?")
        self.assertEqual([(f.id, f.kept, f.rejection) for f in result.findings],
                         [("python-1", False, "replaced after follow-up"), ("python-2", True, None),
                          ("python-3", False, "speculative")])
        judged = json.loads([c for c in model.calls if c["role"] == "aggregator"][1]["prompt"])
        self.assertEqual(judged["round"], 2)
        self.assertEqual([f["id"] for f in judged["findings"]], ["python-2", "python-3"])

    def test_follow_ups_stop_at_the_round_cap(self):
        ask = verdict(follow_ups=[("python", ["calculator.py"], "Check again?")])
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)] * MAX_ROUNDS,
                               "aggregator": [ask] * MAX_ROUNDS})
        result = Team(model).run(self.files())
        self.assertEqual([c.agent for c in result.calls],
                         ["coordinator"] + ["python", "aggregator"] * MAX_ROUNDS)
        self.assertIn(f"Review stopped after {MAX_ROUNDS} aggregation rounds; follow-ups were not run.",
                      result.limitations)
        self.assertEqual([f.kept for f in result.findings], [False] * (MAX_ROUNDS - 1) + [True])

    def test_a_failed_specialist_call_is_retried_up_to_its_limit(self):
        model = ScriptedModel({"coordinator": [PLAN], "python": ["not json", findings(FINDING)],
                               "aggregator": [verdict()]})
        result = Team(model).run(self.files())
        attempts = [c for c in result.calls if c.agent == "python"]
        self.assertEqual([(c.status, c.error) for c in attempts],
                         [("failed", "reply was not JSON"), ("ok", None)])
        self.assertEqual([(f.id, f.kept) for f in result.findings], [("python-1", True)])

        config = agents.defaults()
        config["agents"]["python"]["retries"] = 0
        model = ScriptedModel({"coordinator": [PLAN], "python": [{"findings": "wrong"}, findings(FINDING)]})
        result = Team(model, config=config).run(self.files())
        self.assertEqual([c.status for c in result.calls if c.agent == "python"], ["failed"])
        self.assertIn("python specialist failed: reply did not match the findings contract.",
                      result.limitations)

    def test_retries_are_validated_per_specialist(self):
        config = agents.defaults()
        config["agents"]["python"]["retries"] = 2
        self.assertEqual(agents.retries(agents.validate(config), "python"), 2)
        self.assertEqual(agents.retries(agents.defaults(), "go"), agents.DEFAULT_RETRIES)
        for bad in (-1, agents.MAX_RETRIES + 1, 1.5, True, "2"):
            config["agents"]["python"]["retries"] = bad
            with self.assertRaises(agents.ConfigError):
                agents.validate(config)

    def test_restart_reviews_again_without_the_earlier_findings(self):
        restart = verdict(follow_ups=[("restart", "python", ["calculator.py"], "Focus on the loop.")])
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING), findings(FINDING)],
                               "aggregator": [restart, verdict()]})
        result = Team(model).run(self.files())
        second = [c for c in model.calls if c["role"] == "python"][1]
        self.assertIn("review these files again from the start", second["prompt"])
        self.assertIn("Focus on the loop.", second["prompt"])
        self.assertNotIn('"id": "python-1"', second["prompt"])
        self.assertEqual(result.calls[3].focus, "Restart: Focus on the loop.")
        self.assertEqual([(f.id, f.kept, f.rejection) for f in result.findings],
                         [("python-1", False, "replaced after follow-up"), ("python-2", True, None)])

    def test_aggregator_can_retry_a_specialist_that_failed(self):
        config = agents.defaults()
        config["agents"]["python"]["retries"] = 0
        retry = verdict(follow_ups=[("retry", "python", [], "")])
        model = ScriptedModel({"coordinator": [PLAN], "python": ["not json", findings(FINDING)],
                               "aggregator": [retry, verdict()]})
        result = Team(model, config=config).run(self.files())
        self.assertEqual([(c.agent, c.status) for c in result.calls],
                         [("coordinator", "ok"), ("python", "failed"), ("aggregator", "ok"),
                          ("python", "ok"), ("aggregator", "ok")])
        first = json.loads([c for c in model.calls if c["role"] == "aggregator"][0]["prompt"])
        self.assertEqual(first["failed_specialists"], {"python": ["calculator.py"]})
        self.assertEqual(result.calls[3].focus, "Retry after failure")
        self.assertEqual([(f.id, f.kept) for f in result.findings], [("python-1", True)])

    def test_only_failed_specialists_can_be_retried(self):
        retry = verdict(follow_ups=[("retry", "python", ["calculator.py"], "")])
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)],
                               "aggregator": [retry]})
        result = Team(model).run(self.files())  # python succeeded, so the retry is invalid
        self.assertEqual([(f.kept, f.rejection) for f in result.findings],
                         [(False, "aggregator verdict unavailable")])

    def test_aggregator_can_refuse_a_follow_up_edit(self):
        weaker = dict(FINDING, title="Maybe an issue", description="Could potentially be wrong.")
        ask = verdict(follow_ups=[("python", ["calculator.py"], "Is the overwrite real?")])
        refuse = verdict(refused=[("python", "the original finding was correct")])
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING), findings(weaker)],
                               "aggregator": [ask, refuse]})
        result = Team(model).run(self.files())
        judged = json.loads([c for c in model.calls if c["role"] == "aggregator"][1]["prompt"])
        self.assertEqual([f["id"] for f in judged["edits"]["python"]["replaced"]], ["python-1"])
        self.assertEqual(judged["edits"]["python"]["new_ids"], ["python-2"])
        self.assertEqual([(f.id, f.kept, f.rejection) for f in result.findings],
                         [("python-1", True, None),
                          ("python-2", False, "edit refused: the original finding was correct")])

    def test_invalid_verdicts_withhold_findings(self):
        outside = verdict(follow_ups=[("java", ["calculator.py"], "Is this Java?")])  # not on the team
        for bad in (outside, {"verdict": "MAYBE", "rejected": [], "follow_ups": [], "summary": "?"},
                    {"rejected": [], "summary": "old shape"}):
            model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)],
                                   "aggregator": [bad]})
            result = Team(model).run(self.files())
            self.assertEqual([(f.kept, f.rejection) for f in result.findings],
                             [(False, "aggregator verdict unavailable")])
            self.assertEqual(result.calls[-1].status, "failed")
            self.assertIn("Aggregator unavailable; 1 unvalidated finding(s) withheld.",
                          result.limitations)

    def test_failed_follow_up_keeps_the_earlier_findings(self):
        ask = verdict(follow_ups=[("python", ["calculator.py"], "Check again?")])
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)],
                               "aggregator": [ask, verdict()]})
        result = Team(model).run(self.files())  # the second python reply is missing
        self.assertIn("python specialist failed", " ".join(result.limitations))
        self.assertEqual([(f.id, f.kept) for f in result.findings], [("python-1", True)])

    def test_review_is_a_langgraph_fan_out_that_keeps_plan_order(self):
        both = threading.Barrier(2, timeout=5)  # passes only if both specialists run at once

        class Concurrent(ScriptedModel):
            def complete(self, **kwargs):
                if kwargs["role"] != "aggregator":
                    both.wait()
                return super().complete(**kwargs)

        model = Concurrent({"python": [findings(FINDING)], "javascript": [findings(FINDING)],
                            "aggregator": [verdict()]})
        team = Team(model)
        self.assertEqual(set(team.reviewer.get_graph().nodes),
                         {"__start__", "specialist", "collect", "aggregator", "__end__"})
        result = TeamResult()
        result.plan = {"python": {"files": ["calculator.py"], "focus": ""},
                       "javascript": {"files": ["calculator.py"], "focus": ""}}
        team.execute(result, self.files(), {"calculator.py"})
        self.assertEqual([c.agent for c in result.calls], ["python", "javascript", "aggregator"])
        self.assertEqual([f.id for f in result.findings], ["python-1", "javascript-1"])

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
