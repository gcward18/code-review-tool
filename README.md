# AutoSDE local

Review commit ranges in your local git repositories with a small agent team, and keep every result in a local SQLite database.

- **Team:** a coordinator assigns changed files to language specialists (python, javascript, typescript, java, go, csharp, general). The specialists review in parallel, and an aggregator keeps or rejects their findings.
- **Local:** everything runs on your machine. The only network call is to OpenAI, after each file passes a gitleaks secret scan.
- **Small:** Python 3.12+ standard library only. The web page and database are built in.

## Setup

```sh
export OPENAI_API_KEY=...        # never stored by autosde
brew install gitleaks            # or set AUTOSDE_GITLEAKS=/path/to/gitleaks
pip install -e .                 # optional: adds the `autosde` command; otherwise use `python3 -m autosde`
```

## Use

```sh
autosde review ~/code/calculator main..HEAD     # A..B, A.. (to HEAD) or one commit
autosde projects                                # reviewed projects
autosde reviews calculator                      # reviews for one project (id, name or path)
autosde show 3                                  # one review: agents, findings, limits
autosde show 3 --format sarif --output autosde.sarif   # for VS Code's SARIF Viewer or GitHub
autosde agents calculator                       # which agents ran against the project, and results
autosde serve                                   # http://127.0.0.1:8765/ to submit and browse
```

On the web page, browse your home folder, **Select** a git repository, then pick **From** and **To** from dropdowns of branches and recent commits. The defaults are `main` (or `master`) → `HEAD`; when `main` *is* `HEAD`, From defaults to the previous commit. The review covers every commit after From up to and including To.

Only committed history is reviewed; uncommitted edits are ignored.

### Approval before tokens are spent

Every review is planned first. Built-in rules flag files that cost tokens but rarely contain defects (IDE settings such as `.idea/`, lockfiles, build and vendored output, minified bundles, source maps, data files, OS metadata). The coordinator then sees only file paths and sizes, assigns files to specialists, and may flag more with a reason. The review waits for your approval:

- **CLI:** the plan prints with `[x]` and `[ ]` marks, flag reasons and an estimated cost, then asks `[y]es / [a]ll, including flagged / [N]o`. Use `--yes` to approve as proposed, and `--include-flagged` or `--include PATH` to add flagged files. A non-interactive shell without `--yes` cancels.
- **Web page:** the review page shows a checkbox per file (flagged files unchecked) with Approve and Cancel.

Only approved files reach the specialists. The database records each file's plan, flag and approval. A plan waiting for approval keeps file contents in memory only; if the web server restarts, the plan expires and you resubmit.

## Configuration

| Variable | Default |
| --- | --- |
| `AUTOSDE_MODEL` | `gpt-6-luna` |
| `AUTOSDE_INPUT_PRICE`, `AUTOSDE_OUTPUT_PRICE` | `0.10`, `0.50` USD per 1M tokens (for cost estimates) |
| `AUTOSDE_REASONING_EFFORT` | `low` (`none` to omit) |
| `AUTOSDE_HOME` | `~/.autosde` (database `autosde.db`, mode 0600) |
| `AUTOSDE_DB` | `$AUTOSDE_HOME/autosde.db` |
| `AUTOSDE_GITLEAKS` | `gitleaks` on `PATH` |

## Data

The database stores projects, reviews (range, commits, status, tokens, cost), one row per agent call (role, agent, files, status, time, tokens) and findings (file, lines, severity, title, description, fix, kept or rejected). It never stores source code. Query it with the CLI, the web page, the notebook, or `sqlite3 ~/.autosde/autosde.db`.

## Developing the team

Prompts live in `autosde/prompts/` and are re-read on every call. `notebooks/team.ipynb` runs a free scripted team, then an opt-in paid review, and queries the database.

```sh
python3 -m unittest discover -s tests -t tests
```

The earlier AWS version is preserved in the `Code Review Service` repository at the `aws-deployment-v1` tag.
