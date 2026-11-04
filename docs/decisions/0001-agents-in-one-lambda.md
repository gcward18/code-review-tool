# 1. All review agents run in one worker invocation

- **Status:** accepted
- **Date:** 2026-11-03

## Context

A review runs a coordinator, up to seven language specialists and an aggregator, which can send specialists follow-ups. In AWS the API Lambda queues jobs and the worker Lambda (14-minute timeout) runs them. Each agent could instead be its own Lambda, deployed and versioned on its own.

Each agent is a prompt, a JSON schema and one call through the shared OpenAI adapter. Almost all of a review's time is spent waiting on the model, not computing.

## Decision

The coordinator, every specialist and the aggregator run inside the worker invocation, as one LangGraph graph (`autosde/team.py`). Specialists run in parallel as `Send` branches, up to `workers` at a time. Agents communicate only through the graph: typed state, schema-checked replies and the review's finding list. No agent calls another directly.

Agents stay independently configurable without deploying: each one's model, prompt, file extensions, knowledge bases, tracing and retry limit live in DynamoDB (versioned) in AWS, and in `autosde/prompts/` locally.

## Reasons

- **Latency and cost:** a Lambda per agent adds an invoke, and possibly a cold start, to every specialist call, retry and follow-up round. In-process branches cost a thread.
- **Simple state:** retries, follow-up rounds, refused edits and the finding numbering are graph state inside one invocation. Across Lambdas they would need a store, idempotent hand-offs and partial-failure handling.
- **Same safety:** Gitleaks scanning, the approval step, file scope and retry limits are enforced in the worker either way. A process boundary per agent would not strengthen them.
- **One asset:** the API and worker share one hash-pinned runtime, so there is one artifact to build, scan and deploy.

## Consequences

- A specialist cannot be deployed, scaled or rolled back on its own; prompt and model changes are the unit of change, and they need no deploy.
- One review's agents share the worker's memory, threads and 14-minute timeout. Retry limits and `MAX_ROUNDS` bound the number of calls, but in the worst case (many specialists whose every attempt times out, in every round) a review can still hit that timeout and fail.
- A crash in the worker fails the whole review rather than one agent; a failed specialist call, by contrast, is retried and then reported as a limitation.

## Revisit when

- Specialists need their own toolchains or sandboxes, for example running `tsc`, `mypy` or tests.
- One review's specialists outgrow a single worker's time or memory.
- Different people need to own and release specialists separately.
