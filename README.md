# AutoSDE

A local code reviewer for your git repositories. A small team of AI agents reviews a commit range, you approve the plan before any tokens are spent, and every review is kept in a local SQLite database you can browse, query and chat with.

- **Agent team:** a coordinator assigns changed files to language specialists (Python, JavaScript, TypeScript, Java, Go, C#, and a general catch-all). They review in parallel, and an aggregator keeps or rejects their findings. Fixes come back as diffs with line numbers.
- **Approval first:** files that rarely hold defects (lockfiles, build output, IDE settings, minified bundles) are flagged before anything is sent. You see the plan and an estimated cost, then approve.
- **Local and private:** everything runs on your machine. The only network calls go to OpenAI (and LangSmith, if you turn tracing on), and every payload passes a [gitleaks](https://github.com/gitleaks/gitleaks) secret scan first. The database never stores source code.
- **Small:** Python 3.12+ standard library only. The React web UI is prebuilt, so no Node is needed to run it.

## Quick start

```sh
git clone <this repository> autosde && cd autosde
brew install gitleaks              # or set AUTOSDE_GITLEAKS=/path/to/gitleaks
export OPENAI_API_KEY=...          # read from the environment, never stored
pip install -e .                   # optional: adds the `autosde` command (or use `python3 -m autosde`)
autosde serve                      # open http://127.0.0.1:8765/
```

## The web app

- **Sidebar:** your repositories, found under your home folder. **Recent** lists reviewed projects, **Available** lists the rest, and one search box filters both. Folders macOS hasn't approved for your terminal are hidden; turn on **Settings → Show blocked folders** to see them. ☰ collapses the sidebar.
- **Home:** a chat about all your projects (branches, recent commits, file types and review history), next to your projects and recent reviews.
- **Project page:** four panels. On the left, pick a commit range (**From** is the newer end, **To** lists only older commits) and chat about the project's review history. On the right, its reviews and, if enabled, the agents that ran. Drag the dividers to resize, and use ⤢ to make any panel full screen.
- **Review page:** the plan with a checkbox per file and an estimated cost; after approval, findings with their diffs, and the findings the aggregator rejected and why.
- **Agents:** enable specialists, set file extensions, pick each agent's model from those your key can use, edit every prompt, and see run statistics.
- **Settings:** theme (Light, Dark, Dracula), blocked folders, and whether agent runs appear on project and review pages.

Only committed history is reviewed; uncommitted edits are ignored.

## The command line

```sh
autosde review ~/code/calculator main..HEAD     # A..B, A.. (to HEAD) or one commit
autosde projects                                # reviewed projects
autosde reviews calculator                      # reviews for one project (id, name or path)
autosde show 3                                  # one review: agents, findings, limitations
autosde show 3 --format sarif --output autosde.sarif   # for VS Code's SARIF Viewer or GitHub
autosde agents calculator                       # which agents ran against a project
```

`review` prints the plan with `[x]`/`[ ]` marks, flag reasons and an estimated cost, then asks `[y]es / [a]ll, including flagged / [N]o`. Use `--yes` to approve as proposed, or `--include-flagged` / `--include PATH` to add flagged files.

## Configuration

| Variable | Default |
| --- | --- |
| `OPENAI_API_KEY` | required for reviews and chat |
| `AUTOSDE_MODEL` | `gpt-6-luna` (each agent can override it on the Agents page) |
| `AUTOSDE_INPUT_PRICE`, `AUTOSDE_OUTPUT_PRICE` | `0.10`, `0.50` USD per 1M tokens, for cost estimates |
| `AUTOSDE_REASONING_EFFORT` | `low` (`none` to omit; only sent to reasoning models) |
| `AUTOSDE_HOME` | `~/.autosde` (holds `autosde.db`, mode 0600) |
| `AUTOSDE_DB` | `$AUTOSDE_HOME/autosde.db` |
| `AUTOSDE_GITLEAKS` | `gitleaks` on `PATH` |
| `AUTOSDE_SECRET_ID`, `AUTOSDE_SECRET_PROFILE`, `AUTOSDE_SECRET_REGION` | unset; optionally read the OpenAI key from AWS Secrets Manager (`pip install -e '.[aws]'`) |
| `LANGSMITH_API_KEY`, `LANGSMITH_WORKSPACE_ID` | unset; see LangSmith tracing |

Agent settings (enabled specialists, extensions, models, tracing) are saved in `autosde/prompts/agents.json`, next to the prompts.

## LangSmith tracing

Turn it on from the Agents page and export `LANGSMITH_API_KEY` before starting AutoSDE. Each review becomes one trace with a child run per agent call, containing the full prompt, the reviewed code and the reply. Untick "Trace this agent" to leave an agent out. Text gitleaks flags is withheld.

## Data

The database stores projects, reviews (range, status, tokens, cost), one row per agent call (agent, model, files, status, time, tokens), findings (file, lines, severity, title, description, fix, kept or rejected) and chat history. Query it with the CLI, the web app, the notebook, or `sqlite3 ~/.autosde/autosde.db`.

## Developing

Prompts live in `autosde/prompts/` and are re-read on every call. `notebooks/team.ipynb` runs a free scripted team, then an opt-in paid review.

```sh
python3 -m unittest discover -s tests -t tests   # no network or API key needed
```

The frontend is in `frontend/` (Vite + React). The built UI is committed in `autosde/static/`. After UI changes:

```sh
cd frontend && npm install && npm run build   # writes autosde/static/
npm run dev                                   # live reload; proxies /api to `autosde serve` on :8765
```
