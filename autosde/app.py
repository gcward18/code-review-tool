"""The Review Crew application: everything the JSON API does, independent of how requests
arrive (the local HTTP server in web.py, or API Gateway and Lambda in aws.py).

Storage, the model client, GitHub access and background jobs are passed in, so the same
code runs locally (SQLite, threads, local folders) and in AWS (DynamoDB, worker Lambda,
GitHub only).
"""

import base64
import binascii
import json
import os
import secrets
import time
from pathlib import Path

from autosde import agents, chat, git, github
from autosde.batch import MODES, BatchError, Batches
from autosde.jobs import ThreadJobs
from autosde.knowledge import Knowledge, KnowledgeError, connect
from autosde.llm import OpenAIModel
from autosde.remote import RemoteError, Remotes
from autosde.review import (ReviewError, approve_review, default_selection, estimate_record, files_of,
                            plan_review, start as start_review)
from autosde.scan import ScanUnavailable, scanner

# Never listed or searched: system data and dependency folders.
SKIP = {"Library", "node_modules", "__pycache__", "venv", "Applications"}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, store, settings, *, model_factory=None, port=8765, browse_root=None,
                 github_client=None, knowledge_store=None, remote_store=None, batch_store=None,
                 jobs=None, uploads=None, cloud=False):
        """Local defaults: SQLite connections per call, threads, your home folder.
        In AWS (`cloud=True`) the stores are DynamoDB, jobs invoke the worker Lambda,
        uploads go through S3, and only GitHub repositories can be reviewed."""
        self.store, self.settings, self.port, self.cloud = store, settings, port, cloud
        self.token = secrets.token_urlsafe(24)
        self.model_factory = model_factory or (lambda: OpenAIModel.from_settings(settings))
        # The folder tree never leaves this root (your home folder by default).
        self.browse_root = Path(browse_root or Path.home()).resolve()
        self._repos = None  # (found_at, [paths]) from the last repository walk
        self._models = None  # cached model list; fetched once per start
        self.github_client = github_client or github.Client()  # gh CLI when logged in, else anonymous
        local = lambda: connect(settings.db_path)  # each request (and job) gets its own connection
        self.knowledge_store = knowledge_store or local
        self.remote_store = remote_store or local
        self.batch_store = batch_store or local
        self.jobs = jobs or ThreadJobs(self.run_job)
        self.uploads = uploads  # S3 in AWS: uploaded documents pass through it to the worker
        if not cloud:  # create the local tables once
            Knowledge(store.db, embed=None, scan=None)
            Remotes(store.db, client=self.github_client)
            Batches(store.db)

    def models(self):
        """Models for the Agents page: the key's text models, or what is configured."""
        if self._models is None:
            try:
                self._models = {"models": self.model_factory().models(), "source": "openai"}
            except Exception:
                return {"default": self.settings.model, "models": [], "source": "unavailable"}
        return {"default": self.settings.model, **self._models}

    # --- folders and refs ---------------------------------------------------

    def _inside(self, raw):
        """Resolve a path; None when it is outside the browse root or not a folder."""
        try:
            path = Path(raw).expanduser().resolve() if raw else self.browse_root
        except (OSError, RuntimeError, ValueError):
            return None
        if path != self.browse_root and self.browse_root not in path.parents:
            return None
        return path if path.is_dir() else None

    def _source(self, raw):
        """The GitHub source for an added repository's path, else None (a local repository)."""
        if not github.is_github(raw):
            return None
        if self._remotes().by_path(raw) is None:
            raise ApiError(400, "Add that GitHub repository first.")
        owner, name = github.from_path(raw)
        return github.Repo(owner, name, self.github_client)

    def _repo(self, raw):
        if github.is_github(raw):
            self._source(raw)
            return raw
        if self.cloud:
            raise ApiError(400, "Only GitHub repositories can be reviewed here.")
        path = self._inside(raw)
        if path is None:
            raise ApiError(400, "Choose a folder inside your home folder.")
        try:
            root = git.repo_root(path)
        except git.GitError:
            raise ApiError(400, "That folder is not a git repository.") from None
        if self._inside(str(root)) is None:
            raise ApiError(400, "That repository is outside your home folder.")
        return root

    @staticmethod
    def _readable(path):
        """False for folders macOS has not approved for this app (or that we cannot list)."""
        try:
            with os.scandir(path):
                return True
        except OSError:
            return False

    def repositories(self):
        """(repositories, blocked folders) under the browse root, from a bounded walk.

        Blocked folders are ones macOS has not approved for this app. Cached briefly.
        """
        if self._repos and time.monotonic() - self._repos[0] < 60:
            return self._repos[1]
        found, blocked, deadline = [], [], time.monotonic() + 5
        stack = [(self.browse_root, 0)]
        while stack and time.monotonic() < deadline and len(found) < 1000:
            folder, depth = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    children = [e for e in entries if e.is_dir(follow_symlinks=False)]
            except PermissionError:
                blocked.append(str(folder))
                continue
            except OSError:
                continue
            if any(e.name == ".git" for e in children):
                found.append(str(folder))
                continue
            if depth < 5:
                stack += [(Path(e.path), depth + 1) for e in children
                          if not e.name.startswith(".") and e.name not in SKIP]
        result = (sorted(found, key=str.lower), sorted(blocked, key=str.lower))
        self._repos = (time.monotonic(), result)
        return result

    def workspaces(self):
        """Recent (reviewed) projects, other available repositories, and blocked folders."""
        if self.cloud:  # no local folders in AWS: GitHub repositories only
            return {"github": self._github_repos(), "recent": [], "available": [], "blocked": []}
        found, blocked = self.repositories()
        recent, seen = [], set()
        for p in self.store.projects():
            if self._inside(p["path"]) is None:
                continue
            entry = {"name": p["name"], "path": p["path"], "project_id": p["id"],
                     "reviews": p["reviews"], "last_review": p["last_review"]}
            seen.add(str(Path(p["path"]).resolve()))
            if self._readable(p["path"]):
                recent.append(entry)
            else:
                blocked.append(p["path"])
        available = [{"name": Path(path).name, "path": path} for path in found if path not in seen]
        available.sort(key=lambda r: r["name"].lower())
        return {
            "github": self._github_repos(),
            "recent": recent,
            "available": available,
            "blocked": [{"name": Path(path).name, "path": path}
                        for path in sorted(set(blocked), key=str.lower)],
        }

    def _github_repos(self):
        reviewed = {p["path"]: p for p in self.store.projects()}
        return [{"name": f"{r['owner']}/{r['name']}", "path": r["path"], "remote_id": r["id"],
                 "status": r["status"], "error": r["error"],
                 "reviews": (reviewed.get(r["path"]) or {}).get("reviews", 0)}
                for r in self._remotes().list()]

    def tree(self, raw):
        if self.cloud:
            raise ApiError(404, "Not found.")
        path = self._inside(raw)
        if path is None:
            raise ApiError(400, "Folder outside your home folder.")
        try:
            folders = sorted(
                (p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")
                 and p.name not in SKIP and self._readable(p)),
                key=lambda p: p.name.lower(),
            )[:500]
        except PermissionError:
            raise ApiError(403, "macOS blocked access to this folder. Allow it in System Settings → "
                                "Privacy & Security → Files and Folders.") from None
        return {
            "path": str(path),
            "root": str(self.browse_root),
            "entries": [{"name": p.name, "path": str(p), "repo": (p / ".git").exists()} for p in folders],
        }

    def _github_options(self, source, ref, *, skip_first=False):
        try:
            commits = source.commits(ref, per_page=51 if skip_first else 50)
        except github.GitHubError as error:
            raise ApiError(400, str(error)) from None
        commits = commits[1:] if skip_first else commits
        return [{"value": c["sha"], "label": f"{c['short']} {c['subject'][:80]}", "kind": "commit",
                 "when": c["date"]} for c in commits]

    def repo(self, raw):
        """From choices: HEAD, branches and recent commits. Default HEAD."""
        source = self._source(raw)
        if source is not None:
            try:
                info, branches = source.info(), source.branches()
            except github.GitHubError as error:
                raise ApiError(400, str(error)) from None
            default = info["default_branch"]
            branches = [default] + [b for b in branches if b != default]
            return {"path": raw, "name": f"{source.owner}/{source.name}", "github": True,
                    "project_id": (self.store.find_project(raw) or {}).get("id"),
                    "from": [{"value": b, "label": b, "kind": "branch"} for b in branches]
                            + self._github_options(source, default),
                    "default_from": default}
        root = self._repo(raw)
        branches, commits = git.refs(root)
        return {
            "path": str(root),
            "name": root.name,
            "project_id": (self.store.find_project(str(root)) or {}).get("id"),
            "from": self._options(["HEAD"], branches, commits),
            "default_from": "HEAD",
        }

    def older(self, raw, start):
        """To choices: only refs strictly older than From. Default main, else From's parent."""
        source = self._source(raw)
        if source is not None:
            if start not in {o["value"] for o in self.repo(raw)["from"]}:
                raise ApiError(400, "Pick From from the list.")
            options = self._github_options(source, start, skip_first=True)
            return {"to": options, "default_to": options[0]["value"] if options else None}
        root = self._repo(raw)
        branches, _ = git.refs(root)
        if start not in {o["value"] for o in self.repo(raw)["from"]}:
            raise ApiError(400, "Pick From from the list.")
        names, commits = git.older(root, start, branches)
        options = self._options([], names, commits)
        base = git.default_base(names) or (commits[0].sha if commits else None)
        return {"to": options, "default_to": base}

    @staticmethod
    def _options(heads, branches, commits):
        return (
            [{"value": h, "label": h, "kind": "head"} for h in heads]
            + [{"value": b, "label": b, "kind": "branch"} for b in branches]
            + [{"value": c.sha, "label": f"{c.short} {c.subject[:80]}", "kind": "commit",
                "when": c.when} for c in commits]
        )

    # --- reviews ------------------------------------------------------------

    def _price(self, tokens_in, tokens_out, agents):
        return {"agents": agents, "input_tokens": tokens_in, "output_tokens": tokens_out,
                "max_cost_usd": round((tokens_in * self.settings.input_price
                                       + tokens_out * self.settings.output_price) / 1e6, 6)}

    def review(self, review_id):
        r = self.store.review(review_id)
        if r is None:
            raise ApiError(404, "No such review.")
        if r["status"] == "awaiting_approval":
            record = self.store.plan(review_id)
            if record is None:  # planned by an older version or the CLI; it can't be approved here
                self.store.cancel_review(review_id, status="expired")
                return self.store.review(review_id)
            selection = default_selection(record)
            r["proposed"] = sorted(selection)
            r["estimate"] = self._price(*estimate_record(record, selection))
        return r

    def review_files(self, review_id, path=None):
        """Changed files of a review, or one file's diff, read from the repository now.

        Source code is never stored; it's read from git for display only."""
        r = self.store.review(review_id)
        if r is None:
            raise ApiError(404, "No such review.")
        try:
            if github.is_github(r["project_path"]):
                owner, name = github.from_path(r["project_path"])
                source = github.Repo(owner, name, self.github_client)
                if path is None:
                    return {"files": source.changed_files(r["base_sha"], r["head_sha"])}
                return source.file_diff(r["base_sha"], r["head_sha"], path)
            repo = self._repo(r["project_path"])
            if path is None:
                return {"files": git.changed_files(repo, r["base_sha"], r["head_sha"])}
            return git.file_diff(repo, r["base_sha"], r["head_sha"], path)
        except ApiError:
            raise ApiError(410, "The repository isn't available on this machine any more.") from None
        except git.GitError as error:
            raise ApiError(400, str(error)) from None

    def _awaiting(self, review_id):
        r = self.store.review(review_id)
        if r is None:
            raise ApiError(404, "No such review.")
        record = self.store.plan(review_id) if r["status"] == "awaiting_approval" else None
        if record is None:
            raise ApiError(409, "This plan is no longer waiting for approval.")
        return r, record

    def estimate(self, review_id, files):
        _, record = self._awaiting(review_id)
        return self._price(*estimate_record(record, set(files) & set(files_of(record))))

    def submit(self, body):
        """Create the review and plan it in the background; returns its id at once."""
        root = self._repo(body.get("repo", ""))
        source = self._source(body.get("repo", ""))
        start, end = body.get("from", ""), body.get("to", "")
        if end not in {o["value"] for o in self.older(str(root), start)["to"]}:
            raise ApiError(400, "Pick To from the older commits.")
        try:
            self.model_factory()
        except RuntimeError as error:
            raise ApiError(400, str(error)) from None
        try:
            review_id = start_review(self.store, root, f"{end}..{start}", model_name=self.settings.model,
                                     source=source)
        except (ReviewError, git.GitError) as error:
            raise ApiError(400, str(error)) from None
        self.jobs.submit("plan", review_id=review_id, repo=str(root))
        return {"id": review_id}

    def decide(self, review_id, body, *, approved):
        _, record = self._awaiting(review_id)
        if not approved:
            self.store.cancel_review(review_id)
            return {"id": review_id, "status": "cancelled"}
        selection = sorted(set(body.get("files") or []) & set(files_of(record)))
        self.store.mark_running(review_id)
        self.jobs.submit("approve", review_id=review_id, selection=selection)
        return {"id": review_id, "status": "running"}

    def project(self, project_id):
        project = self.store.find_project(project_id)
        if project is None:
            raise ApiError(404, "No such project.")
        return {**project, "reviews": self.store.reviews(project["id"]),
                "agents": self.store.agents(project["id"])}


    def _project_or_404(self, project_id):
        project = self.store.find_project(project_id)
        if project is None:
            raise ApiError(404, "No such project.")
        return project

    def machine(self):
        """Every readable repository with git metadata, for the Home chat."""
        if self.cloud:
            return [{"name": r["name"], "path": r["path"], "git": {"github": True}} for r in self._github_repos()]
        spaces = self.workspaces()
        repos = spaces["recent"] + spaces["available"]
        for repo in repos:
            try:
                repo["git"] = git.summary(repo["path"])
            except Exception:
                repo["git"] = {"unavailable": True}
        return repos

    def chat(self, project_id, body):
        project = self._project_or_404(project_id) if project_id is not None else None
        try:
            scan = scanner(self.settings.gitleaks)
            repositories = None if project else self.machine()
            return {"messages": chat.ask(self.store, self.model_factory(), project,
                                         body.get("message"), scan=scan,
                                         repositories=repositories)}
        except (chat.ChatError, ScanUnavailable) as error:
            raise ApiError(400, str(error)) from None
        except RuntimeError:
            raise ApiError(503, "The model is not configured (OpenAI key missing).") from None


    # --- knowledge bases ---------------------------------------------------

    def _knowledge(self):
        """Knowledge store embedding through the configured model client."""
        def embed(texts, model, dimensions):
            try:
                client = self.model_factory()
            except RuntimeError:
                raise RuntimeError("OpenAI key missing") from None
            return client.embed(texts, model, dimensions)
        def scan(text):  # gitleaks is only needed when a document is embedded
            return scanner(self.settings.gitleaks)(text)
        return Knowledge(self.knowledge_store(), embed=embed, scan=scan, create=False)

    def _summarizer(self):
        """complete(payload, schema) for chunk summaries, using the chat agent's model."""
        client = self.model_factory()  # RuntimeError (no key) becomes a 503 below
        override = agents.load_config()["agents"].get("chat", {}).get("model")
        def complete(payload, schema):
            reply = client.complete(role="chunk_summary", system=agents.read_prompt("chunk_summary"),
                                    prompt=payload, schema=schema, max_output_tokens=3000, model=override)
            used = (reply.input_tokens, reply.output_tokens)
            if not reply.ok:
                return None, used
            try:
                return json.loads(reply.text), used
            except ValueError:
                return None, used
        return complete

    # --- GitHub repositories and history batches ------------------------------

    def _remotes(self):
        return Remotes(self.remote_store(), client=self.github_client)

    def remotes(self, method, parts, body):
        remotes = self._remotes()
        try:
            if method == "GET" and not parts:
                return remotes.list()
            if method == "GET" and parts == ["status"]:
                return {"gh": bool(self.github_client.gh), "authenticated": self.github_client.authenticated()}
            if method == "POST" and not parts:
                return remotes.get(remotes.add(body.get("repo")))
            if parts and parts[0].isdigit() and method == "POST" and parts[1:] == ["delete"]:
                remotes.remove(int(parts[0]))
                return {"ok": True}
        except RemoteError as error:
            raise ApiError(400, str(error)) from None
        except RuntimeError:  # e.g. a secret that can't be read; never echo its details
            raise ApiError(503, "GitHub access isn't set up correctly on the server.") from None
        raise ApiError(404, "Not found.")

    def _review_commit(self, repo):
        """review(sha) -> (review id, cost) for a batch: plan, then approve as proposed."""
        source = self._source(repo)

        def review(sha):
            review_id = start_review(self.store, repo, sha, model_name=self.settings.model, source=source)
            plan_review(review_id, repo, **self._review_kwargs(source))
            if self.store.plan(review_id) is not None:
                approve_review(review_id, repo, **self._review_kwargs(source))
            return review_id, (self.store.review(review_id) or {}).get("cost_usd", 0.0)
        return review

    def _review_kwargs(self, source):
        return {"store": self.store, "model": self.model_factory(), "settings": self.settings,
                "source": source, "knowledge": None if not self.cloud else self.knowledge_store()}

    def batches(self, method, parts, body, query=None):
        batches = Batches(self.batch_store())
        try:
            if method == "GET" and not parts:
                if query and query.get("repo"):
                    return batches.for_repo(self._repo(query["repo"]))
                return {"modes": MODES}
            if method == "POST" and not parts:
                repo = self._repo(body.get("repo"))
                limit, every = body.get("limit", 20), body.get("every", 1)
                return batches.plan(repo, str(body.get("from") or ""), str(body.get("to") or ""),
                                    mode=body.get("mode"), settings=self.settings, every=every, limit=limit,
                                    source=self._source(body.get("repo")))
            if parts and parts[0].isdigit():
                batch_id, action = int(parts[0]), parts[1:]
                if method == "GET" and not action:
                    return batches.get(batch_id)
                if method == "POST" and action == ["start"]:
                    batch = batches.get(batch_id)
                    try:
                        self.model_factory()
                    except RuntimeError:
                        raise ApiError(503, "Reviews need an OpenAI key (OPENAI_API_KEY).") from None
                    cap = body.get("max_cost")
                    batches.start(batch_id, max_cost=cap if isinstance(cap, (int, float)) else None,
                                  runner=lambda: self.jobs.submit("batch_step", batch_id=batch_id))
                    return batches.get(batch_id)
                if method == "POST" and action == ["cancel"]:
                    return batches.cancel(batch_id)
        except (BatchError, git.GitError) as error:
            raise ApiError(400, str(error)) from None
        raise ApiError(404, "Not found.")

    def knowledge(self, method, parts, body, *, user=None):
        kb = self._knowledge()
        try:
            if method == "GET" and not parts:
                return kb.bases()
            if method == "GET" and parts == ["options"]:
                from autosde.chunking import STRATEGIES
                from autosde.knowledge import DEFAULTS, EMBED_MODELS
                return {"strategies": STRATEGIES, "models": EMBED_MODELS, "defaults": DEFAULTS}
            if method == "POST" and not parts:
                return kb.create(body.get("name"), body.get("description", ""), body.get("settings"))
            if method == "POST" and parts == ["search"]:
                ids = body.get("kb_ids") or []
                if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                    raise KnowledgeError("kb_ids must be a list of ids.")
                top_k = body.get("top_k")
                return {"results": kb.search(ids, str(body.get("query") or "")[:4000],
                                             top_k=top_k if isinstance(top_k, int) and 0 < top_k <= 20 else None)}
            if parts[:1] == ["documents"] and len(parts) >= 2 and parts[1].isdigit():
                doc_id = int(parts[1])
                if method == "GET" and parts[2:] == ["chunks"]:
                    return {"chunks": kb.chunks(doc_id)}
                if method == "POST" and parts[2:] == ["delete"]:
                    kb.delete_document(doc_id)
                    return {"ok": True}
                if method == "POST" and parts[2:] == ["rename"]:
                    return kb.rename(doc_id, body.get("name"))
                if method == "POST" and parts[2:] == ["summarize"]:
                    positions = body.get("positions")
                    if positions is not None and (not isinstance(positions, list) or len(positions) > 40
                                                  or not all(isinstance(p, int) for p in positions)):
                        raise KnowledgeError("positions must be a list of up to 40 chunk positions.")
                    return kb.summarize(doc_id, self._summarizer(), positions=positions)
                if method == "POST" and parts[2:] == ["retry"]:
                    kb.retry(doc_id)
                    self.jobs.submit("reindex", doc_ids=[doc_id])
                    return {"id": doc_id, "status": "processing"}
            if parts and parts[0].isdigit():
                kb_id, rest = int(parts[0]), parts[1:]
                if method == "GET" and not rest:
                    return kb.base(kb_id)
                if method == "POST" and not rest:
                    return kb.update(kb_id, description=body.get("description"),
                                     settings=body.get("settings"))
                if method == "POST" and rest == ["delete"]:
                    kb.delete(kb_id)
                    return {"ok": True}
                if method == "POST" and rest == ["preview"]:
                    doc_id = body.get("doc_id")
                    return kb.preview(kb_id, doc_id=doc_id if isinstance(doc_id, int) else None,
                                      text=body.get("text"), settings=body.get("settings"))
                if method == "POST" and rest == ["rechunk"]:
                    docs, skipped = kb.rechunk(kb_id)
                    if docs:
                        self.jobs.submit("reindex", doc_ids=[d["id"] for d in docs])
                    return {"rechunking": len(docs), "skipped": skipped}
                if method == "POST" and (rest == ["uploads"] or
                        len(rest) == 3 and rest[0] == "uploads" and rest[2] == "complete"):
                    if not self.cloud or self.uploads is None:
                        raise ApiError(404, "Not found.")
                    owner = (user or {}).get("sub")
                    if not isinstance(owner, str) or not owner:
                        raise ApiError(401, "Sign in first.")
                    if rest == ["uploads"]:
                        return self.uploads.initiate(kb, kb_id, owner, body.get("name"), body.get("size"), body.get("sha256"))
                    return self.uploads.complete(kb, kb_id, owner, rest[1], self.jobs)
                if method == "POST" and rest == ["documents"]:
                    try:
                        data = base64.b64decode(body.get("data") or "", validate=True)
                    except (binascii.Error, ValueError):
                        raise KnowledgeError("Upload data must be base64.") from None
                    doc_id = kb.add_document(kb_id, body.get("name"), data)
                    if self.uploads is not None:  # AWS: the bytes reach the worker through S3
                        self.jobs.submit("ingest", doc_id=doc_id, upload=self.uploads.put(doc_id, data))
                    else:
                        self.jobs.submit("ingest", doc_id=doc_id, data=base64.b64encode(data).decode())
                    return {"id": doc_id, "status": "processing"}
        except KnowledgeError as error:
            raise ApiError(400, str(error)) from None
        except RuntimeError:
            raise ApiError(503, "Embedding needs an OpenAI key (OPENAI_API_KEY).") from None
        raise ApiError(404, "Not found.")

    # --- background jobs -------------------------------------------------------

    def run_job(self, name, args):
        """Do one background job. Runs on a thread locally and in the worker Lambda in AWS;
        failures are recorded on the review, batch or document, never raised to the caller."""
        if name in ("plan", "approve"):
            review = self.store.review(args["review_id"])
            repo = review["project_path"]
            source = self._source(repo) if github.is_github(repo) else None
            if name == "plan":
                plan_review(args["review_id"], repo, **self._review_kwargs(source))
            else:
                approve_review(args["review_id"], repo, selection=args.get("selection"),
                               **self._review_kwargs(source))
        elif name == "batch_step":
            batches = Batches(self.batch_store())
            batch = batches.get(args["batch_id"])
            if batches.step(args["batch_id"], self._review_commit(batch["repo"])):
                self.jobs.submit("batch_step", batch_id=args["batch_id"])  # one commit per run
        elif name == "ingest":
            kb = self._knowledge()
            if "upload_ticket" in args:
                self.uploads.ingest(kb, args["upload_ticket"])
            elif "upload" in args:
                data = self.uploads.get(args["upload"])
                try:
                    kb.ingest(args["doc_id"], data)
                finally:
                    self.uploads.delete(args["upload"])
            else:
                kb.ingest(args["doc_id"], base64.b64decode(args["data"]))
        elif name == "reindex":
            kb = self._knowledge()
            kb.reindex([kb.store.get_document(doc_id) for doc_id in args["doc_ids"]])
        else:
            raise ValueError("Unknown job.")
