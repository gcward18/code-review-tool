"""The Lambda handlers end to end, on moto: sign-in claims, routes, jobs and the worker."""

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import boto3
from moto import mock_aws

from autosde import agents, aws, tracing
from autosde.dynamo import create_table
from autosde.llm import ScriptedModel
from helpers import FINDING, PLAN, findings, git, verdict
from test_remote_batch import FakeGitHub, history

GITLEAKS = os.environ.get("AUTOSDE_GITLEAKS")
CLAIMS = {"sub": "user-1", "email": "dev@example.com"}


class Invokes:
    """Stands in for the Lambda client: records worker invocations."""

    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(json.loads(kwargs["Payload"]))
        assert kwargs["InvocationType"] == "Event"


def event(method, path, body=None, query=None, claims=CLAIMS):
    e = {"rawPath": path, "requestContext": {"http": {"method": method}},
         "headers": {"content-type": "application/json"}, "queryStringParameters": query}
    if claims:
        e["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    if body is not None:
        e["body"] = json.dumps(body)
    return e


@mock_aws
@unittest.skipUnless(GITLEAKS, "needs AUTOSDE_GITLEAKS")
class LambdaTests(unittest.TestCase):
    def setUp(self):
        os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
                          TABLE_NAME="autosde-test", BUCKET_NAME="autosde-test-docs", SSM_PREFIX="/autosde/test",
                          WORKER_FUNCTION="autosde-worker", GITLEAKS_PATH=GITLEAKS)
        self.addCleanup(self.reset)
        create_table(boto3.resource("dynamodb"), "autosde-test")
        boto3.client("s3").create_bucket(Bucket="autosde-test-docs")
        ssm = boto3.client("ssm")
        for name, value in (("model", "gpt-6-luna"), ("input_price", "0.10"), ("output_price", "0.50"),
                            ("openai_secret_id", "autosde/openai-api-key")):
            ssm.put_parameter(Name=f"/autosde/test/{name}", Value=value, Type="String")
        boto3.client("secretsmanager").create_secret(Name="autosde/openai-api-key", SecretString="sk-test")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = history(Path(tmp.name) / "up")
        aws._APP = None
        self.app = aws.app()
        self.invokes = Invokes()
        self.app.jobs.client = self.invokes
        self.app.github_client = FakeGitHub(self.local)
        self.app.model_factory = lambda: ScriptedModel(
            {"coordinator": [PLAN], "python": [findings(FINDING)], "aggregator": [verdict()]})

    @staticmethod
    def reset():
        aws._APP = None
        agents.BACKEND = agents.LANGSMITH_KEY_AVAILABLE = None
        tracing.KEY_PROVIDER = tracing.WORKSPACE_ID = None

    def call(self, method, path, body=None, query=None, claims=CLAIMS):
        reply = aws.api(event(method, path, body, query, claims))
        return reply["statusCode"], json.loads(reply["body"])

    def run_jobs(self):
        while self.invokes.calls:
            payload = self.invokes.calls.pop(0)
            self.assertEqual(aws.worker(payload)["outcome"], "ok", payload["job"])

    def test_sign_in_is_required_and_the_session_reports_the_user(self):
        self.assertEqual(self.call("GET", "/api/projects", claims=None)[0], 401)
        status, session = self.call("GET", "/api/session")
        self.assertEqual((status, session["cloud"], session["user"]["email"]), (200, True, "dev@example.com"))
        self.assertNotIn("token", session)
        self.assertEqual(self.call("GET", "/api/tree")[0], 404)  # no local folders in AWS
        self.assertEqual(self.call("GET", "/api/repo", query={"path": "/Users/x/repo"})[0], 400)
        bad = aws.api({**event("POST", "/api/reviews", {}), "headers": {"content-type": "text/plain"}})
        self.assertEqual(bad["statusCode"], 415)

    def test_github_review_runs_through_worker_jobs(self):
        status, remote = self.call("POST", "/api/remotes", {"repo": "octo/calc"})
        self.assertEqual(status, 200)
        path = remote["path"]
        self.assertEqual(self.call("GET", "/api/workspaces")[1]["github"][0]["name"], "octo/calc")
        parent = git(self.local, "rev-parse", "main~1")
        status, started = self.call("POST", "/api/reviews", {"repo": path, "from": "main", "to": parent})
        self.assertEqual(status, 202)
        self.assertEqual(self.invokes.calls[0]["job"], "plan")
        self.assertEqual(self.call("GET", f"/api/reviews/{started['id']}")[1]["status"], "planning")
        self.run_jobs()
        plan = self.call("GET", f"/api/reviews/{started['id']}")[1]
        self.assertEqual((plan["status"], plan["proposed"]), ("awaiting_approval", ["step1.py"]))
        self.call("POST", f"/api/reviews/{started['id']}/approve", {"files": ["step1.py"]})
        self.assertEqual(self.invokes.calls[0], {"job": "approve", "args": {"review_id": started["id"],
                                                                           "selection": ["step1.py"]}})
        self.run_jobs()
        review = self.call("GET", f"/api/reviews/{started['id']}")[1]
        self.assertEqual((review["status"], review["project"]), ("completed", "octo/calc"))
        files = self.call("GET", f"/api/reviews/{started['id']}/files")[1]["files"]
        self.assertEqual([f["path"] for f in files], ["step1.py"])

    def test_batches_run_one_commit_per_worker_invocation(self):
        path = self.call("POST", "/api/remotes", {"repo": "octo/calc"})[1]["path"]
        root = git(self.local, "rev-list", "--max-parents=0", "HEAD")
        plan = self.call("POST", "/api/batches", {"repo": path, "from": "main", "to": root, "mode": "latest",
                                                  "limit": 2})[1]
        self.app.model_factory = lambda: ScriptedModel(
            {"coordinator": [PLAN], "python": [findings()], "aggregator": [verdict()]})
        status, _ = self.call("POST", f"/api/batches/{plan['id']}/start", {"max_cost": plan["max_cost"]})
        self.assertEqual(status, 200)
        steps = 0
        while self.invokes.calls:
            payload = self.invokes.calls.pop(0)
            self.assertEqual(payload["job"], "batch_step")
            aws.worker(payload)
            steps += 1
        batch = self.call("GET", f"/api/batches/{plan['id']}")[1]
        self.assertEqual((steps, batch["status"], batch["done"]), (2, "completed", 2))

    def test_uploads_reach_the_worker_through_s3_and_are_deleted(self):
        self.app.model_factory = lambda: ScriptedModel({})
        base = self.call("POST", "/api/knowledge", {"name": "rules"})[1]
        text = b"# Totals\n\nAccumulate prices with += in loops."
        self.call("POST", f"/api/knowledge/{base['id']}/documents",
                  {"name": "rules.md", "data": base64.b64encode(text).decode()})
        job = self.invokes.calls[0]
        self.assertTrue(job["args"]["upload"].startswith("uploads/"))
        self.run_jobs()
        doc = self.call("GET", f"/api/knowledge/{base['id']}")[1]["documents"][0]
        self.assertEqual(doc["status"], "ready")
        keys = [o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket="autosde-test-docs").get("Contents", [])]
        self.assertFalse([k for k in keys if k.startswith("uploads/")])


if __name__ == "__main__":
    unittest.main()
