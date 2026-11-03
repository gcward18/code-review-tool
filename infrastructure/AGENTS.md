# CDK infrastructure

Read the repository AGENTS.md first. This folder owns the AWS deployment for the local Review Crew codebase; it does not modify the older Code Review Service stacks.

- `bin/`: application entry point. `lib/`: stack and validated public deployment configuration. `tests/`: Vitest assertions against synthesized templates. `scripts/`: CDK CLI wrapper.
- Lambda assets come from `../scripts/build_lambda.py` and are verified before every synthesis. Runtime source, scanner version and every wheel are hash-pinned or recorded in the build manifest. Never bundle `.env`, `agents.json`, databases or a workstation binary.
- Read public account/profile/region/stage values from the sibling Code Review Service `.env`, an explicit `AUTOSDE_DEPLOY_ENV`, or exported environment. Never import or print secrets from that file.
- Model IDs, token prices and complete existing secret ARNs are explicit CloudFormation parameters. Do not invent values or create credentials.
- Grant named actions on the application's exact table/index/secret/function ARNs and document/upload prefixes. Resource suffixes may match generated log streams and objects within a named prefix; never grant an Allow on the bare `*` resource or wildcard actions.
- Retain data and published Lambda versions. Keep logs bounded, body logging disabled, and dev concurrency unreserved. Preserve JWT authorization and required access-token scopes on every API route.
- `synth`, `diff --change-set false`, asset building and local site staging are review work. Deployment, bootstrap, destruction, secret creation and site publishing require explicit authorization.
- Run `npm run build`, `npm test`, `npm run lint`, `npm run format:check`, and a fresh CDK synth after infrastructure changes. Update the deployment guide when parameters, outputs, permissions or limits change.
