# Token Tracking — gap analysis

## TL;DR
PR #18 + #21 shipped a working **frontend, api_handler routing logic, DDB table, env wiring, agent capture, and Vitest suite**. The remaining gaps are **two backend wiring fixes that will cause hard failures in live mode**: (1) `/token-usage` and `/token-usage/summary` are not registered as SAM API Gateway events in `Infra/templates/06-api.yaml`, so API GW will return 404 before reaching the Lambda router; (2) the AgentCore IAM role in `Infra/templates/09-agentcore.yaml` only allows `dynamodb:PutItem` on the `-sessions` table, not on `-token-usage`, so agent writes will be denied. There are also a couple of schema/spec divergences that the next spec revision should reconcile or accept.

## Done (no further work needed)

- **§4 / §12 Sidebar gating** — `ui/src/components/Sidebar.jsx:28` adds the GOVERNANCE entry with `Coins` icon and `adminOnly: true`; `Sidebar.jsx:58` adds the page title; `hasAccess(item.to)` filter at line 111 already gates by persona.
- **§4 ROUTE_ACCESS map** — `ui/src/contexts/PersonaContext.jsx:72` adds `'/token-usage': 'token-usage'`. CISO-only because the access key is in `PERSONAS.ciso.access` at line 54 and nowhere else.
- **§12 App route** — `ui/src/App.jsx:19` imports `TokenTracking`; `App.jsx:165` wraps it in `<Guarded path="/token-usage">`.
- **§12 TokenTracking page** — `ui/src/pages/TokenTracking.jsx` present (370 lines): header, KPI strip (4 cards), filter bar, three Recharts (AreaChart + 2 BarCharts), records table, CSV export. Range options today/7d/30d (no Custom — spec §6 had Custom; see Partial below).
- **§12 useTokenUsage hook** — `ui/src/hooks/useApi.js:534`. Mock branch reads `MOCK_TOKEN_USAGE`, filters client-side, derives summary via `_computeTokenSummary`. Live branch fires `/token-usage` and `/token-usage/summary` in parallel via `Promise.all`.
- **§11 MOCK_TOKEN_USAGE generator** — `ui/src/mockData.js:690-737` is deterministic (seeded mulberry32 at `42`), 30 days, weekend/weekday density model, working-hours skew, 3% guardrail-blocked rate, persona weight skew, specialist fan-out probs. Exported at `mockData.js:739`. `tokenUsageToCsv` helper at `mockData.js:742`.
- **§13 Backend endpoints handlers** — `Infra/functions/api_handler/api_handler.py`:
  - `_require_ciso` at line 459 (uses `_caller_groups`).
  - `_parse_token_usage_filters` at line 472 (accepts `from/to` ISO8601 or `range=today|7d|30d`).
  - `_query_token_usage_records` at line 502 (picks `persona-time-index` or `agent-time-index` GSIs based on filters; fans out across personas when neither filter is set).
  - `_compute_token_summary` at line 563 (matches the JS `_computeTokenSummary` shape).
  - `_handle_list_token_usage` at line 612, `_handle_token_usage_summary` at line 628.
  - Router lines 148-152 dispatch `GET /token-usage` and `GET /token-usage/summary`.
  - `TOKEN_USAGE_TABLE` env var read at line 64; `token_usage_table` resource at line 94.
- **§13 Lambda env var (06-api)** — `Infra/templates/06-api.yaml:208-212` sets `TOKEN_USAGE_TABLE` via `Fn::ImportValue` from `04-storage`.
- **§13 Storage table** — `Infra/templates/04-storage.yaml:337-387` creates `TokenUsageTable`: PAY_PER_REQUEST, PITR on, SSE-KMS with DynamoDBKeyArn, both `persona-time-index` and `agent-time-index` GSIs (ProjectionType: ALL), TTL on `ttl`. **PK is `pk/sk` composite, not the spec's flat `usage_id` HASH** (see Risks). Output + Export `TokenUsageTableName` at lines 519-522.
- **§13 Shared agent helper** — `agents/_shared/token_usage.py` (197 lines). `MODEL_PRICING` dict has Nova 2 Lite + both Claude Sonnet 4.6 key forms (PR #21). `compute_cost` returns `Decimal` (DDB-safe). `extract_usage` tries `metrics.accumulated_usage` then falls back to `result.usage`. `record_usage` zero-skips records with no tokens and no guardrail flag, clamps persona to known set, suffixes `sk` with `uuid.uuid4().hex[:6]` to avoid sub-ms collisions. Best-effort `put_item` wrapped in try/except.
- **§13 Master capture + payload forwarding** — `agents/master_orchestrator/agent.py`:
  - Import at line 38.
  - `_INVOCATION_CTX` dict at line 137 stashes `actor_id/persona/session_id/chat_type` per invocation.
  - `_invoke_runtime` (line 140) injects all four fields into the specialist payload at lines 154-157.
  - Capture call at lines 529-532 after `agent_result = agent(augmented_prompt)` at line 524.
- **§13 Specialist capture** — all three specialists read forwarded fields from payload and call `record_from_agent_result`:
  - `agents/sharepoint_specialist/agent.py:102-113`
  - `agents/awsconfig_specialist/agent.py:209-220`
  - `agents/zscaler_specialist/agent.py:141-152`
- **§13 Dockerfiles** — all four `Dockerfile`s contain `COPY _shared ./_shared` (master:12, sharepoint:9, awsconfig:9, zscaler:9).
- **§13 deploy_agents env var** — `scripts/deploy_agents.py:507` passes `TOKEN_USAGE_TABLE=<env>-<project>-token-usage` to every runtime. `scripts/deploy_agents.py:244-251` merges `_shared/` into each agent's build context before zipping.
- **§13 api_handler-side IAM** — `Infra/templates/02-security.yaml:142-143` wildcard `table/${Environment}-${ProjectName}-*` (+ `/index/*`) already covers the new `-token-usage` table for the api_handler role. No edit needed.
- **§13 Master payload from api_handler** — `Infra/functions/api_handler/api_handler.py:220-266` `_handle_chat` now derives `persona` from `_caller_groups` (line 242-243) and forwards `actor_id`, `session_id`, `chat_type`, `persona` to the master runtime (lines 248-254).
- **§13 caller helpers confirmed** — `_caller_groups` at `api_handler.py:1415` (tolerant of list + comma-string forms); `_caller_claims` at 1430 (3-path resolution: APIGW authorizer → Bearer JWT → direct invoke). Both load-bearing for `_require_ciso`.
- **§13 Vitest tests** — `ui/src/__tests__/tokenTracking.test.jsx` (215 lines). Covers (1) sidebar gating per-persona, (2) `<Guarded>` route gating per-persona, (3) mock render KPI strip + ≥50 rows + chart sections, (4) agent filter narrows table, (5) CSV export invokes `URL.createObjectURL`. Mocks `recharts` to thin stubs and `useAuth` for persona injection.
- **§19 APP_VERSION** — `ui/src/config.js:59` reads `'1.3.0-poc'`. Bumped at least once for prior token-tracking PRs; **next ship needs another bump** per project convention.

## Partial (started, but incomplete or divergent)

- **§13 SAM API Gateway routes — MISSING ROUTE EVENTS** — `Infra/templates/06-api.yaml` has the Lambda env var but **no `TokenUsageGet`/`TokenUsageSummaryGet` `Events` blocks** under `ApiHandlerFunction`. Every other path (`/findings`, `/audit`, `/conversations`, etc.) is registered with a `Type: Api / Path: ... / Method: GET` event (see lines 259-401). Without these, API GW returns 404 for `/token-usage*` before the request ever reaches `api_handler.py`'s router. **Fix: add two events mirroring the `AuditGet` block at lines 309-314.** Tiny (~12 lines).
- **§13 AgentCore IAM — write Resource scoped to `-sessions` only** — `Infra/templates/09-agentcore.yaml:106-116` `SessionsTableWrite` statement only lists `table/${Environment}-${ProjectName}-sessions` under `Resource`. The 4 agents will get `AccessDenied` on `PutItem` to `-token-usage`. **Fix options: (a) widen the Resource to a wildcard like `02-security` does, or (b) add a second Resource entry for the `-token-usage` table.** Tiny (~3 lines). KMS side is already fine: line 178 already includes `DynamoDBKeyArn` in the `KMSDecrypt` statement (the CLAUDE.local.md gotcha is satisfied).
- **§6 Filter bar — "Custom" date range omitted** — `ui/src/pages/TokenTracking.jsx:26-30` has only Today/7d/30d. Spec §6 calls for a Custom option with two date inputs. Either small UI add or accept the divergence (the demo doesn't obviously need it).
- **§7 `user_email` field actually stores Cognito sub** — `agents/_shared/token_usage.py:158` writes `"user_email": actor_id_safe`, but `actor_id` is plumbed in from `_caller_user_id` which returns `claims["sub"]` (a UUID), not the user's email. Spec §7 shows `user_email: "ciso_diana@meridianinsurance.com"`. Mock data uses real emails (`mockData.js:677`), so the table UI will show a UUID in live mode and an email in mock mode. **Fix: either resolve the actual email from claims (`claims["email"]`) on the api_handler side and forward as a separate field, or rename the column.** Small fix; visible-to-CISO so worth doing.
- **§5/§6 KPI strip 4th card — "Active agents" replaced with "Guardrail-blocked"** — `TokenTracking.jsx:154-158` shows blocked-call count instead of "agents active today". Probably an intentional shift (it's actually more useful), but it diverges from the spec wireframe and the user stories. Either revise the spec to confirm or revert.
- **§9 Summary endpoint shape — no `by_agent` / `by_persona` breakdowns** — `_compute_token_summary` at `api_handler.py:590-598` returns `{totalTokens, inputTokens, outputTokens, totalCost, avgPerChat, chats, blocked}`. Spec §9 example shows `by_agent` and `by_persona` maps too. The UI doesn't render those today (it derives both client-side from `records`), so this is currently inert, but anyone consuming the API directly will miss them.

## Missing (not started)

- **No `/token-usage` route in API Gateway** — see Partial above; flagging it again because it's the single thing that will break live mode end-to-end. Without it, a CISO signing in to the deployed UI sees `404` from `useTokenUsage` and the page renders empty even though everything downstream is wired.
- **No IAM write permission for AgentCore on `-token-usage`** — see Partial above; without it, every chat will log `token_usage put_item failed (... AccessDenied ...)` and silently move on. The "best-effort" pattern hides the failure from the user but produces zero rows.
- **CSV export does not stream the full filtered set when over the cap** — `TokenTracking.jsx:108-117` exports `records` (the in-state set), which the live API now caps at `max_items=5000` server-side (`api_handler.py:502, 538`). Spec §13 says "the CSV export uses the full filtered set" — there's no pagination/next_token in the current flow. Probably fine at demo scale; flag for the spec revision.
- **No documented dev-persona switcher** — Spec §11 / §14 / §17 Q6 expects a `DEV_AUTH` mode in `useAuth.js::getGroups()`. The test harness has `isDevAuth/getDevPersonaId/setDevPersona` stubs (`tokenTracking.test.jsx:28-30`), but I didn't trace whether those exports actually exist in `ui/src/hooks/useAuth.js` (the test mock would mask their absence). Worth verifying — if absent, mock-mode CISO testing currently requires editing `mocks.groups` in tests or some other workaround.

## Risks surfaced during the read

- **Storage PK schema differs from spec.** Spec §7 specifies a single-attribute `usage_id` HASH key; the deployed table uses `pk` (HASH) + `sk` (RANGE) where `pk = "persona#<id>"` and `sk = "ts#<timestamp>#<session>#<agent>#<rand>"`. This is actually a better schema for the query patterns (main-table Query for "all rows for persona X in range" without needing the GSI), but the spec, the api_handler query strategy, and the mock fixture all need to agree. Right now: storage uses pk/sk, api_handler queries GSIs only (never the main table), mock data includes pk/sk fields (`mockData.js:672-673`). It works, but the main-table partition is unused. Either update spec to match deployed reality, or restructure to use the main table (cheaper).
- **`user_email` semantic mismatch (live vs mock).** Live writes hold the Cognito `sub` UUID; mock holds real emails. The UI's table column header is "User" and the cell shows `r.user_email` (`TokenTracking.jsx:307`). In live mode CISO will see UUIDs. Spec needs to either define `user_email` as "Cognito sub" (rename) or have api_handler forward the actual email separately.
- **Sessions table cardinality assumption.** `_compute_token_summary` derives `chats` from `len(unique session_ids)`. The spec §9 says "Avg tokens per chat (today)". For specialist rows that share the master's session_id, this counts correctly. For master rows where `session_id == "adhoc"` (direct invocations or testing), the code skips them (`api_handler.py:585-587`), which is correct. No bug — flagged because the metric definition depends on session_id population.
- **Spec §13 assumed `02-security.yaml` wildcard covered agentcore too.** Confirmed wrong: the wildcard at `02-security.yaml:142-143` belongs to the **api_handler** Lambda role, not the AgentCore role. The AgentCore role is in `09-agentcore.yaml` and is scoped tightly to `-sessions` only. Spec §13 row 2 ("**likely no-op**" for 02-security) is correct for api_handler but missed that 09-agentcore needs its own edit.
- **`agent.last_response.metrics.accumulated_usage`** — spec §8 references this accessor. The shipped code in `_shared/token_usage.py:91-99` instead reads `agent_result.metrics.accumulated_usage` (where `agent_result = agent(prompt)`), with a fallback to `agent_result.usage`. This is more robust (uses the return value rather than a side-effect attribute) and matches the actual Strands API as best I can tell from the import. Spec wording should be updated.
- **`_caller_groups` location in api_handler.py.** Spec §10 referenced line 1203; shipped code has it at **line 1415**. Just a line-number drift, not a behavioral issue, but worth correcting in the spec.
- **Master orchestrator MODEL_ID is Claude Sonnet 4.6 in this deploy** (per CLAUDE.local.md). PR #21 was needed because the master writes a Claude model_id into the usage table while specialists write Nova 2 Lite. Mixed-model rows will display correctly because `MODEL_PRICING` has both. No outstanding risk; flagged for the rebuild plan.
- **Range start-of-day edge case.** `TokenTracking.jsx:32-39` `startOfRange('today')` uses **local** time (`setHours(0,0,0,0)`), but the backend `_parse_token_usage_filters` (`api_handler.py:485-486`) uses **UTC** (`now.replace(hour=0,…)`). For a CISO in a non-UTC timezone, "Today" in mock mode and "Today" in live mode will not cover the same window. Small but real demo-day risk.

## Open questions for the user

1. **Schema reconciliation — pk/sk or usage_id?** The deployed table uses `pk`/`sk`, the spec calls for `usage_id`. Should the spec revision adopt the deployed pk/sk schema (and have api_handler use the main table partition where possible) or should we migrate the table to match the spec? Net cost is similar; ergonomically the deployed schema is friendlier for "all rows for persona X" queries.
2. **`user_email` semantics.** Should the field hold the Cognito `sub` (current live behavior, opaque UUID), the actual email (mock behavior, what the CISO would expect to read), or both — `user_id` for sub and `user_email` for email? Forwarding the email from `_handle_chat` is trivial; mock data is already email.
3. **Keep the 4th KPI card as "Guardrail-blocked"?** That replaces the spec's "Active agents" tile. The former is more actionable for governance; the latter is closer to spec. Confirm or revert.
4. **Add the missing "Custom" date range to the filter bar?** Spec §6 lists it; ship code doesn't. Worth adding for the CISO smoke-test, or accept Today/7d/30d as the v1 surface?
5. **CSV export caveats.** With a 5000-record server cap and no `next_token` pagination, very high-volume periods will silently truncate. Demo-acceptable, or should the export use a separate "stream-all-pages" path?

## Recommended direction (optional)

The shipped code is ~85% of the spec. The two critical bugs (missing API GW route events, missing IAM PutItem permission on `-token-usage`) are each ~3-12 lines and would be obvious blockers the first time someone signs in as the CISO in live mode. Beyond those, the largest decision is the `user_email`/`user_id` question — easy to get wrong, embarrassing in front of a CISO. I'd lean toward a small spec revision that (a) acknowledges the pk/sk schema as the canonical shape, (b) defines `user_email` to mean the email (and forward it from `_handle_chat`), (c) adds the two SAM events + the IAM Resource, (d) accepts the KPI-strip 4th-card change as an improvement. Then this ships in a single small PR.

## Verification commands run

```bash
# Frontend wiring
Grep "useTokenUsage|token-usage|tokenUsage|MOCK_TOKEN_USAGE|tokenUsageToCsv" path=ui/src
Read ui/src/components/Sidebar.jsx
Read ui/src/contexts/PersonaContext.jsx
Read ui/src/App.jsx
Read ui/src/pages/TokenTracking.jsx
Read ui/src/hooks/useApi.js  (offset 490, limit 100)
Read ui/src/mockData.js      (offset 620, limit 130)
Read ui/src/config.js
Read ui/src/__tests__/tokenTracking.test.jsx

# Backend wiring
Grep "token.usage|token_usage|TOKEN_USAGE|_require_ciso|TokenUsage" path=Infra
Read Infra/functions/api_handler/api_handler.py (offset 449, limit 200)
Grep "_caller_groups|def _caller_claims" path=Infra/functions/api_handler/api_handler.py
Read Infra/functions/api_handler/api_handler.py (offset 1410, limit 45)
Read Infra/functions/api_handler/api_handler.py (offset 220, limit 75)   # _handle_chat
Grep "Path:|Method:" path=Infra/templates/06-api.yaml                    # confirms no /token-usage events
Grep "token-usage|TokenUsage" path=Infra/templates/06-api.yaml
Read Infra/templates/06-api.yaml (offset 200, limit 60)
Read Infra/templates/04-storage.yaml (offset 325, limit 75)
Grep "DynamoDBKeyArn|KMSDecrypt|kms:Decrypt|token-usage|TokenUsage" path=Infra/templates/09-agentcore.yaml
Grep "dynamodb|table/" path=Infra/templates/09-agentcore.yaml
Read Infra/templates/09-agentcore.yaml (offset 100, limit 90)
Grep "dynamodb|table/|TokenUsage|token-usage" path=Infra/templates/02-security.yaml

# Agents
Glob agents/_shared/**
Read agents/_shared/token_usage.py
Grep "token_usage|record_usage|record_from_agent_result|_shared" path=agents
Read agents/master_orchestrator/agent.py (offset 500, limit 60)
Read agents/master_orchestrator/agent.py (offset 130, limit 50)
Read agents/sharepoint_specialist/agent.py (offset 80, limit 50)
Read agents/awsconfig_specialist/agent.py  (offset 200, limit 30)
Read agents/zscaler_specialist/agent.py    (offset 130, limit 30)
Read agents/{master_orchestrator,sharepoint_specialist,awsconfig_specialist,zscaler_specialist}/Dockerfile
Grep "TOKEN_USAGE_TABLE|token-usage|token_usage" path=scripts
Read scripts/deploy_agents.py (offset 240, limit 30)
Read scripts/deploy_agents.py (offset 495, limit 30)
```
