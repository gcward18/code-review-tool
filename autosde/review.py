"""Capture a commit range, scan it, run the team, and store everything."""

from autosde import git
from autosde.scan import ScanUnavailable, scanner
from autosde.team import Team


class ReviewError(RuntimeError):
    pass


def run_review(repo, range_spec, *, store, model, settings, scan=None, on_start=None):
    """Return the stored review id. Raises ReviewError for problems before the team runs."""
    try:
        changes = git.capture(
            repo,
            range_spec,
            max_file_bytes=settings.max_file_bytes,
            max_total_bytes=settings.max_total_bytes,
        )
    except git.GitError as error:
        raise ReviewError(str(error)) from None
    try:
        scan = scan or scanner(settings.gitleaks)
    except ScanUnavailable as error:
        raise ReviewError(str(error)) from None
    project_id = store.project(changes.repo, changes.name)
    review_id = store.start_review(project_id, changes, model.model)
    if on_start:
        on_start(review_id)
    try:
        # Each file's content and diff are scanned before anything is sent.
        for item in changes.files:
            if item.skipped:
                continue
            rules = scan(item.head + "\n" + item.diff)
            if rules:
                item.skipped = f"possible secret ({', '.join(rules)}); not sent"
                item.head = ""
        result = Team(model).run(changes.files)
        store.finish_review(
            review_id,
            result,
            input_price=settings.input_price,
            output_price=settings.output_price,
        )
    except ScanUnavailable as error:
        store.fail_review(review_id, str(error))
    except Exception as error:
        store.fail_review(review_id, f"{type(error).__name__}")
        raise
    return review_id
