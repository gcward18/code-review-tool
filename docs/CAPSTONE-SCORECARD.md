# Review Crew capstone scorecard

Scored on 2026-11-03 against the capstone rubric adapted for AutoSDE (`docs/CAPSTONE-RUBRIC.md` in the Code Review Service repository). Earlier scores: 58.75 at `2606568` ("Run the coordinator, specialists and aggregator as LangGraph graphs") and 68.75 after the code-checked aggregator and its follow-up loop. This one adds per-specialist retry limits, the aggregator's restart, retry and refuse-edit actions, and the single-invocation architecture decision. Each criterion is read against this app's equivalent component and takes the highest level whose description fully holds.

| # | Criterion | Weight | Score | Weighted |
| --- | --- | --- | --- | --- |
| 1 | Multi-Agent Review Orchestration | 20% | 75 | 15.00 |
| 2 | Aggregation Gate & Follow-up Loop | 20% | 100 | 20.00 |
| 3 | Agent Contracts & Distributed Architecture | 15% | 100 | 15.00 |
| 4 | Secure Code Capture & Context Tools | 15% | 75 | 11.25 |
| 5 | Specialist Knowledge & Report Integrations | 10% | 75 | 7.50 |
| 6 | Reliability, Budgets & Verification | 10% | 75 | 7.50 |
| 7 | Deployment, Code Quality & Documentation | 10% | 75 | 7.50 |
| | **Total** | **100%** | | **83.75** |

## Remarks

### 1. Multi-Agent Review Orchestration: 75

- **Meets Excellent:** `Team` in `autosde/team.py` runs as LangGraph graphs. A one-node planning graph runs the coordinator; after approval, the review graph fans out to the assigned specialists with `Send` (up to `workers` at once) and joins at one aggregator node, with findings numbered in plan order. Assignments are limited to enabled specialists and changed files; a failed coordinator, or files it leaves out, fall back to extension routing. Each specialist gets numbered head files, the diff, its focus prompt and knowledge-base passages; the aggregator gets every finding. A human approves the files and the estimated cost before source reaches the specialists.
- **Holds it at Good:** each file gets one specialist (`path not in assigned`), so a TypeScript Angular file can't get two reviewers. No token or cost budget is enforced while a review runs: the estimate is approved up front, and only batches have a spending cap. There is no review deadline beyond the 14-minute worker timeout. Aggregation rounds are capped at three, and each specialist call at its retry limit.

### 2. Aggregation Gate & Follow-up Loop: 100

- The aggregator returns a strict structured verdict (`VERDICT_SCHEMA`): `COMPLETE` or `CONTINUE`, rejections with reasons, follow-ups and a summary. `parse_verdict` validates it in code: rejections must name current findings, follow-ups must name a specialist on the team and files it reviewed, and `CONTINUE` without a usable follow-up is invalid.
- Each finding reaches the aggregator with the numbered head code around it, so it judges against the code rather than the specialist's account. Its prompt adds that the code must show the claimed problem.
- On `CONTINUE`, the LangGraph review graph loops back (`aggregator` → `specialist` → `collect` → `aggregator`) to only the named specialists, on their own approved files, with an action: answer a `question` (with their current findings), `restart` from scratch, or `retry` a specialist whose every attempt failed (the aggregator also runs when specialists failed without findings, so it can choose this). A follow-up answer replaces that specialist's earlier findings on those files; the aggregator then sees both versions and can `refuse` the edit, restoring the originals and rejecting the new findings. Reviews stop after `MAX_ROUNDS` (3) aggregation rounds, and cost estimates cover every round.
- A missing or invalid verdict withholds every unvalidated finding (listed as rejected, "aggregator verdict unavailable") and records a limitation. The aggregator has no way to mark findings verified.
- **Caveat:** the loop is covered by scripted tests only; no real-model run has returned `CONTINUE`. A custom aggregator prompt saved on the Agents page in AWS overrides the bundled one.

### 3. Agent Contracts & Distributed Architecture: 100

Scored as a design choice on its own terms, as the rubric does for one-attempt calls and the Git-backed context protocol: [decision 1](decisions/0001-agents-in-one-lambda.md) keeps every agent in the worker invocation, with its reasons, consequences and when to revisit it. Under the rubric's literal wording (each specialist its own deployed unit) this criterion would stay at 25 and the total would be 73.75.

- **Meets Excellent on its own terms:** agents communicate only through the LangGraph graph's typed state and schema-checked replies, never directly. Every role's reply is held to a strict JSON schema; specialist findings and the aggregator verdict are validated in code before use. Each agent's model, prompt, extensions, knowledge bases, tracing and retry limit are configurable and versioned (DynamoDB in AWS) and change without a deploy. API and worker are separate Lambdas behind `live` aliases.
- Every role's reply now goes through a validated contract: `parse_plan` checks the coordinator's plan (a plan that breaks it is treated as unavailable, and files are routed by type), `_findings` checks specialist findings, and `parse_verdict` checks the aggregator's verdict.
- **Caveat:** the decision record is new, so its revisit triggers have not been exercised.

### 4. Secure Code Capture & Context Tools: 75

- **Meets Excellent on capture:** `git.capture` reads committed objects (`git show head:path`) with external diff and textconv disabled, and GitHub capture uses exact commit IDs. File and total size limits apply. Every file is Gitleaks-scanned before planning and again after approval, and chat prompts and knowledge documents are scanned too. Rule-based flags keep low-value files away from the model. The store keeps findings, not source.
- **Context tool:** a specialist can list `requested_context` (an unchanged file's path and up to 200 lines, with a reason). `context_reader` reads it at the review's head commit, locally or from GitHub, never from the working tree, and Gitleaks-scans it. It refuses changed files (so a file deselected at approval stays unsent), rule-flagged, binary, oversized and secret-bearing files, and non-repository paths. One context round per specialist task and 10 reads per review; each read is listed in the review's limits, and the cost estimate covers the extra call.
- **Holds it at Good:** the aggregator cannot request code beyond the numbered lines around each finding, so it cannot read what a specialist read. Enabled LangSmith traces contain scanned source by design.

### 5. Specialist Knowledge & Report Integrations: 75

- **Meets Excellent:** per-language focus prompts live in `autosde/prompts/focus/` and are re-read on every call (versioned in DynamoDB in AWS). Prompts treat code, history and uploaded documents as untrusted. Knowledge bases use hybrid vector and BM25 retrieval fused by RRF, attachable per agent. Reviews export as text, JSON and SARIF, and can be browsed through the web UI, the CLI and a chat agent. Setup errors now return readable messages instead of crashing.
- **Holds it at Good:** no recorded real-model run shows that findings follow each language's rules. The deployment status notes that no authenticated inference has run on AWS yet.

### 6. Reliability, Budgets & Verification: 75

- **Meets Good:** each specialist call is retried up to that specialist's `retries` limit (0–3, default 1), on a transport failure or a reply that breaks its schema. Every attempt is recorded as its own agent run with its tokens, and the approved "at most" estimate covers every attempt in every round, so retries are bounded and paid for up front rather than blind. The aggregator can retry a specialist that still failed, restart one whose work is unusable, and refuse a follow-up's edits. Failed specialists become limitations, a failed coordinator falls back to routing, retrieval failures never block a review, an aggregator failure withholds unvalidated findings, and failed reviews record only the error class. Batches stop at their cost cap.
- All 143 backend tests pass with `moto` and `AUTOSDE_GITLEAKS` set (without Gitleaks, five Lambda end-to-end tests skip), with 28 frontend and 13 infrastructure tests.
- **Holds it at Good:** there is no per-call budget reservation and reconciliation, and no partial report when a deadline or budget runs out. Retries and follow-ups are covered by scripted tests only; there is no recorded paid team run.

### 7. Deployment, Code Quality & Documentation: 75

- **Meets Excellent:** a deployed CDK stack (CloudFront, private S3, Cognito with PKCE, an API Gateway JWT scope, API and worker Lambdas with aliases, DynamoDB, SSM for model and prices, Secrets Manager for keys) built from a hash-pinned, allowlisted Lambda asset. Live smoke checks are recorded. The README has architecture, deployment, data-flow and upload diagrams, and the Help page ships Playwright-recorded demo videos.
- **Holds it at Good:** teardown (`cdk destroy`, and what survives it) is not documented.

## Raising the score

| Change | Criteria | Gain |
| --- | --- | --- |
| Let the aggregator request the same unchanged files under the review's quota | 4 → 100 | +3.75 |
| Enforce a per-review token and cost budget during the run, with a partial report when it runs out | 1, 6 | +5.00 to +7.50 |
| Record one paid review per language and add a teardown section | 5, 6, 7 | +2.50 to +7.50 |
