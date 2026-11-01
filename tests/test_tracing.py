import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosde import agents, tracing
from autosde.db import Store
from autosde.llm import ScriptedModel
from autosde.review import run_review
from helpers import FINDING, HEAD, PLAN, findings, repository, settings, verdict

KEY = "lsv2_private-sentinel-key"


class FakeLangSmith:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.get_method(), request.full_url, dict(request.header_items()),
                              json.loads(request.data)))

        class Done:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Done()


class TracingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        prompts = Path(tmp.name) / "prompts"
        shutil.copytree(agents.PROMPTS, prompts, ignore=shutil.ignore_patterns("agents.json"))
        patcher = patch.object(agents, "PROMPTS", prompts)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = repository(tmp.name)
        self.settings = settings(tmp.name)
        self.store = Store(self.settings.db_path)

    def review(self, *, enabled=True, environ=None, scan=lambda text: []):
        agents.save_config({"langsmith": {"enabled": enabled, "project": "team-tests"},
                            "agents": {"aggregator": {"trace": False}}})
        fake = FakeLangSmith()
        real = tracing.traced

        def traced(model, **kwargs):
            wrapped = real(model, environ={"LANGSMITH_API_KEY": KEY} if environ is None else environ,
                           opener=fake, **kwargs)
            self.wrapped = wrapped
            return wrapped

        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)],
                               "aggregator": [verdict()]})
        with patch.object(tracing, "traced", traced):
            run_review(self.repo, "HEAD~1..HEAD", store=self.store, model=model,
                       settings=self.settings, scan=scan)
        if isinstance(self.wrapped, tracing.TracedModel):
            self.wrapped.tracer.flush()
        return fake.requests

    def test_review_is_one_trace_with_full_content_per_traced_agent(self):
        requests = self.review()
        posts = [body for method, _, _, body in requests if method == "POST"]
        root, *calls = posts
        self.assertEqual(root["run_type"], "chain")
        self.assertEqual(root["session_name"], "team-tests")
        self.assertEqual([c["name"] for c in calls], ["coordinator", "python"])  # aggregator off
        for call in calls:
            self.assertEqual(call["trace_id"], root["id"])
            self.assertTrue(call["dotted_order"].startswith(root["dotted_order"] + "."))
        self.assertIn('total = item["price"]', calls[1]["inputs"]["messages"][1]["content"])  # the code is sent
        self.assertIn("Total is overwritten", calls[1]["outputs"]["content"])
        method, url, headers, body = requests[-1]
        self.assertEqual((method, url), ("PATCH", f"https://api.smith.langchain.com/runs/{root['id']}"))
        self.assertEqual(body["outputs"]["findings"][0]["title"], "Total is overwritten")
        self.assertEqual(headers["X-api-key"], KEY)
        self.assertNotIn(KEY, json.dumps([b for *_, b in requests]))

    def test_gitleaks_hits_are_withheld(self):
        scan = lambda text: ["generic-api-key"] if "Total is overwritten" in text else []
        requests = self.review(scan=scan)
        python = next(b for m, _, _, b in requests if m == "POST" and b["name"] == "python")
        self.assertEqual(python["outputs"]["content"], tracing.WITHHELD)

    def test_off_without_switch_or_key(self):
        self.assertEqual(self.review(enabled=False), [])
        self.assertEqual(self.review(environ={}), [])
        self.assertNotIsInstance(self.wrapped, tracing.TracedModel)


if __name__ == "__main__":
    unittest.main()
