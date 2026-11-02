"""History batches and GitHub repositories on the DynamoDB backend."""

import os
import tempfile
import threading
import unittest
from pathlib import Path

import boto3
from moto import mock_aws

from autosde import github
from autosde.batch import BatchError, Batches
from autosde.dynamo import DynamoBatchStore, DynamoRemoteStore, create_table
from autosde.remote import Remotes
from helpers import git, settings
from test_remote_batch import FakeGitHub, history


@mock_aws
class DynamoBatchTests(unittest.TestCase):
    def setUp(self):
        os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="testing",
                          AWS_SECRET_ACCESS_KEY="testing")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = history(Path(tmp.name) / "up")
        self.settings = settings(tmp.name)
        self.table = create_table(boto3.resource("dynamodb"), "autosde-test")
        self.batches = Batches(DynamoBatchStore(self.table))
        self.root = git(self.repo, "rev-list", "--max-parents=0", "HEAD")

    def test_plan_run_cap_cancel_and_list(self):
        batch = self.batches.plan(self.repo, "HEAD", self.root, mode="every", settings=self.settings)
        self.assertEqual((batch["status"], len(batch["commits"])), ("planned", 4))
        self.assertIsInstance(batch["commits"], list)
        seen = []

        def review(sha):
            seen.append(sha)
            return len(seen), 0.01
        self.batches.start(batch["id"], review, max_cost=0.02).join(5)
        done = self.batches.get(batch["id"])
        self.assertEqual((done["status"], done["done"], done["review_ids"]), ("budget", 2, [1, 2]))
        self.assertAlmostEqual(done["spent"], 0.02)
        with self.assertRaises(BatchError):
            self.batches.start(batch["id"], review)

        second = self.batches.plan(self.repo, "HEAD", self.root, mode="merges", settings=self.settings)
        gate, entered = threading.Event(), threading.Event()

        def slow(sha):
            entered.set()
            gate.wait(5)
            return 9, 0.0
        thread = self.batches.start(second["id"], slow)
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.batches.cancel(second["id"])["status"], "cancelling")
        gate.set()
        thread.join(5)
        self.assertEqual(self.batches.get(second["id"])["status"], "completed")  # its only commit finished
        listed = self.batches.for_repo(str(self.repo.resolve()))
        self.assertEqual([b["id"] for b in listed], [second["id"], batch["id"]])  # newest first
        with self.assertRaises(BatchError):
            self.batches.get(999)

    def test_github_repositories(self):
        fake = FakeGitHub(self.repo)
        remotes = Remotes(DynamoRemoteStore(self.table), client=fake)
        remote_id = remotes.add("octo/calc")
        self.assertEqual(remotes.get(remote_id)["path"], "https://github.com/octo/calc")
        self.assertEqual(remotes.by_path("https://github.com/octo/calc")["id"], remote_id)
        with self.assertRaises(github.GitHubError):
            remotes.add("https://github.com/octo/calc")
        with self.assertRaises(github.GitHubError):
            remotes.add("octo/missing")
        remotes.remove(remote_id)
        self.assertEqual(remotes.list(), [])
        remotes.add("octo/calc")  # the path is free again


if __name__ == "__main__":
    unittest.main()
