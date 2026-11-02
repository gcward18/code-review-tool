# AWS deployment files and review

The deployment app is in `infrastructure/`. Its stack is `AutoSDELocal-dev` in account `123456789012`, region `us-east-1`, using profile `your-profile`. This is separate from the older Code Review Service stacks. Synthesis and diff do not deploy it.

## Files

| Path | Purpose |
| --- | --- |
| `infrastructure/bin/app.ts` | CDK entry point and runtime-asset verification |
| `infrastructure/lib/autosde-stack.ts` | Storage, functions, permissions, API, Cognito and frontend hosting |
| `infrastructure/lib/route-lambda-integration.ts` | API invoke permissions scoped to method and resource, including greedy routes |
| `infrastructure/lib/config.ts` | Allowlisted public deployment coordinates |
| `infrastructure/tests/` | Synthesized-template and configuration tests |
| `infrastructure/runtime-requirements.lock` | Hash-pinned Python wheels for Lambda |
| `scripts/build_lambda.py` | Builds and verifies the Linux x86_64 Lambda asset |
| `scripts/publish_site.py` | Locally stages frontend/config; uploads only with `--publish` |
| `infrastructure/parameters.example.json` | Names of the six explicit deployment parameters |

No `.env`, SQLite data, local `agents.json`, notebook, native macOS executable or frontend files are copied into the Lambda asset. Gitleaks 8.30.1 is taken from its official Linux release and checked against a committed SHA-256. Only pinned binary wheels are installed; no source builds or package install hooks run. The manifest checks both source freshness and the complete asset before synthesis.

## Proposed resources

- One on-demand DynamoDB table (`autosde-dev`) with `PK/SK`, `GSI1`, `GSI2`, encryption, point-in-time recovery, deletion protection and TTL cleanup of upload-ticket records.
- Two private, encrypted, versioned S3 buckets: frontend assets and document storage. The document bucket has a deterministic account/region/stage name to keep its exact upload origin in CSP without a dependency cycle. Its CORS rule allows POST only from the deployed CloudFront origin. CloudFront accesses only the site bucket through OAC. Both buckets and the table are retained when the stack is removed.
- Two Python 3.12/x86_64 functions using the same asset, immutable retained versions and `live` aliases. API: 28 seconds/1 GB; worker: 14 minutes/2 GB. No reserved concurrency.
- Two roles with explicit actions and scoped table/index, document/upload-prefix, SSM-prefix, secret and worker-alias resources. Version reads/deletes are scoped to temporary uploads so the worker can consume and remove precisely the version accepted at completion. No wildcard Allow actions or bare `*` Allow resources. Generated log-stream and object names use prefix-scoped resource patterns.
- Seven SSM string parameters containing model/prices, reasoning effort and secret **ARNs**, never secret values. The three secret values are read from existing Secrets Manager secrets at runtime.
- One HTTP API with JWT-protected GET and POST `/api/{proxy+}` routes. Required scope `autosde/access` restricts it to access tokens. Invoke permissions restrict API Gateway to the corresponding method/path and account.
- An invite-only Cognito pool with optional TOTP, a public authorization-code client, and exact CloudFront callback/logout URLs.
- One CloudFront distribution: private S3 default origin, immutable hashed assets, uncached HTML/config and `/api/*` forwarding to API Gateway. Authorization is forwarded; Host is replaced for the origin. No cross-origin browser API configuration or CORS is required.
- Two Lambda log groups retained for seven days. API Gateway body/access logging and CloudFront access logging are disabled. The worker has automatic function-error retries disabled to avoid replaying paid jobs, and permits its intentional bounded batch chain instead of terminating it after recursive invocations.

The default CloudFront certificate/domain is used; no custom domain or ACM certificate is created. Published site files are a separate explicit step, so synthesis does not add a privileged site-upload custom-resource Lambda. Site bucket versions and old hashed assets remain available for rollback.

## Build and review

From the project root (Node 22+ and Python 3.12+):

```sh
npm ci --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
npm ci --prefix infrastructure --ignore-scripts
.venv/bin/python scripts/build_lambda.py
npm run build --prefix infrastructure
npm test --prefix infrastructure
npm run lint --prefix infrastructure
npm run format:check --prefix infrastructure
AWS_ACCOUNT_ID=123456789012 npm run cdk --prefix infrastructure -- synth --no-lookups --quiet
AWS_ACCOUNT_ID=123456789012 npm run cdk --prefix infrastructure -- diff AutoSDELocal-dev --change-set false --no-lookups
```

The CDK wrapper reads only `AWS_ACCOUNT_ID`, `AWS_PROFILE`, `AWS_REGION` and `AUTOSDE_STAGE` from the sibling `Code Review Service/.env`. Exported values win. Use `AUTOSDE_DEPLOY_ENV=/absolute/path/to/private.env` to select a different file. Without an explicit account, the CDK CLI resolves it through the selected profile; an explicit account enables offline synth.

Build output goes to ignored `.build/lambda`; synthesized templates go to ignored `infrastructure/cdk.out`. The current review diff is saved in `.build/deployment-diff.txt`. These review commands perform no bootstrap, deployment, secret creation or upload. Actual deployment progress is tracked separately in `DEPLOYMENT-STATUS.md`.

## Required inputs before deployment

Supply these six values explicitly; there are no model, price or secret defaults:

| Parameter | Value |
| --- | --- |
| `ModelId` | Your selected OpenAI model ID |
| `InputPrice` | Its USD price per million input tokens |
| `OutputPrice` | Its USD price per million output tokens |
| `OpenAISecretArn` | Complete existing OpenAI secret ARN |
| `GitHubSecretArn` | Complete existing read-only GitHub token secret ARN |
| `LangSmithSecretArn` | Complete existing LangSmith key secret ARN |

The ARNs must belong to the target account/region and include their six-character suffixes. Secrets should use the default AWS-managed Secrets Manager key; customer-managed keys need additional explicit KMS permissions. Do not pass secret **values** as CloudFormation parameters or put them into configuration files. Secret creation and data migration are separate tasks requiring authorization.

## Deploy only after explicit approval

The following commands change AWS resources and require deployment authorization. They were not run during infrastructure preparation; subsequent authorized deployment is recorded in `DEPLOYMENT-STATUS.md`.

From `infrastructure/`, pass the six operator-supplied values:

```sh
npm run cdk -- deploy AutoSDELocal-dev \
  --parameters ModelId="$MODEL_ID" \
  --parameters InputPrice="$INPUT_PRICE" \
  --parameters OutputPrice="$OUTPUT_PRICE" \
  --parameters OpenAISecretArn="$OPENAI_SECRET_ARN" \
  --parameters GitHubSecretArn="$GITHUB_SECRET_ARN" \
  --parameters LangSmithSecretArn="$LANGSMITH_SECRET_ARN" \
  --outputs-file ../.build/deployment-outputs.json
```

A modern CDK bootstrap must already exist. Do not run `bootstrap` or change its permissions without separate approval. Deployment requires `CAPABILITY_IAM` and creates the two scoped Lambda roles; CDK's normal security-change review remains enabled.

Then, from the project root, stage and inspect the cloud config:

```sh
.venv/bin/python scripts/publish_site.py --outputs .build/deployment-outputs.json
```

That command performs no AWS writes. It copies the built site to `.build/site` and replaces the bundled local-mode config with `RuntimeConfig` from the deployed stack outputs. After site publication is approved:

```sh
.venv/bin/python scripts/publish_site.py --outputs .build/deployment-outputs.json --publish
```

Publishing first verifies the selected AWS account/region against the stack outputs. It uploads hashed assets, then HTML/config, and invalidates the three frontend entry paths. It never deletes existing site assets or changes local `autosde/static/config.json`.

Read `SiteUrl` and `UserPoolId` from the outputs. Invite the intended user with `aws cognito-idp admin-create-user --user-pool-id ... --username ... --user-attributes Name=email,Value=... Name=email_verified,Value=true --desired-delivery-mediums EMAIL --profile your-profile --region us-east-1` only after explicitly authorizing that invitation. Complete the initial password flow in Cognito's hosted UI.

## Deployment smoke test

After deployment, verify public config selects cloud mode; unauthenticated API requests return 401; sign-in restores a deep link; API session reports the signed-in subject; sign-out clears the hosted-login session; and SARIF downloads include authorization. Plan and approve a scripted/test repository only when the associated inference cost is authorized. Confirm one batch commit per worker invocation and document upload cleanup. Data migration has not happened; the new table starts empty.

Cloud uploads now accept **20 MB per document**, matching local uploads. The browser asks the authenticated API for a ten-minute signed POST form, uploads the raw file directly to S3 without a Cognito header, and completes the ticket through the API. Signed conditions bind the object key, exact size, SHA-256 checksum and encryption. The API checks S3 metadata, pins its version and atomically registers one document; repeated completion queues at most one effective ingest because the worker conditionally claims the durable ticket. The worker verifies bytes again, runs gitleaks before embedding, and deletes the accepted S3 version. Abandoned/replayed upload versions expire through the lifecycle rule; tickets expire through DynamoDB TTL. Legacy inline cloud requests retain a safe Lambda body limit, but the current UI uses direct uploads. API work must finish inside 28 seconds; long-running synchronous chat/model calls can still time out and are not automatically replayed. Worker timeouts disable function-error retries but do not make asynchronous delivery exactly-once.

`npm audit` currently reports two moderate entries for CDK's bundled minimatch 10.2.6 (the library and its dependency); upstream CDK still bundles that version. Application/runtime packages do not include Node tooling. Other tooling uses patched minimatch 11.0.1 and esbuild 0.28.1, validated by build/tests/synth. Do not apply `npm audit fix --force` downgrades. The remaining advisory concerns pathological glob inputs; infrastructure builds use repository-controlled paths.

References: [Lambda payload limits](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html), [HTTP API invoke permissions](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-troubleshooting-lambda.html), [CloudFront S3 origin access control](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_cloudfront_origins-readme.html).

The user selected `gpt-4o` for the initial deployment. Its initial standard-text estimate rates are $2.50 input / $10.00 output per million tokens, verified against [official OpenAI documentation](https://developers.openai.com/api/docs/models/gpt-4o). These seed the system’s SSM settings; agents may override the model in their configuration.
