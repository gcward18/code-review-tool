"""Two-phase review: plan (scan, rules, coordinator), then run on approved files only."""

from dataclasses import dataclass

from autosde import git
from autosde.scan import ScanUnavailable, scanner
from autosde.team import Team, TeamResult


class ReviewError(RuntimeError):
    pass


@dataclass
class Pending:
    """A planned review awaiting approval. File contents live only in memory."""

    review_id: int
    changes: git.Changes
    result: TeamResult
    team: Team

    def estimate(self, selection=None):
        selection = self.result.default_selection() if selection is None else set(selection)
        return self.team.estimate(self.result, self.changes.files, selection)


def prepare(repo, range_spec, *, store, model, settings, scan=None, on_start=None):
    """Capture, scan and plan. Only the coordinator (file paths, no code) calls the model."""
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
        team = Team(model)
        result = team.plan(changes.files)
        store.save_plan(review_id, result, changes.files, input_price=settings.input_price,
                        output_price=settings.output_price)
    except ScanUnavailable as error:
        store.fail_review(review_id, str(error))
        raise ReviewError(str(error)) from None
    except Exception as error:
        store.fail_review(review_id, type(error).__name__)
        raise
    return Pending(review_id, changes, result, team)


def approve(pending, *, store, settings, selection=None):
    """Run specialists and the aggregator on the selected paths (default: unflagged)."""
    store.mark_running(pending.review_id)
    try:
        result = pending.team.execute(pending.result, pending.changes.files, selection)
        store.finish_review(
            pending.review_id,
            result,
            input_price=settings.input_price,
            output_price=settings.output_price,
        )
    except Exception as error:
        store.fail_review(pending.review_id, type(error).__name__)
        raise
    return pending.review_id


def cancel(pending, *, store):
    store.cancel_review(pending.review_id, pending.result)


def run_review(repo, range_spec, *, store, model, settings, scan=None, on_start=None,
               selection=None):
    """Plan and approve in one step (scripts, notebooks, `--yes`)."""
    pending = prepare(repo, range_spec, store=store, model=model, settings=settings, scan=scan,
                      on_start=on_start)
    if not pending.result.plan:
        store.finish_review(pending.review_id, pending.result,
                            input_price=settings.input_price, output_price=settings.output_price)
        return pending.review_id
    return approve(pending, store=store, settings=settings, selection=selection)
