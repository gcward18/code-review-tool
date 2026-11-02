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


class SqliteRemoteStore:
    def __init__(self, db):
        self.db = db
        self.db.executescript(SCHEMA)

    def _all(self, sql, *args):
        return [dict(row) for row in self.db.execute(sql, args)]

    def list(self):
        return self._all("SELECT * FROM remotes ORDER BY owner, name")

    def get(self, remote_id):
        rows = self._all("SELECT * FROM remotes WHERE id = ?", remote_id)
        return rows[0] if rows else None

    def by_path(self, path):
        rows = self._all("SELECT * FROM remotes WHERE path = ?", path)
        return rows[0] if rows else None

    def insert(self, owner, name, url, created_at):
        if self._all("SELECT id FROM remotes WHERE owner = ? AND name = ?", owner, name):
            raise GitHubError(f"{owner}/{name} is already added.")
        return self.db.execute(
            "INSERT INTO remotes(owner, name, url, path, status, created_at) VALUES (?,?,?,?,'ready',?)",
            (owner, name, url, url, created_at)).lastrowid

    def set_path(self, remote_id, url):
        self.db.execute("UPDATE remotes SET path = ?, url = ?, status = 'ready', error = NULL WHERE id = ?",
                        (url, url, remote_id))

    def delete(self, remote_id):
        self.db.execute("DELETE FROM remotes WHERE id = ?", (remote_id,))


class Remotes:
    """`store` is a SQLite connection or a repository store (SQLite or DynamoDB)."""

    def __init__(self, store, *, client=None, clock=time.time):
        import sqlite3

        self.store = SqliteRemoteStore(store) if isinstance(store, sqlite3.Connection) else store
        self.client, self.clock = client, clock
        for row in self.store.list():
            url = f"{PREFIX}{row['owner']}/{row['name']}"
            if row["path"] != url:  # a clone from the earlier version
                self.store.set_path(row["id"], url)

    def list(self):
        return self.store.list()

    def get(self, remote_id):
        row = self.store.get(remote_id)
        if not row:
            raise GitHubError("No such repository.")
        return row

    def by_path(self, path):
        return self.store.by_path(path)

    def add(self, text):
        """Check the repository exists (and is readable) on GitHub, then record it."""
        owner, name = parse(text)
        if any(r["owner"] == owner and r["name"] == name for r in self.store.list()):
            raise GitHubError(f"{owner}/{name} is already added.")
        Repo(owner, name, self.client).info()  # raises a readable GitHubError when unreachable
        return self.store.insert(owner, name, f"{PREFIX}{owner}/{name}", self.clock())

    def remove(self, remote_id):
        """Forget the repository (its reviews are kept)."""
        self.get(remote_id)
        self.store.delete(remote_id)
