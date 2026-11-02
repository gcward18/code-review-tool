"""AutoSDE on AWS: settings from SSM Parameter Store, secrets from Secrets Manager, and the
DynamoDB/S3 backends wired in place of the local SQLite and files.

Parameters live under a prefix such as /autosde/dev/ (model, input_price, output_price,
reasoning_effort, openai_secret_id, langsmith_secret_id, github_secret_id,
langsmith_workspace_id). Secret values are fetched on first use, cached for the process,
and never logged or returned by the API.
"""

import json
import os
from pathlib import Path

from autosde import agents, tracing
from autosde.config import Settings

REQUIRED = ("model", "input_price", "output_price")


class Secrets:
    def __init__(self, client):
        self.client, self._cache = client, {}

    def get(self, secret_id):
        if not secret_id:
            return None
        if secret_id not in self._cache:
            try:
                raw = self.client.get_secret_value(SecretId=secret_id)["SecretString"]
            except Exception:
                raise RuntimeError(f"Secret {secret_id} could not be read") from None
            # A JSON secret may hold {"api_key": ...} or {"token": ...}; a plain string is the value.
            if raw.lstrip().startswith("{"):
                data = json.loads(raw)
                raw = data.get("api_key") or data.get("token") or next(iter(data.values()), "")
            self._cache[secret_id] = raw.strip()
        return self._cache[secret_id]

    def available(self, secret_id):
        try:
            return bool(self.get(secret_id))
        except RuntimeError:
            return False


def load_parameters(ssm, prefix):
    """All parameters under `prefix` as {name: value} (names without the prefix)."""
    prefix = prefix.rstrip("/") + "/"
    values, token = {}, None
    while True:
        kwargs = {"Path": prefix, "Recursive": False, "WithDecryption": True}
        if token:
            kwargs["NextToken"] = token
        page = ssm.get_parameters_by_path(**kwargs)
        for p in page.get("Parameters", []):
            values[p["Name"][len(prefix):]] = p["Value"]
        token = page.get("NextToken")
        if not token:
            return values


def settings_from(parameters, *, gitleaks=None):
    missing = [k for k in REQUIRED if not parameters.get(k)]
    if missing:
        raise RuntimeError(f"Missing SSM parameters: {', '.join(missing)}")
    effort = parameters.get("reasoning_effort", "low")
    return Settings(
        model=parameters["model"],
        input_price=float(parameters["input_price"]),
        output_price=float(parameters["output_price"]),
        reasoning_effort=None if effort == "none" else effort,
        db_path=Path("/tmp/unused.db"),  # no SQLite in AWS
        gitleaks=gitleaks,
        secret_id=parameters.get("openai_secret_id"),
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        langsmith_secret_id=parameters.get("langsmith_secret_id"),
        github_secret_id=parameters.get("github_secret_id"),
    )


class Cloud:
    """Everything the AWS handlers need, built once per Lambda container."""

    def __init__(self, *, table, ssm, secretsmanager, prefix, s3=None, bucket=None, gitleaks=None):
        from autosde.dynamo import (DynamoBatchStore, DynamoConfigStore, DynamoKnowledgeStore,
                                    DynamoRemoteStore, DynamoStore)

        self.parameters = load_parameters(ssm, prefix)
        self.settings = settings_from(self.parameters, gitleaks=gitleaks)
        self.secrets = Secrets(secretsmanager)
        self.table, self.s3, self.bucket = table, s3, bucket
        self.store = DynamoStore(table)
        self.knowledge_store = lambda: DynamoKnowledgeStore(table, s3=s3, bucket=bucket)
        self.batch_store = lambda: DynamoBatchStore(table)
        self.remote_store = lambda: DynamoRemoteStore(table)
        # Agent settings and prompts come from DynamoDB; LangSmith and GitHub from secrets.
        agents.BACKEND = DynamoConfigStore(table)
        agents.LANGSMITH_KEY_AVAILABLE = lambda: self.secrets.available(self.settings.langsmith_secret_id)
        tracing.KEY_PROVIDER = lambda: self.secrets.get(self.settings.langsmith_secret_id)
        tracing.WORKSPACE_ID = self.parameters.get("langsmith_workspace_id") or None

    def model(self):
        from autosde.llm import OpenAIModel

        return OpenAIModel(self.settings.model, api_key=self.secrets.get(self.settings.secret_id),
                           reasoning_effort=self.settings.reasoning_effort)

    def github_client(self):
        from autosde.github import Client

        return Client(token=lambda: self.secrets.get(self.settings.github_secret_id))
