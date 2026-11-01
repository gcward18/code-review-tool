import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosde import agents, git as capture_git
from autosde.llm import ScriptedModel
from autosde.team import Team, route
from helpers import FINDING, PLAN, findings, repository, verdict


class AgentConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.prompts = Path(tmp.name) / "prompts"
        shutil.copytree(agents.PROMPTS, self.prompts, ignore=shutil.ignore_patterns("agents.json"))
        patcher = patch.object(agents, "PROMPTS", self.prompts)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = repository(tmp.name)

    def test_validation_rejects_conflicts_and_keeps_core_agents_on(self):
        with self.assertRaises(agents.ConfigError):
            agents.validate({"agents": {"go": {"extensions": [".py"]}}})
        with self.assertRaises(agents.ConfigError):
            agents.validate({"agents": {"python": {"model": "bad model;"}}})
        clean = agents.validate({"agents": {"general": {"enabled": False}, "rust": {"extensions": ["rs"]}}})
        self.assertTrue(clean["agents"]["general"]["enabled"])
        self.assertEqual(clean["agents"]["rust"]["extensions"], [".rs"])

    def test_disabled_specialist_routes_to_general_and_model_override_is_used(self):
        agents.save_config({"agents": {"python": {"enabled": False},
                                       "aggregator": {"model": "gpt-6-luna-mini"}}})
        self.assertEqual(route("calculator.py"), "general")
        files = capture_git.capture(self.repo, "HEAD~1..HEAD").files
        model = ScriptedModel({"coordinator": [PLAN], "general": [findings(FINDING)],
                               "aggregator": [verdict()]})
        result = Team(model).run(files)  # PLAN names python, which is now off
        self.assertIn("general", result.plan)
        self.assertNotIn("python", result.plan)
        models = {c["role"]: c["model"] for c in model.calls}
        self.assertEqual(models["aggregator"], "gpt-6-luna-mini")
        self.assertIsNone(models["coordinator"])

    def test_update_is_all_or_nothing(self):
        before = agents.snapshot()
        with self.assertRaises(agents.ConfigError):
            agents.update({"prompts": {"coordinator": "new text", "specialist": "no placeholder"}})
        self.assertEqual(agents.snapshot(), before)
        after = agents.update({"prompts": {"coordinator": "new text"}, "focus": {"go": "goroutines"},
                               "config": {"agents": {"java": {"enabled": False}}}})
        self.assertEqual(after["prompts"]["coordinator"], "new text\n")
        self.assertEqual(after["focus"]["go"], "goroutines\n")
        self.assertFalse(json.loads((self.prompts / "agents.json").read_text())["agents"]["java"]["enabled"])


class ReasoningTests(unittest.TestCase):
    def test_reasoning_is_sent_only_to_reasoning_models(self):
        from autosde.llm import reasons

        for model in ("gpt-6-luna", "gpt-5.6-sol", "o3", "o4-mini"):
            self.assertTrue(reasons(model), model)
        for model in ("gpt-4o", "gpt-4.1-mini", "gpt-3.5-turbo"):
            self.assertFalse(reasons(model), model)


class ModelListTests(unittest.TestCase):
    def test_keeps_text_models_and_hides_the_key(self):
        from autosde.llm import OpenAIModel

        listing = {"data": [{"id": i} for i in ("gpt-6-luna", "gpt-4o-audio-preview",
                                                 "text-embedding-3-small", "o3", "whisper-1")]}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return json.dumps(listing).encode()

        model = OpenAIModel("gpt-6-luna", api_key="private-sentinel")
        with patch.object(model._opener, "open", return_value=Response()) as opened:
            self.assertEqual(model.models(), ["gpt-6-luna", "o3"])
        self.assertEqual(opened.call_args[0][0].full_url, "https://api.openai.com/v1/models")
        with patch.object(model._opener, "open", side_effect=OSError("private-sentinel")):
            with self.assertRaises(RuntimeError) as error:
                model.models()
        self.assertNotIn("private-sentinel", str(error.exception))


if __name__ == "__main__":
    unittest.main()
