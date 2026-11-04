# Review Crew capstone scorecard

Scored on 2026-11-03 against the capstone rubric adapted for AutoSDE (`docs/CAPSTONE-RUBRIC.md` in the Code Review Service repository). The first score, 58.75, was taken at `2606568` ("Run the coordinator, specialists and aggregator as LangGraph graphs"); this one adds the code-checked aggregator and its follow-up loop. Each criterion is read against this app's equivalent component and takes the highest level whose description fully holds.

| # | Criterion | Weight | Score | Weighted |
| --- | --- | --- | --- | --- |
| 1 | Multi-Agent Review Orchestration | 20% | 75 | 15.00 |
| 2 | Aggregation Gate & Follow-up Loop | 20% | 100 | 20.00 |
| 3 | Agent Contracts & Distributed Architecture | 15% | 25 | 3.75 |
| 4 | Secure Code Capture & Context Tools | 15% | 50 | 7.50 |
| 5 | Specialist Knowledge & Report Integrations | 10% | 75 | 7.50 |
| 6 | Reliability, Budgets & Verification | 10% | 75 | 7.50 |
| 7 | Deployment, Code Quality & Documentation | 10% | 75 | 7.50 |
| | **Total** | **100%** | | **68.75** |

## Remarks

### 1. Multi-Agent Review Orchestration: 75

- **Meets Excellent:** `Team` in `autosde/team.py` runs as LangGraph graphs. A one-node planning graph runs the coordinator; after approval, the review graph fans out to the assigned specialists with `Send` (up to `workers` at once) and joins at one aggregator node, with findings numbered in plan order. Assignments are limited to enabled specialists and changed files; a failed coordinator, or files it leaves out, fall back to extension routing. Each specialist gets numbered head files, the diff, its focus prompt and knowledge-base passages; the aggregator gets every finding. A human approves the files and the estimated cost before source reaches the specialists.
- **Holds it at Good:** each file gets one specialist (`path not in assigned`), so a TypeScript Angular file can't get two reviewers. No token or cost budget is enforced while a review runs: the estimate is approved up front, and only batches have a spending cap. There is no review deadline beyond the 14-minute worker timeout. Aggregation rounds are capped at two.

### 2. Aggregation Gate & Follow-up Loop: 100

- The aggregator returns a strict structured verdict (`VERDICT_SCHEMA`): `COMPLETE` or `CONTINUE`, rejections with reasons, follow-ups and a summary. `parse_verdict` validates it in code: rejections must name current findings, follow-ups must name a specialist on the team and files it reviewed, and `CONTINUE` without a usable follow-up is invalid.
- Each finding reaches the aggregator with the numbered head code around it, so it judges against the code rather than the specialist's account. Its prompt adds that the code must show the claimed problem.
- On `CONTINUE`, the LangGraph review graph loops back (`aggregator` → `specialist` → `collect` → `aggregator`) to only the named specialists, on their own approved files, with the question and their current findings. Their answer replaces their earlier findings on those files, and the aggregator judges again. Reviews stop after `MAX_ROUNDS` (2) aggregation rounds, and cost estimates cover the second round.
- A missing or invalid verdict withholds every unvalidated finding (listed as rejected, "aggregator verdict unavailable") and records a limitation. The aggregator has no way to mark findings verified.
- **Caveat:** the loop is covered by scripted tests only; no real-model run has returned `CONTINUE`. A custom aggregator prompt saved on the Agents page in AWS overrides the bundled one.

### 3. Agent Contracts & Distributed Architecture: 25

- **In place:** API and worker are separate Lambdas behind `live` aliases. Each agent's model, prompt, file extensions and knowledge bases are configurable per agent and versioned in DynamoDB, and every role's reply is held to a strict JSON schema.
- **Holds it at Needs Improvement:** every agent runs inside the worker invocation, and no specialist can be deployed, versioned or invoked on its own. That is the rubric's single-monolith level, even though the contracts are real. If you keep agents in-process deliberately (see the trade-offs in the Code Review Service README), record it as a design choice and score this criterion on its own terms.

### 4. Secure Code Capture & Context Tools: 50

- **Meets Excellent on capture:** `git.capture` reads committed objects (`git show head:path`) with external diff and textconv disabled, and GitHub capture uses exact commit IDs. File and total size limits apply. Every file is Gitleaks-scanned before planning and again after approval, and chat prompts and knowledge documents are scanned too. Rule-based flags keep low-value files away from the model. The store keeps findings, not source.
- **Holds it at Satisfactory:** specialists cannot ask for more code. They see only their assigned files and diffs, so a change whose meaning depends on an unchanged caller, helper or test is judged without it. Knowledge-base retrieval adds reference passages, not repository code. Enabled LangSmith traces contain scanned source by design.

### 5. Specialist Knowledge & Report Integrations: 75

- **Meets Excellent:** per-language focus prompts live in `autosde/prompts/focus/` and are re-read on every call (versioned in DynamoDB in AWS). Prompts treat code, history and uploaded documents as untrusted. Knowledge bases use hybrid vector and BM25 retrieval fused by RRF, attachable per agent. Reviews export as text, JSON and SARIF, and can be browsed through the web UI, the CLI and a chat agent. Setup errors now return readable messages instead of crashing.
- **Holds it at Good:** no recorded real-model run shows that findings follow each language's rules. The deployment status notes that no authenticated inference has run on AWS yet.

### 6. Reliability, Budgets & Verification: 75

- **Meets Good:** model calls make one HTTPS attempt with no retries. Failed specialists become limitations, a failed coordinator falls back to routing, retrieval failures never block a review, and failed reviews record only the error class. Approval reloads the exact saved commits and scans again. Batches stop at their cost cap.
- **Holds it at Good:** there is no per-call budget reservation and reconciliation, and no partial report when a deadline or budget runs out. An aggregator failure now withholds unvalidated findings. In this checkout 107 of 113 backend tests pass, and 6 modules covering DynamoDB, S3 and uploads cannot import `moto`, which is not in `requirements.txt`. The deployment status reports 130 backend, 28 frontend and 13 infrastructure tests passing in its own environment. There is no recorded paid team run.

### 7. Deployment, Code Quality & Documentation: 75

- **Meets Excellent:** a deployed CDK stack (CloudFront, private S3, Cognito with PKCE, an API Gateway JWT scope, API and worker Lambdas with aliases, DynamoDB, SSM for model and prices, Secrets Manager for keys) built from a hash-pinned, allowlisted Lambda asset. Live smoke checks are recorded. The README has architecture, deployment, data-flow and upload diagrams, and the Help page ships Playwright-recorded demo videos.
- **Holds it at Good:** teardown (`cdk destroy`, and what survives it) is not documented. The README still describes a "standard-library core", which is out of date now that LangGraph is a runtime dependency. `moto` is an undeclared test dependency.

## Raising the score

| Change | Criteria | Gain |
| --- | --- | --- |
| Let specialists request unchanged committed files, read and scanned at the saved commits, under a fixed quota | 4 → 100, 1 helps | +7.50 |
| Record the in-process design choice, or run each specialist as its own Lambda version | 3 → 50 to 100 | +3.75 to +11.25 |
| Enforce a per-review token and cost budget during the run, with a partial report when it runs out | 1, 6 | +5.00 to +7.50 |
| Record one paid review per language, add a teardown section, update the README's dependency claim, and declare `moto` | 5, 6, 7 | +2.50 to +7.50 |
