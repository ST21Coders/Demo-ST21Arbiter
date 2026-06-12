# ARBITER — Deployment Guide for a Fresh AWS Account

End-to-end instructions to deploy ARBITER (infrastructure + Bedrock Knowledge
Base + Guardrail + 4 AgentCore Runtimes + API Lambda + autonomous Scanner +
React UI) into a brand-new AWS account. Designed for a single engineer with
AWS admin credentials.

**Estimated wall-clock time:** 2–3 hours (most of it is CFN/SAM deploys and
CodeBuild image builds).

**Deploy model:** the platform is wired for a **two-pass `deploy.sh`** flow:

1. **First pass** — provisions every stack. Two stacks (`05-compute` and
   `11-scanner`) have feature flags that depend on IDs that don't exist yet
   (the Bedrock KB ID + the master AgentCore runtime ARN). They deploy
   anyway and run in a no-op fallback: files still move raw→processed, but
   the auto-ingest chain and the scheduled scanner are off.
2. **Out-of-band scripts** — `setup_bedrock_kb.py` (creates the KB +
   Guardrail) + `deploy_agents.py` (builds + ships the 5 AgentCore runtimes).
3. **Second pass** — after patching the resulting IDs into
   `Infra/params/dev.json`, re-run `./deploy.sh`. Both stacks pick up the new
   env vars; the F1 auto-detect chain and the scheduled scanner activate.

The deploy script prints a preflight banner showing which features are
currently enabled/disabled based on the params file, so you know what state
you're in at all times.

---

## 0. Prerequisites

### Local tooling

| Tool | Min. version | Check |
|---|---|---|
| AWS CLI v2 | 2.13+ | `aws --version` |
| Python | 3.13 | `python3 --version` |
| Node.js | 20+ | `node --version` |
| npm | 10+ | `npm --version` |
| SAM CLI | 1.130+ | `sam --version` |

### AWS account

- Admin-level credentials configured locally as the default AWS CLI profile.
- Target region: **us-east-1** (other regions need template tweaks — see
  Appendix A).

### Pre-flight cleanup of the copy

The repo may be a copy from a working environment; a few stale artifacts
should be removed before you start:

```bash
cd <Folder-path-to-project>/ST21-ARBITER
rm -rf Infra/.aws-sam           # stale SAM build cache from a previous account
rm -rf ui/node_modules ui/dist  # rebuilt by `npm install`
rm -f  ui/.env.development      # contains the OLD account's IDs; regenerated in Step 9
```

---

## 1. Enable Bedrock model access

The agents need a Bedrock foundation model + the Titan embedding model for
the Knowledge Base. **Defaults shipped in source code:**

| Component | Model id baked into source |
|---|---|
| Master orchestrator + 3 specialists | `us.amazon.nova-2-lite-v1:0` (Amazon Nova — first-party, no Marketplace subscription required) |
| Bedrock KB embeddings | `amazon.titan-embed-text-v2:0` |

Override per-runtime via the `MODEL_ID` env var (set by `scripts/deploy_agents.py`
when `MASTER_MODEL_ID=...` etc. are exported).

1. Open **AWS Bedrock console → Model access** in `us-east-1`.
2. Click **Modify model access** and enable at minimum:
   - `amazon.nova-2-lite-v1:0` (current default — first-party Amazon model)
   - `amazon.titan-embed-text-v2:0` (KB embeddings)
3. **Optional (Claude alternative)** — enable Anthropic models if you want
   higher-quality reasoning on the master orchestrator:
   - `anthropic.claude-haiku-4-5-20251001-v1:0`
   - `anthropic.claude-sonnet-4-6`

> **Marketplace subscription caveat** — Anthropic models are distributed via
> AWS Marketplace and need an active subscription. If you only click "Modify
> model access" without accepting the Marketplace terms, agent invocations
> fail at runtime with `INVALID_PAYMENT_INSTRUMENT` or
> `aws-marketplace:Subscribe` errors. The Bedrock Model Access wizard prompts
> for Marketplace acceptance — complete that flow and confirm a valid payment
> method is on file in **AWS Billing → Payment methods**. Amazon Nova models
> are first-party and don't trigger this flow, which is why the source
> default ships as Nova.

Verify from the CLI:

```bash
aws bedrock list-foundation-models --region us-east-1 \
  --query 'modelSummaries[?contains(modelId, `nova-2-lite`) || contains(modelId, `titan-embed-text-v2`)].[modelId,modelLifecycle.status]' \
  --output table
```

Should show `ACTIVE`. Agents call models via cross-region inference profiles
(`us.amazon.nova-2-lite-v1:0` etc.) — those become usable once the underlying
model is granted.

---

## 2. Pick globally-unique names

Two resource types collide on global namespaces and will fail to create if
another account already used the default names:

| Resource | Default | If collision, change |
|---|---|---|
| S3 buckets (`<env>-<project>-raw`, `-processed`, `-cfn-templates`) | `dev-st21arbiter-poc-*` | bump `Environment` or `ProjectName` |
| Cognito Hosted UI domain prefix | `poc-st21arbiter` | edit `CognitoDomainPrefix` in `Infra/params/dev.json` |

Current values in `Infra/params/dev.json`:

```json
{ "ParameterKey": "ProjectName",          "ParameterValue": "st21arbiter-poc" },
{ "ParameterKey": "CognitoDomainPrefix",  "ParameterValue": "poc-st21arbiter" }
```

> Every command below assumes `ProjectName=st21arbiter-poc` and
> `Environment=dev`. **If you change either, substitute throughout the
> guide** — the resource prefix is `<env>-<project>-…`.

### Params used by feature gates (set later, see Step 5/7)

These three params start **empty** on a fresh account and are populated after
the out-of-band scripts run. The first-pass `deploy.sh` works fine without
them; the second pass turns features on:

| Param | Source | Gates |
|---|---|---|
| `KbId` | output of `scripts/setup_bedrock_kb.py` (Step 5) | F1 auto-ingest chain in `05-compute` |
| `KbDataSourceId` | output of `scripts/setup_bedrock_kb.py` (Step 5) | F1 auto-ingest chain in `05-compute` |
| `MasterAgentRuntimeArn` | output of `scripts/deploy_agents.py` (Step 7) | Scheduled scanner in `11-scanner` |

---

## 3. First-pass infrastructure deploy

**Ten** CloudFormation/SAM stacks, in this order (handled by `Infra/deploy.sh`):

```
00-bootstrap     SAM template bucket + CFN service role
01-network       Single-AZ VPC, subnets, NAT, SGs (PrivateSubnet2 is AgentCore-pinned)
02-security      KMS CMKs (data + ddb) + 2 Lambda IAM roles
03-identity      Cognito User Pool + Client + Hosted UI domain + 4 demo users
04-storage       S3 (raw + processed) + 6 DDB tables + OpenSearch collection
                 (raw bucket emits ObjectCreated → EventBridge default bus)
05-compute       processing-pipeline Lambda + 2 ECR repos (master, zscaler)
                 + EventBridge cron + ObjectCreated rule
06-api           API Gateway + api_handler Lambda + Function URL (for /chat)
11-scanner       scanner Lambda + EventBridge cron (daily 06:00 UTC)
09-agentcore     IAM role + SG + 2 ECR repos (sharepoint, awsconfig) for AgentCore
10-ui-hosting    Private S3 + CloudFront (OAC) + optional WAFv2 for the SPA
```

(`07-bedrock` and `08-observability` are intentionally commented out — the KB
is created via script in Step 5; observability is deferred.)

```bash
cd ST21-ARBITER/Infra
DEMO_PASSWORD='<your-shared-demo-password>' ./deploy.sh
```

Setting `DEMO_PASSWORD` on the deploy line activates `set_demo_passwords`,
which flips the 4 Cognito persona users out of `FORCE_CHANGE_PASSWORD` state
in one shot. Skip it and Step 8.2 has to be run by hand. The password must
satisfy the pool policy: **14+ chars, upper, lower, number, symbol.**

**Expected time:** ~10–15 min total. The preflight banner at the top of the
run shows current feature-gate state:

```
[…] ──── Preflight: optional-feature state ────
[…]   ⚠ KbId/KbDataSourceId empty → F1 auto-ingest chain DISABLED …
[…]   ⚠ MasterAgentRuntimeArn empty → scheduled scanner DISABLED …
```

That's expected on the first pass. The deploy still completes; both stacks
just hold their no-op fallback until you patch the params.

### Verify the 10 stacks landed

```bash
aws cloudformation list-stacks --region us-east-1 \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query 'StackSummaries[?contains(StackName, `st21arbiter-poc`)].StackName' \
  --output text | tr '\t' '\n' | sort
```

You should see all 10 stack names. After this point the rest of the guide
runs against a healthy infra baseline.

### Gotchas at this step

| Symptom | Cause | Fix |
|---|---|---|
| `ROLLBACK_COMPLETE` on bootstrap | Pre-existing S3 bucket with the same name | Pick a different `ProjectName` per Step 2, re-run |
| `AlreadyExists` on Cognito domain | `CognitoDomainPrefix` globally taken | Edit `params/dev.json`, re-run |
| 09-agentcore deploys but agent logs show `KMS Decrypt … DynamoDBKey … denied` | 09-agentcore's `KMSDecrypt` statement is missing `DynamoDBKeyArn` | Already fixed in this repo; don't regress |
| 11-scanner CFN error citing missing `ConflictsTableV2Name` / `ScanRunsTableName` export | 04-storage didn't deploy cleanly | Verify 04-storage in `UPDATE_COMPLETE`, re-run |
| AgentCore-related stack later says "unsupported availability zones" | Account's `us-east-1a` doesn't map to `use1-az1`/`az2`/`az4` | `01-network.yaml` provisions `PrivateSubnet2` in `us-east-1b` (defaults to `use1-az1` in this account). If your account's mapping differs, override `PrivateSubnet2AZ` in `params/dev.json` |

---

## 4. Seed the processed S3 bucket with the baseline corpus

The Bedrock KB indexes from `s3://<env>-<project>-processed/`. The seed
documents live at `BaselineFiles/` in the repo root (regenerated by
`scripts/generate_baseline_corpus.py` if needed):

```bash
# From the project root that has BaselineFiles/
aws s3 sync BaselineFiles/ s3://dev-st21arbiter-poc-processed/ \
  --region us-east-1 \
  --exclude "generate_tree.py" \
  --exclude "_archive/*" \
  --exclude "_source/*"
```

Verify (~20–30 objects):

```bash
aws s3 ls s3://dev-st21arbiter-poc-processed --recursive --summarize | tail -3
```

---

## 5. Create the Bedrock Knowledge Base + Guardrail

```bash
cd ST21-ARBITER/scripts

# venv is required — modern Homebrew / Ubuntu Python blocks system-wide pip
# (PEP 668 externally-managed-environment). venv is in stdlib.
python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt  # boto3 + opensearch-py + requests-aws4auth

AWS_REGION=us-east-1 \
  ENVIRONMENT=dev \
  PROJECT=st21arbiter-poc \
  python3 setup_bedrock_kb.py
```

> Keep the venv activated for every other `python3 scripts/...` command in
> this guide (Steps 7, 10). Re-activate with
> `source scripts/.venv/bin/activate` if you open a new shell.

This script (idempotent) does:

1. Flips the OpenSearch network policy to `AllowFromPublic=true` (dev
   convenience — revert for prod).
2. Creates the vector index `policy-vectors`.
3. Creates the KB IAM role `dev-st21arbiter-poc-kb-role`.
4. Adds the role + your caller ARN to the OpenSearch data access policy.
5. Creates the **Knowledge Base** `dev-st21arbiter-poc-policy-kb`.
6. Attaches the S3 data source pointing at `dev-st21arbiter-poc-processed`.
7. Creates the **Guardrail** `dev-st21arbiter-poc-guardrail` (content
   filters, PII anonymization, denied topics).

**Capture the output:**

```
{
  "knowledgeBaseId": "OTK5NXYZSP",
  "dataSourceId":    "180HKABCDE",
  "guardrailId":     "xuz7pppqqqr"
}
```

### 5.1 Trigger the initial ingestion (KB does NOT auto-ingest)

```bash
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id <KB_ID> \
  --data-source-id <DATA_SOURCE_ID> \
  --description "Initial seed ingestion" \
  --region us-east-1
```

Poll until `status: COMPLETE`:

```bash
aws bedrock-agent get-ingestion-job \
  --knowledge-base-id <KB_ID> --data-source-id <DATA_SOURCE_ID> \
  --ingestion-job-id <JOB_ID> --region us-east-1 \
  --query 'ingestionJob.{status:status,stats:statistics}' --output json
```

~30 s for ~20 small files.

### 5.2 Patch `KbId` + `KbDataSourceId` into params

This is the input that turns the F1 auto-detect chain on:

```bash
# Edit Infra/params/dev.json so these two params have non-empty values:
#   { "ParameterKey": "KbId",            "ParameterValue": "<KB_ID>" },
#   { "ParameterKey": "KbDataSourceId",  "ParameterValue": "<DATA_SOURCE_ID>" }
```

(You'll re-run `deploy.sh` in Step 7.5 to push these to 05-compute.)

---

## 6. Create the AgentCore Memory resource (master-only)

The master orchestrator uses long-term memory for conversation continuity.

```bash
aws bedrock-agentcore-control create-memory \
  --name dev_st21arbiter_poc_master_memory \
  --description "ARBITER master orchestrator long-term memory" \
  --event-expiry-duration 90 \
  --memory-strategies '[{
    "summaryMemoryStrategy": {
      "name": "ConversationSummary",
      "namespaces": ["/summaries/{actorId}/{sessionId}"]
    }
  }]' \
  --region us-east-1 \
  --query 'memory.id' --output text
```

Save the returned ID (looks like `dev_st21arbiter_poc_master_memory-XXXXXXXXXX`).

Wait until status is `ACTIVE` (~1–2 min):

```bash
aws bedrock-agentcore-control get-memory --memory-id <MEMORY_ID> --region us-east-1 \
  --query 'memory.status' --output text
```

---

## 7. Build & deploy the 5 AgentCore Runtimes

`scripts/deploy_agents.py` provisions a CodeBuild project (Graviton/arm64),
builds each agent image, pushes to ECR, then creates/updates the AgentCore
Runtimes and patches the api_handler Lambda with the resulting runtime ARNs
(master + each specialist, for the MCP page's per-agent chat routing).

> **JIRA secret (optional, for the JIRA specialist):** the `jira-specialist`
> runtime reads Jira credentials from Secrets Manager id `dev/st21arbiter-poc/jira`.
> Create it before this step, or the agent runs in "(JIRA not configured)" mode:
> ```bash
> aws secretsmanager create-secret --region us-east-1 \
>   --name dev/st21arbiter-poc/jira \
>   --secret-string '{"url":"https://<your>.atlassian.net","email":"<you>@<co>.com","api_token":"<token>"}'
> ```
> `url` = Jira Cloud base URL · `email` = Atlassian account (USER) · `api_token`
> from id.atlassian.com → Security → API tokens. Never commit these values.

> **ServiceNow secret (optional, for the ServiceNow specialist):** the
> `servicenow-specialist` runtime reads its instance URL + credentials from
> Secrets Manager id `dev/st21arbiter-poc/servicenow`. Create it before this
> step, or the agent runs in "(ServiceNow not configured)" mode (mock CHG ids).
> Basic auth (PDI) or OAuth2 client-credentials are both supported:
> ```bash
> # basic auth (simplest for a ServiceNow PDI)
> aws secretsmanager create-secret --region us-east-1 \
>   --name dev/st21arbiter-poc/servicenow \
>   --secret-string '{"instance_url":"https://devNNNNN.service-now.com","username":"admin","password":"<pwd>"}'
> # OR OAuth2 client-credentials (production-correct; register an OAuth app in
> # ServiceNow → System OAuth → Application Registry):
> #   '{"instance_url":"https://<inst>.service-now.com","client_id":"...","client_secret":"..."}'
> ```
> The integration user needs `cmdb_read` + `itil` roles (read CMDB / create
> change_request). `SERVICENOW_API_BASE` is read from the secret's `instance_url`
> (override via the runtime env only if needed).
>
> **Seed the demo CMDB (Plane-1 stand-in):** instead of running the Service
> Graph Connector for AWS, hand-load a small, relationship-rich CMDB on the PDI
> so impact analysis is demonstrable end to end. The CI set + ownership mirror
> the scan fixtures (`alb-mig-prod-claims-api-001`, `mig-prod-claims-data-primary`,
> `pcx-mig-prod-dev-001`):
> ```bash
> source scripts/.venv/bin/activate
> SN_INSTANCE_URL=https://devNNNNN.service-now.com SN_USERNAME=admin SN_PASSWORD=<pwd> \
>   python3 scripts/seed_servicenow_cmdb.py            # or --from-secret to reuse the secret above
> ```
> **Production path (not built here):** replace the seed with the **Service Graph
> Connector for AWS** (ServiceNow Store) fed by AWS Config Recorder + Aggregator
> over an STS cross-account role; it populates the same `cmdb_ci` / `cmdb_rel_ci`
> contract, so no ARBITER code changes are needed to switch.

```bash
cd ST21-ARBITER
KB_ID=<KB_ID> \
GUARDRAIL_ID=<GUARDRAIL_ID> \
MASTER_MEMORY_ID=<MEMORY_ID> \
AWS_REGION=us-east-1 \
python3 scripts/deploy_agents.py
```

**Expected time:** ~10–15 min. Each agent takes ~3–5 min through CodeBuild
plus ~30 s for the AgentCore Runtime update.

### Verify

```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[?contains(agentRuntimeName, `dev_st21arbiter_poc`)].[agentRuntimeName,status]' \
  --output table
```

Runtimes in `READY` state (sharepoint, awsconfig, zscaler, paloalto, structured, jira, servicenow, master).

```bash
aws lambda get-function-configuration --function-name dev-st21arbiter-poc-api-handler \
  --region us-east-1 \
  --query 'Environment.Variables.{master:MASTER_AGENT_RUNTIME_ARN,mem:MEMORY_ID}'
```

Both fields populated.

> **Gotcha:** `--skip-build` uses `:latest`, which doesn't exist in our ECR
> repos (we tag with Unix timestamps). Always run without `--skip-build` for
> fresh-account deploys.

### 7.5 Second-pass deploy.sh — turn the scheduled scanner on

Grab the master runtime ARN that `deploy_agents.py` just created:

```bash
MASTER_ARN=$(aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[?contains(agentRuntimeName, `master_orchestrator`)].agentRuntimeArn | [0]' \
  --output text)
echo "$MASTER_ARN"
```

Patch `Infra/params/dev.json` so `MasterAgentRuntimeArn` has this value
(plus the `KbId` + `KbDataSourceId` from Step 5.2), then re-run:

```bash
cd ST21-ARBITER/Infra
DEMO_PASSWORD='<your-shared-demo-password>' ./deploy.sh
```

The preflight banner should now read:

```
[…]   ✓ KbId=2ADHACW6LB / DataSource=KLUEZ1RNM5 → F1 auto-ingest chain ENABLED
[…]   ✓ MasterAgentRuntimeArn=arn:aws:bedrock-agentcore:… → scheduled scanner ENABLED
```

The second pass updates two stacks only:

- **05-compute** — patches `processing_pipeline` env vars with `KB_ID`,
  `KB_DATA_SOURCE_ID`, and `SCANNER_LAMBDA_NAME`. The F1 chain
  (S3 ObjectCreated → processing_pipeline → KB ingest → scanner) becomes
  end-to-end.
- **11-scanner** — patches the `MASTER_AGENT_RUNTIME_ARN` env var on the
  scanner Lambda. The EventBridge cron `cron(0 6 * * ? *)` (UTC, daily) +
  manual `POST /scan` calls now hit the real master in scan-mode.

Other stacks are no-ops on the second pass (change-set "didn't contain
changes" → skipped).

---

### 7.6 Re-deploying later — ALWAYS re-run `deploy_agents.py` after `deploy.sh`

**Gotcha (causes "most backend calls return 500"):** `deploy.sh` — and any
standalone `06-api` deploy — redeploys the `api_handler` Lambda from the SAM
template, which **resets these env vars to empty**:
`MASTER_AGENT_RUNTIME_ARN`, `SHAREPOINT_/AWSCONFIG_/ZSCALER_/PALOALTO_/JIRA_/SERVICENOW_RUNTIME_ARN`,
and `MEMORY_ID`. They are only ever populated by `scripts/deploy_agents.py`.
Symptom: data routes (`/findings`, `/dashboard`, `/audit`, `/reports`) keep
returning 200, but every **agent / chat** route fails. The AgentCore runtimes and
Memory are NOT deleted — only the Lambda env is blanked.

**Correct redeploy sequence:**

```bash
# 1. Infra + UI (rebuilds the SPA, syncs to S3, invalidates CloudFront)
cd Infra && DEMO_PASSWORD='<demo-pw>' ./deploy.sh

# 2. ALWAYS run this right after — re-patches the runtime ARNs + MEMORY_ID onto
#    the api_handler Lambda, and ships any agent code changes. Do NOT pass
#    --skip-build (the ECR repos carry timestamp tags, not :latest).
cd ../scripts && source .venv/bin/activate
KB_ID=<id> GUARDRAIL_ID=<id> GUARDRAIL_VERSION=1 MASTER_MEMORY_ID=<id> \
  AWS_REGION=us-east-1 python3 deploy_agents.py
```

**Fast re-patch (no agent rebuild)** — when you only changed infra and the agent
images are unchanged, re-patch the ARNs directly instead of a full
`deploy_agents.py` run:

```bash
python3 - <<'PY'
import boto3
r="us-east-1"; FN="dev-st21arbiter-poc-api-handler"
ctl=boto3.client("bedrock-agentcore-control",region_name=r); lam=boto3.client("lambda",region_name=r)
NAME_ENV={"master_orchestrator":"MASTER_AGENT_RUNTIME_ARN","sharepoint_specialist":"SHAREPOINT_RUNTIME_ARN",
"awsconfig_specialist":"AWSCONFIG_RUNTIME_ARN","zscaler_specialist":"ZSCALER_RUNTIME_ARN",
"paloalto_specialist":"PALOALTO_RUNTIME_ARN","jira_specialist":"JIRA_RUNTIME_ARN","servicenow_specialist":"SERVICENOW_RUNTIME_ARN"}
patch={e:rt["agentRuntimeArn"] for rt in ctl.list_agent_runtimes(maxResults=100)["agentRuntimes"]
       for s,e in NAME_ENV.items() if rt["agentRuntimeName"].endswith(s)}
m=[x for x in ctl.list_memories(maxResults=10)["memories"] if "master_memory" in x["id"]]
if m: patch["MEMORY_ID"]=m[0]["id"]
env=lam.get_function_configuration(FunctionName=FN)["Environment"]["Variables"]; env.update(patch)
lam.update_function_configuration(FunctionName=FN, Environment={"Variables":env})
print("re-patched:", list(patch))
PY
```

**Verify after either path:**

```bash
# ARNs non-empty
aws lambda get-function-configuration --function-name dev-st21arbiter-poc-api-handler \
  --region us-east-1 --query 'Environment.Variables.MASTER_AGENT_RUNTIME_ARN' --output text
# all runtimes READY
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[?contains(agentRuntimeName,`st21arbiter_poc`)].[agentRuntimeName,status]' --output table
# data + agent round-trip (direct invoke, bypasses Cognito)
aws lambda invoke --function-name dev-st21arbiter-poc-api-handler --region us-east-1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{"path":"/reports/catalog","httpMethod":"GET","requestContext":{}}' /tmp/r.json \
  --query 'StatusCode' --output text && python3 -c "import json;print(json.load(open('/tmp/r.json'))['statusCode'])"
```

> **Reporting Lambda note:** `06-api` bundles `reportlab` + `openpyxl` for the
> synchronous `/reports/*` + `/compliance/report` routes. Build on Linux (the
> CodePipeline `deploy` CodeBuild does this) or with `sam build --use-container`
> so the wheels match the Lambda architecture. If reportlab fails to bundle, PDF
> and ZIP reports return a clear `501` but CSV/XLSX/JSON still work.

---

## 8. Cognito setup — callback URLs and the 4 persona users

### 8.1 Whitelist localhost as a callback URL

```bash
USER_POOL_ID=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-UserPoolId'].Value" --output text)
CLIENT_ID=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-UserPoolClientId'].Value" --output text)
CLOUDFRONT_URL=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-UIBaseURL'].Value" --output text)

aws cognito-idp update-user-pool-client \
  --user-pool-id "$USER_POOL_ID" \
  --client-id "$CLIENT_ID" \
  --callback-urls "http://localhost:5173/callback" "${CLOUDFRONT_URL}callback" \
  --logout-urls   "http://localhost:5173/"         "${CLOUDFRONT_URL}" \
  --allowed-o-auth-flows code \
  --allowed-o-auth-scopes openid email profile \
  --allowed-o-auth-flows-user-pool-client \
  --supported-identity-providers COGNITO \
  --explicit-auth-flows ALLOW_REFRESH_TOKEN_AUTH ALLOW_USER_PASSWORD_AUTH ALLOW_USER_SRP_AUTH \
  --region us-east-1
```

`post_deploy_ui.py` (called by `deploy.sh`) already patches the CloudFront
URL into the callbacks list — this command above keeps `localhost:5173`
whitelisted so the Vite dev server works.

### 8.2 The 4 demo persona users

`03-identity.yaml` provisions 4 Cognito users (one per persona) and attaches
each to its group. If you passed `DEMO_PASSWORD=...` to `deploy.sh`,
`set_demo_passwords` flipped them all out of `FORCE_CHANGE_PASSWORD` state
already.

| Email (username) | Group | Persona |
|---|---|---|
| `emp_sarah@meridianinsurance.com` | `employee` | Sarah Chen — Analyst Chat only |
| `grc_priya@meridianinsurance.com` | `grc` | Priya Nair — Dashboard, Findings, Heatmap, Governance, Audit, Analyst Chat |
| `soc_marcus@meridianinsurance.com` | `soc` | Marcus Webb — Dashboard, Findings, Heatmap, Actions, Audit, Analyst Chat |
| `ciso_diana@meridianinsurance.com` | `ciso` | Diana Osei — all pages + cross-role CR approval override |

If `DEMO_PASSWORD` was unset, set passwords after the fact:

```bash
DEMO_PASSWORD='<your-shared-demo-password>'
for email in emp_sarah@meridianinsurance.com grc_priya@meridianinsurance.com \
             soc_marcus@meridianinsurance.com ciso_diana@meridianinsurance.com; do
  aws cognito-idp admin-set-user-password \
    --user-pool-id "$USER_POOL_ID" \
    --username "$email" \
    --password "$DEMO_PASSWORD" \
    --permanent --region us-east-1
done
```

Verify any persona can sign in:

```bash
aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters "USERNAME=ciso_diana@meridianinsurance.com,PASSWORD=$DEMO_PASSWORD" \
  --region us-east-1 \
  --query 'AuthenticationResult.IdToken' --output text | head -c 60
```

Should print the first 60 chars of a JWT.

---

## 9. Configure and start the UI

### 9.1 Generate `.env.development` from current stack outputs

```bash
cd ST21-ARBITER/ui

API_URL=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-ApiEndpoint'].Value" --output text)
CHAT_URL=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-ChatFunctionUrl'].Value" --output text)
USER_POOL_ID=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-UserPoolId'].Value" --output text)
CLIENT_ID=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-UserPoolClientId'].Value" --output text)
DOMAIN_PREFIX="poc-st21arbiter"   # matches CognitoDomainPrefix in params/dev.json

cat > .env.development <<EOF
VITE_API_URL=$API_URL
VITE_CHAT_URL=$CHAT_URL

VITE_COGNITO_REGION=us-east-1
VITE_COGNITO_USER_POOL_ID=$USER_POOL_ID
VITE_COGNITO_CLIENT_ID=$CLIENT_ID
VITE_COGNITO_DOMAIN=$DOMAIN_PREFIX.auth.us-east-1.amazoncognito.com
VITE_COGNITO_REDIRECT_URI=http://localhost:5173/callback
VITE_COGNITO_LOGOUT_URI=http://localhost:5173/
EOF
```

### 9.2 Install + run

```bash
npm install      # ~30–60 s
npm run dev      # serves on http://localhost:5173/
```

> If `npm run dev` reports the port is taken and falls back to `5174`,
> another Vite server (likely an older copy of this project) is holding
> `5173`. The Cognito callback URL is whitelisted only for `5173`, so
> sign-in fails from `5174`. Run `lsof -iTCP:5173 -sTCP:LISTEN`, kill those
> PIDs, then `npm run dev` again.

The production build is pushed to CloudFront by `Infra/post_deploy_ui.py`
(called automatically at the end of `deploy.sh`). The CloudFront URL is
printed in the final lines of the deploy log and exported as
`dev-st21arbiter-poc-UIBaseURL`.

---

## 10. Seed mock data into DynamoDB

The 6 DDB tables (`conflicts`, `conflicts-v2`, `scan-runs`, `change-requests`,
`audit-log`, `sessions`) are empty after Step 3. The UI pages will look bare
until you load some rows.

```bash
source scripts/.venv/bin/activate
python3 scripts/seed_mock_data.py
```

This seeds:

- **12 conflicts** (UC01..UC12, one per use case) — dual-written to
  `conflicts` (legacy SK-coupled) **and** `conflicts-v2` (PK-only with
  `severity-detected-index` / `domain-detected-index` / `scan_run-index`
  GSIs). The dashboard reads from V2.
- **14 compliant rows** in `conflicts-v2` (`compliant=true`,
  `severity=null`) — drives the "compliant alignments" KPI.
- **2 change-requests** (UC07 + UC08 archetypes) with full approver-chain
  state in `change-requests`.
- **8 audit-log entries** — `SCAN_COMPLETED`, `CR_CREATED`, `CR_APPROVED`,
  `INGESTION_COMPLETE`, etc. — populates the Dashboard's Recent Activity
  panel and the Audit Logs page.
- **1 `scan-runs` row** (`scan-seed-bootstrap`, status=COMPLETED) plus 4 more
  historical rows spanning the last 30 days — drives the open-conflicts
  trend line on the Dashboard.

The full canonical mock set lives in `ui/src/mockData.js`; copy more rows
from there if you want a richer demo.

---

## 11. Smoke-test in the browser

Open **http://localhost:5173/** (or the CloudFront URL printed by deploy.sh).

| Check | Expected |
|---|---|
| Land on Dashboard | Redirects to Cognito Hosted UI |
| Sign in as `ciso_diana@…` | Redirects to `/callback`, briefly shows "exchanging…", lands on Dashboard |
| **Dashboard KPIs** | "Policies Indexed", "Active Conflicts" (4 critical / 4 high / 4 medium), "Pending Approvals" populated from seed data |
| **Conflict Heat Map** | 6-row × 2-column grid showing UC counts per domain × source pair |
| **Open Conflicts Trend** | Line chart with 3 series (critical/high/medium) over 30 days — not flat |
| **Recent Activity panel** | Last 5 audit rows; auto-ingest rows formatted as "KB ingest: file.md" |
| **Run AI Scan** button | Click → spinner; polls `GET /scan-runs/{id}` every 2 s; "Scan complete" toast within ~45 s; KPIs refresh |
| **Findings** page | Shows the 12 seed conflicts; row click → /findings/:id detail with split-screen policy/enforcement panes |
| **Action Center** | Shows the 2 seed CRs; Diana can approve all (CISO override) → status flips, conflict transitions to RESOLVED |
| **Audit Logs** | Rows click-expand to show parsed JSON details; chevron flips ▶ → ▼ |
| **Data Pipeline** | Drop a `.md` / `.pdf` / `.txt` file onto the dropzone → 4 chips light up Raw → Processed → KB ingest → Scan within ~30–45 s |
| **MCP Chat** → New → "What does the acceptable use policy say?" | ~30–60 s cold start, then reply citing seed docs (KB hits) |
| **Analyst Chat** | Same multi-turn behavior, separate `sessionIdRef` |
| Refresh page → MCP Chat sidebar | Previous session appears; click → messages reload from AgentCore Memory |

### 11.1 F1 chain (upload → auto-ingest → scan) — explicit checks

After dropping a file on `/pipeline`:

```bash
# 1. File landed in raw bucket
aws s3 ls s3://dev-st21arbiter-poc-raw/uploads/ --recursive | tail -3

# 2. processing_pipeline copied it to processed bucket (within ~5s)
aws s3 ls s3://dev-st21arbiter-poc-processed/ --recursive | tail -3

# 3. KB ingestion job was kicked
aws bedrock-agent list-ingestion-jobs --knowledge-base-id <KB_ID> \
  --data-source-id <DATA_SOURCE_ID> --region us-east-1 \
  --max-results 3 --query 'ingestionJobSummaries[].{job:ingestionJobId,status:status,started:startedAt}'

# 4. Scanner auto-triggered with triggered_by="auto-ingest:<key>"
aws dynamodb scan --table-name dev-st21arbiter-poc-scan-runs \
  --region us-east-1 --max-items 3 \
  --query 'Items[].{id:scan_run_id.S,status:status.S,by:triggered_by.S}'
```

All four steps complete inside ~30–45 s end-to-end.

---

## 12. CLI smoke test (optional)

```bash
# Direct Lambda invoke (bypasses Cognito)
aws lambda invoke --function-name dev-st21arbiter-poc-api-handler --region us-east-1 \
  --payload "$(echo -n '{"httpMethod":"GET","path":"/health"}' | base64)" \
  /tmp/h.json && cat /tmp/h.json

# Chat round-trip via Function URL (gets a JWT for the test user first)
EMAIL=ciso_diana@meridianinsurance.com
ID_TOKEN=$(aws cognito-idp initiate-auth --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters "USERNAME=$EMAIL,PASSWORD=$DEMO_PASSWORD" \
  --region us-east-1 --query 'AuthenticationResult.IdToken' --output text)

CHAT_URL=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-ChatFunctionUrl'].Value" --output text)

curl -s -X POST "${CHAT_URL}chat" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Quick sanity test.","session_id":"cli-smoke-1"}' \
  | python3 -m json.tool

# Manual scan trigger via API Gateway
API_URL=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-ApiEndpoint'].Value" --output text)
curl -s -X POST "${API_URL}scan" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -m json.tool
# → {"scan_run_id":"scan-...","status":"RUNNING"}
```

---

## 13. API surface reference

API Gateway base + `/chat` Function URL together expose:

| Method | Path | Purpose |
|---|---|---|
| GET    | `/health` | liveness |
| POST   | `/scan` | async-invoke scanner Lambda; returns `{scan_run_id, status:"RUNNING"}` |
| GET    | `/scan-runs` | last N scan run rows (Dashboard trend) |
| GET    | `/scan-runs/{id}` | poll for scan completion |
| GET    | `/findings` | list conflicts (v2 if `CONFLICTS_TABLE_V2` set, else legacy) |
| GET    | `/findings/{conflict_id}` | finding detail (split-screen UI) |
| POST   | `/actions` | create change request linked to a conflict |
| POST   | `/actions/{cr_id}/approve` | approve as the calling persona; CISO override approves all PENDING |
| POST   | `/actions/{cr_id}/reject` | reject |
| POST   | `/actions/{cr_id}/execute` | execute approved CR; flips linked conflict → RESOLVED |
| POST   | `/actions/{cr_id}/escalate` | escalate |
| GET    | `/dashboard` | one-shot aggregate: KPIs + heatmap + last-scan + recent-activity + 30-day trend |
| GET    | `/audit` | audit log |
| GET    | `/conversations`, `/conversations/{id}/messages` | chat history (DDB sessions + AgentCore memory) |
| POST   | `/chat` | **Function URL only** — long-running agent call (bypasses APIGW 29 s timeout) |
| GET    | `/mcp-health` | MCP endpoint pings (UI status dots) |
| POST   | `/jira/tickets` | stub returning `{mock_ticket_key:"MIG-MOCK-NNNN"}` |
| POST   | `/uploads/presign` | presigned S3 PUT URL into the raw bucket |
| GET    | `/uploads/list` | list the caller's uploads in raw or processed bucket |

---

## 14. Teardown (optional)

Teardown is destructive and should only be run when you're sure. See
`Infra/destroy.sh` — it reverses the deploy in stack-reverse order. Run by
hand after confirming you've kept anything you need (S3 contents, KB, etc.);
do not embed it in CI.

The companion script does not delete the out-of-band resources (KB,
Guardrail, AgentCore Memory, AgentCore Runtimes) — those are created via
scripts in Steps 5–7 and must be removed manually:

```bash
# AgentCore Runtimes
for r in $(aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[?contains(agentRuntimeName, `dev_st21arbiter_poc`)].agentRuntimeId' --output text); do
  aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$r" --region us-east-1
done

# AgentCore Memory
aws bedrock-agentcore-control delete-memory --memory-id <MEMORY_ID> --region us-east-1

# Knowledge Base (drops data source automatically)
aws bedrock-agent delete-knowledge-base --knowledge-base-id <KB_ID> --region us-east-1

# Guardrail
aws bedrock delete-guardrail --guardrail-identifier <GUARDRAIL_ID> --region us-east-1
```

After that, the rest of `Infra/destroy.sh` can run safely.

> **Versioned-bucket note**: if `delete-bucket` errors with `BucketNotEmpty`
> despite a recursive `rm`, the bucket has versioning enabled — purge object
> versions + delete markers via `list-object-versions` + `delete-objects`.
> `Infra/templates/04-storage.yaml` has `DeletionPolicy: Retain` **removed**
> from the data buckets in dev so future re-deploys don't leak orphans.

---

## Appendix A — Region change

To deploy outside `us-east-1`:

1. Update `params/dev.json` if any region-specific CIDRs are present (the
   defaults aren't region-specific).
2. Pass `AWS_REGION=<region>` to every command in this guide.
3. `EmbeddingModelArn` in `params/dev.json` hard-codes `arn:aws:bedrock:us-east-1:...`
   — change the region prefix.
4. Verify Bedrock + AgentCore model availability in the target region.
5. Override `PrivateSubnet2AZ` to a physical AZ ID supported by AgentCore
   Runtime in that region.
6. Update callback URL whitelisting in Step 8.1 — `localhost:5173/callback`
   is region-agnostic; only the APIGW + Function URL hostnames change.

---

## Appendix B — Architectural gotchas baked into this codebase

Non-obvious fixes that exist in the current code, documented so you don't
accidentally regress them:

1. **DynamoDB GSI permission scope** — `02-security.yaml`'s `DDBReadWrite`
   IAM statement must include both `table/<env>-<project>-*` *and*
   `table/<env>-<project>-*/index/*`. The `Query` action on a GSI requires
   the explicit index ARN.

2. **DynamoDB-key KMS Decrypt for the agent role** — `09-agentcore.yaml`'s
   `KMSDecrypt` statement must include `DynamoDBKeyArn`. Without it,
   `PutItem`/`UpdateItem` on the sessions table silently fails.

3. **API Gateway 4xx CORS** — `06-api.yaml` defines three
   `AWS::ApiGateway::GatewayResponse` resources (UNAUTHORIZED, ACCESS_DENIED,
   DEFAULT_4XX) that inject CORS headers into authorizer denials. Without
   them, the browser sees 401s as CORS failures. Any future change to
   GatewayResponses requires a stage redeployment (SAM normally handles it,
   but adding new SAM `Events` to `ApiHandlerFunction` may not trigger a
   stage redeploy — if a new route returns 403/Missing Authentication Token
   after deploy, force one with
   `aws apigateway create-deployment --rest-api-id <id> --stage-name dev`).

4. **Bedrock IAM action prefix** — `bedrock-agent:StartIngestionJob` looks
   correct (matches the boto3 client name) but **the IAM action is
   `bedrock:StartIngestionJob`**. `02-security.yaml`'s
   `ProcessingPipelineRole` policy uses `bedrock:` for the ingestion-job
   actions; don't change it back.

5. **OpenTelemetry auto-instrumentation** — Every agent's `Dockerfile` wraps
   `python agent.py` with `opentelemetry-instrument`, and every
   `requirements.txt` includes `aws-opentelemetry-distro`. This is what
   populates the AWS console's **AgentCore Observability** tab.

6. **Function URL for /chat** — API Gateway has a 29 s integration timeout;
   agent fan-outs take 30–60 s. `api_handler` exposes itself via both APIGW
   *and* a Lambda Function URL (`AuthType: NONE`). The UI sends `/chat` to
   the Function URL but still attaches a Cognito JWT; the Lambda decodes it
   manually (trusted-issuer pattern for the demo).

7. **JWT-from-header parsing in `_caller_user_id`** —
   `functions/api_handler/api_handler.py` resolves the user in three places
   (APIGW claims → Authorization header JWT → direct-invoke fallback). If
   you change auth, keep all three paths working.

8. **F1 auto-ingest chain has 3 hops** — `RawBucket.NotificationConfiguration.EventBridgeConfiguration`
   in `04-storage.yaml` emits ObjectCreated to the default bus. The
   `ProcessingPipelineObjectCreatedRule` in `05-compute.yaml` is the
   subscriber. `processing_pipeline.py::_handle_single_object_event`
   does: raw→processed copy → `bedrock:StartIngestionJob` → wait up to 180 s
   for `COMPLETE` → `lambda:Invoke` the scanner with
   `triggered_by="auto-ingest:<key>"`. The KB_ID env var gates the whole
   thing; empty KB_ID means files still move but auto-ingest + scanner
   kick are skipped.

9. **Conflict dual-write** — `seed_mock_data.py` and `scanner_lambda` both
   write to `ConflictsTable` (legacy SK-coupled) **and** `ConflictsTableV2`
   (PK-only). The api_handler reads V2 when `CONFLICTS_TABLE_V2` env var is
   set; legacy is the fallback. Don't remove legacy until UI is fully
   migrated.

10. **CISO override on CR approve** — `_approve_change_request` lets a CISO
    user flip every PENDING non-NOTIFICATION row in one call (so a single
    sign-in can demo the full chain). Keep this; the alternative is
    requiring four browser sign-ins to walk through one CR.

11. **Master agent's `eventTimestamp` required** —
    `agents/master_orchestrator/agent.py` passes
    `datetime.now(timezone.utc)` to every `create_event` call; the boto3
    SDK requires it.

12. **Scanner Lambda graceful no-op** — `11-scanner.yaml` deploys with an
    empty `MasterAgentRuntimeArn` parameter and the Lambda logs an error
    + exits 0 if it's missing at invocation time. That's intentional so the
    first-pass deploy succeeds before `deploy_agents.py` runs.

13. **EventBridge Input on the daily cron** — `11-scanner.yaml`'s
    `ScannerSchedule` passes `Input: '{"triggered_by":"schedule"}'`. The
    handler reads this to set the scan-runs `triggered_by` attribute; the
    auto-ingest path uses `triggered_by="auto-ingest:<s3-key>"` instead so
    the Dashboard Recent Activity panel can distinguish them.

14. **CodeBuild builds use timestamped tags, not `:latest`** —
    `deploy_agents.py` tags images with a Unix timestamp; `--skip-build`
    falls back to `:latest`, which doesn't exist. Always run without that
    flag for fresh accounts.

15. **Bedrock SLR removed from OpenSearch data access policy** —
    `04-storage.yaml`'s `OpenSearchDataAccessPolicy.Principal` array does
    NOT include the Bedrock service-linked role
    (`AWSServiceRoleForAmazonBedrock`). That role doesn't exist until a
    Bedrock KB is first created, so referencing it pre-emptively fails the
    EarlyValidation hook. If you re-enable `07-bedrock.yaml`, re-add this
    principal there.

---

## Appendix C — Where to look when something breaks

| Symptom | First place to check |
|---|---|
| API GW returns 401 with no CORS headers | GatewayResponses → force stage redeploy |
| New route returns 403 / "Missing Authentication Token" after deploy | SAM didn't trigger stage redeploy; run `aws apigateway create-deployment --rest-api-id <id> --stage-name dev` |
| `/chat` returns 500 from runtime, no app logs visible | `/aws/bedrock-agentcore/runtimes/<runtime>-DEFAULT` log group |
| Agent works but no Observability traces | `aws-opentelemetry-distro` in `requirements.txt` + `opentelemetry-instrument` in Dockerfile CMD |
| `/chat` returns model-validation error | Model access not granted; OR using non-inference-profile model id (must be `us.amazon.…` / `us.anthropic.…`) |
| `/conversations` empty for a logged-in user | Master wrote rows under `user_id=anonymous` (Authorization header missing); confirm Lambda sees `authorization` header in event |
| DDB Query on GSI fails with `AccessDenied` | IAM resource scope missing `/index/*` |
| Master writes to memory but DDB row not created | Agent role missing `dynamodb:PutItem` on sessions OR missing `kms:Decrypt` on DynamoDB CMK |
| Cognito returns "Incorrect username" for the test user | Email-as-username; create user with email-format username |
| Upload to `/uploads/presign` returns 403 / "Failed to fetch" | API stage didn't redeploy when SAM Events were added; force `create-deployment` |
| Upload presign succeeds but the PUT to S3 fails CORS | `dev-st21arbiter-poc-raw` bucket CORS missing `http://localhost:5173`; patch with `aws s3api put-bucket-cors` |
| File lands in raw bucket but processed never appears | `ProcessingPipelineObjectCreatedRule` mistargeting; check EventBridge default bus rules in CloudWatch |
| File reaches processed but no scan-run row appears | `KB_ID` env var empty on `processing_pipeline` (first-pass deploy); patch `params/dev.json` and re-run `deploy.sh` |
| Scheduled cron fires but the master is never invoked | `MasterAgentRuntimeArn` empty on `scanner` Lambda; second-pass deploy hasn't happened |
| Dashboard Recent Activity shows nothing | `seed_mock_data.py` not run; OR audit-log table empty |
| Audit Logs rows don't expand on click | Hard-refresh; old bundle cached |
| `Sign-in failed: Cognito token exchange failed: 400` after Hosted UI callback | React `StrictMode` double-fires `/callback`; auth code is single-use. `ui/src/hooks/useAuth.js` guards `handleCallback` with a module-level in-flight promise — if the bug recurs, check that guard is intact |
| UI shows mock-looking data even though DDB has rows | An older Vite dev server bound to 5173 is serving a different project. `lsof -iTCP:5173 -sTCP:LISTEN` → kill → `npm run dev` |
| AgentCore runtime `CREATE_FAILED` "unsupported availability zones" | `PrivateSubnet2AZ` not mapping to `use1-az1/2/4`. Verify with `aws ec2 describe-availability-zones`, override the param |
| Preflight banner shows F1 DISABLED but you set KbId | Param value typo or wrong key name (must be `KbId`, not `KBId`); re-check `params/dev.json` |

---

**End of deployment guide.** When something fails, ping with the failure
output or the stack / runtime status and we'll triage from there.
