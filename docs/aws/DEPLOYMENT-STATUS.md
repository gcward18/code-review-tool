# Deployment status

The user authorized deploying the existing version in parallel with direct-to-S3 upload development, with initial model `gpt-4o`. The initial deployment used an isolated snapshot at `/tmp/autosde-deploy-current-h3kk29mh`. After validating the upload update, the user authorized continuing deployment; the 20 MB direct-upload update and its matching frontend are now published.

Initial estimate rates: $2.50 input / $10.00 output per million standard text tokens, verified against [official OpenAI documentation](https://developers.openai.com/api/docs/models/gpt-4o). These are configurable system settings in SSM, rather than hardcoded inference prices.

- Target: `AutoSDELocal-dev`, account `123456789012`, region `us-east-1`, profile `your-profile`.
- Existing OpenAI and LangSmith secret metadata and CDK bootstrap v32 were verified.
- A new Secrets Manager container `autosde/github-token` was created as requested. It has no token value yet; private GitHub access requires the user to populate it securely.
- Deployment is complete (`UPDATE_COMPLETE`) and the snapshot frontend is published at [Review Crew](https://d111111abcdef8.cloudfront.net/).
- API endpoint: `https://abc123defg.execute-api.us-east-1.amazonaws.com`. Cognito pool: `us-east-1_EXAMPLE`.
- The first attempt found an invalid CloudFront header policy. Recovery preserved/imported the only resource actually retained (the new Cognito pool), then deployed the corrected stack. No existing data/resources were deleted.
- Homepage, runtime config and JavaScript return 200; anonymous API requests return 401 through both CloudFront and API Gateway. The final snapshot CDK diff has zero differences.
- The user authorized the first Cognito account invitation, which was sent successfully. The account is enabled and requires an initial password change (`FORCE_CHANGE_PASSWORD`). No live inference or data migration has been performed.
- The [GitHub secret container](https://us-east-1.console.aws.amazon.com/secretsmanager/secret?name=autosde%2Fgithub-token&region=us-east-1) exists with no token value. Populate it securely to enable private-repository access.
- Public deployment outputs and smoke results are copied to ignored `.build/deployment-outputs.json` and `.build/deployment-smoke-checks.json`. The isolated deployment report is `/tmp/autosde-deploy-current-h3kk29mh/DEPLOYMENT-RESULT.md`.
- The deployed frontend now supports 20 MB direct-to-S3 uploads. The API and worker use durable, owner-bound upload tickets and pinned S3 object versions.

The direct-upload stack/code update completed successfully, and the matching frontend/config was published with CloudFront invalidation `I2J0I21PCUYOIK`. Runtime config now includes the private bucket’s `uploadOrigin`. Existing model/price/secret parameters were retained.

Validation: 130 backend tests, 28 frontend tests and 13 infrastructure tests pass; frontend/build, infrastructure type checking, lint, formatting, synthesis and AWS template validation pass. A read-only diff against the live stack is saved at `.build/direct-upload-diff.txt`. It adds upload-ticket TTL, exact-origin S3 CORS, worker versioned-object permissions and the upload CSP/config entry, and updates Lambda code/versions. It does not replace the database, buckets or user pool. Previous published Lambda versions are retained.

Post-update checks: the final CDK diff reports zero differences (`.build/direct-upload-diff-final.txt`). Nine live HTTP checks pass: homepage, exact runtime config, matching JS/CSS asset hashes, anonymous session/API upload refusal, allowed site-origin S3 POST preflight and rejected foreign-origin preflight. The upload origin is present in CSP. Results are in `.build/direct-upload-smoke-checks.json`; deployment and publication logs are `.build/direct-upload-deploy.log` and `.build/direct-upload-publish.log`. An authenticated upload/inference smoke test remains pending the first user's password setup and sign-in; no paid inference was run.

## 2026-11-02: GitHub-without-token fix

- Cause of "Something went wrong" on **+ Add GitHub repository**: the `autosde/github-token` secret has no value, and reading it raised an unhandled `RuntimeError` (API log: `{"event": "api_error", "path": "/api/remotes", "error": "RuntimeError"}`).
- Fix: an empty or unreadable token now means anonymous GitHub access (public repositories work), private repositories get a readable hint naming the secret, and other setup errors return a readable 503. The header no longer shows the Cognito user id.
- Deployed with the owner's approval (Lambda code only; `cdk diff` showed no IAM, data or Cognito changes; previous versions retained). API alias `live` → version 3. Site republished (`index-B8n8aRLg.js`).
- Live checks: anonymous API 401; direct invoke with test claims added `octocat/Hello-World` (200), refused a missing repository with the token hint (400), then removed the test repository.

## 2026-11-03: LangGraph team, aggregator follow-ups and specialist retries

- Deployed `fbcc490` at the owner's request, from a clean snapshot of that commit so uncommitted local edits were not packaged. It includes LangGraph orchestration (`2606568`), the code-checked aggregator and follow-up loop (`fb2e5d9`), and per-specialist retry limits with the aggregator's restart, retry and refuse-edit actions (`fbcc490`).
- Asset rebuilt with `scripts/build_lambda.py` from the hash-pinned lock (45 wheels, now including LangGraph, LangChain Core and Pydantic) and verified: Linux x86_64, no local config, 103 MB unzipped.
- Checks before deploying: 143 backend, 28 frontend and 13 infrastructure tests, infrastructure build, lint, format check and synth all pass. `cdk diff` (`.build/fbcc490-deployment-diff.txt`) changed only the API and worker code, added a version of each and moved both `live` aliases. No IAM, data, Cognito or CloudFront changes. Existing model, price and secret parameters were reused, and previous versions are retained.
- `UPDATE_COMPLETE`: API and worker `live` → version 5. Site republished (`index-BH9dzEZp.js`, with the Agents page's retries control); CloudFront invalidation `IWUUFQKJ4E3NHTD0IP5RWLOHI` completed. Logs: `.build/fbcc490-deploy.log`, `.build/fbcc490-publish.log`.
- Live checks: homepage, bundle and cloud-mode `config.json` return 200; anonymous `/api/session` and `/api/models` return 401; `review-crew.vercel.app` redirects (307) to the site. Direct read-only invokes of the API `live` alias (version 5) with a synthetic `deploy-smoke-check` subject returned 200 for `/api/session` and `/api/agents`, which confirms the LangGraph runtime loads, the default specialist retry limit is 1, and the bundled aggregator prompt with edit refusals is in effect.
- Not yet run: a signed-in review through the UI, and any paid inference with the new follow-up actions.

## 2026-11-03: coordinator plan validation

- Deployed `19155b2` at the owner's request, from a clean snapshot. Relative to the previous deployment, the only runtime change is `a24bfa3`: the coordinator's plan is validated as a contract, and a plan that breaks it falls back to routing by file type.
- Asset rebuilt and verified; 144 backend and 13 infrastructure tests, infrastructure build, lint, format check and synth pass. `cdk diff` (`.build/19155b2-deployment-diff.txt`) changed only the API and worker code, added a version of each and moved both `live` aliases; no IAM, data, Cognito or CloudFront changes. Parameters reused and previous versions retained. The frontend did not change, so the site was not republished.
- `UPDATE_COMPLETE`: API and worker `live` → version 6. Log: `.build/19155b2-deploy.log`.
- Live checks: homepage 200, anonymous API 401, `review-crew.vercel.app` 307. Direct read-only invokes of the API `live` alias (version 6) with the synthetic `deploy-smoke-check` subject returned 200 for `/api/session` and `/api/agents`.
- Not yet run: a signed-in review through the UI, and paid inference.
