"""A self-contained Review Crew demo for screen recordings (scripts/record_docs.mjs).

Runs the real local app on a throwaway home folder with a sample repository, a scripted
model (no API key, no cost) and in-memory agent settings, so recordings never show or
change your own projects, settings or prompts.

    python scripts/demo_server.py --port 8790
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autosde import agents  # noqa: E402
from autosde.config import Settings  # noqa: E402
from autosde.db import Store  # noqa: E402
from autosde.llm import ScriptedModel  # noqa: E402
from autosde.web import App, handler_for  # noqa: E402

BEFORE = '''def calculate_total(items):
    """Sum the price of every item in the cart."""
    return sum(item["price"] for item in items)


def apply_discount(total, percent):
    return total - total * percent / 100
'''
AFTER = '''def calculate_total(items):
    """Sum the price of every item in the cart."""
    total = 0
    for item in items:
        total = item["price"]
    return total


def apply_discount(total, percent):
    if percent > 100:
        percent = 100
    return total - total * percent / 100
'''
PLAN = {"assignments": [{"agent": "python", "files": ["cart/calculator.py"],
                         "focus": "accumulating totals in loops"}],
        "rationale": "One Python file changed; the lockfile is generated.",
        "flagged": [{"file": "package-lock.json", "reason": "generated lockfile"}]}
FINDING = {"file": "cart/calculator.py", "start_line": 5, "end_line": 5, "severity": "high",
           "category": "correctness", "title": "Total is overwritten on every item",
           "description": "The loop assigns `total = item[\"price\"]` instead of adding, so "
                          "`calculate_total` returns only the **last** item's price.",
           "recommendation": "@@ lines 4-5 @@\n  for item in items:\n-         total = item[\"price\"]\n"
                             "+         total += item[\"price\"]",
           "confidence": 0.96}
CHAT = {"answer": "Across the reviews of **shop**, `cart/calculator.py` has one recurring problem:\n\n"
                  "- `calculate_total` overwrites the running total (line 5), so carts with "
                  "several items are under-charged.\n\nFix it with `total += item[\"price\"]`."}


class MemoryConfig:
    """Agent settings and prompts kept in memory for the demo (your files are never written)."""

    def __init__(self):
        self.config, self.prompts = None, {}

    def get_config(self):
        return self.config

    def put_config(self, config):
        self.config = config

    def get_prompt(self, name):
        return self.prompts.get(name)

    def put_prompt(self, name, text):
        self.prompts[name] = text


class SlowScripted(ScriptedModel):
    """Scripted replies with a short pause, so planning and review states are visible on video."""

    def complete(self, **kwargs):
        time.sleep(1.2)
        return super().complete(**kwargs)


def scripted():
    return SlowScripted({"coordinator": [PLAN] * 5,
                         "python": [{"findings": [FINDING], "summary": "One real defect."}] * 5,
                         "general": [{"findings": [], "summary": "Nothing to report."}] * 5,
                         "aggregator": [{"rejected": [], "summary": "The cart total is wrong: "
                                         "`calculate_total` keeps only the last price. Fix the loop "
                                         "before merging; the discount clamp is fine."}] * 5,
                         "chat": [CHAT] * 10})


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "Demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
                        "GIT_COMMITTER_NAME": "Demo", "GIT_COMMITTER_EMAIL": "demo@example.com",
                        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                        "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})


def sample_repository(root):
    repo = root / "projects" / "shop"
    (repo / "cart").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "cart" / "calculator.py").write_text(BEFORE)
    (repo / "README.md").write_text("# Shop\n\nA small cart service.\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Add the cart calculator")
    (repo / "cart" / "calculator.py").write_text(AFTER)
    (repo / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {}}))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Clamp discounts and loop over cart items")
    return repo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--gitleaks", default=None, help="gitleaks binary (defaults to PATH)")
    args = parser.parse_args()
    import shutil

    home = Path(tempfile.mkdtemp(prefix="autosde-demo-"))
    sample_repository(home)
    gitleaks = args.gitleaks or shutil.which("gitleaks")
    if not gitleaks:
        sys.exit("gitleaks is needed (install it or pass --gitleaks).")
    settings = Settings(model="gpt-6-luna (demo)", input_price=0.10, output_price=0.50, reasoning_effort="low",
                        db_path=home / "autosde.db", gitleaks=gitleaks)
    agents.BACKEND = MemoryConfig()
    app = App(Store(settings.db_path), settings, model_factory=scripted, port=args.port, browse_root=home)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(app))
    print(json.dumps({"status": "listening", "url": f"http://127.0.0.1:{args.port}/", "home": str(home)}),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
