"""Two-phase review: plan (scan, rules, coordinator), then run on approved files only."""

from dataclasses import dataclass

from autosde import git, tracing
from autosde.knowledge import Knowledge
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


def retriever_for(store, model, scan, knowledge=None):
    """Hybrid knowledge base search, embedding queries with the review's model client.
    `knowledge` is a knowledge store (DynamoDB in AWS); locally the store's SQLite is used."""
    backend = knowledge if knowledge is not None else getattr(store, "db", None)
    if not hasattr(model, "embed") or backend is None:
        return None
    searcher = Knowledge(backend, embed=model.embed, scan=scan)
    return lambda kb_ids, query: searcher.search(kb_ids, query)


def prepare(repo, range_spec, *, store, model, settings, scan=None, on_start=None, source=None):
    """Capture, scan and plan. Only the coordinator (file paths, no code) calls the model."""
    try:
        # A source (such as a GitHub repository) builds the same Changes without a checkout.
        capture = source.capture if source is not None else lambda spec, **kw: git.capture(repo, spec, **kw)
        changes = capture(
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
        model = tracing.traced(model, review_id=review_id, scan=scan,
                               name=f"{changes.name} {range_spec} (review {review_id})")
        team = Team(model, retriever=retriever_for(store, model, scan))
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
        tracing.finish(pending.team.model, {"status": "failed"})
        raise
    tracing.finish(pending.team.model, {
        "status": "completed", "plan": result.plan,
        "findings": [{"agent": f.agent, "file": f.file, "line": f.start_line, "title": f.title,
                      "severity": f.severity, "kept": f.kept} for f in result.findings],
    })
    return pending.review_id


def cancel(pending, *, store):
    store.cancel_review(pending.review_id, pending.result)
    tracing.finish(pending.team.model, {"status": "cancelled"})


def run_review(repo, range_spec, *, store, model, settings, scan=None, on_start=None,
               selection=None, source=None):
    """Plan and approve in one step (scripts, notebooks, `--yes`)."""
    pending = prepare(repo, range_spec, store=store, model=model, settings=settings, scan=scan,
                      on_start=on_start, source=source)
    if not pending.result.plan:
        store.finish_review(pending.review_id, pending.result,
                            input_price=settings.input_price, output_price=settings.output_price)
        tracing.finish(pending.team.model, {"status": "completed", "plan": {}})
        return pending.review_id
    return approve(pending, store=store, settings=settings, selection=selection)


# --- stateless plan and approval (the web app and AWS) -------------------------------
#
# The plan is saved with the review; approval reads the files again at the planned commits
# and scans them again, so nothing waits in memory between the two (they can even run in
# different processes, as the AWS worker does). Source code is never stored.

def _capture(repo, range_spec, settings, source):
    try:
        if source is not None:
            return source.capture(range_spec, max_file_bytes=settings.max_file_bytes,
                                  max_total_bytes=settings.max_total_bytes)
        return git.capture(repo, range_spec, max_file_bytes=settings.max_file_bytes,
                           max_total_bytes=settings.max_total_bytes)
    except git.GitError as error:
        raise ReviewError(str(error)) from None


def _scan_files(changes, scan):
    for item in changes.files:
        if item.skipped:
            continue
        rules = scan(item.head + "\n" + item.diff)
        if rules:
            item.skipped = f"possible secret ({', '.join(rules)}); not sent"
            item.head = ""


def record_of(result, files, model):
    """What approval needs later: the plan, flags, and sizes for the cost estimate."""
    from autosde.team import numbered, prompt

    return {
        "plan": result.plan, "flagged": result.flagged, "limitations": result.limitations,
        "summary": result.summary,
        "sizes": {f.path: len(numbered(f.head)) + len(f.diff) + 60 for f in files if not f.skipped},
        "prompts": {agent: len(prompt("specialist.md", agent=agent, focus=item["focus"]))
                    for agent, item in result.plan.items()},
        "trace": tracing.root_of(model),
    }


def files_of(record):
    return [p for item in record["plan"].values() for p in item["files"]]


def default_selection(record):
    return {p for p in files_of(record) if p not in record["flagged"]}


def estimate_record(record, selection):
    """(input tokens, output tokens, agents) for approving `selection`, from the saved plan."""
    chars, agents = 0, 0
    for agent, item in record["plan"].items():
        chosen = [p for p in item["files"] if p in selection]
        if chosen:
            agents += 1
            chars += record["prompts"].get(agent, 0) + sum(record["sizes"].get(p, 0) for p in chosen)
    return chars // 4, agents * 4000 + (1500 if agents else 0), agents


def start(store, repo, range_spec, *, model_name, source=None):
    """Create the review row (status planning) so callers get its id before any work runs."""
    if source is not None:
        base, head = source.resolve_range(range_spec)
        path, name = source.path, f"{source.owner}/{source.name}"
    else:
        root = git.repo_root(repo)
        base, head = git.resolve_range(root, range_spec)
        path, name = root, root.name
    if base == head:
        raise ReviewError("Empty range: base and head are the same commit")
    changes = git.Changes(repo=path, name=name, base=base, head=head, range_spec=range_spec)
    return store.start_review(store.project(path, name), changes, model_name)


def plan_review(review_id, repo, *, store, model, settings, scan=None, source=None, knowledge=None):
    """Capture, scan and run the coordinator for a review created by `start`."""
    review = store.review(review_id)
    try:
        scan = scan or scanner(settings.gitleaks)
        changes = _capture(repo, f"{review['base_sha']}..{review['head_sha']}", settings, source)
        store.set_files_changed(review_id, len(changes.files))
        _scan_files(changes, scan)
        model = tracing.traced(model, review_id=review_id, scan=scan,
                               name=f"{review['project']} {review['range_spec']} (review {review_id})")
        team = Team(model, retriever=retriever_for(store, model, scan, knowledge))
        result = team.plan(changes.files)
        if not result.plan:  # nothing to approve
            store.finish_review(review_id, result, input_price=settings.input_price,
                                output_price=settings.output_price)
            tracing.finish(model, {"status": "completed", "plan": {}})
            return
        store.save_plan(review_id, result, changes.files, input_price=settings.input_price,
                        output_price=settings.output_price, record=record_of(result, changes.files, model))
    except (ReviewError, ScanUnavailable) as error:
        store.fail_review(review_id, str(error))
    except Exception as error:
        store.fail_review(review_id, f"Review failed ({type(error).__name__})")


def approve_review(review_id, repo, *, store, model, settings, selection=None, scan=None, source=None,
                   knowledge=None):
    """Run the specialists and aggregator for a saved plan, reading the files again."""
    from autosde.team import Call

    review, record = store.review(review_id), store.plan(review_id)
    try:
        scan = scan or scanner(settings.gitleaks)
        changes = _capture(repo, f"{review['base_sha']}..{review['head_sha']}", settings, source)
        _scan_files(changes, scan)
        model = tracing.traced(model, review_id=review_id, scan=scan, root=record.get("trace"),
                               name=f"{review['project']} {review['range_spec']} (review {review_id})")
        team = Team(model, retriever=retriever_for(store, model, scan, knowledge))
        # The coordinator's call is already stored; carry its tokens into the totals.
        planned = Call(role="coordinator", agent="coordinator", files=[], stored=True,
                       input_tokens=review["input_tokens"], output_tokens=review["output_tokens"])
        result = TeamResult(calls=[planned], plan=record["plan"], flagged=record["flagged"],
                            limitations=list(record["limitations"]), summary=record["summary"])
        chosen = default_selection(record) if selection is None else set(selection) & set(files_of(record))
        result = team.execute(result, changes.files, chosen)
        store.finish_review(review_id, result, input_price=settings.input_price,
                            output_price=settings.output_price)
        tracing.finish(model, {
            "status": "completed", "plan": result.plan,
            "findings": [{"agent": f.agent, "file": f.file, "line": f.start_line, "title": f.title,
                          "severity": f.severity, "kept": f.kept} for f in result.findings]})
    except (ReviewError, ScanUnavailable) as error:
        store.fail_review(review_id, str(error))
    except Exception as error:
        store.fail_review(review_id, f"Review failed ({type(error).__name__})")
