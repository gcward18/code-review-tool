"""GitHub repositories added for review. They are read through the GitHub API (see
github.py); nothing is cloned. Rows from the earlier clone-based version keep working:
their path is rewritten to the repository's GitHub URL."""

import time

from autosde.github import PREFIX, GitHubError, Repo, parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS remotes (
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,      -- https://github.com/<owner>/<name>
    status TEXT NOT NULL,
    error TEXT,
    fetched_at REAL,
    created_at REAL NOT NULL,
    UNIQUE (owner, name)
);
"""
RemoteError = GitHubError


class Remotes:
    def __init__(self, db, *, client=None, clock=time.time):
        self.db, self.client, self.clock = db, client, clock
        self.db.executescript(SCHEMA)
        for row in self._all("SELECT id, owner, name, path FROM remotes"):
            url = f"{PREFIX}{row['owner']}/{row['name']}"
            if row["path"] != url:  # a clone from the earlier version
                self.db.execute("UPDATE remotes SET path = ?, url = ?, status = 'ready', error = NULL"
                                " WHERE id = ?", (url, url, row["id"]))

    def _all(self, sql, *args):
        return [dict(row) for row in self.db.execute(sql, args)]

    def list(self):
        return self._all("SELECT * FROM remotes ORDER BY owner, name")

    def get(self, remote_id):
        rows = self._all("SELECT * FROM remotes WHERE id = ?", remote_id)
        if not rows:
            raise GitHubError("No such repository.")
        return rows[0]

    def by_path(self, path):
        rows = self._all("SELECT * FROM remotes WHERE path = ?", path)
        return rows[0] if rows else None

    def add(self, text):
        """Check the repository exists (and is readable) on GitHub, then record it."""
        owner, name = parse(text)
        if self._all("SELECT id FROM remotes WHERE owner = ? AND name = ?", owner, name):
            raise GitHubError(f"{owner}/{name} is already added.")
        Repo(owner, name, self.client).info()  # raises a readable GitHubError when unreachable
        url = f"{PREFIX}{owner}/{name}"
        cursor = self.db.execute(
            "INSERT INTO remotes(owner, name, url, path, status, created_at) VALUES (?,?,?,?,'ready',?)",
            (owner, name, url, url, self.clock()))
        return cursor.lastrowid

    def remove(self, remote_id):
        """Forget the repository (its reviews are kept)."""
        self.get(remote_id)
        self.db.execute("DELETE FROM remotes WHERE id = ?", (remote_id,))
