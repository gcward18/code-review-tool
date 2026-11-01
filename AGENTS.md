# Repository guidance

## Structure and responsibilities
- `autosde/`: Python application, CLI, web API, persistence, review orchestration and retrieval. Keep domain logic independent of HTTP and CLI presentation.
- `autosde/prompts/`: versioned system prompts and specialist focus files. Treat local agent configuration as user data; preserve unrelated edits.
- `frontend/src/`: React source. Put reusable UI in components, stateful shared behavior in hooks, and pure contracts such as URL parsing in focused modules. Group related feature files into directories as a feature grows; avoid dumping unrelated helpers into a generic utilities module.
- `autosde/static/`: generated frontend distribution. Change frontend source and rebuild; never hand-edit generated assets.
- `tests/`: deterministic Python unit and integration tests. Frontend tests live beside the behavior they validate.
- `docs/`: architecture and feature documentation. `notebooks/`: exploratory workflows, not production dependencies.

## Implementation practices
- Prefer small functions and components with one responsibility, explicit inputs and outputs, and clear names. Reuse existing abstractions before adding dependencies or new ones.
- Keep serialization, validation and routing contracts centralized. Store navigable state in URLs; keep temporary drafts local. Preserve refresh, bookmarks, browser Back/Forward, and usable anchor destinations.
- Handle loading, empty, missing-resource and error states. Cancel or ignore stale asynchronous responses when the selected resource changes; clean up subscriptions and timers.
- Use semantic, keyboard-accessible controls with labels. Maintain existing responsive layouts and theme tokens.
- Validate input at API boundaries. Never log or commit credentials, `.env` contents, source payloads or local databases. Preserve secret scanning and explicit review approval before sending code.
- Keep Python runtime dependencies minimal (standard library by default). Avoid broad refactors for a narrow feature; document meaningful architectural changes and URL contracts.
- Preserve existing user changes. Do not reset unrelated files or overwrite local configuration.

## Validation and delivery
- Run `python3 -m unittest discover -s tests -t tests` for backend checks (use the project virtual environment when appropriate).
- Run `npm test` and `npm run build` in `frontend/` for frontend changes. Commit regenerated `autosde/static/` with source changes.
- Add focused regression coverage for behavioral changes, especially routing, validation and async resource selection; avoid tests that merely repeat implementation details.
- Check the final diff for unrelated edits and generated artifacts. Report behavior changed, checks run, and any remaining limitations.
