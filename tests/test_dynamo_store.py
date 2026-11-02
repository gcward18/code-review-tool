"""The DynamoDB store must answer exactly like the SQLite store for the same history."""

import os
import tempfile
import unittest
from pathlib import Path

import boto3
from moto import mock_aws

from autosde.db import Store
from autosde.dynamo import DynamoStore, create_table
from autosde.llm import ScriptedModel
from autosde.review import approve, cancel, prepare
from helpers import FINDING, PLAN, clean_scan, findings, git, repository, settings, verdict

VOLATILE = {"started_at", "finished_at", "created_at", "last_review", "last_run"}


def normalize(value):
    """Drop timestamps and per-backend ids that legitimately differ."""
    if isinstance(value, dict):
        # Agent runs and findings are numbered per review in DynamoDB, globally in SQLite;
        # the UI only uses those ids as row keys.
        child = "role" in value or "finding_key" in value
        return {k: normalize(v) for k, v in value.items()
                if k not in VOLATILE | {"review_id"} and not (child and k == "id")}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def scripted():
    return ScriptedModel({"coordinator": [PLAN, PLAN, PLAN], "python": [findings(FINDING), findings()],
                          "aggregator": [verdict(), verdict()]})


def exercise(store, repo, cfg):
    """A small history: two completed reviews, one cancelled, one failed, and chats."""
    model = scripted()
    first = prepare(repo, "HEAD~1..HEAD", store=store, model=model, settings=cfg, scan=clean_scan)
    approve(first, store=store, settings=cfg)
    second = prepare(repo, "HEAD~1..HEAD", store=store, model=model, settings=cfg, scan=clean_scan)
    approve(second, store=store, settings=cfg, selection={"calculator.py"})
    third = prepare(repo, "HEAD~1..HEAD", store=store, model=model, settings=cfg, scan=clean_scan)
    cancel(third, store=store)
    project = store.review(first.review_id)["project_id"]
    broken = store.start_review(project, type("C", (), {"range_spec": "x..y", "base": "x", "head": "y",
                                                         "files": []})(), "m")
    store.fail_review(broken, "Review failed (RuntimeError)")
    store.add_chat(project, "user", "What broke?")
    store.add_chat(project, "assistant", "The total.", model="m", input_tokens=5, output_tokens=2)
    store.add_chat(None, "user", "Across projects?")
    other = store.project("/elsewhere/other", "other")
    return project, other, [first.review_id, second.review_id, third.review_id, broken]


@mock_aws
class DynamoParityTests(unittest.TestCase):
    def setUp(self):
        os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="testing",
                          AWS_SECRET_ACCESS_KEY="testing")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = repository(tmp.name)
        self.cfg = settings(tmp.name)
        self.sqlite = Store(Path(tmp.name) / "s.db")
        table = create_table(boto3.resource("dynamodb"), "autosde-test")
        self.dynamo = DynamoStore(table)
        self.ids = {}
        for name, store in (("sqlite", self.sqlite), ("dynamo", self.dynamo)):
            self.ids[name] = exercise(store, self.repo, self.cfg)

    def same(self, query):
        self.assertEqual(normalize(query(self.dynamo, self.ids["dynamo"])),
                         normalize(query(self.sqlite, self.ids["sqlite"])))

    def test_projects_and_lookup(self):
        self.same(lambda s, ids: s.projects())
        self.same(lambda s, ids: s.find_project(ids[0]))
        self.same(lambda s, ids: s.find_project("calculator"))
        self.same(lambda s, ids: s.find_project(str(self.repo)))
        self.assertIsNone(self.dynamo.find_project("nope"))
        self.assertEqual(self.dynamo.project(self.repo.resolve(), "calculator"), self.ids["dynamo"][0])

    def test_review_lists_details_and_numbers(self):
        self.same(lambda s, ids: s.reviews())
        self.same(lambda s, ids: s.reviews(ids[0]))
        self.same(lambda s, ids: s.reviews(limit=2))
        for index in range(4):
            self.same(lambda s, ids, i=index: s.review(ids[2][i]))
        self.assertEqual([r["number"] for r in self.dynamo.reviews(self.ids["dynamo"][0])], [4, 3, 2, 1])
        self.assertIsNone(self.dynamo.review(999))

    def test_history_agents_and_chat(self):
        self.same(lambda s, ids: s.history(ids[0]))
        self.same(lambda s, ids: s.agents())
        self.same(lambda s, ids: s.agents(ids[0]))
        self.same(lambda s, ids: s.chat(ids[0]))
        self.same(lambda s, ids: s.chat(None))
        self.dynamo.clear_chat(self.ids["dynamo"][0])
        self.assertEqual(self.dynamo.chat(self.ids["dynamo"][0]), [])
        self.assertEqual(len(self.dynamo.chat(None)), 1)


if __name__ == "__main__":
    unittest.main()
