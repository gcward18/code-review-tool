import os
import subprocess
from pathlib import Path

from autosde.config import Settings

# Tests never reach real services: without these, a developer's agents.json with
# LangSmith on would trace test reviews to their account.
for _name in [n for n in os.environ if n.startswith(("LANGSMITH_", "LANGCHAIN_", "OPENAI_"))]:
    del os.environ[_name]

BASE = 'def calculate_total(items):\n    return sum(i["price"] for i in items)\n'
HEAD = (
    "def calculate_total(items):\n    total = 0\n    for item in items:\n"
    '        total = item["price"]\n    return total\n'
)
PLAN = {
    "assignments": [{"agent": "python", "files": ["calculator.py"], "focus": "loop totals"}],
    "rationale": "one python file",
}
FINDING = {
    "file": "calculator.py",
    "start_line": 4,
    "end_line": 4,
    "severity": "medium",
    "category": "correctness",
    "title": "Total is overwritten",
    "description": "The loop assigns instead of adding, so only the last price is returned.",
    "recommendation": 'Use total += item["price"].',
    "confidence": 0.95,
}


def findings(*items, summary="ok"):
    return {"findings": list(items), "summary": summary}


def verdict(rejected=(), summary="One real defect.", follow_ups=()):
    """An aggregator reply: COMPLETE, or CONTINUE when `follow_ups` names (agent, files, question)."""
    return {
        "verdict": "CONTINUE" if follow_ups else "COMPLETE",
        "rejected": [{"id": i, "reason": r} for i, r in rejected],
        "follow_ups": [{"agent": a, "files": list(f), "question": q} for a, f, q in follow_ups],
        "summary": summary,
    }


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", "-C", str(root), *args],
        env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()


def repository(tmp, files_after=None):
    """A repo with a base commit and one head commit that introduces the bug."""
    root = Path(tmp) / "calculator"
    root.mkdir()
    git(root, "init", "-q")
    (root / "calculator.py").write_text(BASE)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    (root / "calculator.py").write_text(HEAD)
    for name, content in (files_after or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "bug")
    return root


def settings(tmp, **changes):
    values = dict(model="scripted", input_price=0.10, output_price=0.50, reasoning_effort="low",
                  db_path=Path(tmp) / "db" / "autosde.db", gitleaks=None)
    return Settings(**(values | changes))


clean_scan = lambda text: []  # noqa: E731
