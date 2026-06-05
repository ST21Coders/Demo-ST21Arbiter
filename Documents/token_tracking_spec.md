# Spec — Token Tracking (CISO-only Governance tab)

**Status:** Draft (spec only — no implementation yet)
**Owner:** UI (`ui/`) + API (`Infra/functions/api_handler/`) + Agents (`agents/*/agent.py`) + Storage (`Infra/templates/04-storage.yaml`)
**Version target:** bump `APP_VERSION` in [`ui/src/config.js`](../ui/src/config.js) on ship, per project convention.

---

## 1. Summary

Add a new **Token Tracking** page under the Governance section of the Sidebar, visible only to the CISO persona. It shows a KPI strip (tokens today, estimated cost, average tokens per chat), three Recharts visualizations (tokens over time, tokens by agent, tokens by persona), and a filterable per-record table. Token counts are captured from each Bedrock model invocation made by the four AgentCore Runtimes (master + 3 specialists), persisted into a new `<env>-<project>-token-usage` DynamoDB table, and read back through two new endpoints on the existing `api_handler` Lambda behind Cognito JWT auth — no new AWS services, no real-time streaming. The full UI is demoable in local mock mode at `npm run dev` with zero AWS calls and zero cost.

## 2. Why this matters (problem statement)

The platform invokes four Bedrock AgentCore Runtimes on every analyst chat turn, and each runtime makes one or more `bedrock:InvokeModel` calls against Amazon Nova 2 Lite. Today there is **no surface** that answers a CISO's basic governance questions:

- How much are we spending on the model layer? Is that trending up?
- Which agent is the biggest contributor — is the master fanning out efficiently, or is one specialist dominating?
- Which persona drives the most consumption? Are employees burning tokens that should be reserved for SOC/GRC workflows?
- Per-chat: is the average response size growing, suggesting prompt drift?

These are CISO-scope questions because they trade off cost, governance, and AI-program ROI. The other personas (SOC, GRC, employee) have no business case to see them — exposing per-user token spend to a peer would be inappropriate. This spec adds the smallest possible surface that answers the above, locked to the CISO Cognito group, and grounded in the data Bedrock already returns on every invocation.

## 3. Goals & non-goals

### In scope (v1)

- New page `ui/src/pages/TokenTracking.jsx`, reachable at `/token-usage`, sitting in the GOVERNANCE sidebar group alongside Action Center, Compliance, and Audit Logs.
- **CISO-only** access enforced at three layers: sidebar item hidden, `<Guarded>` route wrapper redirects to AccessDenied on direct URL hit, backend returns 403 if the JWT's `cognito:groups` does not include `ciso`. Frontend gating alone is insufficient.
- **KPI strip** (top of page): tokens today (input + output), estimated cost today (USD), average tokens per chat (today).
- **Three Recharts visualizations**: total tokens over time (stacked area, input vs output), tokens by agent (bar, master / sharepoint / awsconfig / zscaler), tokens by persona (bar, ciso / soc / grc / employee).
- **Filter bar**: time-range selector (Today / 7d / 30d / Custom), agent filter, persona filter — mirrors `AuditLogs.jsx` filter idiom.
- **Per-record table** beneath the charts: timestamp, agent, persona, user (email), session_id, model_id, input_tokens, output_tokens, total, estimated_cost, with CSV export — mirrors `AuditLogs.jsx`.
- New `<env>-<project>-token-usage` DDB table (CMK-encrypted, PAY_PER_REQUEST, TTL on a `ttl` attribute set to 90 days).
- Two new API endpoints on the existing `api_handler` Lambda: `GET /token-usage` and `GET /token-usage/summary`.
- Token usage capture inside each of the four agents, written via `boto3` DDB direct from the AgentCore Runtime IAM role.
- **Realistic mock data** (~30 days of hourly synthetic records) in `ui/src/mockData.js` so the entire page is demoable offline.
- Cost estimate uses **Nova 2 Lite pricing as a constant in code** — not pulled live. Pricing notes in the spec; value lives in one named export so a future price change is a one-line edit.

### Out of scope (v1 — explicit non-goals)

- **No real-time streaming.** The page polls on load + manual refresh; no WebSocket, no Server-Sent Events, no live update mid-chat.
- **No per-token alerting.** No "ping me when usage exceeds X" surface. (Note: `AlarmEmail` already exists in `params/dev.json` for future CloudWatch alarms; out of scope here.)
- **No budget enforcement.** Reporting only — nothing throttles or rejects calls based on spend.
- **No multi-tenant billing.** Single-tenant demo platform; no chargeback codes or department splits beyond `persona`.
- **No backfill of historical usage.** Capture is forward-only — Bedrock does not retain per-invocation token counts outside CloudWatch metrics, and the historical CloudWatch metric data is at minute granularity without persona attribution.
- **No alternative model pricing UI.** Cost is computed off a single `MODEL_PRICING['us.amazon.nova-2-lite-v1:0']` constant. If the runtimes are re-pointed at Claude (per CLAUDE.md, that requires Marketplace acceptance), the constant is updated in the same PR.
- **No Guardrail-blocked-call accounting question resolved in v1.** Guardrail-blocked calls still consume input tokens; spec records them but flags `guardrail_blocked: true` so future analysis can split them out. (Open question #4 below.)

## 4. Personas & access

The Cognito User Pool has four groups: `ciso`, `soc`, `grc`, `employee`. Only `ciso` sees Token Tracking. The other three see no sidebar item and a redirect to `firstAccessiblePath()` if they navigate to `/token-usage` directly.

### How the gating composes (defense in depth)

| Layer | File | Behavior |
|---|---|---|
| Sidebar | [`ui/src/components/Sidebar.jsx`](../ui/src/components/Sidebar.jsx) | The Governance group already filters items via `hasAccess(item.to)`. Add `{ to: '/token-usage', icon: Coins, label: 'Token Tracking', adminOnly: true }` to the GOVERNANCE group. The `adminOnly` flag is purely a visual chip — the real gate is `hasAccess`. |
| Route | [`ui/src/App.jsx`](../ui/src/App.jsx) | Wrap in `<Guarded path="/token-usage">` exactly like every other Governance route. `Guarded` reads `usePersona().hasAccess()` and renders `<AccessDenied />` if blocked. |
| ROUTE_ACCESS | [`ui/src/contexts/PersonaContext.jsx`](../ui/src/contexts/PersonaContext.jsx#L61-L74) | Add `'/token-usage': 'token-usage'` to the `ROUTE_ACCESS` map. |
| Persona capability | Same file, `PERSONAS.ciso.access` | Add `'token-usage'` to the CISO `access` array. **Do not add it to soc/grc/employee.** |
| Backend | [`Infra/functions/api_handler/api_handler.py`](../Infra/functions/api_handler/api_handler.py) | Both new handlers call `_require_ciso(event)` as their first step. Reads `_caller_groups(event)` (already defined at line 1203) and returns `_err(403, "CISO access required")` if `'ciso' not in groups`. |

### What non-CISO users experience

- **Sidebar item:** not rendered. They never see it exists.
- **Direct URL `/token-usage`:** `<Guarded>` renders the existing `<AccessDenied />` panel (the persona-aware "Your role X does not have access to this page" card already in [`App.jsx:83-106`](../ui/src/App.jsx#L83-L106)) with a "Go to my home" button. **Recommended over a redirect** — it's the project's established pattern for blocked routes (matches Findings/Actions/etc. behavior for personas without the right group), and it tells the user *why* they can't see it rather than silently bouncing them, which is less confusing.
- **Direct API call to `/token-usage`** (e.g. via curl with a non-CISO IdToken): 403 with `{"error": "CISO access required"}`.

Note that the existing `_caller_groups()` helper in `api_handler.py` already tolerates both list-form (`["ciso"]`) and comma-string-form (`"ciso,grc"`) `cognito:groups` claims — that path stays as-is.

## 5. User stories (CISO POV)

1. *"As CISO on a Monday morning, I open Token Tracking, see we burned 1.2M tokens last week vs 800K the week before, and I have a number to bring to the CFO conversation."*
2. *"I notice the awsconfig specialist is using 3x the tokens of the others — I drill in on the time-by-agent chart and confirm the spike correlates with the new compliance review push."*
3. *"I filter to the last 30 days and `persona=employee` to see whether non-security users are driving meaningful consumption — they aren't, so I keep the chat surface open to them."*
4. *"I export the table to CSV for the quarterly governance review packet."*
5. *"I switch the time range to a custom window covering the failed-pentest exercise to see what the chat traffic looked like during the incident."*
6. *"A SOC analyst asks me whether they can see the same dashboard. I explain that token cost data is CISO-only governance; they don't see the menu item, by design."*

## 6. UX / screen layout

The page follows the visual language of [`AuditLogs.jsx`](../ui/src/pages/AuditLogs.jsx) (rounded-xl cards, `p-6 space-y-5 page-container` outer, `0 1px 2px rgba(15,23,42,0.04)` shadow) and the KPI-strip pattern from [`ActionCenter.jsx`](../ui/src/pages/ActionCenter.jsx) (4-up grid of stat cards). Recharts is already in the stack (`recharts: ^2.12.7` in `ui/package.json`, used in `Dashboard.jsx`).

### Order top-to-bottom

1. **Header row** — `<h1>Token Tracking</h1>` + subtitle ("Bedrock model usage by agent, persona, and session — CISO governance"); "Export CSV" button on the right (mirrors AuditLogs).
2. **CISO-only notice strip** (rounded amber/indigo banner, mirrors the AuditLogs immutability notice) — short text noting "Visible to CISO only — model usage and cost data is governed under §X policy".
3. **KPI strip** (4 cards):
   - Tokens today (input + output, integer with thousands sep)
   - Estimated cost today (USD, 2dp)
   - Avg tokens per chat (today)
   - Active agents (count of distinct agents that recorded usage today, ranges 0-4)
4. **Filter bar** — time range select (Today / 7d / 30d / Custom + two dates), agent select, persona select, "Refresh" button.
5. **Charts row** — Recharts `ResponsiveContainer` blocks, 3 cards laid out responsively:
   - **Tokens over time** (stacked area chart, x = bucket, two series = `input_tokens` / `output_tokens`). Granularity follows the time range: Today → hourly buckets; 7d → daily buckets; 30d → daily buckets; Custom → server picks granularity = `(to - from) > 3 days ? day : hour`.
   - **Tokens by agent** (bar chart, x = agent name, y = total tokens, color by agent).
   - **Tokens by persona** (bar chart, x = persona, y = total tokens, color by persona — reuse persona colors from `PERSONAS` in `PersonaContext.jsx`).
6. **Per-record table** — paginated locally (50 rows at a time), columns: Timestamp, Agent, Persona, User, Session, Model, Input, Output, Total, Cost, with a small "details" expansion (mirror `AuditLogs`).

### ASCII wireframe

```
┌────────────────────────────────────────────────────────────────────┐
│ Token Tracking                                       [ Export CSV ]│
│ Bedrock model usage by agent, persona, and session — CISO …        │
├────────────────────────────────────────────────────────────────────┤
│ ⓘ Visible to CISO only — model usage and cost data is governed …  │
├────────────────────────────────────────────────────────────────────┤
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐                │
│ │ 1,243,802│ │  $0.187  │ │   3,420  │ │    4/4   │                │
│ │ tokens   │ │ est cost │ │ tok/chat │ │ agents   │                │
│ │ today    │ │ today    │ │ avg today│ │ active   │                │
│ └──────────┘ └──────────┘ └──────────┘ └──────────┘                │
├────────────────────────────────────────────────────────────────────┤
│ Range: [ 7 days ▾ ]   Agent: [ All ▾ ]   Persona: [ All ▾ ]  [↻]   │
├────────────────────────────────────────────────────────────────────┤
│ ┌──────────────────────────┐ ┌─────────────┐ ┌─────────────┐       │
│ │ Tokens over time         │ │ By agent    │ │ By persona  │       │
│ │ [stacked area chart]     │ │ [bar chart] │ │ [bar chart] │       │
│ └──────────────────────────┘ └─────────────┘ └─────────────┘       │
├────────────────────────────────────────────────────────────────────┤
│ TIME    AGENT       PERSONA  USER          SESS    IN   OUT  COST  │
│ 09:43   master      ciso     diana@…       …-abc  142   88  $0.00 │
│ 09:43   sharepoint  ciso     diana@…       …-abc  410  173  $0.00 │
│ …                                                                  │
└────────────────────────────────────────────────────────────────────┘
```

## 7. Data model

### Token usage record (single row in the new table)

```jsonc
{
  "usage_id":        "tu-2026-06-05T09:43:12.314Z-master-7f3a",  // PK
  "timestamp":       "2026-06-05T09:43:12.314Z",                  // ISO8601 UTC
  "agent":           "master",            // master | sharepoint | awsconfig | zscaler
  "persona":         "ciso",              // ciso | soc | grc | employee | unknown
  "user_id":         "<Cognito sub>",     // matches sessions.user_id
  "user_email":      "ciso_diana@meridianinsurance.com",
  "session_id":      "sess-…",            // null for adhoc invocations
  "chat_type":       "analyst",           // analyst | mcp | null
  "model_id":        "us.amazon.nova-2-lite-v1:0",
  "input_tokens":    142,
  "output_tokens":   88,
  "total_tokens":    230,
  "estimated_cost":  0.0000234,           // USD, computed at write time
  "guardrail_blocked": false,
  "ttl":             1773000000           // unix epoch, ~90 days out
}
```

### Proposed DDB schema (`<env>-<project>-token-usage`)

```yaml
TokenUsageTable:
  Type: AWS::DynamoDB::Table
  Properties:
    TableName: !Sub "${Environment}-${ProjectName}-token-usage"
    BillingMode: PAY_PER_REQUEST
    PointInTimeRecoverySpecification:
      PointInTimeRecoveryEnabled: true
    SSESpecification:
      SSEEnabled: true
      SSEType: KMS
      KMSMasterKeyId:
        Fn::ImportValue: !Sub "${Environment}-${ProjectName}-DynamoDBKeyArn"
    AttributeDefinitions:
      - AttributeName: usage_id     ; AttributeType: S
      - AttributeName: timestamp    ; AttributeType: S
      - AttributeName: persona      ; AttributeType: S
      - AttributeName: agent        ; AttributeType: S
    KeySchema:
      - AttributeName: usage_id     ; KeyType: HASH
    GlobalSecondaryIndexes:
      - IndexName: persona-time-index
        KeySchema:
          - AttributeName: persona  ; KeyType: HASH
          - AttributeName: timestamp; KeyType: RANGE
        Projection: { ProjectionType: ALL }
      - IndexName: agent-time-index
        KeySchema:
          - AttributeName: agent    ; KeyType: HASH
          - AttributeName: timestamp; KeyType: RANGE
        Projection: { ProjectionType: ALL }
    TimeToLiveSpecification:
      AttributeName: ttl
      Enabled: true
```

Exports `TokenUsageTableName` follow the same `!Sub "${Environment}-${ProjectName}-TokenUsageTableName"` pattern as the other four tables. Mirrors `ConflictsTableV2` (PK + multi-GSI) and `ScanRunsTable` (TTL-enabled) — both already in `04-storage.yaml`. Both GSIs project ALL because the table is small (worst case: ~5 records per chat turn × hundreds of turns/day) and read patterns need every column for the table view.

### Why a new table beats reusing `sessions` or `audit-log`

| Option | Verdict | Why |
|---|---|---|
| Stuff into `sessions` (add `token_usage` map) | **Rejected.** | Sessions are per-conversation; usage records are per-invocation. A single chat fans out to 4 runtimes, each making one or more model calls — that's 4-8 rows of usage per session row. Forcing this into a list attribute on the session item creates an unbounded list (DDB caps items at 400KB) and makes per-agent/per-time queries impossible. |
| Stuff into `audit-log` | **Rejected.** | Conceptually adjacent (both are append-only event streams), but `audit-log` is governance/compliance audit data with a separate query and retention story (currently scanned wholesale by `/audit`). Mixing schemas means every `audit-log` scan now filters past usage rows, and the `details` JSON-string pattern can't be queried efficiently for per-day per-persona totals. |
| New `token-usage` table | **Chosen.** | Single-responsibility, native PK/GSI structure for the exact query shapes the page needs, independent TTL (90d vs whatever audit-log keeps), and zero blast radius — if we get the schema wrong, we only churn one table. Mirrors the precedent of `ConflictsTableV2` and `ScanRunsTable` being split out as the demo grew. |

## 8. Where token counts come from

### The model layer always returns them

Every `bedrock:InvokeModel` response includes a `usage` block with `inputTokens` and `outputTokens` (Bedrock standard, regardless of Nova/Claude/etc.). Strands Agents surfaces this via `result.metrics.accumulated_usage` after each `agent(prompt)` call (Strands documents this on `AgentResult`). Either source works.

### Where to write the record (capture point in each agent)

For the master ([`agents/master_orchestrator/agent.py`](../agents/master_orchestrator/agent.py)) — the call to capture wraps line ~497:

```python
agent = build_agent()
response = str(agent(augmented_prompt))   # ← line 497
# NEW: persist usage record(s) post-call
_record_usage(
    agent="master",
    user_id=actor_id,
    user_email=_email_from_event(event),   # passed in via payload from api_handler
    persona=_persona_from_groups_in_event(event),
    session_id=session_id,
    chat_type=chat_type,
    model_id=MODEL_ID,
    usage=agent.last_response.metrics.accumulated_usage,  # strands AgentResult.metrics
)
```

Each of the three specialists (`agents/sharepoint_specialist/agent.py`, `agents/awsconfig_specialist/agent.py`, `agents/zscaler_specialist/agent.py`) gets the same hook at its corresponding `agent(query)` call site. Specialists do not receive a `chat_type` or persona — they're invoked agent-to-agent — so for those records we forward the master's context via the existing `payload` JSON the master sends to `runtime_client.invoke_agent_runtime`. Add `actor_id`, `persona`, `session_id`, `chat_type` to that payload (today it's just `{"prompt": query}`). Specialists then read those out of their own request body.

### Direct DDB write from agents, not via api_handler

Two viable paths:

| Path | Verdict | Why |
|---|---|---|
| Agent → `api_handler` → DDB | **Rejected.** | Adds a synchronous network hop on every model call (latency on the chat-critical path), forces api_handler to expose a write endpoint that has to be auth'd separately, and makes the four runtimes depend on the API stack — a circular concern they don't otherwise have. |
| Agent → DDB direct via boto3 | **Chosen.** | The runtimes already have an IAM role (`<env>-<project>-agentcore-role`) with `dynamodb:*` against `table/<env>-<project>-*` (and `/index/*`) per `02-security.yaml`'s wildcard pattern (verified line 142-143). So they can `PutItem` on the new table with no IAM change. Failures are caught and logged but do **not** fail the chat invocation — usage tracking is best-effort, matching the existing memory-write pattern in the master (see line ~191 comment "memory is best-effort and never fails the invocation"). |

**Important nuance for IAM**: the existing agent IAM role wildcard in `02-security.yaml` covers `table/${Environment}-${ProjectName}-*` and `/index/*` — so the new `<env>-<project>-token-usage` table is **already in scope** with no `02-security.yaml` change required for the agent role. The api_handler role's wildcard covers the read side identically. This is a happy accident of the existing wildcard pattern (and the spec calls it out explicitly in Step 4 so the reviewer doesn't expect an IAM diff that isn't there). The only thing we **must** verify is that the DynamoDB CMK ARN is in the agent role's `KMSDecrypt` statement — per the CLAUDE.local.md gotcha "`KMSDecrypt` statement in `09-agentcore.yaml` must include `DynamoDBKeyArn` or `PutItem`/`UpdateItem` silently fails." We confirm this in Step 6 of the build.

### Helper shape (lives in each agent or a shared util)

Each agent gets a small `_record_usage(...)` helper. To avoid copy-paste drift across four codebases, create `agents/_shared/token_usage.py` and import it from each. The function:

1. Builds the row from arguments and `datetime.now(timezone.utc).isoformat()`.
2. Resolves cost via a `MODEL_PRICING` dict keyed on `model_id`.
3. Calls `boto3.resource("dynamodb").Table(TOKEN_USAGE_TABLE).put_item(Item=row)`.
4. On any exception: `log.warning("token usage write failed: %s", e)` and return — never raise.

The `TOKEN_USAGE_TABLE` env var is set on all four runtimes by `scripts/deploy_agents.py` (one-line addition to the runtime env dict).

## 9. API contract

Both endpoints live in `api_handler.py`, behind API Gateway with the existing Cognito JWT authorizer (NOT the Function URL — `/chat` is the only one on the Function URL because it bypasses APIGW's 29s timeout, and these endpoints are sub-second).

### `GET /token-usage`

Returns raw records for the time window, after server-side filter.

**Query params:**
- `from` (ISO8601, required) — window start
- `to` (ISO8601, required) — window end
- `agent` (optional) — `master | sharepoint | awsconfig | zscaler | all` (default `all`)
- `persona` (optional) — `ciso | soc | grc | employee | all` (default `all`)
- `granularity` (optional) — `hour | day` — only used if the response shape is bucketed; v1 returns raw records and the UI buckets client-side. Keep the param accepted but ignored, so we can add server-side bucketing later without breaking clients.

**Example request:**
```
GET /token-usage?from=2026-06-05T00:00:00Z&to=2026-06-05T23:59:59Z&persona=ciso
Authorization: Bearer <CISO IdToken>
```

**Example response (200):**
```json
{
  "from": "2026-06-05T00:00:00Z",
  "to":   "2026-06-05T23:59:59Z",
  "filter": { "agent": "all", "persona": "ciso" },
  "count": 412,
  "records": [
    {
      "usage_id":       "tu-2026-06-05T09:43:12.314Z-master-7f3a",
      "timestamp":      "2026-06-05T09:43:12.314Z",
      "agent":          "master",
      "persona":        "ciso",
      "user_email":     "ciso_diana@meridianinsurance.com",
      "session_id":     "sess-1f2…",
      "chat_type":      "analyst",
      "model_id":       "us.amazon.nova-2-lite-v1:0",
      "input_tokens":   142,
      "output_tokens":  88,
      "total_tokens":   230,
      "estimated_cost": 0.0000234,
      "guardrail_blocked": false
    }
    // …
  ]
}
```

**Error responses:**
- `400` if `from`/`to` missing or unparseable.
- `403` if caller's groups don't include `ciso`.
- `500` if `TOKEN_USAGE_TABLE` env var not configured.

**Query strategy:** use the `persona-time-index` GSI when `persona != "all"`, the `agent-time-index` GSI when `agent != "all"`, and a `Scan` with `FilterExpression` otherwise. At demo scale a Scan is fine (consistent with `_handle_list_findings` and `_handle_list_audit` already doing `Scan(Limit=200)` in the same file). Note the `Limit=500` cap; spec out a `next_token` (LastEvaluatedKey) on responses if and when scale demands it.

### `GET /token-usage/summary`

Returns the KPI strip values for a fixed range.

**Query params:**
- `range` (optional) — `today | 7d | 30d` (default `today`)

**Example response (200):**
```json
{
  "range": "today",
  "total_tokens": 1243802,
  "input_tokens": 803211,
  "output_tokens": 440591,
  "estimated_cost": 0.187,
  "avg_tokens_per_chat": 3420,
  "active_agents": 4,
  "by_agent":   { "master": 410123, "sharepoint": 320001, "awsconfig": 213444, "zscaler": 300234 },
  "by_persona": { "ciso": 740000, "soc": 290000, "grc": 200000, "employee": 13802 }
}
```

The summary endpoint does its own aggregation in the Lambda so the UI doesn't need to download every record just to render the KPI strip. This is the only reason `summary` exists as a separate endpoint — for raw records, the page uses `/token-usage`.

## 10. Authorization

The frontend hides the menu item, but **the backend is the source of truth**. The first thing both handlers do:

```python
def _require_ciso(event) -> dict | None:
    """Return None if the caller is in the 'ciso' Cognito group, else an _err response."""
    groups = _caller_groups(event)
    if "ciso" not in groups:
        return _err(403, "CISO access required")
    return None
```

Used at the top of `_handle_list_token_usage` and `_handle_token_usage_summary`:

```python
def _handle_list_token_usage(event):
    blocked = _require_ciso(event)
    if blocked:
        return blocked
    # … rest of the handler
```

This mirrors how `_caller_groups()` is already used in the `approve` action transition (`api_handler.py:1081-1102`) to detect CISO override — same helper, same JWT path, so all three caller-resolution paths in `_caller_claims` (API GW claims → Authorization header JWT → direct invoke) continue to work.

## 11. Mock data plan

**The priority.** The user wants `npm run dev` with no `VITE_API_URL` set to render the whole page convincingly — KPI strip, three charts, table with paging, filter behavior — with zero AWS calls. Per the project's mock-mode switch (`USE_MOCK = !API_URL` in [`ui/src/config.js`](../ui/src/config.js#L14)), the hook layer detects this and reads from `mockData.js` instead of `apiFetch`.

### Fixtures to add to `ui/src/mockData.js`

Generate ~30 days of synthetic per-hour records at module-load time (a deterministic seed keeps it reproducible across reloads):

```js
// At the bottom of mockData.js, after MOCK_AUDIT:
function _seededRand(seed) { /* mulberry32 — deterministic so charts don't jitter across reloads */ }
function _genMockTokenUsage() {
  const out = []
  const rnd = _seededRand(42)
  const now = Date.now()
  const agents  = ['master', 'sharepoint', 'awsconfig', 'zscaler']
  const personas = ['ciso', 'soc', 'grc', 'employee']
  // Density model: ~6 chat turns per hour during business hours (9-17 UTC), ~1/hr off-hours.
  for (let h = 0; h < 24 * 30; h++) {
    const ts = new Date(now - h * 3600_000)
    const hour = ts.getUTCHours()
    const density = (hour >= 9 && hour <= 17) ? 6 : 1
    for (let i = 0; i < density; i++) {
      const persona = personas[Math.floor(rnd() * personas.length * 0.6)]  // skew toward ciso/soc
      const session_id = `mock-sess-${h}-${i}`
      // Each chat turn = 1 master record + 1-3 specialist records
      const specialists = agents.slice(1).filter(() => rnd() > 0.4)
      for (const ag of ['master', ...specialists]) {
        const input  = Math.floor(80 + rnd() * 600)
        const output = Math.floor(40 + rnd() * 400)
        const total  = input + output
        out.push({
          usage_id:        `tu-${ts.toISOString()}-${ag}-${i}`,
          timestamp:       ts.toISOString(),
          agent:           ag,
          persona,
          user_email:      `${persona}@meridianinsurance.com`,
          session_id,
          chat_type:       'analyst',
          model_id:        'us.amazon.nova-2-lite-v1:0',
          input_tokens:    input,
          output_tokens:   output,
          total_tokens:    total,
          estimated_cost:  total * 0.00000016,  // Nova 2 Lite blended estimate; refined in MODEL_PRICING constant
          guardrail_blocked: false,
        })
      }
    }
  }
  return out
}
export const MOCK_TOKEN_USAGE = _genMockTokenUsage()
```

Why deterministic: charts that re-randomize on every reload look broken to a demo audience. The seed is fixed in code; the data is realistic but stable.

### Mock latency

`sleep(200)` on the list endpoint (matches `useAudit` and `useChangeRequests` patterns); `sleep(150)` on the summary endpoint (it's cheaper conceptually).

### Mock auth / persona override for local testing

The codebase already supports a **DEV_AUTH** mode (referenced in [`PersonaContext.jsx`](../ui/src/contexts/PersonaContext.jsx) and the Settings spec; the TopBar has a dev-persona switcher). That's the canonical way to flip personas locally. Specifically — when `DEV_AUTH` is on, `getGroups()` returns a value driven by `sessionStorage`, and the TopBar exposes a persona picker. So to verify CISO-only gating during dev:
1. Run `npm run dev` (mock mode auto-engages, no AWS calls).
2. Use the TopBar dev-persona switcher to flip between `ciso`, `soc`, `grc`, `employee`.
3. Confirm: only `ciso` sees the Token Tracking item in the sidebar AND can hit `/token-usage` without an AccessDenied panel.

**If the dev override does not exist or has regressed** (verify during Phase 4 step 1 — if `DEV_AUTH` is no longer wired, the smallest change is a sessionStorage-keyed override read inside `getGroups()` guarded by `import.meta.env.DEV`). The spec calls this out so we don't ship without it, but does not pre-design it pending verification.

## 12. Frontend file changes

| File | Status | Change |
|---|---|---|
| [`ui/src/pages/TokenTracking.jsx`](../ui/src/pages/TokenTracking.jsx) | **new** | The page. Composes KPI strip, filter bar, three Recharts charts, and the table. ~350-450 lines; split into `ui/src/components/tokenTracking/*` if it grows past 500. |
| [`ui/src/App.jsx`](../ui/src/App.jsx) | edit | Add `import TokenTracking from './pages/TokenTracking'` and the route line inside `Shell`'s `<Routes>`: `<Route path="/token-usage" element={<Guarded path="/token-usage"><TokenTracking /></Guarded>} />`. Place before the `*` catch-all, alongside the other Guarded routes. |
| [`ui/src/contexts/PersonaContext.jsx`](../ui/src/contexts/PersonaContext.jsx) | edit | Two edits: add `'token-usage'` to `PERSONAS.ciso.access` (only); add `'/token-usage': 'token-usage'` to `ROUTE_ACCESS`. |
| [`ui/src/components/Sidebar.jsx`](../ui/src/components/Sidebar.jsx) | edit | Add `{ to: '/token-usage', icon: Coins, label: 'Token Tracking', adminOnly: true }` to the GOVERNANCE group in `NAV_GROUPS` (line ~23). Add `'/token-usage': 'Token Tracking'` to `PAGE_TITLES`. The `Coins` icon is from `lucide-react` (already imported elsewhere as a 16px monochrome icon, consistent with the rest of the nav). |
| [`ui/src/hooks/useApi.js`](../ui/src/hooks/useApi.js) | edit | Add `useTokenUsage()` hook returning `{ records, summary, loading, load }`. Follows the `useAudit` / `useChangeRequests` shape: mock branch reads from `MOCK_TOKEN_USAGE` + computes a summary client-side; live branch calls `apiFetch('/token-usage?…')` and `apiFetch('/token-usage/summary?…')` in parallel via `Promise.all`. |
| [`ui/src/mockData.js`](../ui/src/mockData.js) | edit | Append `MOCK_TOKEN_USAGE` per §11. |
| [`ui/src/__tests__/tokenTracking.test.jsx`](../ui/src/__tests__/tokenTracking.test.jsx) | **new** | Vitest tests — see §13. |

## 13. Backend file changes

| File | Status | Change |
|---|---|---|
| [`Infra/templates/04-storage.yaml`](../Infra/templates/04-storage.yaml) | edit | Add `TokenUsageTable` resource (§7 schema). Add `TokenUsageTableName` Output + Export. |
| [`Infra/templates/02-security.yaml`](../Infra/templates/02-security.yaml) | **likely no-op** | The api_handler role wildcard `table/${Environment}-${ProjectName}-*` and `/index/*` already covers the new table. The agent role in `09-agentcore.yaml` likewise covers it (verify wildcard match during build). **One thing to verify**: the agent role's `KMSDecrypt` statement must include the `DynamoDBKeyArn` ImportValue — per CLAUDE.local.md, missing this makes `PutItem` silently fail on KMS-encrypted tables. If it's already there (which the existing tables imply), no edit. |
| [`Infra/templates/06-api.yaml`](../Infra/templates/06-api.yaml) | edit | Add `TOKEN_USAGE_TABLE` env var on the api_handler Lambda → `Fn::ImportValue: !Sub "${Environment}-${ProjectName}-TokenUsageTableName"`. |
| [`Infra/templates/09-agentcore.yaml`](../Infra/templates/09-agentcore.yaml) | verify only | Verify the agentcore role's KMSDecrypt covers `DynamoDBKeyArn`; verify the table wildcard covers `-token-usage`. No expected edits. |
| [`Infra/functions/api_handler/api_handler.py`](../Infra/functions/api_handler/api_handler.py) | edit | Add `TOKEN_USAGE_TABLE = os.environ.get(…)` constant, `token_usage_table = ddb.Table(…)` resource, route block for `/token-usage` and `/token-usage/summary` (mirror existing route block style), handlers `_handle_list_token_usage` and `_handle_token_usage_summary`, and the `_require_ciso` helper. ~120 LOC added. |
| [`agents/_shared/token_usage.py`](../agents/_shared/token_usage.py) | **new** | Shared `_record_usage(...)` helper + `MODEL_PRICING` constant + `_compute_cost(...)` function. ~60 LOC. |
| [`agents/master_orchestrator/agent.py`](../agents/master_orchestrator/agent.py) | edit | Import the shared helper; capture `agent.last_response.metrics.accumulated_usage` after the `agent(augmented_prompt)` call at line ~497; call `_record_usage(...)`. Forward `actor_id`, `persona`, `session_id`, `chat_type` into the specialist invocation payload (extend the `_invoke_runtime` call signature). |
| [`agents/sharepoint_specialist/agent.py`](../agents/sharepoint_specialist/agent.py), [`agents/awsconfig_specialist/agent.py`](../agents/awsconfig_specialist/agent.py), [`agents/zscaler_specialist/agent.py`](../agents/zscaler_specialist/agent.py) | edit (each) | Read `actor_id`, `persona`, `session_id`, `chat_type` from the incoming payload (forwarded by master). Capture usage on the `agent(query)` call and write a record with `agent="<this-specialist>"`. |
| [`scripts/deploy_agents.py`](../scripts/deploy_agents.py) | edit | Pass `TOKEN_USAGE_TABLE=<env>-<project>-token-usage` into each runtime's env dict — one-line addition. |
| [`Infra/params/dev.json`](../Infra/params/dev.json) | no change | Table name is derived from `Environment` + `ProjectName` already in params; no new parameter needed. |

### Vitest tests at `ui/src/__tests__/tokenTracking.test.jsx`

Mirroring `settings.test.jsx`:

1. **Sidebar gating** — render `<Sidebar>` for each of the four personas (mock `getGroups()`); assert the Token Tracking item is present only for `ciso`.
2. **Route gating** — render the `Shell` + a memory router at `/token-usage`; for non-CISO personas, expect `<AccessDenied />` content; for CISO, expect the page header "Token Tracking".
3. **Mock data render** — render `<TokenTracking>` with `USE_MOCK=true` and mocked `MOCK_TOKEN_USAGE`; assert the KPI strip shows non-zero numbers, the three charts mount (`recharts` `ResponsiveContainer` renders), and the table shows at least 1 row.
4. **Filter behavior** — flip the agent filter to `sharepoint`; assert only sharepoint rows render.
5. **CSV export** — click Export; assert `URL.createObjectURL` is called (existing mock pattern in `auditLogs.test.jsx`).

Run with `cd ui && npx vitest run src/__tests__/tokenTracking.test.jsx`.

## 14. Local testing plan (the priority)

These are the exact commands. Mock mode auto-engages because `VITE_API_URL` is unset by default in dev. Zero AWS calls. Zero cost.

```bash
# 1. Install + dev server
cd ui
npm install                # if not already
npm run dev                # serves http://localhost:5173/ — Cognito callback whitelist requires :5173
```

```bash
# 2. Verify CISO-only gating (in a separate terminal or just in the browser)
#    Visit http://localhost:5173/  → sign in via the dev-persona switcher in the TopBar
#    For each persona { ciso, soc, grc, employee }:
#      - Confirm sidebar shows / does not show "Token Tracking" in GOVERNANCE
#      - Navigate to http://localhost:5173/token-usage directly
#        ciso → page renders
#        others → AccessDenied panel renders
```

```bash
# 3. Run Vitest unit tests
cd ui
npx vitest run src/__tests__/tokenTracking.test.jsx
# or all tests
npm test
```

**On the dev-persona switcher**: if the existing override is broken or absent (verify in Phase 4 step 1), the smallest change is a `sessionStorage.getItem('arbiter.devPersona')` read in `useAuth.js::getGroups()` gated by `import.meta.env.DEV`, plus a small button in TopBar to set it. The spec leaves the exact wiring to the build phase to avoid duplicating work if it already exists.

## 15. Deployment plan (after local sign-off)

Step-by-step, mirroring the project's existing `Infra/deploy.sh` change-set flow:

1. **Validate templates locally**
   ```bash
   cd Infra
   aws cloudformation validate-template --template-body file://templates/04-storage.yaml --region us-east-1
   aws cloudformation validate-template --template-body file://templates/06-api.yaml    --region us-east-1
   ```
2. **Deploy infra changes**
   ```bash
   ./deploy.sh dev   # creates the change-set for 04-storage (TokenUsageTable + GSIs) and re-deploys 06-api (TOKEN_USAGE_TABLE env)
   ```
3. **Re-deploy agents** so the four runtimes pick up the new env var and the new write path
   ```bash
   cd ../scripts
   source .venv/bin/activate
   KB_ID=<id> GUARDRAIL_ID=<id> MASTER_MEMORY_ID=<id> AWS_REGION=us-east-1 \
     python3 deploy_agents.py
   ```
4. **Re-build + sync UI** (handled by `deploy.sh`'s `post_deploy_ui.py`, or manually)
   ```bash
   cd ../ui
   npm run build
   python3 ../Infra/post_deploy_ui.py
   ```
5. **Smoke test**
   - Sign in as `ciso_diana@meridianinsurance.com`.
   - Open `/token-usage` — expect "0 tokens today" until a fresh chat happens (no backfill).
   - Open Analyst Chat, send a prompt, wait for a reply, refresh `/token-usage` — expect a small set of records (1 master + 1-3 specialists).
   - Sign out, sign in as `grc_priya` — expect no menu item and an AccessDenied panel on direct URL.
   - `aws dynamodb scan --table-name dev-st21arbiter-poc-token-usage --max-items 5 --region us-east-1` — confirm rows are written.

## 16. Cost notes

- **DDB on-demand**: one `PutItem` per model invocation, ~4 invocations per analyst chat turn. At a heavy demo load of ~100 chats/day that's 400 writes/day — sub-cent per month at on-demand pricing.
- **No new services.** Reuses the existing api_handler Lambda, agent runtimes, Cognito, and CMK. The only new resource is the DDB table.
- **TTL 90d** so the table doesn't grow unboundedly — for demo this is more than enough; for production extend if compliance retention demands it.
- `AlarmEmail` is already in `params/dev.json`; future CloudWatch alarms on usage volume would target that (out of scope here).

## 17. Risks / open questions

1. **Backfilling historical usage is impossible.** Bedrock does not retain per-invocation token counts in a queryable form outside CloudWatch metrics (which are minute-aggregated and have no persona attribution). The day-zero view will show "Today: 0" until the first chat turn after deploy. The spec calls this out in the smoke test step; no engineering mitigation possible.
2. **What happens if a Bedrock call fails before the usage record is written?** The shared helper catches all exceptions and logs a warning — usage tracking is best-effort, never breaks the chat. Means our recorded totals will slightly under-count failed calls (which is fine; failed calls don't bill the customer for output tokens anyway — they only bill for the input).
3. **Retention.** v1: 90-day TTL on the `ttl` attribute. Configurable via the `TTL_DAYS` constant in the shared helper. Open question for v2: is 90 days enough for the CISO's annual governance review? If not, lift to 365 or write a parallel summarization Lambda that emits monthly rollups into a separate table.
4. **Should Guardrail-blocked calls count as token usage?** They consume input tokens (the prompt was scored by the guardrail's input model) but produce zero output tokens. v1: record them with `guardrail_blocked: true` so the table shows them; the cost calc includes input only when `guardrail_blocked` is true. The KPI strip's totals include them. Future refinement: an opt-in filter "exclude guardrail-blocked" in the UI.
5. **Specialist invocation context.** Today the master sends only `{"prompt": query}` to each specialist runtime. We need to extend that payload with `actor_id`, `persona`, `session_id`, `chat_type` so the specialist's usage record has the same attribution. This is a small change to `_invoke_runtime` and the three specialists' request parsing — not risky, but it touches all four agent codebases at once. We'll do it as the first build step so the rest of the work has the data to write.
6. **Dev persona switcher might be stale.** Spec leans on a `DEV_AUTH` pattern referenced in the Settings spec and `settings.test.jsx`; if it's drifted, Phase 4 step 1 verifies and patches before continuing.

## 18. Acceptance criteria

- [ ] Sidebar shows **Token Tracking** under GOVERNANCE only when persona = `ciso`.
- [ ] `/token-usage` renders for `ciso`; the other three personas see `<AccessDenied />`.
- [ ] Backend returns 403 on `/token-usage` and `/token-usage/summary` for non-CISO callers (verified with `curl` + a non-CISO IdToken).
- [ ] Mock mode (`USE_MOCK=true`) renders the entire page convincingly: KPI strip with non-zero numbers, all 3 charts mount with visible data, table shows at least 50 rows for a 7d range.
- [ ] Filter bar narrows the records (time range / agent / persona) and the table + charts update accordingly.
- [ ] Export CSV produces a file with the visible rows.
- [ ] In live mode, every analyst chat turn produces ≥1 row in `<env>-<project>-token-usage` within 5 seconds of the reply rendering.
- [ ] Cost estimate matches `MODEL_PRICING['us.amazon.nova-2-lite-v1:0']` × (input + output) — sanity check one row by hand.
- [ ] No new AWS service introduced; only one new DDB table.
- [ ] Vitest tests in `ui/src/__tests__/tokenTracking.test.jsx` pass.

## 19. Rollout checklist

- [ ] Spec reviewed and approved.
- [ ] Phase 4 step 1: verify or patch the dev-persona switcher; confirm `getGroups()` honors it in dev.
- [ ] Phase 4 step 2: add `MOCK_TOKEN_USAGE` to `mockData.js`.
- [ ] Phase 4 step 3: build `TokenTracking.jsx` page + `useTokenUsage` hook against the mock data.
- [ ] Phase 4 step 4: wire route + sidebar + ROUTE_ACCESS + CISO `access` array.
- [ ] Phase 4 step 5: Vitest tests covering gating, render, filter, export.
- [ ] **User sign-off** that the page is demoable locally end-to-end with no AWS calls.
- [ ] Phase 4 step 6: add `TokenUsageTable` to `04-storage.yaml`; validate; change-set deploy.
- [ ] Phase 4 step 7: add `TOKEN_USAGE_TABLE` env to `06-api.yaml`; deploy api_handler.
- [ ] Phase 4 step 8: implement `_handle_list_token_usage`, `_handle_token_usage_summary`, `_require_ciso` in `api_handler.py`; deploy.
- [ ] Phase 4 step 9: implement `agents/_shared/token_usage.py`; wire it into master + 3 specialists; extend master→specialist payload; redeploy via `deploy_agents.py`.
- [ ] Phase 4 step 10: smoke test live as `ciso_diana`; confirm rows appear after a chat turn; confirm `grc_priya` sees AccessDenied.
- [ ] Bump `APP_VERSION` in `ui/src/config.js`.

---

## Open questions (for reviewer)

1. **90-day TTL** — does this fit the CISO's annual-review use case, or do we need 365d in v1?
2. **Cost constant location** — keep `MODEL_PRICING` in `agents/_shared/token_usage.py` (one source for the agents, mirrored as a constant in the UI for display) or move to a small config table in DDB so finance can update without a deploy? Recommendation for v1: in code; revisit when we have a second model.
3. **Should `/token-usage/summary` get a `by_user` breakdown** (top 5 users by tokens)? The KPI strip doesn't need it, but it's a one-line aggregation and might pre-empt the next CISO question. Recommendation: yes, return it; the UI shows it as a small "top users" list in the page chrome.
