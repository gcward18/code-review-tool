"""History batches: review many commits of a repository in one approved run.

A batch is planned first (commits chosen by mode, each with a local cost estimate and
nothing sent anywhere), approved once with a cost cap, then run one review at a time on a
background thread. Each commit becomes a normal review; the batch stops when cancelled or
when the money spent reaches the cap.
"""

import json
import threading
import time

from autosde import git
from autosde.noise import flag
from autosde.team import route

MODES = {
    "every": "Every commit on the branch (first parent; merged PRs count once)",
    "merges": "Merge commits only (one review per merged pull request)",
    "nth": "Every Nth commit",
    "latest": "The latest N commits",
}
MAX_COMMITS = 100
SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    range_spec TEXT NOT NULL,
    mode TEXT NOT NULL,
    commits TEXT NOT NULL,          -- JSON: sha, short, subject, date, files, estimate
    status TEXT NOT NULL,           -- planned | running | cancelling | completed | cancelled | budget
    est_cost REAL NOT NULL,
    max_cost REAL NOT NULL,
    spent REAL DEFAULT 0,
    done INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    review_ids TEXT DEFAULT '[]',
    error TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
"""


class BatchError(ValueError):
    pass


def commits_in(repo, base, head, mode, every=1, limit=20):
    """Commit SHAs (oldest first) in base..head chosen by `mode`, at most `limit`, newest kept."""
    args = ["rev-list", "--reverse", "--first-parent"]
    if mode == "merges":
        args.append("--merges")
    args.append(f"{base}..{head}")
    shas = [s for s in git._git(repo, *args).decode().split() if s]
    if mode == "nth":
        shas = shas[::-1][::max(1, every)][::-1]  # count back from the newest commit
    return shas[-limit:]


def estimate(repo, sha, settings):
    """Rough cost of reviewing one commit, from its changed files (local, nothing sent)."""
    try:
        changes = git.capture(repo, sha, max_file_bytes=settings.max_file_bytes,
                              max_total_bytes=settings.max_total_bytes)
    except git.GitError:
        return None
    sendable = [f for f in changes.files if not f.skipped and not flag(f.path)]
    chars = sum(len(f.head) + len(f.diff) for f in sendable)
    specialists = len({route(f.path) for f in sendable})
    incoming = chars // 4 + 1200 * (specialists + 2) + 60 * len(changes.files)
    outgoing = 1500 + 4000 * specialists + (1500 if specialists else 0)
    price = lambda i, o: (i * settings.input_price + o * settings.output_price) / 1_000_000
    return {"files": len(changes.files), "reviewable": len(sendable), "specialists": specialists,
            "input_tokens": incoming, "max_output_tokens": outgoing,
            "estimate": price(incoming, outgoing // 3), "max": price(incoming, outgoing)}


def estimate_remote(source, sha, settings):
    """The same estimate from GitHub's file list (patch sizes), without downloading files."""
    try:
        base, head = source.resolve_range(sha)
        files, _ = source.compare(base, head)
    except git.GitError:
        return None
    sendable = [f for f in files if f.get("patch") and f["status"] != "removed" and not flag(f["filename"])]
    chars = sum(len(f["patch"]) * 4 for f in sendable)  # head files are usually larger than their patch
    chars = min(chars, settings.max_total_bytes * 2)
    specialists = len({route(f["filename"]) for f in sendable})
    incoming = chars // 4 + 1200 * (specialists + 2) + 60 * len(files)
    outgoing = 1500 + 4000 * specialists + (1500 if specialists else 0)
    price = lambda i, o: (i * settings.input_price + o * settings.output_price) / 1_000_000
    return {"files": len(files), "reviewable": len(sendable), "specialists": specialists,
            "input_tokens": incoming, "max_output_tokens": outgoing,
            "estimate": price(incoming, outgoing // 3), "max": price(incoming, outgoing)}


class SqliteBatchStore:
    def __init__(self, db):
        self.db = db
        self.db.executescript(SCHEMA)

    def get(self, batch_id):
        rows = [dict(r) for r in self.db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,))]
        return rows[0] if rows else None

    def for_repo(self, repo, limit=20):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM batches WHERE repo = ? ORDER BY created_at DESC LIMIT ?", (str(repo), limit))]

    def insert(self, row):
        keys = list(row)
        return self.db.execute(f"INSERT INTO batches({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                               [row[k] for k in keys]).lastrowid

    def update(self, batch_id, **fields):
        self.db.execute(f"UPDATE batches SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                        (*fields.values(), batch_id))


class Batches:
    """`store` is a SQLite connection or a batch store (SQLite or DynamoDB)."""

    def __init__(self, store, *, clock=time.time):
        import sqlite3

        self.store = SqliteBatchStore(store) if isinstance(store, sqlite3.Connection) else store
        self.clock = clock

    @staticmethod
    def _decode(row):
        row = dict(row)
        for key in ("commits", "review_ids"):
            if isinstance(row[key], str):
                row[key] = json.loads(row[key])
        row.setdefault("error", None)
        row.setdefault("finished_at", None)
        return row

    def _row(self, batch_id):
        row = self.store.get(batch_id)
        if not row:
            raise BatchError("No such batch.")
        return self._decode(row)

    def get(self, batch_id):
        return self._row(batch_id)

    def for_repo(self, repo):
        return [self._decode(r) for r in self.store.for_repo(str(repo))]

    def plan(self, repo, start, end, *, mode, settings, every=1, limit=20, source=None):
        """Choose commits in end..start and estimate them; nothing runs until `run`."""
        if mode not in MODES:
            raise BatchError("Choose a batch mode.")
        if type(limit) is not int or not 1 <= limit <= MAX_COMMITS:
            raise BatchError(f"Review between 1 and {MAX_COMMITS} commits per batch.")
        if type(every) is not int or not 1 <= every <= 100:
            raise BatchError("N must be between 1 and 100.")
        if source is not None:
            root = source.path
            base, head = source.resolve_range(f"{end}..{start}")
            shas = source.commits_between(base, head, mode, every, limit)
        else:
            root = git.repo_root(repo)
            base, head = git.resolve_range(root, f"{end}..{start}")
            shas = commits_in(root, base, head, mode, every, limit)
        if not shas:
            raise BatchError("No commits match that range and mode.")
        commits = []
        for sha in shas:
            if source is not None:
                info = source.describe(sha)
                cost = estimate_remote(source, sha, settings)
            else:
                line = git._git(root, "log", "-1", "--format=%h%x1f%s%x1f%cs", sha).decode().strip()
                short, subject, date = (line.split("\x1f") + ["", "", ""])[:3]
                info = {"short": short, "subject": subject[:200], "date": date}
                cost = estimate(root, sha, settings)
            if cost is None:
                continue  # a root commit has no parent to diff against
            commits.append({"sha": sha, **info, **cost})
        if not commits:
            raise BatchError("None of those commits can be reviewed.")
        est = round(sum(c["estimate"] for c in commits), 6)
        cap = round(sum(c["max"] for c in commits), 6)
        batch_id = self.store.insert({
            "repo": str(root), "range_spec": f"{end}..{start}", "mode": mode, "commits": json.dumps(commits),
            "status": "planned", "est_cost": est, "max_cost": cap, "spent": 0.0, "done": 0, "failed": 0,
            "review_ids": "[]", "created_at": self.clock()})
        return self._row(batch_id)

    def start(self, batch_id, review, *, max_cost=None):
        """Approve and run on a background thread. `review(sha)` returns (review id, cost)."""
        batch = self._row(batch_id)
        if batch["status"] != "planned":
            raise BatchError("That batch has already been started.")
        cap = batch["max_cost"] if max_cost is None else float(max_cost)
        if not 0 < cap <= max(batch["max_cost"], 0.000001) * 5:
            raise BatchError("Set a cost cap above zero (up to five times the estimate's maximum).")
        self.store.update(batch_id, status="running", max_cost=cap)
        thread = threading.Thread(target=self.run, args=(batch_id, review), daemon=True)
        thread.start()
        return thread

    def run(self, batch_id, review):
        batch = self._row(batch_id)
        spent, done, failed, ids = 0.0, 0, 0, []
        status = "completed"
        for commit in batch["commits"]:
            if self._row(batch_id)["status"] == "cancelling":  # set by `cancel` from any request
                status = "cancelled"
                break
            if spent >= batch["max_cost"]:
                status = "budget"
                break
            try:
                review_id, cost = review(commit["sha"])
                ids.append(review_id)
                spent += cost or 0.0
                done += 1
            except Exception:
                failed += 1  # one bad commit doesn't stop the batch
            self.store.update(batch_id, spent=round(spent, 6), done=done, failed=failed, review_ids=json.dumps(ids))
        self.store.update(batch_id, status=status, finished_at=self.clock())

    def cancel(self, batch_id):
        batch = self._row(batch_id)
        if batch["status"] == "planned":
            self.store.update(batch_id, status="cancelled", finished_at=self.clock())
        elif batch["status"] == "running":  # stops before the next commit
            self.store.update(batch_id, status="cancelling")
        return self._row(batch_id)
