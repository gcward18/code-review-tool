"""AWS configuration: SSM settings, Secrets Manager secrets and DynamoDB agent config."""

import json
import os
import unittest

import boto3
from moto import mock_aws

from autosde import agents, team, tracing
from autosde.cloud import Cloud, Secrets, settings_from
from autosde.dynamo import create_table

PREFIX = "/autosde/test"


@mock_aws
class CloudTests(unittest.TestCase):
    def setUp(self):
        os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="testing",
                          AWS_SECRET_ACCESS_KEY="testing")
        self.addCleanup(self.reset)
        ssm = boto3.client("ssm")
        for name, value in (("model", "gpt-6-luna"), ("input_price", "0.10"), ("output_price", "0.50"),
                            ("reasoning_effort", "low"), ("openai_secret_id", "autosde/openai-api-key"),
                            ("langsmith_secret_id", "autosde/langsmith-api-key"),
                            ("github_secret_id", "autosde/github-token"), ("langsmith_workspace_id", "ws-1")):
            ssm.put_parameter(Name=f"{PREFIX}/{name}", Value=value, Type="String")
        secrets = boto3.client("secretsmanager")
        secrets.create_secret(Name="autosde/openai-api-key", SecretString=json.dumps({"api_key": "sk-private-sentinel"}))
        secrets.create_secret(Name="autosde/langsmith-api-key", SecretString="lsv2_private-sentinel")  # gitleaks:allow
        secrets.create_secret(Name="autosde/github-token", SecretString="ghtoken-private-sentinel")
        self.table = create_table(boto3.resource("dynamodb"), "autosde-test")
        self.cloud = Cloud(table=self.table, ssm=ssm, secretsmanager=secrets, prefix=PREFIX, gitleaks="/opt/gitleaks")

    @staticmethod
    def reset():
        agents.BACKEND = agents.LANGSMITH_KEY_AVAILABLE = None
        tracing.KEY_PROVIDER = tracing.WORKSPACE_ID = None

    def test_settings_and_secrets(self):
        s = self.cloud.settings
        self.assertEqual((s.model, s.input_price, s.output_price, s.reasoning_effort, s.gitleaks),
                         ("gpt-6-luna", 0.10, 0.50, "low", "/opt/gitleaks"))
        self.assertEqual(self.cloud.model()._key, "sk-private-sentinel")
        self.assertNotIn("sentinel", repr(self.cloud.model()))
        with self.assertRaises(RuntimeError) as missing:
            settings_from({"model": "m"})
        self.assertIn("input_price", str(missing.exception))
        with self.assertRaises(RuntimeError) as unreadable:
            Secrets(boto3.client("secretsmanager")).get("autosde/nope")
        self.assertNotIn("sentinel", str(unreadable.exception))

    def test_agent_settings_and_prompts_live_in_dynamodb_with_versions(self):
        snapshot = agents.snapshot()
        self.assertTrue(snapshot["langsmith_key"])
        bundled = (agents.PROMPTS / "specialist.md").read_text()
        self.assertEqual(snapshot["prompts"]["specialist"], bundled)  # default until edited
        agents.update({"config": {"agents": {"python": {"model": "gpt-6-luna-mini"}}},
                       "prompts": {"specialist": "You are the {agent} reviewer. Cloud copy."},
                       "focus": {"python": "Check loops."}})
        self.assertEqual(agents.load_config()["agents"]["python"]["model"], "gpt-6-luna-mini")
        self.assertEqual(team.prompt("specialist.md", agent="python"), "You are the python reviewer. Cloud copy.\n")
        self.assertEqual(agents.read_text("focus/python.md"), "Check loops.\n")
        self.assertEqual((agents.PROMPTS / "specialist.md").read_text(), bundled)  # files untouched
        agents.update({"prompts": {"specialist": "Second {agent} version."}})
        self.assertEqual([v["version"] for v in agents.BACKEND.versions("specialist.md")], [1, 2])

    def test_langsmith_key_and_github_token_come_from_secrets(self):
        from autosde.llm import ScriptedModel

        config = agents.validate({"langsmith": {"enabled": True, "project": "p"}, "agents": {}})
        traced = tracing.traced(ScriptedModel({}), review_id=1, name="t", scan=lambda t: [], config=config, environ={})
        self.assertIsInstance(traced, tracing.TracedModel)
        self.assertEqual(traced.tracer._headers["x-tenant-id"], "ws-1")

        sent = []

        class Opener:
            def open(self, request, timeout):
                sent.append(dict(request.header_items()))

                class Reply:
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def read(self, n):
                        return b'{"default_branch": "main", "private": true}'
                return Reply()
        client = self.cloud.github_client()
        client.opener = Opener()
        self.assertFalse(client.authenticated())  # the token replaces the gh CLI
        self.assertEqual(client.get("repos/octo/calc")["default_branch"], "main")
        self.assertEqual(sent[0]["Authorization"], "Bearer ghtoken-private-sentinel")


if __name__ == "__main__":
    unittest.main()
