"""Review GitHub repositories straight from the GitHub API, with no clone.

Requests go through the `gh` CLI when it is installed and logged in, so private
repositories work and AutoSDE never sees a token. Without `gh`, public repositories are
read anonymously over HTTPS (GitHub allows about 60 anonymous requests an hour).

A GitHub project is stored with the path `https://github.com/<owner>/<name>`.
"""

import difflib
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from autosde.git import ChangedFile, Changes, GitError

API = "https://api.github.com"
PREFIX = "https://github.com/"
_PART = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})"
_FORMS = (
    re.compile(rf"^(?:https://)?github\.com/({_PART})/({_PART}?)(?:\.git)?/?$"),
    re.compile(rf"^git@github\.com:({_PART})/({_PART}?)(?:\.git)?$"),
    re.compile(rf"^({_PART})/({_PART}?)(?:\.git)?$"),
)
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class GitHubError(GitError):
    pass


def parse(text):
    """(owner, name) from `owner/name`, a github.com URL or an SSH address."""
    text = (text or "").strip()
    for form in _FORMS:
        match = form.match(text)
        if match and match[2] not in ("", ".", ".."):
            name = match[2][:-4] if match[2].endswith(".git") else match[2]
            return match[1], name
    raise GitHubError("Enter a GitHub repository as owner/name or its github.com URL.")


def is_github(path):
    return isinstance(path, str) and path.startswith(PREFIX)


def from_path(path):
    owner, name = path[len(PREFIX):].split("/", 1)
    return owner, name


def _check_ref(ref):
    if not ref or ref.startswith("-") or ".." in ref or not _REF.match(ref):
        raise GitHubError(f"Unknown commit or branch: {ref}")
    return ref


class Client:
    """GET requests against the GitHub API: via `gh api` when available, else anonymous HTTPS."""

    def __init__(self, *, gh=None, opener=None):
        self.gh = shutil.which("gh") if gh is None else gh
        self._authed = None
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def authenticated(self):
        if self._authed is None:
            self._authed = bool(self.gh) and subprocess.run(
                [self.gh, "auth", "status"], capture_output=True, timeout=15).returncode == 0
        return self._authed

    def get(self, path, *, raw=False):
        accept = "application/vnd.github.raw" if raw else "application/vnd.github+json"
        if self.authenticated():
            result = subprocess.run([self.gh, "api", "-H", f"Accept: {accept}", path],
                                    capture_output=True, timeout=60)
            if result.returncode != 0:
                tail = result.stderr.decode("utf-8", "replace").lower()
                if "404" in tail or "not found" in tail:
                    raise GitHubError("GitHub says that doesn't exist or you can't access it.")
                if "rate limit" in tail:
                    raise GitHubError("GitHub's rate limit was reached; try again later.")
                raise GitHubError("GitHub request failed.")
            body = result.stdout
        else:
            request = urllib.request.Request(f"{API}/{path}", headers={
                "Accept": accept, "User-Agent": "autosde", "X-GitHub-Api-Version": "2022-11-28"})
            try:
                with self.opener.open(request, timeout=60) as response:
                    body = response.read(20_000_000)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    raise GitHubError("GitHub says that doesn't exist, or it's private: log in with "
                                      "`gh auth login` to review private repositories.") from None
                if error.code in (403, 429):
                    raise GitHubError("GitHub's anonymous rate limit was reached; log in with "
                                      "`gh auth login` for higher limits.") from None
                raise GitHubError(f"GitHub request failed (HTTP {error.code}).") from None
            except Exception as error:
                raise GitHubError(f"GitHub request failed ({type(error).__name__}).") from None
        return body if raw else json.loads(body or b"null")


class Repo:
    """One GitHub repository as a review source (the same shapes the local git source gives)."""

    def __init__(self, owner, name, client=None):
        self.owner, self.name = owner, name
        self.client = client or Client()
        self.path = f"{PREFIX}{owner}/{name}"
        self._contents = {}

    def _api(self, tail, **query):
        q = f"?{urllib.parse.urlencode(query)}" if query else ""
        return f"repos/{self.owner}/{self.name}/{tail}{q}"

    def info(self):
        data = self.client.get(f"repos/{self.owner}/{self.name}")
        return {"default_branch": data["default_branch"], "private": data["private"],
                "description": data.get("description") or ""}

    def branches(self):
        return [b["name"] for b in self.client.get(self._api("branches", per_page=100))]

    def commits(self, ref, *, page=1, per_page=30):
        rows = self.client.get(self._api("commits", sha=_check_ref(ref), page=page, per_page=per_page))
        return [{"sha": c["sha"], "short": c["sha"][:7],
                 "subject": (c["commit"]["message"] or "").splitlines()[0][:200],
                 "author": (c["commit"].get("author") or {}).get("name", ""),
                 "date": (c["commit"].get("committer") or {}).get("date", "")[:10],
                 "parents": [p["sha"] for p in c.get("parents", [])]} for c in rows]

    def resolve(self, ref):
        return self.client.get(self._api(f"commits/{_check_ref(ref)}"))["sha"]

    def resolve_range(self, spec):
        """`A..B` or a single commit `C` (C's first parent..C), as full SHAs."""
        if ".." in spec:
            base, head = spec.split("..", 1)
            head = head or "HEAD"
            return self.resolve(base), self.resolve(head)
        data = self.client.get(self._api(f"commits/{_check_ref(spec)}"))
        if not data.get("parents"):
            raise GitHubError("That is the repository's first commit; it has nothing to compare against.")
        return data["parents"][0]["sha"], data["sha"]

    def compare(self, base, head):
        data = self.client.get(self._api(f"compare/{base}...{head}"))
        return data.get("files") or [], data.get("commits") or []

    def content(self, path, ref, *, limit=None):
        key = (path, ref)
        if key not in self._contents:
            try:
                raw = self.client.get(self._api(f"contents/{urllib.parse.quote(path)}", ref=ref), raw=True)
            except GitHubError:
                raw = None
            if isinstance(raw, (dict, list)):  # a JSON reply means a directory or submodule
                raw = None
            self._contents[key] = raw
        return self._contents[key]

    def capture(self, spec, *, max_file_bytes=64_000, max_total_bytes=400_000):
        """The same `Changes` the local source builds: head content and a diff per file."""
        base, head = self.resolve_range(spec)
        if base == head:
            raise GitHubError("Empty range: base and head are the same commit")
        files, _ = self.compare(base, head)
        changes = Changes(repo=self.path, name=f"{self.owner}/{self.name}", base=base, head=head,
                          range_spec=spec)
        total = 0
        for f in files:
            if f["status"] == "removed":
                continue
            patch = f.get("patch")
            path = f["filename"]
            diff = f"--- a/{f.get('previous_filename', path)}\n+++ b/{path}\n{patch}" if patch else ""
            item = ChangedFile(path=path, status={"added": "A", "renamed": "R"}.get(f["status"], "M"),
                               head="", diff=diff)
            if not patch:
                item.skipped = "binary or too large for GitHub to diff"
            else:
                raw = self.content(path, head)
                if raw is None:
                    item.skipped = "content unavailable"
                elif b"\0" in raw:
                    item.skipped = "binary"
                elif len(raw) > max_file_bytes:
                    item.skipped = f"larger than {max_file_bytes} bytes"
                elif total + len(raw) > max_total_bytes:
                    item.skipped = "review size limit reached"
                else:
                    try:
                        item.head = raw.decode("utf-8")
                        total += len(raw)
                    except UnicodeDecodeError:
                        item.skipped = "not UTF-8 text"
            changes.files.append(item)
        return changes

    def commits_between(self, base, head, mode, every=1, limit=20):
        """Commits in base..head chosen like the local batch modes (first parent line)."""
        _, commits = self.compare(base, head)
        line, want = [], head
        by_sha = {c["sha"]: c for c in commits}
        while want in by_sha:  # walk first parents back from head
            line.append(by_sha[want])
            parents = by_sha[want].get("parents") or []
            want = parents[0]["sha"] if parents else None
        line.reverse()
        if mode == "merges":
            line = [c for c in line if len(c.get("parents") or []) > 1]
        shas = [c["sha"] for c in line]
        if mode == "nth":
            shas = shas[::-1][::max(1, every)][::-1]
        return shas[-limit:]

    def describe(self, sha):
        data = self.client.get(self._api(f"commits/{sha}"))
        return {"short": sha[:7], "subject": (data["commit"]["message"] or "").splitlines()[0][:200],
                "date": (data["commit"].get("committer") or {}).get("date", "")[:10]}

    def changed_files(self, base, head):
        files, _ = self.compare(base, head)
        return [{"path": f["filename"], "status": {"added": "A", "removed": "D", "renamed": "R"}.get(f["status"], "M"),
                 "old_path": f.get("previous_filename"), "added": f.get("additions", 0),
                 "removed": f.get("deletions", 0), "binary": "patch" not in f and f.get("changes", 0) == 0}
                for f in files]

    def file_diff(self, base, head, path, *, max_lines=20_000):
        """Whole-file diff rows built from both versions, like git's -U<everything>."""
        files = {f["path"]: f for f in self.changed_files(base, head)}
        if path not in files:
            raise GitHubError("That file isn't part of this review.")
        f = files[path]
        old = b"" if f["status"] == "A" else self.content(f["old_path"] or path, base) or b""
        new = b"" if f["status"] == "D" else self.content(path, head) or b""
        if b"\0" in old or b"\0" in new:
            return {"binary": True, "rows": []}
        return rows_between(old.decode("utf-8", "replace"), new.decode("utf-8", "replace"), max_lines)


def rows_between(old, new, max_lines=20_000):
    a, b = old.splitlines(), new.splitlines()
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            rows += [{"t": " ", "o": i1 + k + 1, "n": j1 + k + 1, "s": a[i1 + k]} for k in range(i2 - i1)]
        else:
            rows += [{"t": "-", "o": i + 1, "n": None, "s": a[i]} for i in range(i1, i2)]
            rows += [{"t": "+", "o": None, "n": j + 1, "s": b[j]} for j in range(j1, j2)]
        if len(rows) >= max_lines:
            return {"binary": False, "rows": rows[:max_lines], "truncated": True}
    return {"binary": False, "rows": rows, "truncated": False}
