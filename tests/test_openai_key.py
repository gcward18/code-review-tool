import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from autosde import llm
from autosde.llm import OpenAIModel
from helpers import settings


def boto_session(secret_string=None, error=None):
    client = MagicMock()
    if error is not None:
        client.get_secret_value.side_effect = error
    else:
        client.get_secret_value.return_value = {"SecretString": secret_string}
    session = MagicMock()
    session.return_value.client.return_value = client
    return session, client


class OpenAIKeyTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("OPENAI_API_KEY", None)

    def test_explicit_key_and_environment_never_touch_aws(self):
        with patch.object(llm, "_key_from_secrets_manager", side_effect=AssertionError("AWS")):
            self.assertEqual(OpenAIModel("m", api_key="sk-explicit", secret_id="s")._key, "sk-explicit")
            os.environ["OPENAI_API_KEY"] = "sk-env"
            self.assertEqual(OpenAIModel("m", secret_id="s")._key, "sk-env")

    def test_secret_is_the_fallback_and_stays_out_of_the_environment(self):
        for stored in ("sk-raw\n", json.dumps({"api_key": "sk-json"})):
            session, client = boto_session(stored)
            with patch("boto3.Session", session):
                model = OpenAIModel("m", secret_id="autosde/key", aws_profile="dev", aws_region="eu-west-1")
            self.assertIn(model._key, {"sk-raw", "sk-json"})
            self.assertNotIn("OPENAI_API_KEY", os.environ)
            session.assert_called_with(profile_name="dev", region_name="eu-west-1")
            client.get_secret_value.assert_called_with(SecretId="autosde/key")
            self.assertNotIn(model._key, repr(model))

    def test_no_secret_configured_means_no_aws_call_and_a_clear_error(self):
        with patch("boto3.Session", side_effect=AssertionError("AWS")):
            with self.assertRaises(RuntimeError) as error:
                OpenAIModel("m")
        self.assertIn("OPENAI_API_KEY", str(error.exception))

    def test_aws_failures_raise_a_fixed_message_without_details(self):
        session, _ = boto_session(error=RuntimeError("arn:aws:secretsmanager:private-sentinel"))
        with patch("boto3.Session", session):
            with self.assertRaises(RuntimeError) as error:
                OpenAIModel("m", secret_id="s")
        self.assertEqual(str(error.exception), "OpenAI key could not be read from Secrets Manager")
        self.assertIsNone(error.exception.__cause__)
        self.assertNotIn("private-sentinel", repr(error.exception))

    def test_missing_boto3_says_how_to_install_it(self):
        with patch.dict("sys.modules", {"boto3": None}):
            with self.assertRaises(RuntimeError) as error:
                OpenAIModel("m", secret_id="s")
        self.assertIn(".[aws]", str(error.exception))

    def test_from_settings_passes_the_secret_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings(tmp, model="gpt-6-luna", secret_id="autosde/key", aws_profile="dev")
        with patch.object(llm, "_key_from_secrets_manager", return_value="sk-secret") as lookup:
            model = OpenAIModel.from_settings(config)
        lookup.assert_called_once_with("autosde/key", "dev", "us-east-1")
        self.assertEqual((model.model, model.reasoning_effort, model._key), ("gpt-6-luna", "low", "sk-secret"))


if __name__ == "__main__":
    unittest.main()
