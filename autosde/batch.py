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


class Batches:
    def __init__(self, db, *, clock=time.time):
        self.db, self.clock = db, clock
        self.db.executescript(SCHEMA)

    def _all(self, sql, *args):
        return [dict(row) for row in self.db.execute(sql, args)]

    def _row(self, batch_id):
        rows = self._all("SELECT * FROM batches WHERE id = ?", batch_id)
        if not rows:
            raise BatchError("No such batch.")
        row = rows[0]
        row["commits"] = json.loads(row["commits"])
        row["review_ids"] = json.loads(row["review_ids"])
        return row

    def get(self, batch_id):
        return self._row(batch_id)

    def for_repo(self, repo):
        return [self._row(r["id"]) for r in self._all(
            "SELECT id FROM batches WHERE repo = ? ORDER BY created_at DESC LIMIT 20", str(repo))]

    def plan(self, repo, start, end, *, mode, settings, every=1, limit=20):
        """Choose commits in end..start and estimate them; nothing runs until `run`."""
        if mode not in MODES:
            raise BatchError("Choose a batch mode.")
        if type(limit) is not int or not 1 <= limit <= MAX_COMMITS:
            raise BatchError(f"Review between 1 and {MAX_COMMITS} commits per batch.")
        if type(every) is not int or not 1 <= every <= 100:
            raise BatchError("N must be between 1 and 100.")
        root = git.repo_root(repo)
        base, head = git.resolve_range(root, f"{end}..{start}")
        shas = commits_in(root, base, head, mode, every, limit)
        if not shas:
            raise BatchError("No commits match that range and mode.")
        commits = []
        for sha in shas:
            info = git._git(root, "log", "-1", "--format=%h%x1f%s%x1f%cs", sha).decode().strip()
            short, subject, date = (info.split("\x1f") + ["", "", ""])[:3]
            cost = estimate(root, sha, settings)
            if cost is None:
                continue  # a root commit has no parent to diff against
            commits.append({"sha": sha, "short": short, "subject": subject[:200], "date": date, **cost})
        if not commits:
            raise BatchError("None of those commits can be reviewed.")
        est = round(sum(c["estimate"] for c in commits), 6)
        cap = round(sum(c["max"] for c in commits), 6)
        cursor = self.db.execute(
            "INSERT INTO batches(repo, range_spec, mode, commits, status, est_cost, max_cost, created_at)"
            " VALUES (?,?,?,?,'planned',?,?,?)",
            (str(root), f"{end}..{start}", mode, json.dumps(commits), est, cap, self.clock()))
        return self._row(cursor.lastrowid)

    def start(self, batch_id, review, *, max_cost=None):
        """Approve and run on a background thread. `review(sha)` returns (review id, cost)."""
        batch = self._row(batch_id)
        if batch["status"] != "planned":
            raise BatchError("That batch has already been started.")
        cap = batch["max_cost"] if max_cost is None else float(max_cost)
        if not 0 < cap <= max(batch["max_cost"], 0.000001) * 5:
            raise BatchError("Set a cost cap above zero (up to five times the estimate's maximum).")
        self.db.execute("UPDATE batches SET status = 'running', max_cost = ? WHERE id = ?", (cap, batch_id))
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
            self.db.execute("UPDATE batches SET spent = ?, done = ?, failed = ?, review_ids = ? WHERE id = ?",
                            (round(spent, 6), done, failed, json.dumps(ids), batch_id))
        self.db.execute("UPDATE batches SET status = ?, finished_at = ? WHERE id = ?",
                        (status, self.clock(), batch_id))

    def cancel(self, batch_id):
        batch = self._row(batch_id)
        if batch["status"] == "planned":
            self.db.execute("UPDATE batches SET status = 'cancelled', finished_at = ? WHERE id = ?",
                            (self.clock(), batch_id))
        elif batch["status"] == "running":  # stops before the next commit
            self.db.execute("UPDATE batches SET status = 'cancelling' WHERE id = ?", (batch_id,))
        return self._row(batch_id)
