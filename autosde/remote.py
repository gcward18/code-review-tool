"""GitHub repositories cloned for review, under ~/.autosde/repos/<owner>/<name>.

Clones and fetches use the machine's own git credentials (SSH keys, the git credential
helper or `gh auth setup-git`), so AutoSDE never sees a token. Only git runs: hooks are
disabled and no repository code is executed.
"""

import re
import shutil
import subprocess
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS remotes (
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,              -- the clone URL (https or ssh)
    path TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,           -- cloning | ready | fetching | failed
    error TEXT,
    fetched_at REAL,
    created_at REAL NOT NULL,
    UNIQUE (owner, name)
);
"""
_PART = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})"
_FORMS = (
    re.compile(rf"^(?:https://)?github\.com/({_PART})/({_PART}?)(?:\.git)?/?$"),
    re.compile(rf"^git@github\.com:({_PART})/({_PART}?)(?:\.git)?$"),
    re.compile(rf"^({_PART})/({_PART}?)(?:\.git)?$"),
)
# Git settings for every clone and fetch: no hooks, no prompts, no LFS downloads.
SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=",
        "-c", "filter.lfs.required=false"]


class RemoteError(ValueError):
    pass


def parse(text):
    """(owner, name, clone url) from `owner/name`, a github.com URL, or an SSH address."""
    text = (text or "").strip()
    for number, form in enumerate(_FORMS):
        match = form.match(text)
        if match:
            owner, name = match[1], match[2]
            if name.endswith(".git"):
                name = name[:-4]
            if not name or name in (".", ".."):
                break
            url = f"git@github.com:{owner}/{name}.git" if number == 1 else f"https://github.com/{owner}/{name}.git"
            return owner, name, url
    raise RemoteError("Enter a GitHub repository as owner/name or its github.com URL.")


def _run(args, cwd=None, timeout=600):
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
           "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
    import os

    result = subprocess.run(["git", *SAFE, *args], cwd=cwd, capture_output=True, timeout=timeout,
                            env={**os.environ, **env})
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").lower()
        if "not found" in tail or "repository not found" in tail:
            raise RemoteError("GitHub says the repository doesn't exist or you can't access it.")
        if "authentication" in tail or "permission denied" in tail or "could not read username" in tail:
            raise RemoteError("Git couldn't authenticate. Check that `git clone` works for this "
                              "repository in your terminal (SSH key or `gh auth setup-git`).")
        raise RemoteError(f"git {args[0]} failed (exit {result.returncode}).")
    return result


class Remotes:
    """Cloned GitHub repositories, recorded in the AutoSDE database."""

    def __init__(self, db, root, *, clock=time.time, clone_url=None):
        self.db, self.root, self.clock = db, Path(root), clock
        self.clone_url = clone_url or (lambda url: url)  # tests point this at a local repo
        self.db.executescript(SCHEMA)

    def _all(self, sql, *args):
        return [dict(row) for row in self.db.execute(sql, args)]

    def list(self):
        return self._all("SELECT * FROM remotes ORDER BY owner, name")

    def get(self, remote_id):
        rows = self._all("SELECT * FROM remotes WHERE id = ?", remote_id)
        if not rows:
            raise RemoteError("No such repository.")
        return rows[0]

    def add(self, text):
        owner, name, url = parse(text)
        path = (self.root / owner / name).resolve()
        if self._all("SELECT id FROM remotes WHERE owner = ? AND name = ?", owner, name):
            raise RemoteError(f"{owner}/{name} is already added.")
        cursor = self.db.execute(
            "INSERT INTO remotes(owner, name, url, path, status, created_at) VALUES (?,?,?,?,'cloning',?)",
            (owner, name, url, str(path), self.clock()))
        return cursor.lastrowid

    def clone(self, remote_id):
        """Clone with full history but blobs fetched on demand (run on a background thread)."""
        remote = self.get(remote_id)
        path = Path(remote["path"])
        try:
            if path.exists():
                shutil.rmtree(self._inside(path))  # a half-finished earlier clone
            path.parent.mkdir(parents=True, exist_ok=True)
            _run(["clone", "--filter=blob:none", "--no-tags", "--", self.clone_url(remote["url"]), str(path)])
            self.db.execute("UPDATE remotes SET status = 'ready', error = NULL, fetched_at = ? WHERE id = ?",
                            (self.clock(), remote_id))
        except (RemoteError, OSError, subprocess.TimeoutExpired) as error:
            message = str(error) if isinstance(error, RemoteError) else f"Clone failed ({type(error).__name__})."
            self.db.execute("UPDATE remotes SET status = 'failed', error = ? WHERE id = ?", (message, remote_id))

    def fetch(self, remote_id):
        """Bring every branch up to date and fast-forward the checked-out one."""
        remote = self.get(remote_id)
        path = Path(remote["path"])
        self.db.execute("UPDATE remotes SET status = 'fetching', error = NULL WHERE id = ?", (remote_id,))
        try:
            _run(["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"], cwd=path)
            branch = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path).stdout.decode().strip()
            if branch and branch != "HEAD":
                _run(["merge", "--ff-only", f"origin/{branch}"], cwd=path)
            self.db.execute("UPDATE remotes SET status = 'ready', fetched_at = ? WHERE id = ?",
                            (self.clock(), remote_id))
        except (RemoteError, OSError, subprocess.TimeoutExpired) as error:
            message = str(error) if isinstance(error, RemoteError) else f"Fetch failed ({type(error).__name__})."
            self.db.execute("UPDATE remotes SET status = 'ready', error = ? WHERE id = ?", (message, remote_id))

    def _inside(self, path):
        path = Path(path).resolve()
        if self.root.resolve() not in path.parents:
            raise RemoteError("Refusing to touch a folder outside the AutoSDE clone directory.")
        return path

    def remove(self, remote_id):
        """Forget the repository and delete its clone (reviews already saved are kept)."""
        remote = self.get(remote_id)
        path = Path(remote["path"])
        if path.exists():
            shutil.rmtree(self._inside(path))
        self.db.execute("DELETE FROM remotes WHERE id = ?", (remote_id,))
