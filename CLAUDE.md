# CLAUDE.md

This file is generated on Sridhar's desktop. It provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**ARBITER (ST21)** is a multi-agent compliance/policy-conflict-detection demo. A React SPA talks to a Lambda API which orchestrates four Bedrock AgentCore Runtimes (1 master + 3 specialists) backed by a Bedrock Knowledge Base. Everything provisions into a single AWS account via CloudFormation/SAM stacks. Single-AZ, dev-only — see [`Infra/templates/`](Infra/templates/) descriptions for the trim policy.

Authoritative references:
- End-to-end deployment runbook: [`instructions/DEPLOYMENT.md`](instructions/DEPLOYMENT.md)
- Architecture diagram (draw.io): [`Documents/arbiter_st21_architecture.drawio`](Documents/arbiter_st21_architecture.drawio)
- AgentCore deep-dive: [`agents/README.md`](agents/README.md)
- Original infra conventions (still relevant): [`infra-scripts-guide.md`](infra-scripts-guide.md)

## Architecture (big picture)

Three planes, glued by Cognito JWT auth:

1. **UI plane** ([`ui/`](ui/)) — Vite + React 18 SPA, Cognito Hosted UI for sign-in, in-memory state (no Redux). Two API surfaces: API Gateway for short DDB ops, **Lambda Function URL** for `/chat` (long-running agent calls — bypasses API GW's 29s integration timeout). UI auto-falls back to bundled [`mockData.js`](ui/src/mockData.js) when `VITE_API_URL` is empty (the `USE_MOCK` switch in [`ui/src/config.js`](ui/src/config.js)).

2. **API plane** ([`Infra/functions/api_handler/`](Infra/functions/api_handler/)) — single Python 3.13 Lambda behind both API Gateway (Cognito JWT authorizer) and a Function URL (`AuthType=NONE`, decodes JWT manually). Routes: `/findings`, `/change-requests`, `/audit-logs`, `/conversations[/{id}/messages]`, `/chat`. Sessions/conflicts/CRs/audit live in 4 DynamoDB tables. `/chat` invokes the master AgentCore Runtime synchronously. A second Lambda, [`processing_pipeline`](Infra/functions/processing_pipeline/), runs S3→text-extract→KB-sync.

3. **Agent plane** ([`agents/`](agents/)) — four Bedrock AgentCore Runtimes, each a Strands `Agent` wrapped in `bedrock_agentcore.runtime.BedrockAgentCoreApp` on port 8080. Master orchestrator fans out via tools (`sharepoint_lookup`, `awsconfig_lookup`, `zscaler_lookup`) to three specialists. Default foundation model for all four runtimes is **Amazon Nova 2 Lite** (`us.amazon.nova-2-lite-v1:0`) — first-party, no AWS Marketplace subscription required. Each agent reads `MODEL_ID` from its runtime env, so you can override per-runtime (e.g. `MASTER_MODEL_ID=us.anthropic.claude-sonnet-4-6` on a `deploy_agents.py` run) once Marketplace terms for Anthropic models are accepted. Specialists hit the Bedrock KB; KB indexes from `s3://dev-<project>-processed/` into an OpenSearch Serverless vector collection. All Bedrock calls go through a Guardrail (content/PII/denied-topics). Master uses AgentCore Memory for long-term conversation continuity, mirrored to the DDB `sessions` table.

4. **Persona/RBAC plane** — Cognito User Pool has 4 groups (`ciso`, `soc`, `grc`, `employee`) plus one user per group: `ciso_daiana@…`, `soc_marcus@…`, `grc_priya@…`, `emp_sarah@…`. The UI reads `cognito:groups` from the IdToken and pins `personaId` in [`ui/src/contexts/PersonaContext.jsx`](ui/src/contexts/PersonaContext.jsx). The Personas page is read-only — no in-session switching. Page access is enforced client-side by `<Guarded path="...">` in [`ui/src/App.jsx`](ui/src/App.jsx).

### Infrastructure stacks (load order, handled by [`Infra/deploy.sh`](Infra/deploy.sh))

```
00-bootstrap  → SAM template bucket + CFN service role
01-network    → VPC, 1 public + 2 private subnets, NAT, gateway endpoints, SGs
02-security   → 2 KMS CMKs (DataAtRest, DynamoDB) + 2 Lambda IAM roles
03-identity   → Cognito User Pool + SPA client + Hosted UI domain
04-storage    → S3 (raw, processed) + 4 DDB tables + OSS collection + VPC endpoint
05-compute    → processing_pipeline Lambda + 2 ECR repos (master, zscaler)  [SAM]
06-api        → API GW + api_handler Lambda + Function URL                  [SAM]
09-agentcore  → IAM role + SG + 2 ECR repos (sharepoint, awsconfig)
10-ui-hosting → Private S3 + CloudFront (OAC) + optional WAFv2 for the SPA
```

`07-bedrock` and `08-observability` are intentionally **deferred** — KB is created via [`scripts/setup_bedrock_kb.py`](scripts/setup_bedrock_kb.py), observability is later. The KB resource itself is therefore not in CFN.

After the stacks land, `deploy.sh` runs [`Infra/post_deploy_ui.py`](Infra/post_deploy_ui.py): patches the Cognito client with the CloudFront callback URLs, writes `ui/.env.production`, runs `npm run build`, syncs `ui/dist` to S3, and invalidates CloudFront `/*`.

### Cross-cutting wiring that needs reading multiple files

- **AgentCore subnet:** `PrivateSubnet2` (in [`Infra/templates/01-network.yaml`](Infra/templates/01-network.yaml)) is provisioned in a different AZ from `PrivateSubnet1` specifically because AgentCore Runtime only supports physical AZ IDs `use1-az1/2/4`. [`scripts/deploy_agents.py`](scripts/deploy_agents.py) attaches runtimes to `PrivateSubnet2Id`.
- **Runtime name encoding:** `deploy_agents.py` builds runtime names with `.replace("-", "_")[:63]`. IAM resource ARNs in [`Infra/templates/09-agentcore.yaml`](Infra/templates/09-agentcore.yaml) for AgentCore memory + log-groups use `!Join + !Split` to mirror that conversion — keep them in sync.
- **JWT path:** `/chat` arrives at the Function URL with no APIGW authorizer; [`api_handler.py`](Infra/functions/api_handler/api_handler.py)'s `_caller_user_id` resolves the caller in three ways (API GW claims → Authorization-header JWT → direct-invoke fallback). All three paths must keep working.
- **MCP sidebar is cosmetic:** [`MCPChat.jsx`](ui/src/pages/MCPChat.jsx)'s `MCP_SERVERS` array is hardcoded UI candy. The chat send always calls the master orchestrator; the selected server doesn't change routing.

## Common commands

All commands assume working dir is the repo root unless noted.

### UI ([`ui/`](ui/))

```bash
cd ui
npm install                     # one-time
npm run dev                     # Vite dev server on http://localhost:5173/  (Cognito callback whitelist)
npm run build                   # production bundle into ui/dist/
npm run preview                 # serve the built bundle
npm test                        # Vitest, single run
npm run test:watch              # Vitest watch mode
npm run test:coverage           # Vitest with coverage
npx vitest run src/__tests__/helpers.test.js                    # single test file
npx vitest run -t 'buildConflictMatrix produces stable output'  # single test by name
```

Test files live at [`ui/src/__tests__/`](ui/src/__tests__/) (`helpers.test.js`, `edgeCases.test.js`, `mockData.test.js`).

### Infrastructure ([`Infra/`](Infra/))

```bash
cd Infra
./deploy.sh                     # provisions/updates all 8 stacks in order
./destroy.sh                    # tears them down in reverse order

aws cloudformation validate-template --template-body file://templates/01-network.yaml --region us-east-1
```

`deploy.sh` reads `ProjectName` from [`params/dev.json`](Infra/params/dev.json) — don't hardcode it. Each CFN update goes through change-sets (logs the diff before executing). SAM stacks use `--no-confirm-changeset`.

### Python scripts ([`scripts/`](scripts/))

```bash
cd scripts
python3 -m venv .venv && source .venv/bin/activate   # PEP 668: system pip is blocked
pip install -r requirements.txt

# One-time, after infra is up:
AWS_REGION=us-east-1 ENVIRONMENT=dev PROJECT=st21arbiter-poc python3 setup_bedrock_kb.py

# After KB + Memory exist:
KB_ID=<id> GUARDRAIL_ID=<id> MASTER_MEMORY_ID=<id> AWS_REGION=us-east-1 \
  python3 deploy_agents.py            # builds 4 agent images via CodeBuild → ECR → AgentCore Runtimes

python3 seed_mock_data.py             # seeds the 4 DDB tables for demo
```

`deploy_agents.py` provisions a CodeBuild project (Graviton/arm64) on first run, then reuses it. Never pass `--skip-build` on a fresh account — ECR repos don't carry a `:latest` tag.

### Agents — local iteration

```bash
cd agents/sharepoint_specialist
pip install -r requirements.txt
KB_ID=<kb_id> python agent.py         # serves AgentCore runtime locally on :8080
curl -X POST http://localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"<question>"}'
```

Each agent's `Dockerfile` wraps `python agent.py` with `opentelemetry-instrument` and requires `aws-opentelemetry-distro` in `requirements.txt` so AgentCore Observability traces populate.

## Where things live

| Concern | Location |
|---|---|
| CFN/SAM templates | [`Infra/templates/`](Infra/templates/) (`00-`…`09-`) |
| Stack parameters per env | [`Infra/params/<env>.json`](Infra/params/) |
| Lambda source | [`Infra/functions/api_handler/`](Infra/functions/api_handler/), [`Infra/functions/processing_pipeline/`](Infra/functions/processing_pipeline/) |
| Agent source + Dockerfiles | [`agents/<name>/`](agents/) |
| UI source | [`ui/src/`](ui/src/) (pages in `pages/`, hooks in `hooks/`, components in `components/`) |
| KB seed documents | [`BaselineFiles/`](BaselineFiles/) — synced to `s3://<env>-<project>-processed/` in deploy step |
| Provisioning scripts | [`scripts/`](scripts/) |
| Runbook | [`instructions/DEPLOYMENT.md`](instructions/DEPLOYMENT.md) |

## Conventions

- **Resource naming**: `<environment>-<project>-<type>` for dash-separated names; `<environment>_<project>_<type>` for AgentCore underscore-converted names. `environment=dev` and `project` come from [`Infra/params/dev.json`](Infra/params/dev.json).
- **Region**: `us-east-1` is hard-assumed throughout. Cross-region deploys require the template tweaks documented in DEPLOYMENT.md Appendix A.
- **No checked-in secrets**: `.env*` files, AWS access keys, KB-source PDFs in [`BaselineFiles/`](BaselineFiles/) are gitignored.
