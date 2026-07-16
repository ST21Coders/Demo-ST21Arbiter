# JIRA Specialist — "not responding" troubleshooting runbook

Operator runbook for when the **Jira** card on the MCP Chat page returns nothing, an error, or an
off-topic answer. Work the checks **top-down** and stop at the first one that fails.

**Facts used below** (from `Infra/params/dev.json` + conventions):
- Account `669810405473`, region `us-east-1`, env `dev`, project `st21arbiter-poc` → prefix `dev-st21arbiter-poc`.
- api_handler Lambda: `dev-st21arbiter-poc-api-handler`.
- Jira secret: `dev/st21arbiter-poc/jira` (JSON `{url, email, api_token[, confluence_url]}`).
- Default Jira project key (code fallback in [agent.py](../agents/jira_specialist/agent.py)): `DEVARBITER`.
- The agent talks to Jira via an **in-process `mcp-atlassian` stdio subprocess** (basic auth to
  `<site>.atlassian.net`), not raw REST. Credentials come from Secrets Manager, read **fresh on every
  invocation** (secret edits take effect on the next call — no redeploy).

> The ranking below is a **hypothesis to validate**, not a certainty. Record which check first fails
> and which fix restores responses — that is the actual root cause.

---

## Part A — Ranked diagnosis

### 1. Blank `JIRA_RUNTIME_ARN` → Jira chats silently answered by the master orchestrator ⭐ most likely

A `06-api` SAM redeploy blanks this Lambda's `*_RUNTIME_ARN` env to `""`. Historically `_handle_chat`
then fell through to the master via `... or MASTER_AGENT_RUNTIME_ARN`, so a Jira prompt got a
compliance-flavored answer with no Jira data — reads as "the Jira agent is broken."
(This code now **fails loudly with a 503** for a known specialist with a blank ARN — see the fix note —
so post-fix the symptom is a clear 503, not a silent misroute.)

**Check (read-only):**
```bash
aws lambda get-function-configuration --region us-east-1 \
  --function-name dev-st21arbiter-poc-api-handler \
  --query 'Environment.Variables.JIRA_RUNTIME_ARN' --output text
```
Empty / `None` / `""` ⇒ this is it.

**Fix:** re-run the agent deploy, which re-patches all `*_RUNTIME_ARN` + `MEMORY_ID` onto the Lambda:
```bash
cd scripts && source .venv/bin/activate
KB_ID=<id> GUARDRAIL_ID=<id> GUARDRAIL_VERSION=1 MASTER_MEMORY_ID=<id> \
  python3 deploy_agents.py --agents jira-specialist   # re-patch step runs regardless of --agents scope
```

### 2. Runtime not deployed / not READY / status = PLACEHOLDER

`/agent-status` reports `PLACEHOLDER` when the ARN is unset or the runtime isn't in
`list_agent_runtimes`; the UI then disables or mislabels the Jira card.

**Check:**
```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query "agentRuntimes[?contains(agentRuntimeName,'jira')].[agentRuntimeName,status]" --output table
```
Not `READY` (or absent) ⇒ this is it.

**Fix:** `python3 scripts/deploy_agents.py --agents jira-specialist` (do **not** pass `--skip-build` on a
repo whose ECR has no `:latest`).

### 3. Jira secret missing / empty → agent replies `"(JIRA not configured …)"`

**Check (do NOT print the secret value):**
```bash
aws secretsmanager describe-secret --region us-east-1 --secret-id dev/st21arbiter-poc/jira \
  --query '[Name,ARN]' --output text
# confirm the required keys exist without revealing values:
aws secretsmanager get-secret-value --region us-east-1 --secret-id dev/st21arbiter-poc/jira \
  --query 'SecretString' --output text | python3 -c "import sys,json; d=json.load(sys.stdin); print({k:('set' if d.get(k) else 'MISSING') for k in ('url','email','api_token','confluence_url')})"
```
Any of `url`/`email`/`api_token` = `MISSING` ⇒ create/repair per
[DEPLOYMENT.md](DEPLOYMENT.md) (§ Jira secret). No redeploy needed — next invocation reads it fresh.

### 4. Wrong token type → `mcp-atlassian` returns nothing / errors ⭐ prime suspect once 1–3 pass

`mcp-atlassian` uses **basic auth against the site URL** `https://<site>.atlassian.net`. A **scoped** or
**OAuth** Atlassian token is **rejected** there (it only works against `https://api.atlassian.com/ex/jira/{cloudId}`),
so the same token that succeeds in Postman returns nothing here.

**Check:**
```bash
# use the email + token from the secret; 200 = classic token OK, 401/403 = scoped/OAuth (wrong type)
curl -s -o /dev/null -w "%{http_code}\n" -u "<email>:<api_token>" \
  https://<site>.atlassian.net/rest/api/3/myself
```
**Fix:** mint a **classic, unscoped** API token at id.atlassian.com → Security → API tokens; set the
secret's `url` to the **site** URL; verify the curl returns `200`. Prefer a **dedicated service account**
scoped to `DEVARBITER` (see Tier-0 hardening in `Documents/Gateway_recommendations.md`).

### 5. Subprocess hangs (egress blocked / handshake stalls) → true "no response"

The runtime is VPC-attached (`PrivateSubnet2` → NAT → `*.atlassian.net`). If egress is blocked or the
MCP handshake stalls, the invocation used to hang until the runtime's own timeout.

**Hardening applied** ([agent.py](../agents/jira_specialist/agent.py)): the `mcp-atlassian` startup is now
bounded by `MCP_STARTUP_TIMEOUT` (default **25 s**, env-overridable) and each deterministic tool call by
`MCP_TOOL_TIMEOUT_SECONDS` (default **45 s**). A stuck start now returns a fast, legible
`"(JIRA timeout/connectivity: could not initialize the Atlassian MCP server within 25s — check VPC egress …)"`
instead of a silent hang.

**Check (runtime logs):**
```bash
LG=$(aws logs describe-log-groups --region us-east-1 \
  --query "logGroups[?contains(logGroupName,'jira')].logGroupName" --output text | head -1)
aws logs tail "$LG" --region us-east-1 --since 30m --format short
```
Interpretation of the last invocation:
- logs `JIRA specialist: …prompt=` then **silence** → a hang (egress/SG). Confirm NAT + the runtime SG
  allow `443` egress; confirm DNS resolves `<site>.atlassian.net`.
- emits `(JIRA timeout/connectivity: …)` → egress/token; see checks 4–5.
- emits `(JIRA error: <Type>: …)` → a caught tool/auth error; see checks 3–4.

### 6. Model access (only if `MODEL_ID` was overridden to Claude)

Default `us.amazon.nova-2-lite-v1:0` is first-party and needs no Marketplace subscription. If the runtime
`MODEL_ID` was overridden to an Anthropic Claude model without an accepted Marketplace subscription, calls
fail with `INVALID_PAYMENT_INSTRUMENT` / `aws-marketplace:Subscribe`.

**Check:** confirm the runtime's `MODEL_ID` env = `us.amazon.nova-2-lite-v1:0` (or a subscribed model).

---

## Part B — Sample test prompts (project `DEVARBITER`)

Run from the **MCP Chat** page with the **jira** card selected (sends `target="jira"`), or by direct
`invoke_agent_runtime` with `{"prompt": "…", "session_id": "<fresh-uuid>"}`. **Use a fresh `session_id`
each run** — the guardrail replays conversation history, so a stale/`adhoc` session can skew results.
Ordered easy → hard:

| # | Prompt | Exercises | Pass = |
|---|---|---|---|
| 1 | `List all Jira projects I can see.` | `jira_get_all_projects` | Real project list incl. `DEVARBITER`; proves creds + egress |
| 2 | `Show me the open work items in the DEVARBITER project.` | `jira_search` (`project = DEVARBITER AND statusCategory != Done`) | Real issues cited by key (e.g. `DEVARBITER-12`) |
| 3 | `Summarize DEVARBITER-1 and its current status.` | `jira_get_issue` | Accurate summary + status, no fabrication |
| 4 | `What DEVARBITER work items were updated in the last 7 days?` | `jira_search` (date JQL) | Recent issues, or a clear "none" |
| 5 | `Create a Task in DEVARBITER titled "ARBITER smoke test" with a one-line description.` | `jira_create_issue` (write) | Returns a real key + `<site>/browse/<KEY>` that resolves |
| 6 | `POST /jira/transition {"issue_key":"DEVARBITER-<n>","transition":"Done","comment":"L1 auto-resolve"}` | deterministic `action` path | Transition applied + `JIRA_TRANSITIONED` audit row |

**Reading failures:**
- `"(JIRA not configured …)"` → **check 3** (secret).
- `"(JIRA timeout/connectivity: …)"` → **check 5** then **4** (egress / token).
- `"(JIRA error: …)"` → **check 4/3** (token type / secret).
- A compliance-style answer with **no Jira data** → **check 1** (misroute; pre-fix behavior).
- HTTP **503 "Runtime ARN for 'jira' not configured …"** → **check 1** (post-fix loud signal — re-run `deploy_agents.py`).

---

## Related
- Architecture / hardening path: [`Documents/Gateway_recommendations.md`](../Documents/Gateway_recommendations.md)
- Agent source: [`agents/jira_specialist/agent.py`](../agents/jira_specialist/agent.py)
- Routing: `_handle_chat` in [`Infra/functions/api_handler/api_handler.py`](../Infra/functions/api_handler/api_handler.py)
