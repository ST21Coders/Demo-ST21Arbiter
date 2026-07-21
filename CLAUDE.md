# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository. Merge with the private [`CLAUDE.local.md`](CLAUDE.local.md) (account IDs, personal rules) when present.

Authoritative references:
- End-to-end deployment runbook: [`instructions/DEPLOYMENT.md`](instructions/DEPLOYMENT.md)
- Architecture diagram (draw.io): [`Documents/arbiter_st21_architecture.drawio`](Documents/arbiter_st21_architecture.drawio)
- AgentCore deep-dive: [`agents/README.md`](agents/README.md)
- RAG enhancements plan: [`Documents/rag_enhancements_plan.md`](Documents/rag_enhancements_plan.md)
- Original infra conventions (still relevant): [`infra-scripts-guide.md`](infra-scripts-guide.md)

## Project Overview

**ARBITER (ST21)** is a multi-agent **compliance / policy-conflict-detection** demo that has grown a second surface: **per-group data analytics RAG**. A React SPA talks to a single Lambda API which orchestrates a fleet of Bedrock AgentCore Runtimes (1 master orchestrator + 12 specialists = 13 runtimes) backed by a Bedrock Knowledge Base and Amazon S3 Vectors. Everything provisions into a single AWS account via CloudFormation/SAM. Single-AZ, dev-only.

Two things the product does:
1. **Compliance orchestration** — the Analyst page asks the **master orchestrator**, which fans out to specialists (`sharepoint_lookup`, `awsconfig_lookup`, `zscaler_lookup`, `paloalto_lookup`, `structured_lookup`, `sales_lookup`, `hr_lookup`, `jira_lookup`, `servicenow_lookup`) to find policy conflicts across SharePoint docs, AWS Config, Zscaler/Palo Alto rules, HR policy, and structured data, then recommends actions (Change Requests, JIRA tickets, ServiceNow, Confluence).
2. **Data-group RAG** — the **Data Pipeline** page ingests a project's group of files into S3 Vectors (+ Glue/Athena for tabular). Chatting a selected group routes to a **reusable** specialist pointed at *that group's* index/table: DocuSearch (unstructured) → `hr_specialist`; Structured Analytics (tabular + vector) → `sales_specialist`; CSV-only (Glue SQL) → `structured_specialist`. One agent serves any number of groups — no new agent per dataset.

Everything is glued by **Cognito JWT auth** and a **Persona/RBAC** model: the Cognito User Pool has 4 groups (`ciso`, `soc`, `grc`, `employee`) with one demo user each (`ciso_daiana@…`, `soc_marcus@…`, `grc_priya@…`, `emp_sarah@…`). The UI reads `cognito:groups` from the IdToken and pins `personaId` in [`ui/src/contexts/PersonaContext.jsx`](ui/src/contexts/PersonaContext.jsx); page access is enforced client-side by `<Guarded path="...">` in [`ui/src/App.jsx`](ui/src/App.jsx). No in-session persona switching.

## Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | Vite + React 18 SPA, `react-router-dom` 6, Tailwind CSS, `lucide-react` icons, `recharts`, `date-fns`; Vitest + Testing Library. Cognito Hosted UI for sign-in, in-memory state (no Redux). |
| **API** | Single Python 3.13 Lambda ([`api_handler`](Infra/functions/api_handler/api_handler.py)) behind **both** API Gateway (Cognito JWT authorizer) and a **Lambda Function URL** (`AuthType=NONE`, decodes JWT manually — used by `/chat` and data-pipeline ops to bypass API GW's 29 s timeout). |
| **Agents** | [Strands Agents](agents/) `Agent` wrapped in `bedrock_agentcore.runtime.BedrockAgentCoreApp` on port 8080; **Amazon Nova 2 Lite** (`us.amazon.nova-2-lite-v1:0`) default model (per-runtime override via `MODEL_ID`); Bedrock **Guardrail** (content/PII/denied-topics) on every model call; AgentCore **Memory** for master conversation continuity. `jira_specialist` is **MCP-based** (Strands `MCPClient` → `mcp-atlassian` over stdio). |
| **RAG library** | [`rag_src/arbiter_rag/`](rag_src/arbiter_rag/) — shared, single-source-of-truth library used by the notebooks, the `data_ingest` worker, and the specialist agents. Modules: `embeddings` (Titan Text v2), `vectors` (S3 Vectors), `retrieval` (hybrid), `lexical` (BM25), `rerank`, `athena_sql` (text-to-SQL), `chunking`, `loaders`, `serialization`, `guardrails`, `generation`, `ingest`, `evaluation`, `observability`, `preflight`, `config`. |
| **Data stores** | DynamoDB (5 tables: sessions, conflicts-v2, change-requests, audit-logs, data-jobs); S3 (raw, processed, ui-hosting); **S3 Vectors** buckets (`docs-vectors`, `analytics-vectors`, plus built-in `sales-vectors`/`hr-vectors`); AWS Glue Data Catalog + Athena (structured DB `dev_<project>_structured`); Bedrock Knowledge Base over OpenSearch Serverless. |
| **IaC / build** | CloudFormation + AWS SAM; CodeBuild (Graviton/arm64) builds agent + `data_ingest` container images → ECR; KMS CMKs for encryption. |

## Infrastructure components

### The four planes

1. **UI plane** ([`ui/`](ui/)) — SPA served from a private S3 bucket behind CloudFront (OAC). Two API surfaces: API Gateway for short DDB ops; **Lambda Function URL** for `/chat` and `/data-pipeline/ingest` (long-running). UI auto-falls back to bundled [`mockData.js`](ui/src/mockData.js) when `VITE_API_URL` is empty (`USE_MOCK` in [`ui/src/config.js`](ui/src/config.js)).

2. **API plane** ([`Infra/functions/`](Infra/functions/)) — the [`api_handler`](Infra/functions/api_handler/) Lambda serves ~40 routes (`/findings`, `/change-requests`, `/audit-logs`, `/conversations`, `/chat`, `/dashboard`, `/scan[-runs]`, `/config-drift/*`, `/servicenow/*`, `/jira/*`, `/compliance/*`, `/reports/*`, `/token-usage/*`, `/agent-status`, and the data-grouping/pipeline routes `/data-grouping/*`, `/data-pipeline/ingest`, `/data-jobs`). Supporting Lambdas: [`processing_pipeline`](Infra/functions/processing_pipeline/) (S3 → text-extract → KB sync), [`scanner`](Infra/functions/scanner/) (autonomous conflict re-scan), [`data_ingest`](Infra/functions/data_ingest/) (async chunk/embed → S3 Vectors worker; container-image Lambda), [`audit_cognito_subscriber`](Infra/functions/audit_cognito_subscriber/).

3. **Agent plane** ([`agents/`](agents/)) — 1 master + 12 specialists, each its own AgentCore Runtime. Master fans out via `@tool`s to specialists. Compliance specialists (`sharepoint`/`awsconfig`/`zscaler`/`paloalto`) hit the Bedrock KB; `structured`/`sales`/`hr` do S3-Vectors + Athena RAG (parameterized per request for any group's index/table); `jira`/`servicenow` are integration agents; `claim`/`fraud`/`debug` are lightweight advisory agents (Smart Rabbit catalog). Shared helpers in [`agents/_shared/`](agents/_shared/) (e.g. token-usage recording).

4. **Persona/RBAC plane** — see Project Overview.

### Infrastructure stacks (load order in [`Infra/deploy.sh`](Infra/deploy.sh))

```
00-bootstrap   → SAM template bucket + CFN service role
01-network     → VPC, 1 public + 2 private subnets (PrivateSubnet2 dedicated to AgentCore), NAT, endpoints, SGs
02-security    → KMS CMKs (DataAtRest, DynamoDB) + Lambda/exec IAM roles
03-identity    → Cognito User Pool + SPA client + Hosted UI domain + 4 persona users
04-storage     → S3 (raw, processed) + 5 DDB tables + OSS collection + S3 Vectors buckets + VPC endpoint
05-compute     → processing_pipeline Lambda + ECR repos                        [SAM]
06-api         → API GW + api_handler Lambda + Function URL + GatewayResponses  [SAM]
11-scanner     → autonomous scanner Lambda + EventBridge schedule              [SAM]
13-data-ingest → async data-ingest worker (container-image Lambda; needs Docker + ECR) [SAM]
09-agentcore   → IAM role + SG + ECR repos + S3-Vectors query grants for AgentCore
10-ui-hosting  → private S3 + CloudFront (OAC) + optional WAFv2 for the SPA
12-cicd-pipeline → (optional) CodePipeline V2 + CodeBuild that runs deploy.sh   [AWS CI/CD]
```

`07-bedrock` and `08-observability` are intentionally **deferred** — the KB is created out-of-band via [`scripts/setup_bedrock_kb.py`](scripts/setup_bedrock_kb.py) (OpenSearch index must pre-exist), dashboards/alarms come later. The KB resource is therefore not in CFN.

### Cross-cutting wiring (needs reading multiple files)

- **AgentCore subnet:** `PrivateSubnet2` (in [`01-network.yaml`](Infra/templates/01-network.yaml)) is in a different AZ from `PrivateSubnet1` because AgentCore Runtime only supports physical AZ IDs `use1-az1/2/4`. [`deploy_agents.py`](scripts/deploy_agents.py) attaches runtimes to `PrivateSubnet2Id`.
- **Runtime name encoding:** `deploy_agents.py` builds runtime names with `.replace("-", "_")[:63]`. IAM ARNs in [`09-agentcore.yaml`](Infra/templates/09-agentcore.yaml) (memory + log-groups) use `!Join + !Split` to mirror that — keep them in sync.
- **JWT path:** `/chat` hits the Function URL with no APIGW authorizer; `api_handler`'s `_caller_user_id` resolves the caller three ways (API GW claims → Authorization-header JWT → direct-invoke fallback). All three must keep working.
- **Per-group agent routing:** the [`data-jobs`](Infra/templates/04-storage.yaml) DDB table (GSI `by-project`) is the routing source of truth. `_handle_chat` calls `_resolve_group_vector_route` — a selected `data_group` with a SUCCEEDED vector-ingest job routes to the capability-matched agent (docusearch→hr, structured_analytics→sales) pointed at that group's index + Glue table. Group registry lives in `s3://<env>-<project>-processed/projects/<projectId>/metadata/project.json`. The MCPChat **and** AnalystView group dropdowns merge browser-`localStorage` groups with the remote `/data-grouping/projects` list.
- **`deploy.sh` blanks runtime ARNs:** a `06-api` redeploy resets the api_handler `*_RUNTIME_ARN`/`MEMORY_ID` env to `""`; re-run `deploy_agents.py` afterward (it re-patches them) or `/chat` 500s.
- **KMS on sessions:** the AgentCore role's `KMSDecrypt` in `09-agentcore.yaml` must include `DynamoDBKeyArn` or `sessions` `PutItem`/`UpdateItem` silently fail.

## CICD process (local and AWS)

### Local (primary, from a workstation)

```bash
cd Infra
./deploy.sh          # provisions/updates the stack set in order (change-sets for CFN, --no-confirm-changeset for SAM)
./destroy.sh         # tears down in reverse order
aws cloudformation validate-template --template-body file://templates/01-network.yaml --region us-east-1
```

`deploy.sh` reads `ProjectName` from [`params/dev.json`](Infra/params/dev.json), runs a **two-pass** flow (some stacks — 05-compute env, 11-scanner runtime ARN — need exports produced by later stacks, so a second pass wires them), then runs [`Infra/post_deploy_ui.py`](Infra/post_deploy_ui.py): patch Cognito callback URLs → write `ui/.env.production` → `npm run build` → sync `ui/dist` to S3 → invalidate CloudFront `/*`. It also sets the 4 demo passwords from `DEMO_PASSWORD`.

Agent + KB provisioning is a separate step ([`scripts/`](scripts/), activate the venv first):

```bash
cd scripts && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt   # PEP 668
AWS_REGION=us-east-1 ENVIRONMENT=dev PROJECT=st21arbiter-poc python3 setup_bedrock_kb.py   # one-time; then start-ingestion-job
KB_ID=<id> GUARDRAIL_ID=<id> GUARDRAIL_VERSION=1 MASTER_MEMORY_ID=<id> AWS_REGION=us-east-1 \
  python3 deploy_agents.py                       # builds agent images via CodeBuild(arm64) → ECR → AgentCore Runtimes
python3 deploy_agents.py --agents sales-specialist hr-specialist   # rebuild a subset in place (same ARNs)
python3 seed_mock_data.py                        # seed demo DDB tables
```

`deploy_agents.py` provisions a CodeBuild project (arm64) on first run, then reuses it. **Never** `--skip-build` on a fresh account — ECR repos carry no `:latest`. Pass `GUARDRAIL_VERSION` as an integer (`1`), not dev.json's display string.

### AWS-hosted (optional continuous deploy)

[`12-cicd-pipeline.yaml`](Infra/templates/12-cicd-pipeline.yaml) — a **CodePipeline V2** triggered by a push to the linked GitHub repo runs a single CodeBuild project that executes `Infra/deploy.sh` (the same script). Demo-grade; local `deploy.sh` remains the primary path.

### Automated tests (GitHub Actions)

[`.github/workflows/daily-tests.yml`](.github/workflows/daily-tests.yml) — cron `0 12 * * *` (UTC), Node 20 + Python 3.13. Runs **Vitest** (UI unit) and **pytest** (`rag_src`/adversarial). Defaults to **mock** mode ($0 AWS spend); `workflow_dispatch` can select `live`. Test dirs: [`ui/src/__tests__/`](ui/src/__tests__/), [`rag_src/tests/`](rag_src/tests/), [`tests/`](tests/), [`tests-e2e/`](tests-e2e/), [`tests-adversarial/`](tests-adversarial/).

### Local dev commands

```bash
cd ui && npm install && npm run dev            # Vite on http://localhost:5173/ (Cognito callback whitelist)
npm run build | npm run preview | npm test     # bundle | serve bundle | Vitest single run
cd agents/sales_specialist && pip install -r requirements.txt
KB_ID=<id> python agent.py                     # serve an AgentCore runtime locally on :8080
curl -X POST localhost:8080/invocations -H 'Content-Type: application/json' -d '{"prompt":"…"}'
```

## Folder Structure

```
Demo-ST21Arbiter/
├─ ui/                      Vite + React SPA
│  └─ src/{pages,hooks,components,contexts}, __tests__, mockData.js, config.js
├─ Infra/
│  ├─ templates/           CFN/SAM stacks 00-bootstrap … 13-data-ingest
│  ├─ functions/           Lambda source: api_handler, processing_pipeline, scanner,
│  │                       data_ingest, audit_cognito_subscriber
│  ├─ params/<env>.json    Stack params (ProjectName, CIDRs, GuardrailId, …)
│  ├─ deploy.sh / destroy.sh / post_deploy_ui.py
├─ agents/                 1 master + 12 specialists, each with agent.py + Dockerfile + requirements.txt
│  ├─ master_orchestrator/  sharepoint_/awsconfig_/zscaler_/paloalto_specialist   (KB compliance)
│  ├─ structured_/sales_/hr_specialist   (S3 Vectors + Athena RAG, per-group)
│  ├─ jira_/servicenow_specialist        (integrations; jira = MCP-based)
│  └─ _shared/             cross-agent helpers (token usage, …)
├─ rag_src/
│  ├─ arbiter_rag/         shared RAG library (single source of truth — notebook == production)
│  ├─ config/ data_generators/ eval/ notebooks/ tests/
├─ scripts/                provisioning + seeding (deploy_agents, setup_bedrock_kb, ingest_*_vectors,
│                          import_sales_data, seed_*, aws_health_check, …)
├─ BaselineFiles/          KB seed docs → synced to s3://<env>-<project>-processed/ (source PDFs gitignored)
├─ instructions/           DEPLOYMENT.md runbook
├─ Documents/              architecture .drawio, rag_enhancements_plan.md, proposal decks
├─ data/ docs/ testing/ tests/ tests-e2e/ tests-adversarial/
└─ CLAUDE.md / CLAUDE.local.md / infra-scripts-guide.md
```

## Conventions

- **Resource naming:** `<environment>-<project>-<type>` for dash-separated names; `<environment>_<project>_<type>` for AgentCore underscore-converted names (`deploy_agents.py` converts dashes→underscores). `environment=dev`, `project` from [`params/dev.json`](Infra/params/dev.json) — never hardcode it.
- **Region:** `us-east-1` is hard-assumed throughout. Cross-region needs the tweaks in DEPLOYMENT.md Appendix A.
- **notebook == production:** the specialist agents, the `data_ingest` worker, and the notebooks all import the same [`arbiter_rag`](rag_src/arbiter_rag/) modules. Fix RAG logic there once; never fork it into an agent.
- **Foundation model default is Nova 2 Lite** on all runtimes (first-party, no Marketplace subscription). Anthropic Claude models need an accepted AWS Marketplace subscription; each `agent.py` reads `MODEL_ID` from env for per-runtime override.
- **CFN discipline:** always `validate-template` a changed template; use **change-sets** (wired into `deploy.sh`), never bare `create/update-stack`. Confirm before destructive AWS actions (delete-stack, s3 rm, delete-agent-runtime, delete-knowledge-base/memory, kms schedule-key-deletion).
- **Every agent `Dockerfile`** wraps `python agent.py` with `opentelemetry-instrument` and pulls `aws-opentelemetry-distro` — required for AgentCore Observability traces. Specialist Dockerfiles must `COPY` any sibling `.py` modules or the runtime crashes `ModuleNotFound`.
- **UI gotchas:** dev server must bind port **5173** (Cognito callback whitelist); React `StrictMode` double-fires effects in dev (guard single-use API calls — see `useAuth.js` `handleCallback`); the SPA client has **no secret**.
- **No checked-in secrets:** `.env*`, AWS keys, and `BaselineFiles/` source PDFs are gitignored.
