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
