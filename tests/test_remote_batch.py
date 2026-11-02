import functools
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from autosde.batch import BatchError, Batches, commits_in
from autosde.db import Store
from autosde.remote import Remotes, RemoteError, parse
from helpers import git, repository, settings


def history(root):
    """A repository with a feature branch merged in, then two more commits on main."""
    Path(root).mkdir(parents=True, exist_ok=True)
    repo = repository(root)
    git(repo, "checkout", "-qb", "feature")
    (repo / "feature.py").write_text("def feature():\n    return 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add feature")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    for n in range(2):
        (repo / f"step{n}.py").write_text(f"VALUE = {n}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", f"step {n}")
    return repo


class ParseTests(unittest.TestCase):
    def test_accepts_github_forms_and_rejects_others(self):
        for text, url in (("octo/repo", "https://github.com/octo/repo.git"),
                          ("https://github.com/octo/repo", "https://github.com/octo/repo.git"),
                          ("github.com/octo/repo.git", "https://github.com/octo/repo.git"),
                          ("git@github.com:octo/repo.git", "git@github.com:octo/repo.git")):
            self.assertEqual(parse(text), ("octo", "repo", url), text)
        for bad in ("", "octo", "https://gitlab.com/octo/repo", "file:///etc", "octo/repo; rm -rf /",
                    "--upload-pack=x/y", "../x"):
            with self.assertRaises(RemoteError, msg=bad):
                parse(bad)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.upstream = history(self.tmp / "up")
        self.store = Store(self.tmp / "k.db")
        self.remotes = Remotes(self.store.db, self.tmp / "repos", clone_url=lambda url: str(self.upstream))

    def test_clone_fetch_and_remove(self):
        remote_id = self.remotes.add("octo/calculator")
        self.remotes.clone(remote_id)
        remote = self.remotes.get(remote_id)
        self.assertEqual(remote["status"], "ready")
        path = Path(remote["path"])
        self.assertEqual(path, (self.tmp / "repos" / "octo" / "calculator").resolve())
        self.assertTrue((path / "step1.py").exists())
        with self.assertRaises(RemoteError):
            self.remotes.add("https://github.com/octo/calculator")  # same repository

        (self.upstream / "new.py").write_text("NEW = 1\n")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "upstream change")
        self.remotes.fetch(remote_id)
        self.assertTrue((path / "new.py").exists())
        self.assertEqual(self.remotes.get(remote_id)["status"], "ready")

        self.remotes.remove(remote_id)
        self.assertFalse(path.exists())
        self.assertEqual(self.remotes.list(), [])

    def test_failed_clone_records_a_readable_error(self):
        broken = Remotes(self.store.db, self.tmp / "repos", clone_url=lambda url: str(self.tmp / "missing"))
        remote_id = broken.add("octo/missing")
        broken.clone(remote_id)
        remote = broken.get(remote_id)
        self.assertEqual(remote["status"], "failed")
        self.assertTrue(remote["error"])


class BatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = history(self.tmp)
        self.settings = settings(tmp.name)
        self.store = Store(self.settings.db_path)
        self.batches = Batches(self.store.db)
        self.root = git(self.repo, "rev-list", "--max-parents=0", "HEAD")

    def subjects(self, shas):
        return [git(self.repo, "log", "-1", "--format=%s", s) for s in shas]

    def test_modes_choose_commits(self):
        head = git(self.repo, "rev-parse", "HEAD")
        every = self.subjects(commits_in(self.repo, self.root, head, "every"))
        self.assertEqual(every, ["bug", "Merge feature", "step 0", "step 1"])
        self.assertEqual(self.subjects(commits_in(self.repo, self.root, head, "merges")), ["Merge feature"])
        self.assertEqual(self.subjects(commits_in(self.repo, self.root, head, "nth", every=2)),
                         ["Merge feature", "step 1"])
        self.assertEqual(self.subjects(commits_in(self.repo, self.root, head, "latest", limit=2)),
                         ["step 0", "step 1"])

    def test_plan_estimates_without_sending_and_validates(self):
        batch = self.batches.plan(self.repo, "HEAD", self.root, mode="every", settings=self.settings)
        self.assertEqual(batch["status"], "planned")
        self.assertEqual(len(batch["commits"]), 4)
        merge = next(c for c in batch["commits"] if c["subject"] == "Merge feature")
        self.assertEqual((merge["files"], merge["reviewable"]), (1, 1))  # diff against the first parent
        self.assertGreater(batch["max_cost"], batch["est_cost"])
        self.assertGreater(batch["est_cost"], 0)
        for bad in ({"mode": "all"}, {"mode": "every", "limit": 0}, {"mode": "nth", "every": 0}):
            with self.assertRaises(BatchError):
                self.batches.plan(self.repo, "HEAD", self.root, settings=self.settings,
                                  **{"limit": 5, "every": 1, **bad})

    def test_run_reviews_each_commit_and_stops_at_the_cap_or_on_cancel(self):
        batch = self.batches.plan(self.repo, "HEAD", self.root, mode="every", settings=self.settings)
        seen = []

        def review(sha):
            seen.append(sha)
            if len(seen) == 2:
                raise RuntimeError("one commit fails")
            return len(seen), 0.01
        self.batches.start(batch["id"], review, max_cost=0.02).join(5)
        done = self.batches.get(batch["id"])
        self.assertEqual((done["status"], done["done"], done["failed"]), ("budget", 2, 1))
        self.assertEqual(done["review_ids"], [1, 3])
        self.assertEqual(seen, [c["sha"] for c in batch["commits"][:3]])
        with self.assertRaises(BatchError):
            self.batches.start(batch["id"], review)  # already run

        second = self.batches.plan(self.repo, "HEAD", self.root, mode="every", settings=self.settings)
        gate, entered = threading.Event(), threading.Event()

        def slow(sha):
            entered.set()
            gate.wait(5)
            return 1, 0.0
        thread = self.batches.start(second["id"], slow)
        self.assertTrue(entered.wait(5))  # cancel while the first review is in progress
        self.assertEqual(self.batches.cancel(second["id"])["status"], "cancelling")
        gate.set()
        thread.join(5)
        stopped = self.batches.get(second["id"])
        self.assertEqual((stopped["status"], stopped["done"]), ("cancelled", 1))


class DiffTests(unittest.TestCase):
    def test_changed_files_and_whole_file_diffs(self):
        from autosde import git as capture_git

        with tempfile.TemporaryDirectory() as tmp:
            repo = repository(tmp)
            base = git(repo, "rev-parse", "HEAD")
            body = "\n".join(f"line {n}" for n in range(1, 41)) + "\n"
            (repo / "long.txt").write_text(body)
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "long")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "long.txt").write_text(body.replace("line 20\n", "line twenty\n"))
            git(repo, "mv", "calculator.py", "calc.py")
            (repo / "img.bin").write_bytes(b"\x00\x01")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "edit, rename, binary")
            head = git(repo, "rev-parse", "HEAD")
            files = {f["path"]: f for f in capture_git.changed_files(repo, base, head)}
            self.assertEqual((files["long.txt"]["added"], files["long.txt"]["removed"]), (1, 1))
            self.assertEqual((files["calc.py"]["status"], files["calc.py"]["old_path"]), ("R", "calculator.py"))
            self.assertTrue(files["img.bin"]["binary"])
            diff = capture_git.file_diff(repo, base, head, "long.txt")
            self.assertEqual(len(diff["rows"]), 41)  # every line, so context can expand
            changed = [r for r in diff["rows"] if r["t"] != " "]
            self.assertEqual([(r["t"], r["o"], r["n"], r["s"]) for r in changed],
                             [("-", 20, None, "line 20"), ("+", None, 20, "line twenty")])
            self.assertEqual(diff["rows"][-1], {"t": " ", "o": 40, "n": 40, "s": "line 40"})
            self.assertTrue(capture_git.file_diff(repo, base, head, "img.bin")["binary"])
            with self.assertRaises(capture_git.GitError):
                capture_git.file_diff(repo, base, head, "../../etc/passwd")


class ApiTests(unittest.TestCase):
    def setUp(self):
        from autosde.web import App, handler_for

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.upstream = history(self.tmp / "up")
        self.settings = settings(tmp.name)
        self.store = Store(self.settings.db_path)
        local = functools.partial(Remotes, clone_url=lambda url: str(self.upstream))
        patcher = patch("autosde.web.Remotes", local)
        patcher.start()
        self.addCleanup(patcher.stop)
        from autosde.llm import ScriptedModel
        self.app = App(self.store, self.settings, model_factory=lambda: ScriptedModel({}), port=0,
                       browse_root=tmp.name)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.app))
        self.app.port = server.server_address[1]
        self.url = f"http://127.0.0.1:{self.app.port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def call(self, path, body=None):
        headers = {"Origin": self.url}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers |= {"Content-Type": "application/json", "X-AutoSDE-Token": self.app.token}
        request = urllib.request.Request(self.url + path, data=data, headers=headers,
                                         method="POST" if body is not None else "GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def test_add_remote_appears_in_the_sidebar_and_batches_plan_on_it(self):
        added = self.call("/api/remotes", {"repo": "octo/calculator"})
        for _ in range(50):
            remote = self.call("/api/remotes")[0]
            if remote["status"] != "cloning":
                break
            threading.Event().wait(0.1)
        self.assertEqual(remote["status"], "ready", remote["error"])
        github = self.call("/api/workspaces")["github"]
        self.assertEqual([g["name"] for g in github], ["octo/calculator"])
        root = git(self.upstream, "rev-list", "--max-parents=0", "HEAD")
        plan = self.call("/api/batches", {"repo": added["path"], "from": "HEAD", "to": root,
                                          "mode": "merges", "limit": 5})
        self.assertEqual([c["subject"] for c in plan["commits"]], ["Merge feature"])
        self.assertEqual(self.call(f"/api/batches?repo={urllib.request.quote(added['path'])}")[0]["id"], plan["id"])
        self.assertEqual(self.call(f"/api/batches/{plan['id']}/cancel", {})["status"], "cancelled")
        self.assertIn("merges", self.call("/api/batches")["modes"])
        from autosde.review import run_review
        from autosde.llm import ScriptedModel
        from helpers import PLAN, FINDING, clean_scan, findings, verdict
        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)], "aggregator": [verdict()]})
        review_id = run_review(added["path"], "HEAD", store=self.store, model=model, settings=self.settings,
                               scan=clean_scan)
        listing = self.call(f"/api/reviews/{review_id}/files")["files"]
        self.assertEqual([f["path"] for f in listing], ["step1.py"])
        rows = self.call(f"/api/reviews/{review_id}/files?path=step1.py")["rows"]
        self.assertEqual(rows, [{"t": "+", "o": None, "n": 1, "s": "VALUE = 1"}])

        self.call(f"/api/remotes/{added['id']}/delete", {})
        self.assertFalse(Path(added["path"]).exists())
        with self.assertRaises(urllib.error.HTTPError) as gone:
            self.call(f"/api/reviews/{review_id}/files")
        self.assertEqual(gone.exception.code, 410)


if __name__ == "__main__":
    unittest.main()
