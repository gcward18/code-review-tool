import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from autosde import github
from autosde.batch import BatchError, Batches, commits_in
from autosde.db import Store
from autosde.remote import Remotes
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


class FakeGitHub:
    """Answers the GitHub API calls AutoSDE makes, from a local repository."""

    def __init__(self, repo, owner="octo", name="calc"):
        self.repo, self.prefix, self.calls = repo, f"repos/{owner}/{name}", []

    def authenticated(self):
        return True

    def _commit(self, sha):
        full = git(self.repo, "rev-parse", sha)
        parents = git(self.repo, "log", "-1", "--format=%P", full).split()
        return {"sha": full, "parents": [{"sha": p} for p in parents],
                "commit": {"message": git(self.repo, "log", "-1", "--format=%B", full),
                           "author": {"name": "Dev"},
                           "committer": {"date": git(self.repo, "log", "-1", "--format=%cI", full)}}}

    def get(self, path, *, raw=False):
        from urllib.parse import parse_qs, unquote, urlsplit
        self.calls.append(path)
        parts = urlsplit(path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        tail = parts.path[len(self.prefix):].lstrip("/")
        if not parts.path.startswith(self.prefix):
            raise github.GitHubError("GitHub says that doesn't exist or you can't access it.")
        if tail == "":
            return {"default_branch": "main", "private": True, "description": "test"}
        if tail == "branches":
            return [{"name": b} for b in git(self.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()]
        if tail == "commits":
            shas = git(self.repo, "rev-list", "--first-parent", query["sha"]).split()
            per, page = int(query.get("per_page", 30)), int(query.get("page", 1))
            return [self._commit(s) for s in shas[(page - 1) * per:page * per]]
        if tail.startswith("commits/"):
            return self._commit(tail.split("/", 1)[1])
        if tail.startswith("compare/"):
            base, head = tail.split("/", 1)[1].split("...")
            files = []
            for line in git(self.repo, "diff", "--name-status", base, head).splitlines():
                status, name = line.split("\t")[0], line.split("\t")[-1]
                patch = git(self.repo, "diff", "-U3", base, head, "--", name).split("\n@@", 1)
                files.append({"filename": name, "status": {"A": "added", "D": "removed"}.get(status[0], "modified"),
                              "patch": "@@" + patch[1] if len(patch) > 1 else None,
                              "additions": 1, "deletions": 0})
            shas = git(self.repo, "rev-list", "--reverse", f"{base}..{head}").split()
            return {"files": files, "commits": [self._commit(s) for s in shas]}
        if tail.startswith("contents/"):
            name = unquote(tail.split("/", 1)[1])
            import subprocess
            out = subprocess.run(["git", "-C", str(self.repo), "show", f"{query['ref']}:{name}"], capture_output=True)
            if out.returncode:
                raise github.GitHubError("missing")
            return out.stdout
        raise AssertionError(f"unexpected GitHub call {path}")


class GitHubSourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = history(Path(tmp.name) / "up")
        self.fake = FakeGitHub(self.local)
        self.source = github.Repo("octo", "calc", self.fake)
        self.settings = settings(tmp.name)
        self.store = Store(self.settings.db_path)

    def test_parse_accepts_github_forms_only(self):
        for text in ("octo/calc", "https://github.com/octo/calc", "github.com/octo/calc.git",
                     "git@github.com:octo/calc.git"):
            self.assertEqual(github.parse(text), ("octo", "calc"), text)
        for bad in ("", "octo", "https://gitlab.com/octo/calc", "file:///etc", "octo/calc; rm -rf /", "../x"):
            with self.assertRaises(github.GitHubError, msg=bad):
                github.parse(bad)

    def test_capture_matches_the_local_source(self):
        from autosde import git as capture_git

        remote = self.source.capture("HEAD")
        local = capture_git.capture(self.local, "HEAD")
        self.assertEqual(remote.repo, "https://github.com/octo/calc")
        self.assertEqual((remote.base, remote.head), (local.base, local.head))
        self.assertEqual([(f.path, f.head) for f in remote.files], [(f.path, f.head) for f in local.files])
        with self.assertRaises(github.GitHubError):
            self.source.resolve_range("--upload-pack=evil")

    def test_review_batch_and_diff_from_github_without_a_clone(self):
        from autosde.llm import ScriptedModel
        from autosde.review import run_review
        from helpers import FINDING, PLAN, clean_scan, findings, verdict

        model = ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)], "aggregator": [verdict()]})
        review_id = run_review("https://github.com/octo/calc", "HEAD", store=self.store, model=model,
                               settings=self.settings, scan=clean_scan, source=self.source)
        review = self.store.review(review_id)
        self.assertEqual((review["project"], review["project_path"]), ("octo/calc", "https://github.com/octo/calc"))
        root = git(self.local, "rev-list", "--max-parents=0", "HEAD")
        batch = Batches(self.store.db).plan("https://github.com/octo/calc", "main", root, mode="every",
                                            settings=self.settings, source=self.source)
        self.assertEqual([c["subject"] for c in batch["commits"]], ["bug", "Merge feature", "step 0", "step 1"])
        merges = Batches(self.store.db).plan("x", "main", root, mode="merges", settings=self.settings,
                                             source=self.source)
        self.assertEqual([c["subject"] for c in merges["commits"]], ["Merge feature"])
        files = self.source.changed_files(review["base_sha"], review["head_sha"])
        self.assertEqual([f["path"] for f in files], ["step1.py"])
        rows = self.source.file_diff(review["base_sha"], review["head_sha"], "step1.py")["rows"]
        self.assertEqual(rows, [{"t": "+", "o": None, "n": 1, "s": "VALUE = 1"}])

    def test_rows_between_aligns_old_and_new_lines(self):
        result = github.rows_between("a\nb\nc\n", "a\nB\nc\nd\n")
        self.assertEqual([(r["t"], r["o"], r["n"]) for r in result["rows"]],
                         [(" ", 1, 1), ("-", 2, None), ("+", None, 2), (" ", 3, 3), ("+", None, 4)])

    def test_remotes_check_github_and_upgrade_old_clone_rows(self):
        remotes = Remotes(self.store.db, client=self.fake)
        added = remotes.get(remotes.add("https://github.com/octo/calc"))
        self.assertEqual(added["path"], "https://github.com/octo/calc")
        with self.assertRaises(github.GitHubError):
            remotes.add("octo/calc")
        with self.assertRaises(github.GitHubError):
            remotes.add("octo/missing")  # GitHub says it doesn't exist
        self.store.db.execute("UPDATE remotes SET path = '/old/clone/octo/calc'")
        self.assertEqual(Remotes(self.store.db, client=self.fake).list()[0]["path"], "https://github.com/octo/calc")


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
        from autosde.llm import ScriptedModel
        from autosde.web import App, handler_for
        from helpers import FINDING, PLAN, findings, verdict

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = history(Path(tmp.name) / "up")
        self.settings = settings(tmp.name)
        self.store = Store(self.settings.db_path)
        model = lambda: ScriptedModel({"coordinator": [PLAN], "python": [findings(FINDING)], "aggregator": [verdict()]})
        self.app = App(self.store, self.settings, model_factory=model, port=0, browse_root=tmp.name)
        self.app.github_client = FakeGitHub(self.local)
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

    def test_github_repository_reviews_without_cloning(self):
        from unittest.mock import patch

        from helpers import clean_scan

        added = self.call("/api/remotes", {"repo": "octo/calc"})
        path = added["path"]
        self.assertEqual([g["name"] for g in self.call("/api/workspaces")["github"]], ["octo/calc"])
        repo = self.call(f"/api/repo?{urllib.parse.urlencode({'path': path})}")
        self.assertTrue(repo["github"])
        self.assertEqual(repo["default_from"], "main")
        older = self.call(f"/api/older?{urllib.parse.urlencode({'path': path, 'from': 'main'})}")
        parent = git(self.local, "rev-parse", "main~1")
        self.assertEqual(older["default_to"], parent)
        with patch("autosde.review.scanner", return_value=clean_scan):
            review_id = self.call("/api/reviews", {"repo": path, "from": "main", "to": parent})["id"]
            for _ in range(50):
                review = self.call(f"/api/reviews/{review_id}")
                if review["status"] == "awaiting_approval":
                    break
                threading.Event().wait(0.1)
        self.assertEqual(review["project"], "octo/calc")
        files = self.call(f"/api/reviews/{review_id}/files")["files"]
        self.assertEqual([f["path"] for f in files], ["step1.py"])
        root = git(self.local, "rev-list", "--max-parents=0", "HEAD")
        plan = self.call("/api/batches", {"repo": path, "from": "main", "to": root, "mode": "merges", "limit": 5})
        self.assertEqual([c["subject"] for c in plan["commits"]], ["Merge feature"])
        with self.assertRaises(urllib.error.HTTPError) as unknown:
            self.call(f"/api/repo?{urllib.parse.urlencode({'path': 'https://github.com/other/repo'})}")
        self.assertEqual(unknown.exception.code, 400)  # only added repositories
        self.call(f"/api/remotes/{added['id']}/delete", {})
        self.assertEqual(self.call("/api/remotes"), [])


if __name__ == "__main__":
    unittest.main()
