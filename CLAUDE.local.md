# CLAUDE.local.md — ST21-ARBITER (personal, gitignored)

Personal project rules for ST21-ARBITER. **Not** checked into source control (see `.gitignore`).
Keep this file under ~200 lines. Move detailed material into [`.claude/rules/`](.claude/rules/) and import here via `@path` if it grows.

## Project facts (source of truth)

- AWS account: **669810405473** · region: **us-east-1** · environment: **dev**
- Project name is set in [`Infra/params/dev.json`](Infra/params/dev.json) under `ProjectName` — currently `st21arbiter-poc`. Never hardcode it; resource names are `<env>-<project>-…`.
- Stack/runtime/log-group names use **underscores** wherever AgentCore is involved (`deploy_agents.py` converts dashes to underscores). All other names use dashes.
- Canonical architecture diagram: [`Documents/arbiter_st21_architecture.drawio`](Documents/arbiter_st21_architecture.drawio).
- Deployment runbook: [`instructions/DEPLOYMENT.md`](instructions/DEPLOYMENT.md). When deployment reality drifts from the doc, update the doc.

## Universal rules

- **Confirm before destructive AWS actions.** `delete-stack`, `s3 rm`, `delete-bucket`, `delete-agent-runtime`, `delete-knowledge-base`, `delete-memory`, `kms schedule-key-deletion`, `iam delete-role`, killing PIDs.
- **Always `aws cloudformation validate-template` before deploying** a changed template.
- **Use change-sets** for CFN updates (already wired in [`Infra/deploy.sh`](Infra/deploy.sh)); never `create-stack` / `update-stack` directly.
- **Read deployed state before assuming.** Use `aws cloudformation list-exports` / `list-stacks` / `describe-stacks`; don't assume names from old session memory.
- **Never commit secrets.** `.env.development`, raw access keys, `BaselineFiles/` source PDFs all stay out of git.

## Frontend — `ui/` (Vite + React + Cognito)

- Dev server **must** bind to port 5173 (Cognito callback whitelist). If another `vite` is holding it: `lsof -iTCP:5173 -sTCP:LISTEN`, kill that PID, restart `npm run dev`.
- `.env.development` is account-specific; regenerate it after every redeploy of `03-identity` or `06-api`. Source values are CFN exports — see DEPLOYMENT.md §9.1.
- Mock mode auto-engages when `VITE_API_URL` is empty (`USE_MOCK = !API_URL` in [`ui/src/config.js`](ui/src/config.js)). If the UI shows mock-looking data unexpectedly, check this first.
- React `StrictMode` is enabled in [`ui/src/main.jsx`](ui/src/main.jsx) and **double-fires effects in dev**. Any single-use API call invoked from a `useEffect` (token exchange, idempotent POST) **must** be guarded with a module-level in-flight promise — see [`ui/src/hooks/useAuth.js`](ui/src/hooks/useAuth.js) `handleCallback` for the canonical pattern.
- Cognito client has **no secret** (public SPA client). Don't add one.
- The MCP server list in [`ui/src/pages/MCPChat.jsx`](ui/src/pages/MCPChat.jsx) routes per-agent: each card's `id` is sent as `target` to `sendChat()`, and `_handle_chat` in [`api_handler.py`](Infra/functions/api_handler/api_handler.py) resolves it to that specialist runtime's ARN (absent/unknown target → master orchestrator, which the Analyst page uses). Specialist ARNs are patched onto the api_handler Lambda by `deploy_agents.py` (`SHAREPOINT_/AWSCONFIG_/ZSCALER_/JIRA_RUNTIME_ARN`). `servicenow` is a static placeholder until its agent ships. Live status: `GET /agent-status` → `useAgentStatus()`.

## Backend — `Infra/functions/api_handler/` + `agents/`

- `/chat` traffic uses the **Lambda Function URL** (`AuthType=NONE`), not API Gateway, to bypass APIGW's 29 s integration timeout. The Lambda decodes the Cognito JWT manually (trusted-issuer pattern). Keep both code paths working in `_caller_user_id`.
- DDB Query on a GSI requires the IAM resource ARN to include `/index/*`. Both `table/<env>-<project>-*` **and** `table/<env>-<project>-*/index/*` must be scoped in `02-security.yaml`.
- The `sessions` table is encrypted with the DynamoDB CMK. The AgentCore role's `KMSDecrypt` statement in `09-agentcore.yaml` **must** include `DynamoDBKeyArn` or `PutItem`/`UpdateItem` silently fails.
- Every agent's `Dockerfile` wraps `python agent.py` with `opentelemetry-instrument` and pulls in `aws-opentelemetry-distro`. Removing either breaks AgentCore Observability — don't touch unless replacing the stack.
- The master agent's `create_event` calls require `eventTimestamp=datetime.now(timezone.utc)` (boto3 SDK requirement). Keep it.
- **Foundation model default is Amazon Nova 2 Lite** (`us.amazon.nova-2-lite-v1:0`) on all 4 runtimes. Anthropic Claude models (Sonnet 4.6 / Haiku 4.5) require an AWS Marketplace subscription + a valid payment instrument on the account. Until that's accepted in the Bedrock Model Access wizard, agent invocations on Claude return `INVALID_PAYMENT_INSTRUMENT` or `aws-marketplace:Subscribe` errors. Each `agents/*/agent.py` reads `MODEL_ID` from env, so future deploys can override via `MASTER_MODEL_ID=...` to `deploy_agents.py`.
- **Cognito persona binding**: 4 groups (`ciso`/`soc`/`grc`/`employee`) + 4 users (`ciso_daiana@`, `soc_marcus@`, `grc_priya@`, `emp_sarah@`). The UI reads `cognito:groups` from the IdToken via `getGroups()` in [`ui/src/hooks/useAuth.js`](ui/src/hooks/useAuth.js); `PersonaContext` pins `personaId` from that. No in-app persona switching. Passwords are set by `deploy.sh::set_demo_passwords` from the `DEMO_PASSWORD` env var on the deploy line; if you forget to export it, users land in `FORCE_CHANGE_PASSWORD` state and the Hosted UI lies about it ("Invalid username or password").

## Infrastructure — `Infra/templates/*.yaml`, `Infra/deploy.sh`

- Stack order (handled by [`Infra/deploy.sh`](Infra/deploy.sh)): 00-bootstrap → 01-network → 02-security → 03-identity → 04-storage → 05-compute → 06-api → 09-agentcore. 07-bedrock and 08-observability are intentionally deferred (KB via script, dashboards later).
- `deploy.sh` reads `ProjectName` from `params/dev.json` (don't re-hardcode it in the shell).
- SAM stacks (`05-compute`, `06-api`) use `--no-confirm-changeset` (non-interactive); the rest use the explicit change-set flow.
- 06-api defines `AWS::ApiGateway::GatewayResponse` for UNAUTHORIZED / ACCESS_DENIED / DEFAULT_4XX so the SPA can render auth errors (CORS headers on 4xx). Any change to these requires a stage redeployment.
- 04-storage's OpenSearch data access policy does **not** include the Bedrock service-linked role (`AWSServiceRoleForAmazonBedrock`) — that role doesn't exist until a KB is first created. If you ever re-enable `07-bedrock.yaml`, re-add the principal there.
- `04-storage.yaml` has `DeletionPolicy: Retain` **removed** from data buckets in dev so failed rollbacks clean up. Don't re-add Retain to dev templates.
- **AgentCore Runtime AZ constraint:** only physical AZ IDs `use1-az1`, `use1-az2`, `use1-az4` are supported in us-east-1. AZ name ↔ ID mapping is account-specific. In this account: `us-east-1b → use1-az1 ✓`. `PrivateSubnet2` in `01-network.yaml` is dedicated to AgentCore for this reason; `deploy_agents.py` reads `PrivateSubnet2Id`. Don't move AgentCore back to `PrivateSubnet1`.
- IAM resource ARNs for AgentCore log groups + memory use `!Join + !Split` to convert dashes in ProjectName to underscores (matching `deploy_agents.py`'s `runtime_name.replace("-", "_")`). Don't simplify those `!Sub` blocks back to a single string.

## Scripts — `scripts/*.py`

- **Always activate the venv** before running any Python script: `source scripts/.venv/bin/activate`. Homebrew Python blocks system-wide pip via PEP 668.
- All scripts read `PROJECT` from env var with `st21arbiter-poc` as default. Setting `PROJECT=...` explicitly is still recommended for clarity.
- `deploy_agents.py` builds images via CodeBuild on Graviton (arm64) and tags with Unix timestamp. **Never run with `--skip-build`** on a fresh deploy — `:latest` doesn't exist in our ECR repos.
- KB ingestion is **not** auto-triggered by `setup_bedrock_kb.py`. Always run `aws bedrock-agent start-ingestion-job` after the script completes (Step 5 of DEPLOYMENT.md).

## Operational checks

After any redeploy, verify:

```bash
aws cloudformation list-stacks --region us-east-1 --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query 'StackSummaries[?contains(StackName, `st21arbiter-poc`)].StackName' --output text  # 8 stacks
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[?contains(agentRuntimeName, `st21arbiter_poc`)].[agentRuntimeName,status]' --output table  # 5× READY (sharepoint/awsconfig/zscaler/jira/master)
```

## Known gotchas (one-liners)

- **Bootstrap `ROLLBACK_COMPLETE`** → S3 bucket name globally taken. Bump `ProjectName`.
- **Cognito `AlreadyExists`** → `CognitoDomainPrefix` globally taken. Bump it.
- **AgentCore `CREATE_FAILED ... unsupported availability zones`** → subnet AZ not in `use1-az1/2/4`. Override `PrivateSubnet2AZ`.
- **Sign-in 400 on `/callback`** → StrictMode double-fire reused the auth code; check `handleCallback`'s inflight guard.
- **UI shows stale "mock" data** → wrong Vite dev server on 5173, or `VITE_API_URL` empty.
- **CodeBuild "no :latest tag"** → ran `deploy_agents.py --skip-build` on fresh account.

## Key paths cheat-sheet

- Params (project name, CIDRs, etc): [`Infra/params/dev.json`](Infra/params/dev.json)
- Deploy / destroy: [`Infra/deploy.sh`](Infra/deploy.sh), [`Infra/destroy.sh`](Infra/destroy.sh)
- Lambdas: [`Infra/functions/api_handler/`](Infra/functions/api_handler/), [`Infra/functions/processing_pipeline/`](Infra/functions/processing_pipeline/)
- Agents: [`agents/master_orchestrator/`](agents/master_orchestrator/), [`agents/sharepoint_specialist/`](agents/sharepoint_specialist/), [`agents/zscaler_specialist/`](agents/zscaler_specialist/), [`agents/awsconfig_specialist/`](agents/awsconfig_specialist/)
- UI auth + config: [`ui/src/hooks/useAuth.js`](ui/src/hooks/useAuth.js), [`ui/src/config.js`](ui/src/config.js)
- Mock data: [`ui/src/mockData.js`](ui/src/mockData.js)
- Setup scripts: [`scripts/setup_bedrock_kb.py`](scripts/setup_bedrock_kb.py), [`scripts/deploy_agents.py`](scripts/deploy_agents.py), [`scripts/seed_mock_data.py`](scripts/seed_mock_data.py)
- KB seed source: [`BaselineFiles/`](BaselineFiles/) → synced to `s3://dev-st21arbiter-poc-processed/`

## Memory / personal notes

For learnings Claude should remember automatically (debugging insights, build commands it discovers), let auto-memory at `~/.claude/projects/<project>/memory/` handle it. Only put **rules** in this file.
