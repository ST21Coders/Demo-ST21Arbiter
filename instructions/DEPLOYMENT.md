# ARBITER — Deployment Guide for a Fresh AWS Account

End-to-end instructions to deploy ARBITER (infrastructure + Bedrock Knowledge Base
+ Guardrail + 4 AgentCore Runtimes + API Lambda + React UI) into a brand-new
AWS account. Designed for a single engineer with AWS admin credentials.

Estimated wall-clock time: **2–3 hours** (most of it is CFN/SAM deploys and
CodeBuild image builds).

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
- The target region: **us-east-1** (other regions need template tweaks — see
  "Region change" appendix).

### Pre-flight cleanup of the copy
The folder ST21-ARBITER was copied from a working environment; a few stale
artifacts should be removed before you start:

```bash
cd <Folder-path-to-project>/ST21-ARBITER
rm -rf Infra/.aws-sam           # stale SAM build cache from the previous account
rm -rf ui/node_modules ui/dist  # will be rebuilt by `npm install`
rm -f  ui/.env.development      # contains the OLD account's IDs; regenerated in Step 9
```

---

## 1. Enable Bedrock model access

The agents need a Bedrock foundation model + the Titan embedding model for the
Knowledge Base. **Defaults shipped in source code:**

| Component | Model id baked into source |
|---|---|
| Master orchestrator + 3 specialists | `us.amazon.nova-2-lite-v1:0` (Amazon Nova — first-party, no Marketplace subscription required) |
| Bedrock KB embeddings | `amazon.titan-embed-text-v2:0` |

Override per-runtime via `MODEL_ID` env var (set by `scripts/deploy_agents.py`
when `MASTER_MODEL_ID=...` is exported).

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
> will fail at runtime with `INVALID_PAYMENT_INSTRUMENT` or
> `aws-marketplace:Subscribe` errors. The Bedrock Model Access wizard prompts
> for Marketplace acceptance — make sure to complete that flow, and confirm a
> valid payment method is on file in **AWS Billing → Payment methods**. Amazon
> Nova models are first-party and don't trigger this flow, which is why the
> source default ships as Nova.

Verify from the CLI:
```bash
aws bedrock list-foundation-models --region us-east-1 \
  --query 'modelSummaries[?contains(modelId, `nova-2-lite`) || contains(modelId, `titan-embed-text-v2`)].[modelId,modelLifecycle.status]' \
  --output table
```
Should show `ACTIVE`. The agents call models via cross-region inference
profiles (`us.amazon.nova-2-lite-v1:0` etc.) — those are usable once the
underlying model is granted.

---

## 2. Pick globally-unique names

Two resource types collide on global namespaces and will fail to create if
another account in the world already used the default names:

| Resource | Default | If collision, change |
|---|---|---|
| S3 buckets (`<env>-<project>-raw`, `-processed`, `-cfn-templates`) | `dev-st21arbiter-poc-*` | bump `Environment` or `ProjectName` |
| Cognito Hosted UI domain prefix | `poc-st21arbiter` | edit `CognitoDomainPrefix` in `Infra/params/dev.json` |

Current values in `Infra/params/dev.json` (edit if either name collides globally):
```json
{ "ParameterKey": "ProjectName",          "ParameterValue": "st21arbiter-poc" },
{ "ParameterKey": "CognitoDomainPrefix",  "ParameterValue": "poc-st21arbiter" }
```

> Whatever you pick here, every command below assumes `ProjectName=st21arbiter-poc`
> and `Environment=dev`. **If you change either, substitute throughout the
> guide** — the resource prefix is `<env>-<project>-…`.

---

## 3. Deploy the infrastructure stacks

Eight CloudFormation/SAM stacks in this order (handled by `Infra/deploy.sh`):

```
00-bootstrap     SAM template bucket + CFN service role
01-network       Single-AZ VPC, subnets, NAT, SGs
02-security      KMS CMKs (data + ddb) + 2 Lambda IAM roles
03-identity      Cognito User Pool + Client + Hosted UI domain
04-storage       S3 (raw + processed) + 4 DDB tables + OpenSearch collection
05-compute       processing-pipeline Lambda + 2 ECR repos (master, zscaler)
06-api           API Gateway + api_handler Lambda + Function URL
09-agentcore     IAM role + SG + 2 ECR repos (sharepoint, awsconfig) for AgentCore
```

(`07-bedrock` and `08-observability` are intentionally commented out in
`deploy.sh` — KB is created via script in Step 5; observability is deferred.)

```bash
cd ST21-ARBITER/Infra
./deploy.sh
```

**Expected time:** ~10–15 min total. Bootstrap stack takes the longest first
time (NAT gateway warm-up; the rest are quick).

**Verify**:
```bash
aws cloudformation list-stacks --region us-east-1 \
  --stack-status-filter CREATE_COMPLETE \
  --query 'StackSummaries[?contains(StackName, `st21arbiter-poc`)].[StackName,StackStatus]' --output table
```
You should see 8 stacks in `CREATE_COMPLETE`.

**Gotchas you may hit** (all known from this codebase's history):

| Symptom | Cause | Fix |
|---|---|---|
| `ROLLBACK_COMPLETE` on bootstrap | Pre-existing S3 bucket with the same name (someone else's account took it) | Pick a different ProjectName per Step 2, re-run |
| 09-agentcore deploys but you see `KMS Decrypt … DynamoDBKey … denied` later in agent logs | Stack is already corrected in this repo. If you patched anything, ensure 09-agentcore.yaml's `KMSDecrypt` statement includes `DynamoDBKeyArn` import |
| `AlreadyExists` on Cognito domain | `CognitoDomainPrefix` collision | Edit params/dev.json, re-run |
| Step 7 AgentCore runtime `CREATE_FAILED` with "subnets in unsupported availability zones" | AgentCore Runtime requires specific physical AZ IDs (us-east-1: `use1-az1`/`az2`/`az4`). Your account's `us-east-1a` may map to an unsupported AZ ID — the mapping is randomized per-account. | `01-network.yaml` provisions a second `PrivateSubnet2` in `us-east-1b` (defaults to `use1-az1`) specifically for AgentCore; `deploy_agents.py` reads `PrivateSubnet2Id` to attach runtimes. Verify your account's mapping with `aws ec2 describe-availability-zones`; if `us-east-1b` is also unsupported, override `PrivateSubnet2AZ` in `params/dev.json`. |

---

## 4. Re-seed the raw S3 bucket with demo policy docs

The Bedrock KB indexes from `s3://<env>-<project>-processed/`. The repo doesn't
include the seed PDFs, but a copy lives at `BaselineFiles/` in the source
project root. Mirror them into the processed bucket:

```bash
# From the project root that has BaselineFiles/
aws s3 sync BaselineFiles/ s3://dev-st21arbiter-poc-processed/ \
  --region us-east-1 \
  --exclude "generate_tree.py"
```

Verify (~20 objects, ~200 KB):
```bash
aws s3 ls s3://dev-st21arbiter-poc-processed --recursive --summarize | tail -3
```

If `BaselineFiles/` isn't present in your ST21-ARBITER copy, drop any PDFs/JSON/TXT
documents you want indexed into that bucket. The KB ingestion will pick them up.

---

## 5. Set up the Bedrock Knowledge Base + Guardrail

```bash
cd ST21-ARBITER/scripts

# Use a venv — modern Homebrew / Ubuntu Python blocks system-wide pip installs
# (PEP 668 "externally-managed-environment"). venv is in stdlib, no extra tools.
python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt        # boto3 + opensearch-py + requests-aws4auth

AWS_REGION=us-east-1 \
  ENVIRONMENT=dev \
  PROJECT=st21arbiter-poc \
  python3 setup_bedrock_kb.py
```

> Keep the venv activated for any other `python3 scripts/...` commands later
> in this guide (Steps 7, 10, 12). Re-activate with `source scripts/.venv/bin/activate`
> if you open a new shell.

This script (idempotent) does:
1. Flips the OpenSearch network policy to `AllowFromPublic=true` so it can be
   reached from the CLI (dev convenience — revert for prod).
2. Creates the vector index `policy-vectors`.
3. Creates the KB IAM role `dev-st21arbiter-poc-kb-role`.
4. Adds the role + your caller ARN to the OpenSearch data access policy.
5. Creates the **Knowledge Base** `dev-st21arbiter-poc-policy-kb`.
6. Attaches the S3 data source pointing at `dev-st21arbiter-poc-processed`.
7. Creates the **Guardrail** `dev-st21arbiter-poc-guardrail` (content filters,
   PII anonymization, denied topics).

**Capture the output** — it prints IDs like below. Capture these values, you'll need next:

```
{
  "knowledgeBaseId": "OTK5NXYZSP",
  "dataSourceId":    "180HKABCDE",
  "guardrailId":     "xuz7pppqqqr"
}
```

Then trigger the initial ingestion job (the script doesn't auto-trigger it):

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
20 small files take ~30 s.

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

## 7. Build & deploy the 4 AgentCore Runtimes

`scripts/deploy_agents.py` does everything — provisions a CodeBuild project
(Graviton, native arm64), builds each agent image, pushes to ECR, then
creates/updates the AgentCore Runtimes and patches the api_handler Lambda
with the resulting master ARN.

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

**Verify**:
```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[?contains(agentRuntimeName, `dev_st21arbiter_poc`)].[agentRuntimeName,status]' --output table
```
Four runtimes in `READY` state.

```bash
aws lambda get-function-configuration --function-name dev-st21arbiter-poc-api-handler \
  --region us-east-1 \
  --query 'Environment.Variables.{master:MASTER_AGENT_RUNTIME_ARN,mem:MEMORY_ID}'
```
Both fields populated.

**Gotcha note**: `--skip-build` uses `:latest` which doesn't exist in our ECR
repos (we tag with timestamps). Always run without `--skip-build` for fresh
account deploys.

---

## 8. Cognito setup — callback URLs and a test user

### 8.1 Whitelist localhost as a callback URL
```bash
USER_POOL_ID=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-UserPoolId'].Value" --output text)
CLIENT_ID=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-UserPoolClientId'].Value" --output text)

aws cognito-idp update-user-pool-client \
  --user-pool-id "$USER_POOL_ID" \
  --client-id "$CLIENT_ID" \
  --callback-urls "http://localhost:5173/callback" "https://example.cloudfront.net/callback" \
  --logout-urls "http://localhost:5173/" "https://example.cloudfront.net/" \
  --allowed-o-auth-flows code \
  --allowed-o-auth-scopes openid email profile \
  --allowed-o-auth-flows-user-pool-client \
  --supported-identity-providers COGNITO \
  --explicit-auth-flows ALLOW_REFRESH_TOKEN_AUTH ALLOW_USER_PASSWORD_AUTH ALLOW_USER_SRP_AUTH \
  --region us-east-1
```

### 8.2 The 4 demo persona users

`03-identity.yaml` provisions 4 Cognito users (one per persona) and attaches
each to its group. `deploy.sh` then runs `admin-set-user-password --permanent`
using the **shared** `DEMO_PASSWORD` env var you exported on the deploy line.

| Email (username) | Group | Persona |
|---|---|---|
| `emp_sarah@meridianinsurance.com` | `employee` | Sarah Chen — Analyst Chat only |
| `grc_priya@meridianinsurance.com` | `grc` | Priya Nair — Dashboard, Findings, Heatmap, Governance, Audit, Analyst Chat |
| `soc_marcus@meridianinsurance.com` | `soc` | Marcus Webb — Dashboard, Findings, Heatmap, Actions, Audit, Analyst Chat |
| `ciso_diana@meridianinsurance.com` | `ciso` | Diana Osei — all pages |

To run the deploy with passwords applied in one shot (choose your own password
matching the pool policy: 14+ chars, upper, lower, number, symbol):
```bash
DEMO_PASSWORD='<your-shared-demo-password>' ./deploy.sh
```

If `DEMO_PASSWORD` is unset, the deploy still creates the users but leaves
them in `FORCE_CHANGE_PASSWORD` state (Cognito Hosted UI then reports
"Invalid username or password" on sign-in — see Appendix C). To set passwords
after the fact:
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

# Resolve everything from CFN
API_URL=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-ApiEndpoint'].Value" --output text)
CHAT_URL=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-ChatFunctionUrl'].Value" --output text)
USER_POOL_ID=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-UserPoolId'].Value" --output text)
CLIENT_ID=$(aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-UserPoolClientId'].Value" --output text)
# Cognito domain prefix matches the param you set in Step 2:
DOMAIN_PREFIX="poc-st21arbiter"

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

> If `npm run dev` reports the port is taken and falls back to `5174`, another
> Vite server (perhaps from an older copy of this project) is holding `5173`.
> The Cognito callback URL is whitelisted only for `5173`, so sign-in will
> fail from `5174`. Run `lsof -iTCP:5173 -sTCP:LISTEN` to find the offender,
> kill its `npm`/`vite` PIDs, then `npm run dev` again.

---

## 10. Seed mock data into DynamoDB (Dashboard / Action Center / Audit)

These tables are empty after Step 3. The UI pages (`Dashboard`, `Findings`,
`Action Center`, `Audit Logs`) will look empty until you load some rows.

Script lives at `scripts/seed_mock_data.py` if you keep one around, or run
this inline:

```
python3 scripts/seed_mock_data.py
```

(The full canonical mock set lives in `ui/src/mockData.js` — copy more
conflicts/CRs from there if you want a richer demo.)

---

## 11. Smoke test in the browser

Open **http://localhost:5173/** in a browser.

| Check | Expected |
|---|---|
| Land on Dashboard | Eventually redirects to Cognito Hosted UI |
| Sign in with the email + password from Step 8.2 | Redirects to `/callback`, briefly shows "exchanging…", lands on Dashboard |
| Dashboard | Shows seed conflicts with severity breakdown |
| **Findings** | Shows the 3 conflicts |
| **Action Center** | Shows 1 CR |
| **Audit Logs** | Shows 2 entries |
| **MCP Chat** → click `+ New` → send "What does the acceptable use policy say?" | ~30–60 s cold start, then a real reply citing the seed docs |
| Refresh page → MCP Chat sidebar | The session you just had appears |
| Click the session | Messages reload from AgentCore Memory in chronological order |
| **Analyst Chat** | Same multi-turn behavior, separate `sessionIdRef` |

---

## 12. Smoke test from the CLI (optional)

```bash
# Direct Lambda invoke (bypasses Cognito)
aws lambda invoke --function-name dev-st21arbiter-poc-api-handler --region us-east-1 \
  --payload "$(echo -n '{"httpMethod":"GET","path":"/health"}' | base64)" \
  /tmp/h.json && cat /tmp/h.json

# Chat round-trip via Function URL (gets a JWT for the test user first)
ID_TOKEN=$(aws cognito-idp initiate-auth --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters "USERNAME=$EMAIL,PASSWORD=$PASSWORD" \
  --region us-east-1 --query 'AuthenticationResult.IdToken' --output text)

curl -s -X POST "$CHAT_URL""chat" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Quick sanity test.","session_id":"cli-smoke-1"}' \
  | python3 -m json.tool
```

---

## 13. Teardown (optional)

To remove everything when you're done:

```bash
# 1. Delete AgentCore Runtimes
for r in $(aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[?contains(agentRuntimeName, `dev_st21arbiter_poc`)].agentRuntimeId' --output text); do
  aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$r" --region us-east-1
done

# 2. Delete Memory resource
aws bedrock-agentcore-control delete-memory --memory-id <MEMORY_ID> --region us-east-1

# 3. Delete Knowledge Base (also drops data source automatically)
aws bedrock-agent delete-knowledge-base --knowledge-base-id <KB_ID> --region us-east-1

# 4. Delete Guardrail
aws bedrock delete-guardrail --guardrail-identifier <GUARDRAIL_ID> --region us-east-1

# 5. Empty + delete S3 buckets (raw, processed, cfn-templates)
for b in dev-st21arbiter-poc-raw dev-st21arbiter-poc-processed dev-st21arbiter-poc-cfn-templates; do
  aws s3 rm "s3://$b" --recursive
  # versioned buckets: also purge versions + delete markers (see DELETION-NOTES below)
  aws s3api delete-bucket --bucket "$b" --region us-east-1
done

# 6. Delete IAM roles (these have DeletionPolicy: Retain in some templates)
aws iam delete-role-policy --role-name dev-st21arbiter-poc-kb-role --policy-name KBPolicy
aws iam delete-role --role-name dev-st21arbiter-poc-kb-role

# 7. Delete CFN stacks in reverse order
for s in 09-agentcore 06-api 05-compute 04-storage 03-identity 02-security 01-network 00-bootstrap; do
  aws cloudformation delete-stack --stack-name "dev-st21arbiter-poc-$s" --region us-east-1
  aws cloudformation wait stack-delete-complete --stack-name "dev-st21arbiter-poc-$s" --region us-east-1
done
```

**Versioned-bucket deletion note**: if `delete-bucket` errors with
`BucketNotEmpty` despite the recursive `rm`, the bucket has versioning enabled
— you also need to purge all object versions + delete markers via
`list-object-versions` + `delete-objects`. The `Infra/templates/04-storage.yaml`
intentionally has `DeletionPolicy: Retain` removed for these buckets in dev so
that future re-deploys don't leak orphans; the bootstrap-templates bucket and
the data buckets in 04-storage are the only ones to worry about.

---

## Appendix A — Region change

If you need to deploy to a region other than us-east-1:

1. Update `params/dev.json` if any region-specific CIDRs are present (they're not in this default config).
2. Pass `AWS_REGION=<region>` when running every command in this guide.
3. In `params/dev.json`, the `EmbeddingModelArn` is built dynamically from
   `${AWS::Region}` in 07-bedrock.yaml (which is currently skipped). The
   `setup_bedrock_kb.py` script derives it from `AWS_REGION` automatically.
4. Verify Bedrock model availability in your chosen region — Claude models
   are not in every region.
5. Whitelist callback URLs accordingly (`http://localhost:5173/callback` is
   region-agnostic; only the API GW + Function URL hostnames change).

---

## Appendix B — Architectural gotchas baked into this codebase

These are non-obvious fixes that exist in the current code; documented so you
don't accidentally regress them:

1. **OpenSearch GSI permission scope** — `02-security.yaml`'s `DDBReadWrite`
   IAM statement must include both `table/<env>-<project>-*` *and*
   `table/<env>-<project>-*/index/*`. The `Query` action on a GSI requires
   the explicit index ARN.

2. **DynamoDB-key KMS Decrypt for the agent role** —
   `09-agentcore.yaml`'s `KMSDecrypt` statement must include
   `DynamoDBKeyArn` (the sessions table is KMS-encrypted; without this,
   `PutItem`/`UpdateItem` from the master agent silently fails).

3. **API Gateway 4xx CORS** —
   `06-api.yaml` defines three `AWS::ApiGateway::GatewayResponse` resources
   (UNAUTHORIZED, ACCESS_DENIED, DEFAULT_4XX) that inject CORS headers into
   authorizer denials. Without them, the browser sees 401 responses as CORS
   failures and the SPA cannot react. Any future change to GatewayResponses
   requires a stage redeployment (SAM normally handles this).

4. **OpenTelemetry auto-instrumentation** — Every agent's
   `Dockerfile` wraps `python agent.py` with `opentelemetry-instrument`, and
   every `requirements.txt` includes `aws-opentelemetry-distro`. This is what
   makes the AWS console's **AgentCore Observability** tab populate.

5. **Function URL for /chat** — API Gateway has a hard 29 s integration
   timeout; agent fan-outs take 30–60 s. The `api_handler` exposes itself
   via both API Gateway *and* a Lambda Function URL (`AuthType: NONE`). The
   UI sends `/chat` requests to the Function URL but still attaches a
   Cognito JWT in the Authorization header; the Lambda decodes it manually
   (no signature verify — trusted issuer pattern for the demo).

6. **JWT-from-header parsing in `_caller_user_id`** —
   `functions/api_handler/api_handler.py` looks up the user in three places
   (API GW claims → Authorization header JWT → direct-invoke fallback). If
   you change auth, keep all three paths working.

7. **Bedrock SLR removed from OpenSearch data access policy** —
   `04-storage.yaml`'s `OpenSearchDataAccessPolicy.Principal` array does NOT
   include `arn:…:role/aws-service-role/bedrock.amazonaws.com/AWSServiceRoleForAmazonBedrock`.
   That role doesn't exist until a Bedrock KB is first used, so referencing
   it pre-emptively fails the EarlyValidation hook. If you later re-enable
   `07-bedrock.yaml`, re-add this principal at that time.

8. **deploy.sh's `--no-confirm-changeset`** — `Infra/deploy.sh` must use
   `--no-confirm-changeset` (not `--confirm-changeset`) for SAM stacks so
   the script is non-interactive.

9. **CodeBuild builds with `:latest` tag** — `deploy_agents.py` tags
   images with a Unix timestamp. The `--skip-build` flag falls back to
   `:latest` which doesn't exist; always run without that flag for fresh
   accounts.

10. **Master agent's `eventTimestamp` required** —
    `agents/master_orchestrator/agent.py` passes `datetime.now(timezone.utc)`
    to every `create_event` call; the boto3 SDK requires it.

---

## Appendix C — Where to look when something breaks

| Symptom | First place to check |
|---|---|
| API GW returns 401 with no CORS headers | GatewayResponses → force stage redeploy |
| `/chat` returns 500 from runtime, no app logs visible | `/aws/bedrock-agentcore/runtimes/<runtime>-DEFAULT` log group |
| Agent invocation works but no Observability traces | `aws-opentelemetry-distro` in requirements.txt + `opentelemetry-instrument` in Dockerfile CMD |
| `/chat` returns model-validation error | Model access not granted in console; OR using non-inference-profile model ID (must be `us.anthropic.…`) |
| `/conversations` returns empty for a logged-in user | Master agent wrote rows under `user_id=anonymous` (Authorization header missing); confirm Lambda sees `authorization` header in event |
| DDB Query on GSI fails with `AccessDenied` | IAM resource scope missing `/index/*` |
| Master writes to memory but DDB row not created | Agent role missing `dynamodb:PutItem` on sessions table OR missing `kms:Decrypt` on DynamoDB CMK |
| Cognito returns "Incorrect username" for the test user | Pool requires email as username; create user with email-format username |
| SAM build can't find Lambda source | Templates' `CodeUri` should point at `../functions/<name>/`, not `../src/<name>/` |
| Bootstrap stack collision (`AlreadyExists` on bucket or role) | A prior failed deploy left orphans — delete the role/bucket manually, re-run; the template has `Retain` removed in the current code so rollback should clean up cleanly going forward |
| `Sign-in failed: Cognito token exchange failed: 400` after the Hosted UI redirects you back | React `StrictMode` double-fires the `/callback` effect in dev. The auth code is single-use, so the second exchange always 400s. `ui/src/hooks/useAuth.js` guards `handleCallback` with a module-level in-flight promise — if the bug recurs, check that guard is intact |
| UI shows mock-looking data even though DDB tables are empty | An older Vite dev server bound to port 5173 is serving a different project. `lsof -iTCP:5173 -sTCP:LISTEN` to find the right PID, then kill it and restart `npm run dev` |
| Step 7 runtime `CREATE_FAILED` "unsupported availability zones" | AgentCore Runtime constraint (use1-az1/2/4 in us-east-1). `01-network.yaml` provisions `PrivateSubnet2` in a supported AZ; `deploy_agents.py` reads `PrivateSubnet2Id`. Verify your `PrivateSubnet2AZ` param maps to a supported physical AZ |

---

**End of deployment guide.** When you're done, ping me with any failure
output or the stack/runtime status and I'll triage from there.
