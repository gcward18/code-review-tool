import json
import tempfile
import unittest

from autosde import chat
from autosde.db import Store
from autosde.llm import ScriptedModel
from autosde.review import run_review
from helpers import FINDING, PLAN, clean_scan, findings, repository, settings, verdict


class ChatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = settings(tmp.name)
        self.store = Store(self.settings.db_path)
        repo = repository(tmp.name)
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)],
                               "aggregator": [verdict()]})
        run_review(repo, "HEAD~1..HEAD", store=self.store, model=model, settings=self.settings,
                   scan=clean_scan)
        self.project = self.store.projects()[0]

    def test_answers_from_review_history_and_keeps_the_conversation(self):
        model = ScriptedModel({"chat": [{"answer": "Review 1 found an overwritten total."},
                                        {"answer": "Line 4."}]})
        chat.ask(self.store, model, self.project, "What was found?", scan=clean_scan)
        messages = chat.ask(self.store, model, self.project, "Which line?", scan=clean_scan)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"] * 2)
        self.assertEqual(messages[-1]["content"], "Line 4.")
        sent = json.loads(model.calls[1]["prompt"])
        self.assertEqual(sent["reviews"][0]["findings"][0]["title"], "Total is overwritten")
        self.assertEqual(sent["reviews"][0]["review"], 1)
        self.assertNotIn("id", sent["reviews"][0])  # the model sees review numbers, not ids
        self.assertEqual(sent["conversation"][0]["content"], "What was found?")
        self.assertIn("project historian", model.calls[0]["system"])

    def test_refuses_flagged_payloads_and_failed_calls_without_saving(self):
        model = ScriptedModel({"chat": []})
        with self.assertRaises(chat.ChatError) as error:
            chat.ask(self.store, model, self.project, "private-sentinel",
                     scan=lambda text: ["generic-api-key"] if "private-sentinel" in text else [])
        self.assertNotIn("private-sentinel", str(error.exception))
        self.assertEqual(model.calls, [])
        with self.assertRaises(chat.ChatError):
            chat.ask(self.store, model, self.project, "hello", scan=clean_scan)  # no reply
        self.assertEqual(self.store.chat(self.project["id"]), [])

    def test_home_chat_sees_every_repository_and_keeps_its_own_history(self):
        from autosde import git

        repos = [{"name": "calculator", "path": self.project["path"],
                  "git": git.summary(self.project["path"])},
                 {"name": "other", "path": "/elsewhere/other", "git": {"commits": 3}}]
        model = ScriptedModel({"chat": [{"answer": "calculator has one open finding."}]})
        messages = chat.ask(self.store, model, None, "What needs attention?", scan=clean_scan,
                            repositories=repos)
        self.assertEqual(messages[-1]["content"], "calculator has one open finding.")
        sent = json.loads(model.calls[0]["prompt"])
        self.assertEqual(sent["scope"], "all projects")
        calc, other = sent["projects"]
        self.assertEqual(calc["commits"], 2)
        self.assertIn(".py", calc["top_file_types"])
        self.assertIn("Total is overwritten", calc["reviews"][0]["kept_findings"][0])
        self.assertNotIn("reviews", other)
        self.assertEqual(self.store.chat(self.project["id"]), [])  # separate from project chats
        self.store.clear_chat(None)
        self.assertEqual(self.store.chat(None), [])

    def test_review_numbers_count_per_project_by_run_time(self):
        import time

        other = self.store.project("/elsewhere/other", "other")
        changes = type("C", (), {"range_spec": "a..b", "base": "a", "head": "b", "files": []})()
        for _ in range(2):
            self.store.start_review(other, changes, "m")
            time.sleep(0.01)
        self.store.start_review(self.project["id"], changes, "m")
        numbers = {(r["project"], r["number"]) for r in self.store.reviews()}
        self.assertEqual(numbers, {("calculator", 1), ("calculator", 2), ("other", 1), ("other", 2)})
        newest = self.store.reviews(self.project["id"])[0]
        self.assertEqual(self.store.review(newest["id"])["number"], 2)


if __name__ == "__main__":
    unittest.main()
