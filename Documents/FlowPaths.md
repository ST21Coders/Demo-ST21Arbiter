# ARBITER Flow Paths — Function URL vs API Gateway

When to use each invocation surface, the per-endpoint routing, the actual request flow through the system, and the edge cases that bite.

---

## 1. Selection rule

The split lives in [useApi.js:262-271](../ui/src/hooks/useApi.js#L262-L271) on the client side and in the router at [api_handler.py:64-96](../Infra/functions/api_handler/api_handler.py#L64-L96) on the server side:

> **Function URL → `/chat` only. Everything else → API Gateway.**

The driver is **timeout, not auth**:

- API Gateway REST caps integration time at **29 seconds** (hard limit).
- Master-orchestrator fan-out to three specialist runtimes routinely exceeds that.
- Function URL allows up to **15 minutes** ([06-api.yaml:202-206](../Infra/templates/06-api.yaml#L202-L206)).

The **same `api_handler` Lambda answers both surfaces** — they differ in only three things:

| Aspect | API Gateway | Function URL |
|---|---|---|
| Auth | Cognito JWT authorizer (gateway validates signature, `exp`, `aud`) | `AuthType=NONE`; JWT decoded in Lambda code ([api_handler.py:316-329](../Infra/functions/api_handler/api_handler.py#L316-L329)) |
| Timeout | 29 s (hard cap) | 15 min |
| CORS on 4xx | `AWS::ApiGateway::GatewayResponse` injects headers ([06-api.yaml:135-165](../Infra/templates/06-api.yaml#L135-L165)) | Lambda's `_cors_headers` ([api_handler.py:380-386](../Infra/functions/api_handler/api_handler.py#L380-L386)) |

---

## 2. Per-endpoint routing

| Endpoint | Surface | Why |
|---|---|---|
| `POST /chat` | **Function URL** | Long-running agent fan-out (20–60 s typical, can spike higher) |
| `GET /findings` | API Gateway | DDB scan — sub-second |
| `GET /actions` | API Gateway | DDB scan |
| `GET /audit` | API Gateway | DDB scan |
| `GET /conversations` | API Gateway | DDB GSI query |
| `GET /conversations/{id}` | API Gateway | DDB `get_item` |
| `GET /conversations/{id}/messages` | API Gateway | AgentCore `list_events` — synchronous, fast |
| `GET /health` | API Gateway (unauth) | Smoke check; explicitly `Authorizer: NONE` on this route only ([06-api.yaml:292-299](../Infra/templates/06-api.yaml#L292-L299)) |

---

## 3. Flow paths

### Path 1 — short ops via API Gateway

```
Browser (SPA, IdToken in sessionStorage)
   │  GET /findings   Authorization: Bearer <IdToken>
   ▼
API Gateway REST  ─── Cognito JWT authorizer (validates signature, exp, aud) ───►  401 if invalid
   │  claims injected at event.requestContext.authorizer.claims
   ▼
api_handler Lambda (VPC, in PrivateSubnet1)
   │  _caller_user_id reads claims.sub
   ▼
DynamoDB (via Gateway Endpoint, no NAT hop)
   │
   ▼
JSON response  ──►  back through API GW (Lambda Proxy integration)  ──►  Browser
```

**Round-trip:** tens to hundreds of milliseconds.

---

### Path 2 — long-running `/chat` via Function URL

```
Browser (SPA)
   │  POST /chat   Authorization: Bearer <IdToken>
   │  body: { prompt, session_id, chat_type }
   ▼
Lambda Function URL  (AuthType=NONE — no gateway auth check)
   │  rawPath="/chat"
   ▼
api_handler Lambda
   │  _caller_user_id decodes JWT payload (no signature verify — trusted Cognito issuance)
   │  invoke_agent_runtime(MASTER_AGENT_RUNTIME_ARN, payload)
   ▼
Bedrock AgentCore — Master Orchestrator runtime (in PrivateSubnet2)
   │  Strands agent loops: thinks → calls tools
   ├──►  sharepoint_lookup  →  SharePoint specialist runtime  →  Bedrock KB  →  OpenSearch Serverless
   ├──►  awsconfig_lookup   →  AWS Config specialist runtime
   └──►  zscaler_lookup     →  Zscaler specialist runtime
   │  (Master aggregates findings, runs Nova 2 Lite for final answer)
   │  create_event       →  AgentCore Memory   (this turn's user + assistant messages)
   │  PutItem/UpdateItem →  DDB sessions table (index row + counters)
   ▼
{"result": "..."}  ──►  api_handler wraps as {"reply", "session_id"}  ──►  Browser
```

**Round-trip:** ~20–60 s typical; occasionally longer on cold-start of a specialist container.

---

## 4. Edge cases worth knowing

### 4.1 Mock mode

- Trigger: `VITE_API_URL` empty → `USE_MOCK=true` ([config.js:14](../ui/src/config.js#L14)).
- Effect: the SPA never makes a network call; `sendChat()` returns `(mock reply) You asked: ...` from [useApi.js:267-270](../ui/src/hooks/useApi.js#L267-L270).
- When to suspect it: you're seeing mock data unexpectedly after a redeploy — `.env.development` likely regenerated empty.

### 4.2 Function URL fallback

- Rule: `CHAT_URL = VITE_CHAT_URL || API_URL || ''` ([config.js:10](../ui/src/config.js#L10)).
- Effect: if you forget to set `VITE_CHAT_URL`, `/chat` quietly falls back to API Gateway and dies at 29 s.
- When to suspect it: chat works for short questions but 504s on multi-tool ones.

### 4.3 Direct AgentCore invocation

- Surface: **neither** — SigV4 against the AgentCore data plane.
- Bypasses the Lambda entirely.
- Only path that works with pure IAM (no Cognito).
- See Step A1–A3 in [API_Usage.md](API_Usage.md).

### 4.4 `/health`

- The only unauthenticated route on the API.
- Reachable from anywhere on the internet without a token.
- Purpose: smoke / health checks only.
- **Do not add business logic to it.**

---

# AgentCore in Private Subnets — How and Why

The diagram shows AgentCore runtimes inside `PrivateSubnet2`, but the placement model isn't obvious. This section explains it.

---

## 5. The VPC placement mechanism

AgentCore Runtime containers do **not** live inside `PrivateSubnet2` the way an EC2 instance does. AWS runs the container on **managed compute** outside your VPC and attaches an **Elastic Network Interface (ENI)** into your subnet. The ENI becomes the container's network identity — the same trick Lambda uses when VPC-attached.

So "AgentCore is in `PrivateSubnet2`" is shorthand for: **its outbound traffic exits via an ENI in your subnet, governed by your security group and route table.**

How this is wired in code:

```python
# scripts/deploy_agents.py:303-309
network_config = {
    "networkMode": "VPC",                    # ← the flag that plants the ENI
    "networkModeConfig": {
        "subnets": ["<PrivateSubnet2Id>"],   # ENI lands here
        "securityGroups": ["<AgentCoreSGId>"],
    },
}
agentcore_control.create_agent_runtime(... networkConfiguration=network_config ...)
```

Without `networkMode: "VPC"`, the runtime defaults to `PUBLIC` mode and runs on AWS shared compute with no VPC presence at all.

---

## 6. Component glossary

Every box that matters in the request path, in one place.

| Component | What it is | Purpose |
|---|---|---|
| **VPC** `10.20.0.0/20` | The private IP space | Network boundary; everything below lives inside it |
| **PrivateSubnet1** `10.20.4.0/22` · `use1-az6` | Subnet for Lambdas + OSS VPC interface endpoint | Hosts `api_handler` ENI and OpenSearch interface endpoint ENI |
| **PrivateSubnet2** `10.20.8.0/22` · `use1-az1` | Subnet dedicated to AgentCore | The only AZ available — AgentCore requires `use1-az1/2/4` |
| **NAT Gateway** (in PublicSubnet1) | Egress translator | Gives ENIs in private subnets outbound 443 to AWS services that don't have a VPC endpoint |
| **DDB Gateway Endpoint** | Route-table entry | DDB calls skip NAT, stay on the AWS backbone |
| **S3 Gateway Endpoint** | Route-table entry | S3 calls skip NAT |
| **OSS Interface Endpoint** | ENI in PrivateSubnet1 | OpenSearch Serverless reachable from inside VPC only |
| **LambdaSG** | SG on `api_handler` ENI | Egress 443 only (DDB / Bedrock / S3 / OSS) |
| **AgentCoreSG** | SG on AgentCore ENI | Egress 443 only ([09-agentcore.yaml:222-227](../Infra/templates/09-agentcore.yaml#L222-L227)) |
| **AgentCoreRuntimeRole** | IAM role assumed by `bedrock-agentcore.amazonaws.com` | What the agent container's AWS calls are signed as |
| **AgentCore Runtime** (4×) | Managed container hosting a Strands Agent | Runs the Python agent code; one per agent (master + 3 specialists) |
| **Bedrock KB** | Managed retrieval over OpenSearch | Vector search across policy PDFs |
| **Bedrock Guardrail** | Content / PII / denied-topics filter | Wraps every model call (request + response) |
| **AgentCore Memory** | Managed session store | Holds `(actorId, sessionId) → events`; supports `list_events` + summarization |
| **DDB `sessions` table** | Index of conversations | Lets the UI list conversations per user without scanning Memory |
| **Bedrock Foundation Model** | Nova 2 Lite inference endpoint | Generates the text; managed, regional, not in VPC |

---

## 7. Sequenced request flow

Follows `User → api_handler → AgentCore → KB + DDB + Guardrails + Memory → Model → User`, with the network mechanism at every hop.

### Step 7.1 — Browser to `api_handler`

```
[User browser, anywhere on internet]
        │
        │  POST /chat   Authorization: Bearer <Cognito IdToken>
        ▼
[Lambda Function URL endpoint]              ← public AWS-managed URL
        │
        │  (Lambda service routes to your function)
        ▼
[api_handler Lambda] — runs on ENI in PrivateSubnet1
   LambdaSG attached    ·    role: ApiHandlerRole
```

- The Function URL is **not** in the VPC — it's a public AWS endpoint. The Lambda's **execution environment** is, via an ENI in PrivateSubnet1.
- `_caller_user_id` decodes the JWT in-process to identify the user.

### Step 7.2 — `api_handler` calls AgentCore

```
[api_handler in PrivateSubnet1]
        │
        │  boto3: bedrock-agentcore.invoke_agent_runtime(arn=<MASTER_ARN>, payload=...)
        │  HTTPS 443 → bedrock-agentcore.us-east-1.amazonaws.com  (via NAT)
        ▼
[AgentCore Control Plane]
        │
        │  routes to the master runtime container
        ▼
[Master Orchestrator container] — managed compute, ENI in PrivateSubnet2
   AgentCoreSG attached    ·    role: AgentCoreRuntimeRole
```

- `api_handler` does not connect "directly" to the container — it calls the **AgentCore data-plane API**, and AgentCore dispatches into your container.
- The container's outbound calls originate from `PrivateSubnet2` via its ENI.

### Step 7.3 — Master fans out to specialists

```
[Master container in PrivateSubnet2]
        │
        │  Strands @tool functions: sharepoint_lookup / awsconfig_lookup / zscaler_lookup
        │  Each tool calls bedrock-agentcore.invoke_agent_runtime(specialist_arn, ...)
        ▼
[SharePoint Specialist]   [AWS Config Specialist]   [Zscaler Specialist]
       container                 container                  container
   all three ENI-attached to PrivateSubnet2, AgentCoreSG, same IAM role
```

- IAM permission for this lateral hop: `InvokeOtherAgentRuntimes` on `AgentCoreRuntimeRole` ([09-agentcore.yaml:98-105](../Infra/templates/09-agentcore.yaml#L98-L105)).

### Step 7.4 — Specialist fetches from KB

```
[Specialist container]
        │
        │  bedrock-agent-runtime.retrieve(knowledgeBaseId=KB_ID, query=...)
        │  HTTPS 443 → bedrock-agent-runtime.us-east-1.amazonaws.com  (via NAT)
        ▼
[Bedrock Knowledge Base — managed]
        │
        │  embeds the query → vector search
        ▼
[OpenSearch Serverless]
        │
        │  reachable ONLY via the OSS VPC Interface Endpoint in PrivateSubnet1
        │  data-access policy permits requests originating from inside the VPC
        ▼
   relevant policy chunks  →  back to specialist
```

- This is the load-bearing reason for VPC placement: OSS won't accept the KB's lookup unless it traverses your VPC endpoint.

### Step 7.5 — Master invokes the Foundation Model with Guardrail

```
[Master container]
        │
        │  Strands → BedrockModel.converse(model_id="us.amazon.nova-2-lite-v1:0",
        │                                  guardrailConfig={id: GUARDRAIL_ID, version: DRAFT})
        │  HTTPS 443 → bedrock-runtime.us-east-1.amazonaws.com  (via NAT)
        ▼
[Bedrock Runtime] — managed regional service
        │
        │  applies Guardrail on INPUT  (PII, denied topics, content categories)
        │  invokes Nova 2 Lite
        │  applies Guardrail on OUTPUT
        ▼
   completion text → back to master
```

- The model and the guardrail are **regional managed services**, not in your VPC. The agent calls them through 443 + NAT.

### Step 7.6 — Master persists the turn

Two writes, both from the master container's ENI in PrivateSubnet2:

```
A) Memory write (full conversational payload)
   [Master] → bedrock-agentcore.create_event(memoryId, actorId, sessionId, payload)
           ↓
   [AgentCore Memory — managed]
           ↓ (async strategy)
           summarization rolls events into /summaries/{actor}/{session}

B) Sessions-table index write (so the UI can list conversations)
   [Master] → dynamodb.PutItem / UpdateItem on dev-st21arbiter-poc-sessions
           ↓ via DDB Gateway Endpoint (no NAT hop)
   [DynamoDB]
```

- Memory holds the **content**; DDB holds a tiny index row (`session_id`, `user_id`, `title`, `message_count`).

### Step 7.7 — Response to user

```
[Master container] returns {"result": "<final answer>"}
        ▲
        │
[AgentCore Control Plane] delivers payload back to caller
        ▲
        │
[api_handler Lambda] wraps as {"reply": ..., "session_id": ...}
        ▲
        │
[Lambda Function URL] sends HTTP 200 with CORS headers
        ▲
        │
[User browser] renders the assistant message
```

---

## 8. Where each thing runs — cheat-sheet

| Component | Compute location | Network identity in your VPC? |
|---|---|---|
| Cognito Hosted UI / User Pool | AWS-managed, regional | No |
| CloudFront + S3 UI | AWS-managed, edge | No |
| API Gateway | AWS-managed, regional | No |
| Lambda Function URL (the URL itself) | AWS-managed | No |
| **`api_handler` Lambda execution** | AWS-managed | **Yes — ENI in PrivateSubnet1** |
| **AgentCore Runtime containers (×4)** | AWS-managed | **Yes — ENI in PrivateSubnet2** |
| Bedrock Foundation Model | AWS-managed, regional | No (called via NAT) |
| Bedrock Guardrail | AWS-managed, regional | No (called via NAT) |
| Bedrock Knowledge Base | AWS-managed, regional | No |
| OpenSearch Serverless | AWS-managed | Reached **through** OSS VPC interface endpoint in PrivateSubnet1 |
| DynamoDB | AWS-managed, regional | Reached **through** DDB Gateway Endpoint |
| S3 (UI bucket, KB processed bucket) | AWS-managed, regional | Reached **through** S3 Gateway Endpoint |
| AgentCore Memory | AWS-managed | No (called via NAT) |

So **two things actually project into your VPC**: the `api_handler` Lambda's ENI in PrivateSubnet1, and the AgentCore runtime containers' ENIs in PrivateSubnet2. Everything else is regional AWS service-plane traffic that those ENIs reach through NAT or a VPC endpoint — but all outbound calls **originate from inside your network**, which is the entire point of the VPC placement.

---

# AWS Resource ↔ Endpoint Mapping

The wire-level endpoints every component talks to, the AWS service identifier (for IAM / SigV4), the auth method, and how to resolve the actual value in this account.

---

## 9. Public / external-facing endpoints

These have account-specific URLs. The browser hits the first four directly.

| Resource | Endpoint pattern | Auth | How to resolve |
|---|---|---|---|
| **CloudFront distribution (UI)** | `https://<distribution-id>.cloudfront.net` | None (public read of SPA) | `aws cloudfront list-distributions --query "DistributionList.Items[?Comment=='dev-st21arbiter-poc-ui'].DomainName"` |
| **Cognito Hosted UI** | `https://poc-st21arbiter.auth.us-east-1.amazoncognito.com` | OAuth Authorization-Code + PKCE | Domain prefix set in [03-identity.yaml](../Infra/templates/03-identity.yaml) `CognitoDomainPrefix` |
| **API Gateway (REST)** | `https://<apiId>.execute-api.us-east-1.amazonaws.com/dev` | Cognito JWT in `Authorization: Bearer …` | CFN export `dev-st21arbiter-poc-ApiEndpoint` |
| **Lambda Function URL (`/chat`)** | `https://<urlid>.lambda-url.us-east-1.on.aws/` | `AuthType=NONE`; JWT decoded in Lambda | CFN export `dev-st21arbiter-poc-ChatFunctionUrl` |

Get them all in one shot:

```bash
aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?contains(Name, 'st21arbiter-poc') && (contains(Name, 'ApiEndpoint') || contains(Name, 'ChatFunctionUrl'))].[Name,Value]" \
  --output table
```

---

## 10. AWS service endpoints called from inside the system

Regional service hostnames. The SDK constructs these automatically when you pass `region_name="us-east-1"`. SigV4 is the auth on every one.

| Resource | Hostname | SigV4 service name | Called by |
|---|---|---|---|
| Cognito Identity Provider | `cognito-idp.us-east-1.amazonaws.com` | `cognito-idp` | Postman / SPA during `InitiateAuth` |
| Lambda (control & invoke) | `lambda.us-east-1.amazonaws.com` | `lambda` | CFN / CLI |
| **AgentCore data plane** | `bedrock-agentcore.us-east-1.amazonaws.com` | `bedrock-agentcore` | `api_handler` (master invoke); master agent (specialist invokes); also `create_event`, `list_events`, `retrieve_memory_records` |
| **AgentCore control plane** | `bedrock-agentcore-control.us-east-1.amazonaws.com` | `bedrock-agentcore-control` | `deploy_agents.py` only (create / update runtimes) |
| **Bedrock Runtime** (model inference) | `bedrock-runtime.us-east-1.amazonaws.com` | `bedrock-runtime` | Each agent's `BedrockModel.converse` |
| **Bedrock Agent Runtime** (KB retrieval) | `bedrock-agent-runtime.us-east-1.amazonaws.com` | `bedrock-agent-runtime` | Specialists hitting `retrieve` on the KB |
| **Bedrock Agent** (control) | `bedrock-agent.us-east-1.amazonaws.com` | `bedrock-agent` | `setup_bedrock_kb.py`, ingestion jobs |
| Bedrock (model listing, guardrail mgmt) | `bedrock.us-east-1.amazonaws.com` | `bedrock` | One-time setup; not in hot path |
| DynamoDB | `dynamodb.us-east-1.amazonaws.com` (resolved via Gateway Endpoint from VPC) | `dynamodb` | `api_handler`, master agent |
| S3 | `s3.us-east-1.amazonaws.com` (resolved via Gateway Endpoint from VPC) | `s3` | KB ingestion, UI deploy |
| **OpenSearch Serverless data plane** | `<collection-id>.us-east-1.aoss.amazonaws.com` (resolved via Interface Endpoint from VPC) | `aoss` | KB internally — your code never calls this directly |
| KMS | `kms.us-east-1.amazonaws.com` | `kms` | DDB CMK decrypt during DDB calls |
| ECR (container pulls) | `<account>.dkr.ecr.us-east-1.amazonaws.com` | `ecr` | AgentCore pulling agent images |
| CloudWatch Logs | `logs.us-east-1.amazonaws.com` | `logs` | Lambda + AgentCore log shipping |
| STS | `sts.us-east-1.amazonaws.com` | `sts` | All IAM-role assume operations |

---

## 11. VPC endpoints (private paths into AWS service hostnames)

These shortcut the public hostnames above so traffic stays on the AWS backbone instead of going out the NAT.

| AWS service | VPC endpoint type | Subnet placement | What uses it |
|---|---|---|---|
| DynamoDB | **Gateway endpoint** (prefix-list route) | Attached to private route table; reachable from both `PrivateSubnet1` and `PrivateSubnet2` | `api_handler` reads/writes; master agent writes to `sessions` |
| S3 | **Gateway endpoint** (prefix-list route) | Same as above | KB ingestion fetches; UI sync; agent KB document reads |
| OpenSearch Serverless | **Interface endpoint** (ENI in subnet) | `PrivateSubnet1` only (`OpenSearchSG`, ingress 443 from `LambdaSG` / `AgentCoreSG`) | Bedrock KB → OSS vector queries |

> Five further interface endpoints (Bedrock / SecretsManager / KMS / Logs / ECR) were **dropped in the dev trim** — those calls fall back through NAT. Re-add them by un-commenting the `VPCEndpoint` resources in [04-storage.yaml](../Infra/templates/04-storage.yaml) if you need full no-Internet egress.

---

## 12. Project-specific endpoint values

These exist only after deploy. Pull them once and cache in `.env`:

```bash
aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?contains(Name, 'st21arbiter-poc')].[Name,Value]" \
  --output table
```

The values you'll most often use:

| Logical name | CFN export | Used in |
|---|---|---|
| API Gateway base URL | `dev-st21arbiter-poc-ApiEndpoint` | `VITE_API_URL` |
| `/chat` Function URL | `dev-st21arbiter-poc-ChatFunctionUrl` | `VITE_CHAT_URL` |
| Cognito User Pool ID | `dev-st21arbiter-poc-UserPoolId` | `VITE_COGNITO_USER_POOL_ID`, Postman `InitiateAuth` |
| Cognito App Client ID | `dev-st21arbiter-poc-UserPoolClientId` | `VITE_COGNITO_CLIENT_ID`, Postman `InitiateAuth` |
| Cognito issuer URL | `dev-st21arbiter-poc-CognitoIssuerURL` | `api_handler` JWT decode |
| AgentCore runtime ARNs (×4) | *not in CFN — published by `deploy_agents.py`* | `aws bedrock-agentcore-control list-agent-runtimes` |
| KB ID, Memory ID, Guardrail ID | *not in CFN* | Run `aws bedrock-agent list-knowledge-bases`, `aws bedrock-agentcore-control list-memories`, `aws bedrock list-guardrails` |
| OSS Collection endpoint | `dev-st21arbiter-poc-OpenSearchCollectionEndpoint` | `setup_bedrock_kb.py` |

For everything not exported via CFN (KB ID, Memory ID, runtime ARNs), the canonical source is the relevant AWS CLI `list-*` command — never hardcode from old session memory.
