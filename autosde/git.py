"""Read a committed range from a local repository. Uncommitted edits are ignored."""

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
    try:
        base_sha = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
        head_sha = _git(repo, "rev-parse", "--verify", f"{head}^{{commit}}")
    except GitError:
        raise GitError(f"Unknown commit range: {spec}") from None
    return base_sha.decode().strip(), head_sha.decode().strip()


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
