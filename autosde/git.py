"""Read a committed range from a local repository. Uncommitted edits are ignored."""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass
class ChangedFile:
    path: str
    status: str  # A (added), M (modified), R (renamed)
    head: str  # full file at the head commit
    diff: str  # unified diff for this file, base..head
    skipped: str | None = None  # reason it was not sent to an agent


@dataclass
class Changes:
    repo: Path
    name: str
    base: str
    head: str
    range_spec: str
    files: list[ChangedFile] = field(default_factory=list)


def _git(repo, *args, check=True):
    # Hooks, pagers and external diff tools are disabled for every call.
    result = subprocess.run(
        [
            "git",
            "--no-pager",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise GitError(f"git {args[0]} failed")
    return result.stdout


def repo_root(path):
    try:
        top = _git(Path(path).expanduser(), "rev-parse", "--show-toplevel")
    except (GitError, OSError):
        raise GitError(f"Not a git repository: {path}") from None
    return Path(top.decode().strip())


def resolve_range(repo, spec):
    """`A..B`, `A..` (A to HEAD) or a single commit `C` (meaning C~1..C)."""
    if ".." in spec:
        base, head = spec.split("..", 1)
        head = head or "HEAD"
    else:
        base, head = f"{spec}~1", spec
    if not base or base.startswith("-") or head.startswith("-"):
        # A leading dash would be read by git as an option.
        raise GitError(f"Unknown commit range: {spec}")
    try:
        base_sha = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
        head_sha = _git(repo, "rev-parse", "--verify", f"{head}^{{commit}}")
    except GitError:
        raise GitError(f"Unknown commit range: {spec}") from None
    return base_sha.decode().strip(), head_sha.decode().strip()


@dataclass
class Commit:
    sha: str
    short: str
    subject: str
    when: str


def refs(repo, *, limit=50):
    """Local branches and the most recent commits on HEAD, for range pickers."""
    branches = [
        line
        for line in _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        .decode("utf-8", "replace")
        .splitlines()
        if line
    ]
    log = _git(
        repo, "log", f"-n{limit}", "--format=%H%x1f%h%x1f%s%x1f%cr", "HEAD", check=False
    ).decode("utf-8", "replace")
    commits = [Commit(*line.split("\x1f", 3)) for line in log.splitlines() if line.count("\x1f") == 3]
    return branches, commits


def summary(repo, *, commits=5):
    """Metadata only, for the all-projects chat: branch, recent commit subjects, languages."""
    text = lambda *args: _git(repo, *args, check=False).decode("utf-8", "replace").strip()
    files = text("ls-files").splitlines()
    extensions = {}
    for name in files:
        suffix = Path(name).suffix.lower() or Path(name).name
        extensions[suffix] = extensions.get(suffix, 0) + 1
    log = text("log", f"-n{commits}", "--format=%cs %s", "HEAD")
    return {
        "branch": text("rev-parse", "--abbrev-ref", "HEAD") or None,
        "branches": len(text("for-each-ref", "--format=x", "refs/heads").splitlines()),
        "commits": int(text("rev-list", "--count", "HEAD") or 0),
        "tracked_files": len(files),
        "top_file_types": dict(sorted(extensions.items(), key=lambda kv: -kv[1])[:6]),
        "recent_commits": log.splitlines(),
    }


def resolve_ref(repo, ref):
    """Commit SHA for a branch or ref, or None."""
    if ref.startswith("-"):
        return None
    out = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return out.decode().strip() or None


def older(repo, ref, branches, *, limit=100):
    """Branches and commits strictly older than `ref` (its ancestors), newest first."""
    sha = resolve_ref(repo, ref)
    if sha is None:
        return [], []
    log = _git(repo, "log", f"-n{limit}", "--format=%H%x1f%h%x1f%s%x1f%cr", f"{sha}~1",
               check=False).decode("utf-8", "replace")
    commits = [Commit(*line.split("\x1f", 3)) for line in log.splitlines() if line.count("\x1f") == 3]
    names = []
    for name in branches:
        tip = resolve_ref(repo, name)
        is_ancestor = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", tip or "", sha],
            capture_output=True, timeout=30,
        ).returncode == 0
        if tip and tip != sha and is_ancestor:
            names.append(name)
    return names, commits


def default_base(branches):
    """`main`, else `master`, else None (the caller falls back to an older commit)."""
    return next((name for name in ("main", "master") if name in branches), None)


def capture(path, spec, *, max_file_bytes=64_000, max_total_bytes=400_000):
    repo = repo_root(path)
    base, head = resolve_range(repo, spec)
    if base == head:
        raise GitError("Empty range: base and head are the same commit")
    changes = Changes(repo=repo, name=repo.name, base=base, head=head, range_spec=spec)
    # External diff tools and textconv filters never run on reviewed content.
    listing = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z",
                   "--find-renames", base, head)
    parts = listing.decode("utf-8", "replace").split("\0")
    total, index = 0, 0
    while index < len(parts) - 1:
        status = parts[index][:1]
        if status == "R":
            path_, index = parts[index + 2], index + 3
        else:
            path_, index = parts[index + 1], index + 2
        if status == "D":
            continue  # deleted files have no head content to review
        try:
            raw = _git(repo, "show", f"{head}:{path_}")
        except GitError:
            continue
        diff = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "-U3", base, head,
                    "--", path_).decode(
            "utf-8", "replace"
        )
        item = ChangedFile(path=path_, status=status, head="", diff=diff)
        if b"\0" in raw:
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


def changed_files(repo, base, head):
    """Files changed in base..head with line counts, for the review's file list."""
    numstat = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--numstat", "-z", "--find-renames",
                   base, head).decode("utf-8", "replace")
    statuses = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", "--find-renames",
                    base, head).decode("utf-8", "replace").split("\0")
    kinds, index = {}, 0
    while index < len(statuses) - 1:
        code = statuses[index][:1]
        if code == "R":
            kinds[statuses[index + 2]] = ("R", statuses[index + 1])
            index += 3
        else:
            kinds[statuses[index + 1]] = (code, None)
            index += 2
    files, parts, index = [], numstat.split("\0"), 0
    while index < len(parts) - 1:
        added, removed, path = (parts[index].split("\t") + ["", "", ""])[:3]
        if path == "":  # a rename: "added\tremoved\t" then old and new paths
            path, index = parts[index + 2], index + 3
        else:
            index += 1
        status, old = kinds.get(path, ("M", None))
        files.append({"path": path, "status": status, "old_path": old,
                      "added": None if added == "-" else int(added or 0),
                      "removed": None if removed == "-" else int(removed or 0),
                      "binary": added == "-"})
    return files


def file_diff(repo, base, head, path, *, max_lines=20_000):
    """The whole file as diff rows (context, +, -) with old and new line numbers.

    The viewer folds unchanged runs itself, so it can show any amount of context."""
    files = {f["path"]: f for f in changed_files(repo, base, head)}
    if path not in files:
        raise GitError("That file isn't part of this review.")
    if files[path]["binary"]:
        return {"binary": True, "rows": []}
    old = files[path]["old_path"] or path
    raw = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "-U1000000", "--find-renames",
               base, head, "--", old, path).decode("utf-8", "replace")
    rows, old_no, new_no, started = [], 0, 0, False
    for line in raw.splitlines():
        if line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            old_no, new_no, started = int(match[1]) - 1, int(match[2]) - 1, True
            continue
        if not started:
            continue  # diff --git, index, ---/+++ and rename headers
        kind = line[:1]
        if kind == "\\":
            continue  # "\ No newline at end of file"
        if kind == "+":
            new_no += 1
            rows.append({"t": "+", "o": None, "n": new_no, "s": line[1:]})
        elif kind == "-":
            old_no += 1
            rows.append({"t": "-", "o": old_no, "n": None, "s": line[1:]})
        else:
            old_no += 1
            new_no += 1
            rows.append({"t": " ", "o": old_no, "n": new_no, "s": line[1:]})
        if len(rows) >= max_lines:
            return {"binary": False, "rows": rows, "truncated": True}
    return {"binary": False, "rows": rows, "truncated": False}
