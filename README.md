# Review Crew

**A team of AI agents that reviews your git commits, locally or on AWS.** A coordinator routes changed files to language and framework specialists, an aggregator checks every finding against the code, and nothing reaches a model until you approve the files and the cost.

<table><tr>
<td align="center"><a href="frontend/src/docs/review.webm"><img src="frontend/src/docs/review.jpg" alt="Running a review video" width="280"></a><br><b>Running a review</b></td>
<td align="center"><a href="frontend/src/docs/agents.webm"><img src="frontend/src/docs/agents.jpg" alt="Configuring agents video" width="280"></a><br><b>Configuring agents</b></td>
<td align="center"><a href="frontend/src/docs/knowledge.webm"><img src="frontend/src/docs/knowledge.jpg" alt="Knowledge bases (RAG) video" width="280"></a><br><b>Knowledge bases (RAG)</b></td>
</tr></table>

<sub>Click a preview to play it. Recorded with Playwright against a demo repository and a scripted model.</sub>

## Highlights

- **Agent team:** Python, JavaScript, TypeScript, Java, Go, C# and a general specialist review in parallel. Angular and React files also get a framework specialist.
- **Aggregation gate:** the aggregator judges findings against numbered code, rejects weak ones with reasons, and can question, restart or retry a specialist for up to three rounds.
- **Approval and budget:** rules flag low-value files, you pick files and approve an "at most" cost, and that budget is enforced while the review runs.
- **Context on demand:** specialists and the aggregator can request unchanged files, which are read at the head commit and Gitleaks-scanned, within a quota.
- **Secure by default:** only committed code is reviewed, every payload is Gitleaks-scanned, and the store keeps findings, not source.
- **RAG knowledge bases:** structure-aware chunking with hybrid vector and BM25 retrieval fused by RRF, attachable per agent.
- **Everywhere you look:** live agent activity, a review UI, a CLI, SARIF export, a chat agent over review history, and optional LangSmith traces.
- **Two modes, one codebase:** SQLite and threads locally, or Lambda, DynamoDB, S3, CloudFront and Cognito on AWS.

## Architecture

### Agent team

LangGraph runs a coordinator graph, then (after approval) a review graph that fans out to specialists and loops back through the aggregator. All agents share one worker invocation by design ([decision 1](docs/decisions/0001-agents-in-one-lambda.md)).

```mermaid
flowchart TD
    Source["Committed files and diffs<br/>Local git or GitHub API"] --> Scan["Gitleaks scan<br/>Exclude flagged files"]
    Scan --> Coordinator["Coordinator<br/>File metadata and routing rules"]
    Coordinator --> Plan["Saved plan<br/>Assignments, flags, sizes and estimate"]
    Plan --> Approval{"User selects files<br/>and approves"}
    Approval -->|Approved paths| Reload["Reload exact commits<br/>and scan again"]
    Approval -->|Cancel| Cancelled["Cancelled review"]

    subgraph Specialists["Parallel specialist calls"]
        Python["Python"]
        Web["JavaScript and TypeScript,<br/>plus Angular and React on framework files"]
        Other["Java, Go, C#, general<br/>and configured specialists"]
    end
    Reload --> Python & Web & Other
    Knowledge["Attached knowledge bases<br/>Vector + BM25 retrieval, fused by RRF"] -. Relevant passages .-> Python & Web & Other
    Python & Web & Other --> Aggregator["Aggregator<br/>Check findings against the code,<br/>keep or reject with reasons"]
    Aggregator -. "CONTINUE: question, restart or retry<br/>(two more rounds at most)" .-> Python & Web & Other
    Aggregator --> History[("Review history<br/>Findings, runs, tokens and cost")]
    History --> Results["Review UI, CLI and SARIF"]
    History --> Chat["Chat agent<br/>Home or project questions"]
    Question["Question and conversation"] --> Chat
    Chat --> Answer["Answer saved to chat history"]
```

### Deployment

```mermaid
flowchart LR
    Browser["Browser<br/>React UI and runtime config"]
    subgraph AWS["AWS deployment · CDK"]
        CDN["CloudFront<br/>HTTPS, CSP and API forwarding"]
        Site[("Private S3 site bucket<br/>Built UI and config.json")]
        Cognito["Cognito hosted login<br/>Authorization code + PKCE"]
        Gateway["API Gateway HTTP API<br/>JWT + autosde/access scope"]
        API["API Lambda · live alias<br/>Shared router and application"]
        Worker["Worker Lambda · live alias<br/>Plan, approve, ingest and batch jobs"]
        Data[("DynamoDB<br/>Reviews, findings, config,<br/>chunks, vectors and upload tickets")]
        Documents[("Private, versioned S3<br/>Temporary uploads and large document text")]
        Settings["SSM Parameter Store<br/>Model, prices and secret ARNs"]
        Secrets["Secrets Manager<br/>OpenAI, GitHub and LangSmith keys"]
        Logs["CloudWatch Logs<br/>Seven-day retention"]
    end
    GitHub["GitHub API<br/>Repository metadata, files and diffs"]
    OpenAI["OpenAI<br/>Agent calls and embeddings"]
    LangSmith["LangSmith<br/>Optional traces"]

    Browser -->|Load UI and call /api| CDN
    CDN -->|Origin access control| Site
    CDN -->|Forward bearer token| Gateway
    Browser <-->|Sign-in and token exchange| Cognito
    Cognito -. Token issuer .-> Gateway
    Gateway --> API
    API -->|Asynchronous job invocation| Worker
    Worker -->|Next batch commit| Worker
    API & Worker <--> Data
    API & Worker <--> Documents
    Browser -->|Signed POST, up to 20 MB| Documents
    Settings & Secrets --> API & Worker
    API & Worker --> GitHub & OpenAI
    API & Worker -. Optional .-> LangSmith
    API & Worker --> Logs
```

### Review data flow

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant UI as React UI
    participant API as Shared API
    participant Store as Review store
    participant Worker as Background worker
    participant Repo as Git or GitHub
    participant Team as Agent team
    participant Model as OpenAI

    Note over UI,API: Cloud requests carry a Cognito access token, which API Gateway validates
    User->>UI: Choose repository and commit range
    UI->>API: Request review plan
    API->>Repo: Resolve exact base and head commits
    API->>Store: Create planning review with commit IDs
    API-->>Worker: Queue plan job with review ID
    API-->>UI: Review ID
    Worker->>Repo: Read files and diffs at saved commits
    Worker->>Worker: Gitleaks scan, excluding flagged files
    Worker->>Team: Plan reviewable files
    Team->>Model: Coordinator call with file metadata
    Model-->>Team: Assignments, flags and rationale
    Team-->>Worker: Validated plan with routing fallback
    Worker->>Store: Save plan, sizes, usage and trace reference
    UI->>API: Poll review status
    API->>Store: Read plan and estimate
    API-->>UI: Awaiting approval
    User->>UI: Select paths and approve
    UI->>API: Approve selected paths
    API->>Store: Mark review running
    API-->>Worker: Queue approval job with review ID and selection
    Worker->>Store: Load saved plan and trace reference
    Worker->>Repo: Reload files at the same commits
    Worker->>Worker: Scan files again
    Worker->>Team: Execute approved assignments
    opt Agent has attached knowledge bases
        Team->>Store: Load chunks and vectors
        Team->>Model: Embed retrieval query
        Team->>Team: Fuse vector and BM25 rankings into context
    end
    loop Assigned specialists, up to four calls in parallel
        Team->>Model: Approved code, diffs, focus and optional context
        Model-->>Team: Structured candidate findings
    end
    opt Candidate findings exist
        Team->>Model: Aggregator call with findings, their numbered code and changed-line ranges
        Model-->>Team: Verdict, rejections, reasons and summary
        loop CONTINUE: question, restart or retry (two more rounds at most)
            Team->>Model: Named specialists on files they reviewed
            Model-->>Team: Their complete findings for those files
            Team->>Model: Aggregator judges again, seeing replaced findings
            Model-->>Team: Verdict, rejections, refused edits and summary
        end
    end
    Team-->>Worker: Findings, agent runs and usage
    Worker->>Store: Save completed review and cost
    UI->>API: Poll review status
    API->>Store: Read completed review
    API-->>UI: Findings, rejected findings and run statistics
```

### Knowledge upload data flow

```mermaid
sequenceDiagram
    autonumber
    participant UI as React UI
    participant API as API Lambda
    participant S3 as Private S3 document bucket
    participant Store as DynamoDB
    participant Worker as Worker Lambda
    participant Model as OpenAI embeddings

    UI->>UI: Check 20 MB limit and calculate SHA-256
    UI->>API: Initiate upload with name, size and checksum
    API->>Store: Save owner-bound pending upload ticket
    API-->>UI: Signed POST URL, fields and ticket ID
    UI->>S3: POST raw file and signed fields, without Cognito token
    S3-->>UI: Upload accepted
    UI->>API: Complete ticket with Cognito token
    API->>Store: Read ticket, verify owner, base and expiry
    API->>S3: HEAD object, verify size, checksum and metadata
    S3-->>API: Verified metadata and version ID
    API->>Store: Atomically register document and pin accepted version
    API-->>Worker: Queue ingest job with ticket ID only
    API-->>UI: Document ID and processing status
    Worker->>Store: Conditionally claim ticket once
    Worker->>S3: Read the pinned object version
    Worker->>Worker: Verify bytes, extract text and run gitleaks
    alt Scan passes
        opt Extracted text exceeds the inline storage limit
            Worker->>S3: Persist scanned text under documents/
        end
        Worker->>Store: Store inline text or S3 document reference
        Worker->>Worker: Chunk text with configured strategy
        Worker->>Model: Embed chunks
        Model-->>Worker: Vectors and token usage
        Worker->>Store: Save chunks and vectors, mark document ready
    else Extraction or scan fails
        Worker->>Store: Mark document failed, skip embedding
    end
    Worker->>S3: Delete accepted temporary object version
    Worker->>Store: Mark upload ticket done
    UI->>API: Poll knowledge base documents
    API->>Store: Read document status
    API-->>UI: Ready or failed
    Note over S3,Store: Lifecycle expires abandoned uploads, DynamoDB TTL expires tickets
```

## Quick start

```sh
brew install gitleaks
export OPENAI_API_KEY=...
pip install -e .            # add '.[pdf]' for PDF knowledge uploads
autosde serve               # http://127.0.0.1:8765/
```

From the command line:

```sh
autosde review ~/code/shop main..HEAD             # plan, approve, review
autosde show 3 --format sarif --output out.sarif  # export findings
```

## Configuration

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Model and embedding calls (never stored) |
| `AUTOSDE_MODEL`, `AUTOSDE_INPUT_PRICE`, `AUTOSDE_OUTPUT_PRICE` | Default model and prices for estimates |
| `AUTOSDE_HOME` | Data folder (default `~/.autosde`) |
| `AUTOSDE_GITLEAKS` | Gitleaks binary (default: on `PATH`) |
| `LANGSMITH_API_KEY` | Optional tracing |

Per-agent models, prompts, extensions, retry limits and knowledge bases are set on the **Agents** page.

## Developing

```sh
pip install -e ".[dev]"
AUTOSDE_GITLEAKS="$(command -v gitleaks)" python3 -m unittest discover -s tests -t tests
cd frontend && npm install && npm run build   # writes autosde/static/
npm run record-docs -- --gitleaks "$(command -v gitleaks)"   # re-record the videos
```

More: [RAG](docs/RAG.md) · [AWS deployment](docs/aws/DEPLOYMENT.md) · [Frontend config](docs/aws/FRONTEND.md) · [Infrastructure](infrastructure/)

## Built with

<p align="center">
<a href="https://github.com/python/cpython"><img alt="Python 3.12" src="https://img.shields.io/badge/Python_3.12-3776AB?style=for-the-badge&logo=python&logoColor=white"></a>
<a href="https://github.com/langchain-ai/langgraph"><img alt="LangGraph" src="https://img.shields.io/badge/LangGraph-1C3C3C?style=for-the-badge&logo=langgraph&logoColor=white"></a>
<a href="https://github.com/langchain-ai/langsmith-sdk"><img alt="LangSmith" src="https://img.shields.io/badge/LangSmith-1C3C3C?style=for-the-badge&logo=langchain&logoColor=white"></a>
<a href="https://github.com/openai/openai-python"><img alt="OpenAI" src="https://img.shields.io/badge/OpenAI-412991?style=for-the-badge&logo=openai&logoColor=white"></a>
<a href="https://github.com/gitleaks/gitleaks"><img alt="Gitleaks" src="https://img.shields.io/badge/Gitleaks-DC2626?style=for-the-badge&logo=git&logoColor=white"></a>
<a href="https://github.com/facebook/react"><img alt="React" src="https://img.shields.io/badge/React-20232A?style=for-the-badge&logo=react&logoColor=white"></a>
<a href="https://github.com/vitejs/vite"><img alt="Vite" src="https://img.shields.io/badge/Vite-646CFF?style=for-the-badge&logo=vite&logoColor=white"></a>
<a href="https://github.com/microsoft/playwright"><img alt="Playwright" src="https://img.shields.io/badge/Playwright-2EAD33?style=for-the-badge&logo=playwright&logoColor=white"></a>
<a href="https://github.com/sqlite/sqlite"><img alt="SQLite" src="https://img.shields.io/badge/SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white"></a>
<a href="https://github.com/aws/aws-cdk"><img alt="AWS CDK" src="https://img.shields.io/badge/AWS_CDK-FF9900?style=for-the-badge&logo=amazonwebservices&logoColor=white"></a>
<a href="https://github.com/microsoft/TypeScript"><img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-3178C6?style=for-the-badge&logo=typescript&logoColor=white"></a>
<a href="https://github.com/boto/boto3"><img alt="Boto3" src="https://img.shields.io/badge/Boto3-232F3E?style=for-the-badge&logo=amazonwebservices&logoColor=white"></a>
<a href="https://github.com/getmoto/moto"><img alt="Moto" src="https://img.shields.io/badge/Moto-232F3E?style=for-the-badge&logo=amazonwebservices&logoColor=white"></a>
<a href="https://github.com/py-pdf/pypdf"><img alt="pypdf" src="https://img.shields.io/badge/pypdf-EC1C24?style=for-the-badge&logo=adobeacrobatreader&logoColor=white"></a>
<a href="https://github.com/mermaid-js/mermaid"><img alt="Mermaid" src="https://img.shields.io/badge/Mermaid-FF3670?style=for-the-badge&logo=mermaid&logoColor=white"></a>
</p>
