"""autosde: review commit ranges in local repositories; results go to SQLite."""

import argparse
import sys
from pathlib import Path

from autosde import report
from autosde.config import load_settings
from autosde.db import Store
from autosde.llm import OpenAIModel
from autosde.review import ReviewError, run_review


def _table(rows, columns):
    if not rows:
        return "(none)\n"
    widths = [max(len(c), *(len(str(r.get(c, "") or "")) for r in rows)) for c in columns]
    out = ["  ".join(c.ljust(w) for c, w in zip(columns, widths))]
    out += ["  ".join(str(r.get(c, "") or "").ljust(w) for c, w in zip(columns, widths)) for r in rows]
    return "\n".join(out) + "\n"


def _emit(text, output):
    if output:
        Path(output).write_text(text)
        print(f"wrote {output}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def _render(review, fmt):
    if fmt == "json":
        return report.as_json(review) + "\n"
    if fmt == "sarif":
        return report.as_json(report.sarif(review)) + "\n"
    return report.text(review)


def _project_id(store, ref):
    if ref is None:
        return None
    project = store.find_project(ref)
    if project is None:
        raise SystemExit(f"Unknown project: {ref} (see `autosde projects`)")
    return project["id"]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="autosde", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    review = commands.add_parser("review", help="Review a commit range (sends code to the model)")
    review.add_argument("repo", help="Path to a local git repository")
    review.add_argument("range", help="A..B, A.. (to HEAD), or one commit C (C~1..C)")
    review.add_argument("--format", choices=("text", "json", "sarif"), default="text")
    review.add_argument("--output", help="Write the result to a file")

    commands.add_parser("projects", help="List reviewed projects")
    reviews = commands.add_parser("reviews", help="List reviews")
    reviews.add_argument("project", nargs="?", help="Project id, name or path")
    show = commands.add_parser("show", help="Show one review")
    show.add_argument("review_id", type=int)
    show.add_argument("--format", choices=("text", "json", "sarif"), default="text")
    show.add_argument("--output")
    agents = commands.add_parser("agents", help="Which agents ran, per project")
    agents.add_argument("project", nargs="?")
    serve = commands.add_parser("serve", help="Local web page to submit and browse reviews")
    serve.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)
    settings = load_settings()
    store = Store(settings.db_path)

    if args.command == "review":
        try:
            model = OpenAIModel(settings.model, reasoning_effort=settings.reasoning_effort)
            print(f"Reviewing {args.range} in {args.repo} with {settings.model}...", file=sys.stderr)
            review_id = run_review(args.repo, args.range, store=store, model=model, settings=settings)
        except (ReviewError, RuntimeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        stored = store.review(review_id)
        _emit(_render(stored, args.format), args.output)
        return 0 if stored["status"] == "completed" else 1
    if args.command == "projects":
        rows = store.projects()
        for row in rows:
            row["last_review"] = report.when(row["last_review"])
        sys.stdout.write(_table(rows, ("id", "name", "reviews", "last_review", "path")))
    elif args.command == "reviews":
        rows = store.reviews(_project_id(store, args.project))
        for row in rows:
            row["started"] = report.when(row["started_at"])
            row["range"] = f"{row['base_sha'][:7]}..{row['head_sha'][:7]}"
            row["cost"] = f"${row['cost_usd']:.4f}"
        sys.stdout.write(_table(rows, ("id", "project", "started", "range", "status",
                                       "findings", "agents", "cost")))
    elif args.command == "show":
        stored = store.review(args.review_id)
        if stored is None:
            print(f"No review #{args.review_id}", file=sys.stderr)
            return 1
        _emit(_render(stored, args.format), args.output)
    elif args.command == "agents":
        rows = store.agents(_project_id(store, args.project))
        for row in rows:
            row["last_run"] = report.when(row["last_run"])
        sys.stdout.write(_table(rows, ("project", "role", "agent", "runs", "succeeded",
                                       "failed", "findings_kept", "last_run")))
    elif args.command == "serve":
        from autosde.web import serve as run_server

        run_server(store, settings, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
