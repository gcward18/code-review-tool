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

KEY = "lsv2_private-sentinel-key"  # gitleaks:allow (fake test key)


class FakeLangSmith:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        if request.full_url.endswith("/api/v1/workspaces"):
            payload = json.dumps([{"id": "ws-1"}]).encode()

            class Listing:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self, n):
                    return payload

            return Listing()
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
        tracing._WORKSPACES.clear()
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
        return fake.requests

    def test_knowledge_searches_appear_as_retriever_runs_with_documents(self):
        from autosde.knowledge import Knowledge
        from autosde.llm import ScriptedModel as Model

        kb = Knowledge(self.store.db, embed=Model({}).embed, scan=lambda text: [])
        base = kb.create("rules")
        guide = b"# Totals\n\nAlways accumulate prices with += in loops; assigning keeps the last price."
        doc = kb.add_document(base["id"], "rules.md", guide)
        kb.ingest(doc, guide)
        agents.save_config({"langsmith": {"enabled": True, "project": "team-tests"},
                            "agents": {"python": {"knowledge": [base["id"]]}}})
        fake = FakeLangSmith()
        real = tracing.traced
        traced = lambda model, **kw: real(model, environ={"LANGSMITH_API_KEY": KEY}, opener=fake, **kw)
        model = Model({"coordinator": [PLAN], "python": [findings(FINDING)], "aggregator": [verdict()],
                       "chat": [{"answer": "Use +=. [1]"}]})
        with patch.object(tracing, "traced", traced):
            run_review(self.repo, "HEAD~1..HEAD", store=self.store, model=model, settings=self.settings,
                       scan=lambda text: [])
        posts = [b for m, _, _, b in fake.requests if m == "POST"]
        search = next(b for b in posts if b["run_type"] == "retriever")
        self.assertEqual(search["name"], "python knowledge search")
        self.assertEqual(search["parent_run_id"], posts[0]["id"])
        document = search["outputs"]["documents"][0]
        self.assertIn("accumulate prices", document["page_content"])
        self.assertEqual((document["metadata"]["source"], document["metadata"]["rank"]), ("rules/rules.md", 1))
        self.assertIn("calculator.py", search["inputs"]["query"])

        from autosde import chat
        agents.save_config({"langsmith": {"enabled": True, "project": "team-tests"},
                            "agents": {"chat": {"knowledge": [base["id"]]}}})
        fake.requests.clear()
        project = self.store.projects()[0]
        with patch.object(tracing, "traced", traced):
            chat.ask(self.store, model, project, "How should totals add up?", scan=lambda text: [])
        names = [(b["run_type"], b["name"]) for m, _, _, b in fake.requests if m == "POST"]
        self.assertEqual(names, [("chain", "chat · calculator"), ("retriever", "chat knowledge search"),
                                 ("llm", "chat")])
        patched = [b for m, _, _, b in fake.requests if m == "PATCH"][0]
        self.assertEqual(patched["outputs"]["answer"], "Use +=. [1]")

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
        self.assertEqual(headers["X-tenant-id"], "ws-1")
        self.assertNotIn(KEY, json.dumps([b for *_, b in requests]))

    def test_gitleaks_hits_are_withheld(self):
        scan = lambda text: ["generic-api-key"] if "Total is overwritten" in text else []
        requests = self.review(scan=scan)
        python = next(b for m, _, _, b in requests if m == "POST" and b["name"] == "python")
        self.assertEqual(python["outputs"]["content"], tracing.WITHHELD)

    def test_suite_has_no_real_keys(self):
        import os

        self.assertNotIn("LANGSMITH_API_KEY", os.environ)

    def test_off_without_switch_or_key(self):
        self.assertEqual(self.review(enabled=False), [])
        self.assertEqual(self.review(environ={}), [])
        self.assertNotIsInstance(self.wrapped, tracing.TracedModel)


if __name__ == "__main__":
    unittest.main()
