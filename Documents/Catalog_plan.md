# ServiceNow-via-Gateway + Agent Catalog + Smart Rabbit page

## Context

Three enhancements to ARBITER (ST21):

1. **ServiceNow connectivity hardening** — the `servicenow_specialist` already talks to a live ServiceNow instance over direct REST (Table API) with Secrets Manager auth. Per user decision, its **read path moves behind a Bedrock AgentCore Gateway with an OpenAPI target** (credential injected at the edge; agent code never touches the ServiceNow key). Additionally the agent gains the missing **list-CIs-by-CMDB-class** capability ("List all CIs of the Web Server class" currently has no tool — everything resolves single CIs).
2. **Agent catalog** — segregate the fleet into 6 catalog groups (IT_Assist_Admin, IT_Assist_Work, Employee_Assist, Data_Assist, Insurance_Assist, OnCall_Assist), adding **three new lightweight runtimes**: `claim_specialist`, `fraud_specialist` (Insurance), `debug_specialist` (OnCall) — Nova 2 Lite + guardrail + domain prompt, no data backends yet.
3. **New "Smart Rabbit" page** under the INTELLIGENCE sidebar section, visible to **all 4 personas**: two dropdowns (catalog group → agent) + chat; group/agent switchable mid-conversation. **No new `rabbit21` orchestrator runtime** (user-confirmed): the existing `target` → `SPECIALIST_RUNTIME_ARNS` direct-routing in `_handle_chat` does the job.

Verified session facts: boto3 1.43.14 in `scripts/.venv` has the full `bedrock-agentcore-control` gateway API (`create_gateway`, `create_gateway_target` with `openApiSchema.inlinePayload`, `create_api_key_credential_provider`). `/agent-status` is driven by `_AGENT_DISPLAY_NAMES` ([api_handler.py:4806](Infra/functions/api_handler/api_handler.py#L4806)) × `SPECIALIST_RUNTIME_ARNS` (line 86). The shared `AgentCoreRuntimeRole` log-group ARNs are wildcarded (`${RuntimePrefix}_*`, [09-agentcore.yaml:249](Infra/templates/09-agentcore.yaml#L249)) — new runtimes need no log-group IAM. `Infra/deploy.sh` filters params per template, so new dev.json keys are safe.

## Architecture decisions

- **D1 — Gateway inbound auth: CUSTOM_JWT via a Cognito M2M app client** (resource server + client-credentials client on the existing user pool). AWS_IAM would require SigV4-signing the MCP streamable-HTTP transport, which neither `mcp` nor Strands `MCPClient` supports out of the box. The M2M client secret is never stored — the runtime role gets `cognito-idp:DescribeUserPoolClient` and reads it at cold start.
- **D2 — Gateway scope: reads only** (`get_table`/`get_one`). Writes (create_change, comments, impact-analysis drafting) stay on the battle-tested direct-REST path, so all existing `/servicenow/*` deterministic endpoints work regardless of gateway state. The gateway path requires a ServiceNow **Inbound REST API key** (`api_key` added to the existing secret) since OpenAPI targets mandate api-key/OAuth outbound auth.
- **D3 — Seam inside `ServiceNowClient.get_table`/`get_one`** with automatic fallback: gateway-first when `SERVICENOW_GATEWAY_URL` is set, direct REST on any error or when unset. All 12 existing tool signatures untouched; local dev unaffected.
- **D4 — Smart Rabbit uses `chat_type: 'rabbit'`** so its sessions don't pollute the MCP Admin list; one-word backend change to the type filter. The structured-inventory target override at [api_handler.py:449](Infra/functions/api_handler/api_handler.py#L449) must be scoped so it can't hijack an explicit specialist target.
- **D5 — New agents cloned from `sharepoint_specialist`** (simplest template, ~120 lines) minus the KB tool; shared `AgentCoreRuntimeRole` (no secrets → no dedicated role needed).

## Phase A — ServiceNow via AgentCore Gateway

1. **[Infra/templates/03-identity.yaml](Infra/templates/03-identity.yaml)** — add `AWS::Cognito::UserPoolResourceServer` (identifier `${Environment}-${ProjectName}-gateway`, scope `invoke`) + `GatewayM2MClient` (`GenerateSecret: true`, `AllowedOAuthFlows: [client_credentials]`, scope `<identifier>/invoke`). Do **not** touch the SPA client (must stay secretless). Export `GatewayM2MClientId` and `UserPoolArn`.
2. **[Infra/templates/09-agentcore.yaml](Infra/templates/09-agentcore.yaml)** — `ServicenowAgentRuntimeRole` (line 517): add Sid `CognitoM2MClientRead` (`cognito-idp:DescribeUserPoolClient` on imported UserPoolArn). New `ServicenowGatewayRole` (trust `bedrock-agentcore.amazonaws.com` + `aws:SourceAccount` condition, mirroring runtime-role trust): `bedrock-agentcore:GetResourceApiKey`/`GetWorkloadAccessToken*` on `token-vault/default*` + `workload-identity-directory/default*`, Secrets Manager read on `secret:bedrock-agentcore-identity!*`. Export `ServicenowGatewayRoleArn`.
3. **New `scripts/gateway/servicenow_table_openapi.json`** — minimal Table API spec: `GET /api/now/table/{tableName}` (`operationId: getTableRecords`; params `sysparm_query/sysparm_fields/sysparm_limit/sysparm_display_value`) and `GET /api/now/table/{tableName}/{sysId}` (`getRecordById`); `securitySchemes: apiKey in header x-sn-apikey` + global security. Script injects `servers[0].url` from the secret's `instance_url`. Gateway tool names become `servicenow-table___getTableRecords` / `___getRecordById`.
4. **New `scripts/setup_servicenow_gateway.py`** (boto3, `ENVIRONMENT`/`PROJECT`/`AWS_REGION` envs, idempotent create-or-update, following `deploy_agents.py` conventions): read secret `${ENV}/${PROJECT}/servicenow` (fail loudly if no `api_key`, with instructions); API-key credential provider `${ENV}-${PROJECT}-servicenow-apikey`; gateway `${ENV}-${PROJECT}-servicenow-gw` (`protocolType='MCP'`, `authorizerType='CUSTOM_JWT'`, customJWTAuthorizer with Cognito discovery URL + allowedClients=[M2M client id], roleArn from export); OpenAPI target `servicenow-table` with `credentialLocation: HEADER`, `credentialParameterName: x-sn-apikey`; poll READY; print gateway URL + env values.
5. **[agents/servicenow_specialist/agent.py](agents/servicenow_specialist/agent.py)** —
   - New envs: `SERVICENOW_GATEWAY_URL`, `SERVICENOW_GW_TOKEN_URL`, `SERVICENOW_GW_CLIENT_ID`, `SERVICENOW_GW_USER_POOL_ID`, `SERVICENOW_GW_SCOPE`.
   - Lazy `_GatewayTools` singleton (mirror `_CLIENT`/`_CLIENT_TRIED`, lines 236–252): fetch M2M client secret via `DescribeUserPoolClient`; client-credentials token from Cognito token endpoint (cached, refresh ~60 s early); per-call Strands `MCPClient(lambda: streamablehttp_client(url, headers={Authorization: Bearer}))` `with`-block + `call_tool_sync` (jira lifecycle discipline); parse MCP text content back to Table-API-shaped `result` lists.
   - `ServiceNowClient.get_table`/`get_one` (lines 160–200 region): gateway-first when configured, fallback to existing `requests` path on any exception; `post_table`/`patch_table` untouched.
   - **New `@tool list_cis_by_class(ci_class, fields="", limit=20)`**: friendly-name map (web server→`cmdb_ci_web_server`, application→`cmdb_ci_appl`, load balancer→`cmdb_ci_lb`, database→`cmdb_ci_db_instance`, network→`cmdb_ci_network`, server→`cmdb_ci_server`; raw `cmdb_ci_*` pass-through) → `get_table("cmdb_ci", query=f"sys_class_name={cls}")`, default fields `name,sys_class_name,operational_status,support_group,correlation_id,sys_id`, limit clamped 1–100, "(ServiceNow not configured)" degradation. Register in `build_agent()` (line ~1026) + one SYSTEM_PROMPT line.
   - `requirements.txt` += `mcp>=1.0.0`.
6. **[scripts/deploy_agents.py](scripts/deploy_agents.py)** — in servicenow env assembly (`main()`): auto-discover the gateway via `list_gateways` by name; if READY, set the 5 `SERVICENOW_GW*` vars (token URL from dev.json `CognitoDomainPrefix`, ids from CFN exports); otherwise set nothing (fallback mode).
7. **[scripts/seed_servicenow_cmdb.py](scripts/seed_servicenow_cmdb.py)** — add ~3 `cmdb_ci_web_server` CIs (+ optional `cmdb_rel_ci` rows to the ALB) so the demo query returns rows.

## Phase B — Three new specialist runtimes

1. **`agents/claim_specialist/`, `agents/fraud_specialist/`, `agents/debug_specialist/`** — `agent.py` cloned from [agents/sharepoint_specialist/agent.py](agents/sharepoint_specialist/agent.py) minus the KB tool: no-tool Strands `Agent` (MODEL_ID env default Nova 2 Lite, guardrail envs), standard entrypoint payload extraction, `record_from_agent_result(agent="claim"|"fraud"|"debug", …)` token usage. Domain system prompts: claims intake/adjudication (never invent policy numbers or commit coverage decisions); fraud analyst (red flags, SIU referral criteria — indicators, not verdicts); on-call debug (triage, log/stack-trace interpretation, runbook next steps, blameless). Dockerfile + requirements.txt copied from sharepoint_specialist.
2. **09-agentcore.yaml** — `ClaimSpecialistRepo`/`FraudSpecialistRepo`/`DebugSpecialistRepo` mirroring `ServicenowSpecialistRepo` (lines 140–153) + `...RepoUri` exports. Shared role's wildcard log-group ARN already covers the new runtime names.
3. **deploy_agents.py** — three `AGENTS` entries before master (`claim-specialist` / `ClaimSpecialistRepoUri` / `ClaimModelId` / `CLAIM_MODEL_ID`, etc., empty `env_overrides`, shared role); `arn_env_map` += `CLAIM_RUNTIME_ARN` / `FRAUD_RUNTIME_ARN` / `DEBUG_RUNTIME_ARN`.
4. **[api_handler.py](Infra/functions/api_handler/api_handler.py)** — `SPECIALIST_RUNTIME_ARNS` (line 86) += `claim`/`fraud`/`debug`; `_AGENT_DISPLAY_NAMES` (line 4806) += the three display names; scope the line-449 structured override to raw target ∈ {empty, `master`, `structured`}; extend the conversations type filter (line 4092) tuple with `"rabbit"`.
5. **[Infra/params/dev.json](Infra/params/dev.json)** — `ClaimModelId`/`FraudModelId`/`DebugModelId` = `us.amazon.nova-2-lite-v1:0`.

## Phase C — Smart Rabbit UI

1. **New `ui/src/agentCatalog.js`** — `AGENT_CATALOG`: it_assist_admin (sharepoint, zscaler, awsconfig, paloalto), it_assist_work (servicenow, jira), employee_assist (hr), data_assist (structured, sales), insurance_assist (claim, fraud), oncall_assist (debug); each agent `{id, name, description}` where `id` === `sendChat` target (names lifted from `MCP_SERVERS` in [MCPChat.jsx:27-132](ui/src/pages/MCPChat.jsx#L27-L132)); `findAgent(id)` helper.
2. **New `ui/src/pages/SmartRabbit.jsx`** (modeled on MCPChat, leaner): two Analyst-style compact `<select>`s (group → agent; group change auto-selects first agent); `useAgentStatus()` + MCPChat's `deriveStatus` pattern for a READY dot, send disabled when not chattable; `send()` mints `sess-<uuid12>` and calls `sendChat({prompt, session_id, chat_type: 'rabbit', target})` (no data_group fields); `useConversations({type: 'rabbit'})` for the local session rail; switching group/agent mid-conversation keeps session + appends a system note "Switched to <Agent> (<Group>)"; lucide `Rabbit` icon.
3. **Wiring** — [Sidebar.jsx](ui/src/components/Sidebar.jsx) INTELLIGENCE `NAV_GROUPS` item `/smart-rabbit` + `PAGE_TITLES`; [TopBar.jsx](ui/src/components/TopBar.jsx) `ROUTE_META`; [App.jsx](ui/src/App.jsx) import + `<Guarded path="/smart-rabbit">` route; [PersonaContext.jsx](ui/src/contexts/PersonaContext.jsx) `ROUTE_ACCESS['/smart-rabbit'] = 'smart-rabbit'` + key appended to **all four** personas' access arrays (explicit key keeps Settings' routeAccess crossing correct); [useApi.js](ui/src/hooks/useApi.js) `useAgentStatus` mock map (line 729) += claim/fraud/debug READY.

## Phase D — Tests

- `ui/src/__tests__/agentCatalog.test.js`: 6 groups, expected ids, global id uniqueness, `findAgent`.
- `ui/src/__tests__/smartRabbit.send.test.jsx` (pattern of `useApi.bulkDelete.test.jsx`: mock `../config` + `../hooks/useAuth`, stub fetch): POST body carries `target` + `chat_type: 'rabbit'`; render test for group-switch behavior + system note.
- Backend sanity: `python3 -m py_compile` on touched Python; `aws cloudformation validate-template` on 03-identity + 09-agentcore (user runs AWS commands themselves — provide copy-paste).

## Deployment & verification order

1. Validate templates → `Infra/deploy.sh` (change-sets; note 06-api redeploy blanks `*_RUNTIME_ARN`).
2. User creates a ServiceNow Inbound REST API key; adds `api_key` to the `dev/st21arbiter-poc/servicenow` secret.
3. venv → `python3 setup_servicenow_gateway.py` (gateway + target READY, prints URL).
4. `seed_servicenow_cmdb.py --from-secret` (web-server CIs).
5. `deploy_agents.py` full run (CodeBuild arm64 builds new images; never `--skip-build`; `GUARDRAIL_VERSION=1` integer) — re-patches all 13 ARNs onto api_handler.
6. `cd ui && npm test && npm run build` → post_deploy_ui / CloudFront invalidation.
7. E2E: 13 runtimes READY; `/agent-status` lists claim/fraud/debug; sign in as `emp_sarah` → Smart Rabbit visible; IT Assist – Work → ServiceNow → "List all CIs of the Web Server class" returns seeded rows via `servicenow-table___getTableRecords` (check OTel traces); mid-conversation switch to Insurance Assist → Claim works; Analyst + MCP Admin unchanged.

## Risks / gotchas

- deploy.sh 06-api redeploy blanks runtime ARNs — always run `deploy_agents.py` afterward.
- Un-scoped line-449 override could hijack Smart Rabbit prompts containing list+tables+files+group keywords — fixed in B4.
- Direct-specialist chats persist no server-side history (same limitation MCPChat has today) — Smart Rabbit's session rail is local-optimistic.
- Basic-auth-only ServiceNow secret → gateway script fails loudly; agent retains full function via direct REST fallback. PDIs hibernate — wake before demo.
- Missing `DescribeUserPoolClient` grant → silent fallback to direct REST (log a warning in the agent).
- 3 new runtimes/ECR repos add modest consumption cost + CodeBuild minutes.
- CLAUDE.local.md discipline: validate-template, change-sets only, scripts venv, no hardcoded project name, user runs AWS CLI commands themselves.
