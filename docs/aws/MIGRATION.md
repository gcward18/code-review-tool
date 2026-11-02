# AutoSDE on AWS: migration plan

Target: account `123456789012`, region `us-east-1`, profile `your-profile`, stage `dev` (from the Code Review Service `.env`). The local app keeps working; the same code runs in AWS with different storage and configuration backends.

## Architecture

```
Browser ──► CloudFront ──► S3 (private, the built React app + config.json)
   │
   │  Cognito hosted login (invite-only, PKCE) ──► ID/access token
   ▼
API Gateway HTTP API ──(Cognito JWT authorizer)──► api Lambda  (≤28 s, JSON routes)
                                                     │  async invoke
                                                     ▼
                                                  worker Lambda (≤15 min): plan, review,
                                                  batch step, ingest, summaries
        DynamoDB (one table)   S3 (documents, extracted text)   Secrets Manager   SSM Parameter Store
```

| Today (local) | AWS |
| --- | --- |
| SQLite `~/.autosde/autosde.db` | DynamoDB table `autosde-dev` (on-demand, point-in-time recovery, retained) |
| `http.server` + threads | API Gateway HTTP API → `api` Lambda; long work → async `worker` Lambda |
| Plans held in memory until approval | Plan stored in DynamoDB; approval re-reads the files from GitHub by commit SHA and re-scans (no source stored) |
| Local folders and GitHub (via `gh`) | GitHub only, via the API with a read-only token |
| Built UI served by Python | S3 bucket (private) behind CloudFront with origin access control |
| No login (localhost + token) | Cognito user pool, invite-only, hosted login page, optional TOTP MFA; every API route needs a valid token |
| Knowledge chunks + FTS5 in SQLite | Chunks and vectors in DynamoDB; hybrid search (vectors + BM25) inside Lambda, cached per warm container |
| Uploaded files read in memory | Browser uploads to S3 with a presigned URL; worker extracts, scans, chunks, embeds; extracted text kept in S3 |
| `prompts/agents.json`, prompt files | DynamoDB config items with versions (edited from the Agents page) |
| Environment variables | SSM Parameter Store `/autosde/dev/*` (model, prices, reasoning effort, LangSmith project/endpoint/enabled) |
| OpenAI key via env or Secrets Manager | Secrets Manager `autosde/openai-api-key` (exists) |
| `LANGSMITH_API_KEY` env | Secrets Manager `autosde/langsmith-api-key` (you create it) |
| `gh` login | Secrets Manager `autosde/github-token`: a fine-grained, read-only token (you create it) |
| gitleaks on PATH | gitleaks binary packaged in the worker Lambda |

## Code changes

1. **Storage layer.** Split `Store`, `Knowledge`, `Batches` and `Remotes` into an interface with two implementations: SQLite (today's code) and DynamoDB (single-table: projects, reviews with per-project numbers, agent runs, findings, review files, chat, knowledge bases, documents, chunks, batches, repositories, config). Tests run both against the same suite, DynamoDB through moto.
2. **Configuration layer.** Settings from environment locally, from SSM + Secrets Manager in AWS. Agent config and prompts from files locally, from DynamoDB in AWS.
3. **API refactor.** Move the routes out of `web.py` into a framework-free router used by both the local server and a Lambda handler. Background work goes through a `jobs` interface: threads locally, async `worker` invokes in AWS (a batch re-invokes the worker for each next commit, so no run approaches the 15-minute limit).
4. **Stateless approval.** Approval rebuilds the reviewed files from GitHub at the planned SHAs instead of holding them in memory.
5. **Frontend.** Read `config.json` (API URL, Cognito domain and client) at start-up; PKCE login and logout; send the token on every request. Local mode keeps today's per-start token.
6. **Infrastructure.** New CDK TypeScript app in `infrastructure/`, following the Code Review Service conventions (least-privilege IAM on exact ARNs, retained data, no reserved concurrency, bounded log retention, no body logging, Vitest template tests). It reads account, region, profile and stage from the Code Review Service `.env`.
7. **Data migration.** A script copies your SQLite data (projects, reviews, findings, chats, knowledge bases with their chunks and embeddings, batches, repositories) and `agents.json` and prompts into DynamoDB, and the settings into SSM. It runs as a dry run first and reports counts.

## What you'll do

- Approve each deploy (`cdk deploy`); I run `synth` and `diff` and show you the changes. IAM changes may need you to run the deploy yourself, as with the earlier stacks.
- Create two secrets: `autosde/langsmith-api-key` and `autosde/github-token` (fine-grained, read-only contents and metadata on the repositories you want reviewed).
- Create your Cognito user (one command, printed after deploy).

## Cost (idle dev, estimated)

About $2/month: three secrets ($1.20), and pennies for DynamoDB on-demand, Lambda, API Gateway, CloudFront and S3. Cognito is free at this size. Model and embedding usage is billed as today.

## Limits

- No local-folder reviews in AWS.
- Knowledge search loads a base's vectors into Lambda memory: fine up to roughly 20,000 chunks per base.
- API calls stop at 28 seconds; chat answers that take longer return a "still working" response and finish in the background.
- The account's 10 concurrent Lambdas cap parallel work (batches run one commit at a time).

## Order of work

1. Storage and configuration layers, with tests on both backends.
2. Router, Lambda handlers and jobs; stateless approval.
3. Frontend login and runtime config.
4. CDK app with tests; `synth` and `diff` for your review.
5. First deploy (your approval), secrets, user, smoke test.
6. Data migration (dry run, then real), and the operations runbook.
