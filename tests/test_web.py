import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from autosde.db import Store
from autosde.llm import ScriptedModel
from autosde.web import App, handler_for
from helpers import FINDING, PLAN, clean_scan, findings, git, repository, settings, verdict


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = repository(self.tmp.name, {"package-lock.json": "{}\n"})
        self.settings = settings(self.tmp.name)
        self.store = Store(self.settings.db_path)
        self.app = App(self.store, self.settings, model_factory=self.scripted, port=0,
                       browse_root=self.tmp.name)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.app))
        self.app.port = server.server_address[1]
        self.url = f"http://127.0.0.1:{self.app.port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def tearDown(self):
        self.tmp.cleanup()

    def scripted(self):
        return ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)],
                              "aggregator": [verdict()], "chat": [{"answer": "One finding."}]})

    def get(self, route, **query):
        url = self.url + route + ("?" + urllib.parse.urlencode(query) if query else "")
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read())

    def post(self, path, body, *, token=None, origin=None, content_type="application/json"):
        request = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": content_type, "X-AutoSDE-Token": token or self.app.token,
                     "Origin": origin or self.url})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def status(self, error_context):
        error = error_context.exception
        error.close()
        return error.code

    def wait(self, review_id, status):
        for _ in range(100):
            review = self.get(f"/api/reviews/{review_id}")
            if review["status"] == status:
                return review
            threading.Event().wait(0.1)
        self.fail(f"review never reached {status}: {review['status']}")

    def test_tree_lists_folders_marks_repositories_and_stays_inside_root(self):
        (Path(self.tmp.name) / "plain").mkdir()
        root = self.get("/api/tree")
        entries = {e["name"]: e for e in root["entries"]}
        self.assertTrue(entries["calculator"]["repo"])
        self.assertFalse(entries["plain"]["repo"])
        for outside in ("/etc", str(Path(self.tmp.name) / "..")):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.get("/api/tree", path=outside)
            self.assertEqual(self.status(error), 400)

    def test_workspaces_split_recent_available_and_blocked(self):
        nested = Path(self.tmp.name) / "work" / "api"
        nested.mkdir(parents=True)
        git(nested, "init", "-q")
        locked = Path(self.tmp.name) / "locked"
        locked.mkdir()
        project = self.store.project(self.repo, "calculator")
        self.store.start_review(project, type("C", (), {"range_spec": "a..b", "base": "a",
                                                         "head": "b", "files": []})(), "m")
        locked.chmod(0)  # like a folder macOS has not approved
        try:
            spaces = self.get("/api/workspaces")
            self.assertNotIn("locked", [e["name"] for e in self.get("/api/tree")["entries"]])
        finally:
            locked.chmod(0o755)
        self.assertEqual([r["name"] for r in spaces["recent"]], ["calculator"])
        self.assertEqual(spaces["recent"][0]["reviews"], 1)
        self.assertEqual([r["name"] for r in spaces["available"]], ["api"])
        self.assertEqual([r["name"] for r in spaces["blocked"]], ["locked"])

    def test_to_lists_only_commits_older_than_from(self):
        git(self.repo, "checkout", "-qb", "feature")
        for n in range(2):
            (self.repo / f"f{n}.py").write_text(f"x = {n}\n")
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-qm", f"feature {n}")
        repo = self.get("/api/repo", path=str(self.repo))
        self.assertEqual(repo["default_from"], "HEAD")
        self.assertIn("feature", [o["value"] for o in repo["from"] if o["kind"] == "branch"])
        head, parent, main = (git(self.repo, "rev-parse", r) for r in ("HEAD", "HEAD~1", "main"))
        older = self.get("/api/older", path=str(self.repo), **{"from": "HEAD"})
        values = [o["value"] for o in older["to"]]
        self.assertEqual(older["default_to"], "main")  # main is older than HEAD here
        self.assertNotIn(head, values)
        self.assertNotIn("feature", values)  # the From branch itself is not older
        self.assertIn(parent, values)
        # Picking an older From narrows To further.
        narrower = [o["value"] for o in self.get("/api/older", path=str(self.repo),
                                                 **{"from": parent})["to"]]
        self.assertNotIn(parent, narrower)
        self.assertLess(len(narrower), len(values))
        self.assertEqual(self.get("/api/older", path=str(self.repo), **{"from": main})["default_to"],
                         git(self.repo, "rev-parse", "main~1"))
        root = git(self.repo, "rev-list", "--max-parents=0", "HEAD")
        self.assertEqual(self.get("/api/older", path=str(self.repo), **{"from": root})["to"], [])

    def test_submit_plan_approve_review_and_sarif(self):
        base = git(self.repo, "rev-parse", "HEAD~1")
        with patch("autosde.review.scanner", return_value=clean_scan):
            review_id = self.post("/api/reviews", {"repo": str(self.repo), "from": "HEAD",
                                                   "to": base})["id"]
            plan = self.wait(review_id, "awaiting_approval")
        self.assertEqual(plan["proposed"], ["calculator.py"])  # the lockfile is flagged
        self.assertIn("lockfile", next(f["flag"] for f in plan["files"] if f["path"] == "package-lock.json"))
        everything = self.post(f"/api/reviews/{review_id}/estimate",
                               {"files": ["calculator.py", "package-lock.json"]})
        self.assertGreater(everything["input_tokens"], plan["estimate"]["input_tokens"])
        self.post(f"/api/reviews/{review_id}/approve", {"files": ["calculator.py"]})
        review = self.wait(review_id, "completed")
        self.assertEqual(review["findings"][0]["title"], "Total is overwritten")
        self.assertEqual(review["range_spec"], f"{base}..HEAD")
        with urllib.request.urlopen(f"{self.url}/api/reviews/{review_id}.sarif", timeout=10) as r:
            self.assertEqual(json.loads(r.read())["version"], "2.1.0")
        self.assertEqual(self.get("/api/projects")[0]["name"], "calculator")
        project = self.get("/api/projects/1")
        self.assertIn("coordinator", [a["agent"] for a in project["agents"]])
        runs = self.get("/api/agent-runs")
        self.assertEqual({(r["project"], r["agent"]) for r in runs},
                         {("calculator", "coordinator"), ("calculator", "python"),
                          ("calculator", "aggregator")})
        with self.assertRaises(urllib.error.HTTPError) as again:  # already decided
            self.post(f"/api/reviews/{review_id}/approve", {"files": []})
        self.assertEqual(self.status(again), 409)

    def test_project_chat_round_trip(self):
        project = self.store.project(self.repo, "calculator")
        with patch("autosde.web.scanner", return_value=clean_scan):
            reply = self.post(f"/api/projects/{project}/chat", {"message": "Summarize"})
        self.assertEqual(reply["messages"][-1]["content"], "One finding.")
        self.assertEqual(len(self.get(f"/api/projects/{project}/chat")["messages"]), 2)
        self.assertEqual(self.post(f"/api/projects/{project}/chat/clear", {})["messages"], [])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(f"/api/projects/{project}/chat", {"message": "x"}, token="wrong")
        self.assertEqual(self.status(error), 403)

    def test_home_chat_round_trip(self):
        with patch("autosde.web.scanner", return_value=clean_scan):
            reply = self.post("/api/chat", {"message": "Which repos are here?"})
        self.assertEqual(reply["messages"][-1]["content"], "One finding.")
        self.assertEqual(len(self.get("/api/chat")["messages"]), 2)
        self.assertEqual(self.post("/api/chat/clear", {})["messages"], [])

    def test_cancel_and_expiry(self):
        base = git(self.repo, "rev-parse", "HEAD~1")
        with patch("autosde.review.scanner", return_value=clean_scan):
            review_id = self.post("/api/reviews", {"repo": str(self.repo), "from": "HEAD",
                                                   "to": base})["id"]
            self.wait(review_id, "awaiting_approval")
        self.assertEqual(self.post(f"/api/reviews/{review_id}/cancel", {})["status"], "cancelled")
        self.assertEqual(self.get(f"/api/reviews/{review_id}")["status"], "cancelled")
        from autosde.review import prepare

        orphan = prepare(self.repo, "HEAD~1..HEAD", store=self.store, model=self.scripted(),
                         settings=self.settings, scan=clean_scan)  # e.g. after a restart
        self.assertEqual(self.get(f"/api/reviews/{orphan.review_id}")["status"], "expired")
        handing_over = prepare(self.repo, "HEAD~1..HEAD", store=self.store, model=self.scripted(),
                               settings=self.settings, scan=clean_scan)
        self.app.planning.add(handing_over.review_id)
        self.assertEqual(self.get(f"/api/reviews/{handing_over.review_id}")["status"], "planning")

    def test_posts_need_token_json_same_origin_and_offered_refs(self):
        base = git(self.repo, "rev-parse", "HEAD~1")
        good = {"repo": str(self.repo), "from": "HEAD", "to": base}
        cases = [
            (dict(token="wrong"), good, 403),
            (dict(origin="https://evil.example"), good, 403),
            (dict(content_type="text/plain"), good, 415),
            ({}, good | {"to": "--output=/tmp/x"}, 400),
            ({}, good | {"to": "HEAD"}, 400),  # To must be older than From
            ({}, good | {"from": "nope"}, 400),
            ({}, good | {"repo": "/etc"}, 400),
        ]
        for options, body, code in cases:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post("/api/reviews", body, **options)
            self.assertEqual(self.status(error), code, (options, body))
        self.assertEqual(self.store.reviews(), [])

    def test_models_lists_the_keys_models(self):
        models = self.get("/api/models")
        self.assertEqual(models["source"], "openai")
        self.assertIn("gpt-6-luna-mini", models["models"])
        self.assertEqual(models["default"], self.settings.model)

    def test_frontend_is_served_with_a_strict_policy(self):
        with urllib.request.urlopen(self.url + "/", timeout=10) as response:
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertIn('<div id="root">', response.read().decode())
        with urllib.request.urlopen(self.url + "/reviews/5", timeout=10) as response:
            self.assertIn('<div id="root">', response.read().decode())  # client-side route
        request = urllib.request.Request(self.url + "/api/session", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(self.status(error), 403)


if __name__ == "__main__":
    unittest.main()
