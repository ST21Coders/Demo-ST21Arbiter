# JIRA Specialist — Security Review & AgentCore Gateway Recommendations

**Scope:** the `jira_specialist` AgentCore runtime — ARBITER's first agent that calls a service
**outside AWS** (Atlassian Jira Cloud over the internet).
**Question:** is this a candidate for **AgentCore Gateway**, or is it already secured enough?
**Author lens:** AWS Security Architect. **Date:** 2026-06-07.

---

## 1. Current posture

| Area | Today | Assessment |
|---|---|---|
| Runtime isolation | Bedrock AgentCore Runtime — dedicated microVM per session, `PrivateSubnet2` | ✅ Strong |
| Credential | Jira **email + long-lived API token** (basic auth) in Secrets Manager `dev/st21arbiter-poc/jira`; agent reads it via `get_secret_value` | ⚠️ Static, no rotation |
| MCP server | `mcp-atlassian` runs **inside the container** (stdio); token passed via subprocess env; HTTPS to `*.atlassian.net` | ✅ TLS · ⚠️ self-hosted, broad tools |
| Inbound auth | `api_handler` / master call via `invoke_agent_runtime` = **IAM SigV4** | ✅ |
| IAM role | **Shared** `AgentCoreRuntimeRole` (Config, KB, sessions DDB, memory, all secrets under path) | ⚠️ Over-privileged |
| Egress | Security group allows `443 → 0.0.0.0/0` via NAT | ⚠️ No domain allowlist |
| Model safety | Guardrail applied; writes are deterministic + human-in-the-loop (UI form) | ✅ |
| Audit | `api_handler` writes an audit row; AgentCore Observability traces | ✅ |

---

## 2. Verdict

**Acceptable for a dev demo; not production-grade.** The isolation, TLS, IAM inbound, KMS, and
guardrail foundations are solid. The exposure is the classic third-party-integration set:

1. **Static long-lived token** — no rotation; full blast radius until manually revoked.
2. **Credential scope = the *user's* Jira permissions** — a personal account can act on *every*
   project, not just `DEVARBITER`. Use a dedicated, project-scoped **service account**.
3. **Over-privileged IAM role** — the Jira agent inherits far more than it needs.
4. **Unrestricted egress** — a compromised dependency could exfiltrate to any HTTPS endpoint.
5. **Over-broad MCP tool surface** — `mcp-atlassian` exposes many write/Confluence tools that go unused.
6. **Prompt-injection exposure** — Jira issue text returned to the LLM is untrusted input.

---

## 3. Is AgentCore Gateway the right candidate? — Yes

Agent → external SaaS with credential injection is exactly what **AgentCore Gateway + Identity** are
built for. Confirmed capabilities (AWS docs):

- **AgentCore Gateway** — turns an API into a **managed MCP target**. **Inbound** auth: IAM / OAuth
  (Cognito, Auth0, Okta, OIDC) / API key. **Outbound** auth: **Basic auth (username+password)**,
  **OAuth 2.0 with automatic refresh**, API key, IAM, custom Lambda — credentials held in Secrets
  Manager, **never in your code**. (Jira uses email+token basic auth → directly supported.)
- **AgentCore Identity (Outbound Auth)** — brokers external credentials via a managed **Token Vault**
  (OAuth 2.0 / API key), delivered to the agent at runtime via ARNs. Usable **with or without** Gateway.

### Three tiers (increasing change)

| Tier | What | When |
|---|---|---|
| **0 — Harden in place** | Dedicated Jira **service account** scoped to `DEVARBITER`; **per-agent least-privilege IAM role**; **egress allowlist**; `mcp-atlassian` **tool/project scoping**; **token rotation runbook** | **Now** — highest security-per-effort, no new services |
| **1 — AgentCore Identity** | Move the token into an Identity **credential provider** (API key/OAuth); agent fetches from the **Token Vault** by ARN instead of reading Secrets Manager; OAuth auto-refresh | Middle step if a full Gateway move isn't yet justified |
| **2 — AgentCore Gateway** | Front Jira as a **managed MCP target**; Gateway does outbound (Basic/OAuth) + inbound (IAM/OAuth) auth; agent **never holds the token**; retire embedded `mcp-atlassian` | **Strategic target** — pays off as more SaaS agents arrive (ServiceNow next) |

**Recommendation:** ship **Tier 0 now** (closes the high-severity gaps cheaply, independent of
everything else), and adopt **Tier 2 (Gateway)** as the production end-state once multiple
external-SaaS specialists exist — that's where centralized credential governance + observability pay
off. **Tier 1** is the optional in-between.

---

## 4. Tier 0 — concrete checklist

**Credential hygiene (manual):**
- [ ] Revoke any leaked API token at id.atlassian.com → Security → API tokens.
- [ ] Create a dedicated **service account**, grant it permission **only on `DEVARBITER`**, mint its token.
- [ ] Update Secrets Manager `dev/st21arbiter-poc/jira` with the new email + token.

**Code / infra:**
- [ ] `mcp-atlassian` scoping env: `JIRA_PROJECTS_FILTER=DEVARBITER`, `ENABLED_TOOLS` allowlist
      (search / get-issue / get-project / create-issue); no Confluence creds → Confluence tools unloaded.
- [ ] Dedicated least-privilege `JiraAgentRuntimeRole` (only: its secret, `bedrock:InvokeModel`/
      `ApplyGuardrail`, ECR pull, its log group, KMS decrypt, X-Ray, VPC ENI, token-usage `PutItem`).
- [ ] Wire the JIRA runtime to that role in `deploy_agents.py` (per-agent role).

**Guidance (Tier-0+ follow-ups):**
- [ ] **Egress allowlist** to `*.atlassian.net` — needs AWS **Network Firewall** or a forward proxy
      (security groups can't match domains). Avoid a brittle CIDR list.
- [ ] **Rotation runbook** — Atlassian tokens don't auto-rotate; rotate quarterly (or automate via a
      Secrets Manager rotation Lambda that mints a new token).

---

## 5. References

- Gateway inbound/outbound auth — https://docs.aws.amazon.com/help-panel/bedrock-agentcore/latest/console/hp-gateway-gateway-auth.html
- Identity Outbound Auth (Token Vault) — https://docs.aws.amazon.com/help-panel/bedrock-agentcore/latest/console/hp-identity-outbound.html
- Manage outbound credential providers — https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-outbound-credential-provider.html
- Connect to private identity providers (VPC Lattice) — https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-private-idp.html

---

## 6. Implementation status & concrete migration steps (addendum — 2026-07-16)

Written while diagnosing a "Jira agent not responding" report. Context: the agent is **already
MCP-based** (embedded stdio `mcp-atlassian` driven by a Strands `MCPClient`), not a raw REST client —
so the architecture question is *which MCP topology*, not "MCP vs API". Operator runbook for the
responsiveness issue itself: [`instructions/JIRA_TROUBLESHOOTING.md`](../instructions/JIRA_TROUBLESHOOTING.md).

### 6.1 Tier-0 status — what is already done vs still open

| Tier-0 item (§4) | Status | Evidence / gap |
|---|---|---|
| Dedicated least-privilege `JiraAgentRuntimeRole` | ✅ **Done** | `Infra/templates/09-agentcore.yaml` (role + `secretsmanager:GetSecretValue` scoped to `.../jira-*`, no Config/KB/sessions/memory grants); wired in `scripts/deploy_agents.py` via `role_export` |
| `mcp-atlassian` tool allowlist (`ENABLED_TOOLS`) | ✅ **Done** | Set in `deploy_agents.py` env override + read in `agent.py` (`JIRA_ENABLED_TOOLS`) |
| MCP startup / tool-call timeouts (fail-fast, not hang) | ✅ **Done (2026-07-16)** | `agent.py` — `MCP_STARTUP_TIMEOUT` (25 s) on `MCPClient`, `MCP_TOOL_TIMEOUT_SECONDS` (45 s) on `call_tool_sync`; legible `(JIRA timeout/connectivity…)` message |
| Loud failure on unpatched specialist ARN (no silent master misroute) | ✅ **Done (2026-07-16)** | `_handle_chat` in `api_handler.py` — known target + blank ARN → 503, not master fallback |
| `JIRA_PROJECTS_FILTER=DEVARBITER` scoping | ⛔ **Open** | Left empty by design today; set it (env-only) once a service account exists so reads/writes can't touch other projects |
| Dedicated **service account** scoped to `DEVARBITER` | ⛔ **Open** | Secret currently holds a personal-scope token → blast radius = the user's full Jira |
| **Egress allowlist** to `*.atlassian.net` | ⛔ **Open** | SG allows `443 → 0.0.0.0/0`; needs AWS Network Firewall or a forward proxy (SGs can't match domains) |
| **Token rotation** runbook / automation | ⛔ **Open** | Static long-lived token; rotate quarterly or via a Secrets Manager rotation Lambda |

**Next Tier-0 actions (cheap, high value):** create the scoped service account + set `JIRA_PROJECTS_FILTER`,
then stand up the egress allowlist and the rotation runbook.

### 6.2 Tier-2 (AgentCore Gateway + Identity) — migration sketch

Adopt when a **second** external-SaaS specialist arrives (ServiceNow) so centralized credential
governance + observability pay off. The agent then **holds no Atlassian credential** and the embedded
`mcp-atlassian` subprocess is retired. Plan-only — confirm exact CLI/SDK/console steps against current
AgentCore docs (§5) at build time.

1. **Outbound credential provider (AgentCore Identity Token Vault).** Register the Jira credential —
   Basic auth (email + token) today, or OAuth 2.0 with auto-refresh for the service account — as a
   managed credential provider. Removes the `get_secret_value` read from the agent.
2. **Gateway + Jira target.** Create an AgentCore Gateway and add Jira as a **managed MCP target**
   (from the Jira REST OpenAPI / a Lambda target), bound to the credential provider for **outbound**
   auth and to **IAM (SigV4)** for **inbound** auth (same trust model `api_handler`/master already use
   via `invoke_agent_runtime`).
3. **Repoint the agent.** Swap `_build_mcp_client` from `stdio_client(StdioServerParameters(command="mcp-atlassian", …))`
   to an HTTP transport against the Gateway MCP endpoint (e.g. `mcp.client.streamable_http.streamablehttp_client(gateway_url, …)`
   with SigV4/OAuth headers). The rest of the agent (`Agent(tools=jira_mcp.list_tools_sync())`, the
   deterministic `_create_issue`/`_transition_issue`/`_add_comment` paths) is **unchanged** — it already
   speaks MCP, so only the transport + auth wiring move.
4. **Scope tools at the Gateway** (Cedar / target tool selection) to the same allowlist `ENABLED_TOOLS`
   enforces today; drop `mcp-atlassian` from `agents/jira_specialist/requirements.txt` and the
   `JIRA_SECRET_ID` read from the runtime role.
5. **Egress simplifies:** the runtime now calls the Gateway (AWS-side), so the `*.atlassian.net`
   allowlist moves to the Gateway/target rather than the runtime SG.

**Alternative to step 2/3 — Atlassian hosted Remote MCP.** Instead of fronting the REST API through a
Gateway target, point the transport at Atlassian's hosted Remote MCP server over OAuth (token brokered
by the Identity Token Vault). Removes self-hosting of `mcp-atlassian` but couples you to Atlassian's
hosted endpoint + OAuth lifecycle. Same agent-side repoint as step 3.

