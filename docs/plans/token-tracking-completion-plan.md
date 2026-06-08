# Plan — Token Tracking Completion

**Spec:** [docs/specs/token-tracking-completion-spec.md](../specs/token-tracking-completion-spec.md)
**Original spec:** [Documents/token_tracking_spec.md](../../Documents/token_tracking_spec.md)
**Gap analysis:** [docs/research/token-tracking-gap-analysis.md](../research/token-tracking-gap-analysis.md)

## Approach in one paragraph

This PR closes the last 15% of the Token Tracking feature in five layers, ordered to keep the working tree green. We start with doc-only edits to the stale original spec so future readers stop tripping over it. Next we widen the shared backend contract (`record_usage` kwarg + `_compute_token_summary` breakdowns) since every other code change depends on those signatures. Then we plumb the user's email through the call chain — api_handler → master → 3 specialists — as a single lockstep group (no intermediate step is callable in isolation, but together they preserve mock-mode and live-mode behavior). Then frontend UI surfaces (UTC fix, KPI subtitle, per-persona cost, per-user breakdown card) and their Vitest coverage. Infra template edits (SAM Events block, IAM widening) come last so the human operator deploying knows nothing is dangling. Version bump is the second-to-last task before final test run.

## Files this PR touches

**Docs**
- `Documents/token_tracking_spec.md` — reconcile §5/§6/§7/§8/§10/§13 with shipped reality

**Backend (Python)**
- `agents/_shared/token_usage.py` — add `user_email` kwarg to `record_usage` and `record_from_agent_result`
- `Infra/functions/api_handler/api_handler.py` — forward `claims["email"]` in `_handle_chat`; enrich `_compute_token_summary` with `by_agent` / `by_persona` / `by_user`

**Agents (Python)**
- `agents/master_orchestrator/agent.py` — read `user_email`, stash in `_INVOCATION_CTX`, forward to specialists, pass to `record_from_agent_result`
- `agents/sharepoint_specialist/agent.py` — read + forward `user_email`
- `agents/awsconfig_specialist/agent.py` — read + forward `user_email`
- `agents/zscaler_specialist/agent.py` — read + forward `user_email`

**Frontend (JS)**
- `ui/src/pages/TokenTracking.jsx` — UTC `today` boundary, KPI subtitle, per-persona cost in chart, new `<UserBreakdownCard />`
- `ui/src/config.js` — bump `APP_VERSION` to `1.4.0-poc`

**Tests**
- `ui/src/__tests__/tokenTracking.test.jsx` — new cases for `user_email` distinct from `user_id`, per-user breakdown card render, KPI subtitle copy

**Infra (YAML)**
- `Infra/templates/06-api.yaml` — add `TokenUsageGet` + `TokenUsageSummaryGet` Events
- `Infra/templates/09-agentcore.yaml` — widen `SessionsTableWrite` Resource to also cover `-token-usage`

## Task list

The plan's task checklist is the **source of truth for build progress**.

### Phase 1 — Reconcile the original spec (doc only, no code risk)

- [x] **1. Reconcile §7 data model in `Documents/token_tracking_spec.md`.** — Replaced the `usage_id` flat-HASH JSON example and YAML schema with the deployed pk/sk composite shape (`pk = persona#<id>`, `sk = ts#<timestamp>#<session>#<agent>#<rand>`), named both GSIs, noted the main-partition Query path, and defined `user_id` (Cognito sub) vs `user_email` (forwarded claim).
  - **What:** Replace the `usage_id` HASH PK example (lines ~134-152) and the YAML schema block (lines ~154-189) with the deployed pk/sk composite shape: `pk = "persona#<id>"`, `sk = "ts#<timestamp>#<session>#<agent>#<rand>"`. Note both GSIs by name (`persona-time-index`, `agent-time-index`) and add a sentence noting that "all rows for persona X in range" can use the main partition without a GSI. Define `user_id` as Cognito `sub` and `user_email` as the user's real email forwarded from `_handle_chat`.
  - **Where:** `Documents/token_tracking_spec.md` lines ~130-200 (§7).
  - **Why:** Spec §4.5 — stop the original spec from contradicting shipped code.
  - **Verify:** `grep -n "usage_id" Documents/token_tracking_spec.md` returns only references inside §5 user stories or commented examples, not the schema definition.

- [x] **2. Reconcile KPI strip in §5 / §6 of `Documents/token_tracking_spec.md`.** — Swapped "Active agents" for "Guardrail-blocked" in §6 KPI list and updated the ASCII wireframe's 4th card from `4/4 agents active` to `N blocked / input billed`. No §5 user story referenced "active agents".
  - **What:** Replace "Active agents (count of distinct agents…)" with "Guardrail-blocked (count of blocked invocations…)". Update the ASCII wireframe `4/4 agents active` block (lines ~110-114) to read `N blocked / input billed` instead. Update any user-story callouts that reference "active agents".
  - **Where:** `Documents/token_tracking_spec.md` lines ~89-114 (§6) and any §5 user stories mentioning active agents.
  - **Why:** Spec §4.5.
  - **Verify:** `grep -ni "active agents" Documents/token_tracking_spec.md` returns no matches.

- [x] **3. Drop the "Custom" date range from §6 filter bar in `Documents/token_tracking_spec.md`.** — Removed `Custom + two dates` from the §3 in-scope bullet and the §6 filter-bar line; stripped the `Custom → server picks granularity` clause from the granularity description. The §6 wireframe line already showed only a single example range and needed no edit.
  - **What:** Remove "Custom + two dates" from the filter-bar prose (line ~94) and the wireframe (line ~116). Strip the "Custom → server picks granularity" clause from the granularity description on line ~96. Leave Today / 7d / 30d only.
  - **Where:** `Documents/token_tracking_spec.md` §6 (lines ~94-116) and any in-scope bullet in §3 (line ~32).
  - **Why:** Spec §4.5 — accept Today/7d/30d as v1 surface.
  - **Verify:** `grep -ni "Custom" Documents/token_tracking_spec.md` returns no date-range-related matches.

- [x] **4. Note the 5000-row CSV cap in §13 of `Documents/token_tracking_spec.md`.** — Added a "CSV export row cap" subsection at the end of §13 (before the Vitest tests subsection) noting `_query_token_usage_records`'s `max_items=5000` server-side cap and the explicit no-`next_token` v1 behavior.
  - **What:** Add an inline note that the CSV export uses the in-state `records` array, which the live API caps at 5000 rows server-side (per `_query_token_usage_records`'s `max_items=5000`). Larger-than-cap exports are accepted v1 behavior; no `next_token` cursor pagination.
  - **Where:** `Documents/token_tracking_spec.md` §13 (around the file-changes table mentioning the CSV export, or as a separate paragraph at end of §13).
  - **Why:** Spec §4.5.
  - **Verify:** `grep -n "5000" Documents/token_tracking_spec.md` returns at least one match in §13.

- [x] **5. Fix drifted line numbers + accessor names in §8 / §10 of `Documents/token_tracking_spec.md`.** — Updated both `_caller_groups` line refs (62 + 360) from line 1203 to line 1415; rewrote the §8 capture-point code block to use `agent_result = agent(prompt)` then `agent_result.metrics.accumulated_usage` instead of `agent.last_response.metrics.accumulated_usage`; updated the §13 master_orchestrator row to match.
  - **What:** Update `_caller_groups` reference from "line 1203" to "line 1415" (§10, line ~62 and ~355). Update the capture-point code block (§8, lines ~210-225) to use `agent_result.metrics.accumulated_usage` (where `agent_result = agent(prompt)`) instead of `agent.last_response.metrics.accumulated_usage`. Update the §13 row for `master_orchestrator/agent.py` (line ~448) to match.
  - **Where:** `Documents/token_tracking_spec.md` lines ~62, ~210-225, ~355, ~448.
  - **Why:** Spec §4.5 — line numbers and Strands accessor have drifted.
  - **Verify:** `grep -n "last_response" Documents/token_tracking_spec.md` returns no matches; `grep -n "1203" Documents/token_tracking_spec.md` returns no matches.

### Phase 2 — Shared backend contracts (signature changes other code depends on)

- [x] **6. Add `user_email` kwarg to `agents/_shared/token_usage.py`.** — Added keyword-only `user_email: str = ""` to both `record_usage` and `record_from_agent_result`; `record_usage` writes `(user_email or "")[:200] or actor_id_safe` to the row so legacy callers without the kwarg still get a non-empty `user_email`; `record_from_agent_result` forwards the kwarg through.
  - **What:** Add a keyword-only `user_email: str = ""` parameter to both `record_usage` (current signature at lines 115-126) and `record_from_agent_result` (lines 175-184). Inside `record_usage`, use `user_email` for the row's `user_email` attribute instead of the current `actor_id_safe` fallback (line 158). If `user_email` is empty, fall back to `actor_id_safe` so legacy callers do not regress. `record_from_agent_result` forwards the new kwarg through to `record_usage`.
  - **Where:** `agents/_shared/token_usage.py` lines 115-198.
  - **Why:** Spec §4.2 — `user_email` carries the user's real email; `user_id` (via `actor_id`) keeps the Cognito `sub`.
  - **Verify:** `grep -n "user_email" agents/_shared/token_usage.py` shows the new kwarg on both function signatures; default of `""` preserves caller backwards-compat.

- [x] **7. Enrich `_compute_token_summary` in `api_handler.py` with three breakdown maps.** — Extended the existing loop with per-record agent/persona/user buckets accumulating tokens/cost/count (cost as float to match the existing `totalCost` pattern); `by_user` tracks `_latest_ts` per email so `persona` reflects the most-recent row; output dict rounds each bucket's cost to 6 dp (no Decimal leakage). Verified by inline smoke run: existing keys preserved, `by_agent`/`by_persona`/`by_user` populated, JSON serializes cleanly.
  - **What:** In one additional pass over `records` (extend the loop at lines 569-587 or add a second pass), produce: `by_agent: { agent_id: { tokens, cost, count } }`, `by_persona: { persona: { tokens, cost, count } }`, and `by_user: { user_email: { tokens, cost, count, persona } }` (persona on `by_user` is the persona on the user's most-recent row in the window — track latest `ts` as you go). Keep `cost` accumulation as `Decimal` per the file's existing pattern; serialize to `float` only in the returned dict. Add the three maps to the returned object alongside the existing KPI fields.
  - **Where:** `Infra/functions/api_handler/api_handler.py` `_compute_token_summary` at lines 563-598.
  - **Why:** Spec §4.4 — symmetric API + future-proof.
  - **Verify:** Hit `/token-usage/summary` against mock-mode handler (or run a unit-style invocation locally) and confirm `by_agent`, `by_persona`, `by_user` keys appear in the response shape; `by_user` keys are emails; existing KPI keys (`totalTokens`, etc.) unchanged.

### Phase 3 — Email plumbing (lockstep group; tasks 8-12 must all land together)

> **Lockstep note:** Tasks 8-12 modify a single call chain (api_handler → master → 3 specialists, sharing the helper from task 6). The implementer should not stop mid-chain. Old api_handler + new agents is safe (agents read `payload.get("user_email", "")`); new api_handler + old agents is safe (agents ignore the extra payload key). All-new is the goal.

- [x] **8. Forward `claims["email"]` from `_handle_chat` in `api_handler.py`.** — After `_caller_groups(event)` at line 242, pulled `claims = _caller_claims(event)` then `user_email = (claims.get("email") or "")[:200]`; added `"user_email": user_email` to the master invoke payload alongside the existing four keys.
  - **What:** After the existing `_caller_groups(event)` call at line 242, extract the caller's claims and pull `email` (with `""` fallback). Add `user_email` to the payload dict at lines 248-254 alongside the existing `actor_id` / `persona` / `session_id` / `chat_type` fields.
  - **Where:** `Infra/functions/api_handler/api_handler.py` `_handle_chat`, lines 232-254.
  - **Why:** Spec §4.2 — single source for the real email.
  - **Verify:** `grep -n "user_email" Infra/functions/api_handler/api_handler.py` shows the new field in `_handle_chat`'s payload dict; the surrounding `actor_id` / `persona` / `session_id` / `chat_type` keys are unchanged.

- [x] **9. Read + forward `user_email` in `master_orchestrator/agent.py`.** — Read `user_email` in `invoke()`, added it to `_INVOCATION_CTX.update(...)`, passed `user_email=user_email` into `record_from_agent_result(...)`, and added `"user_email": _INVOCATION_CTX.get("user_email", "")` to the specialist payload built in `_invoke_runtime`.
  - **What:** Three edits in `invoke()` (entrypoint at line 482). (a) After the existing `persona = ...` read at line 498, read `user_email = (payload.get("user_email") or "")[:200]`. (b) Add `"user_email": user_email` to the `_INVOCATION_CTX.update(...)` block at lines 501-504. (c) Pass `user_email=user_email` to the `record_from_agent_result(...)` call at lines 529-532. Separately, in `_invoke_runtime` (line 140), add `"user_email": _INVOCATION_CTX.get("user_email", "")` to the JSON payload alongside the existing four keys at lines 154-157.
  - **Where:** `agents/master_orchestrator/agent.py` lines 130-167 (`_INVOCATION_CTX` + `_invoke_runtime`) and lines 482-532 (`invoke`).
  - **Why:** Spec §4.2.
  - **Verify:** `grep -n "user_email" agents/master_orchestrator/agent.py` shows three new references (read, ctx update, record_from_agent_result kwarg) and one in the specialist payload.

- [x] **10. Read + forward `user_email` in `sharepoint_specialist/agent.py`.** — Read `user_email = (payload.get("user_email") or "")[:200]` next to the existing attribution reads and passed `user_email=user_email` into `record_from_agent_result(...)`.
  - **What:** In `invoke()` (line 95), add `user_email = (payload.get("user_email") or "")[:200]` next to the existing `actor_id` / `persona` / `session_id` / `chat_type` reads at lines 102-105. Pass `user_email=user_email` into the `record_from_agent_result(...)` call at lines 110-113.
  - **Where:** `agents/sharepoint_specialist/agent.py` lines 95-114.
  - **Why:** Spec §4.2.
  - **Verify:** `grep -n "user_email" agents/sharepoint_specialist/agent.py` shows two references (read + kwarg pass).

- [x] **11. Read + forward `user_email` in `awsconfig_specialist/agent.py`.** — Same pattern as task 10: read `user_email` from the payload and forwarded it into `record_from_agent_result(...)`.
  - **What:** Same pattern as task 10, in `invoke()` at lines 204-221.
  - **Where:** `agents/awsconfig_specialist/agent.py` lines 204-221.
  - **Why:** Spec §4.2.
  - **Verify:** `grep -n "user_email" agents/awsconfig_specialist/agent.py` shows two references.

- [x] **12. Read + forward `user_email` in `zscaler_specialist/agent.py`.** — Same pattern as task 10: read `user_email` from the payload and forwarded it into `record_from_agent_result(...)`.
  - **What:** Same pattern as task 10, in `invoke()` at lines 136-153.
  - **Where:** `agents/zscaler_specialist/agent.py` lines 136-153.
  - **Why:** Spec §4.2.
  - **Verify:** `grep -n "user_email" agents/zscaler_specialist/agent.py` shows two references.

### Phase 4 — Frontend (UTC fix → KPI subtitle → per-persona cost → per-user card)

- [x] **13. Use UTC midnight for the `today` range boundary in `TokenTracking.jsx`.** — Rewrote `startOfRange('today')` to construct the boundary via `Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 0, 0, 0, 0)`; non-today branch unchanged.
  - **What:** Rewrite `startOfRange('today')` (lines 32-39) to build the boundary via `Date.UTC(...)`: take `new Date()`, then construct `Date.UTC(y, m, d, 0, 0, 0, 0)` from its `getUTCFullYear/Month/Date`. This matches the backend `_parse_token_usage_filters` UTC cutoff. Keep the non-today branch unchanged.
  - **Where:** `ui/src/pages/TokenTracking.jsx` lines 32-39.
  - **Why:** Spec §4.3 — fixes the local-vs-UTC drift between mock and live mode.
  - **Verify:** Open the page in mock mode in a non-UTC timezone (or run the test in task 18), confirm the "Today" filter yields the same record set whether system tz is UTC or PT.

- [x] **14. Update the "Estimated cost" KPI subtitle copy in `TokenTracking.jsx`.** — Added inline `PERSONA_LABELS` map and a `costSubtext` useMemo; renders `Across all personas · Nova 2 Lite list pricing` when filter is `all`, else `<Label> · Nova 2 Lite list pricing` (e.g. `CISO · Nova 2 Lite list pricing`).
  - **What:** Replace the hardcoded `subtext="Nova 2 Lite list pricing"` (line 146) with a computed value: when `personaFilter === 'all'`, render `"Across all personas · Nova 2 Lite list pricing"`; otherwise render `"<Persona display name> · Nova 2 Lite list pricing"`. Reuse the persona display map (e.g. `{ ciso: 'CISO', soc: 'SOC', grc: 'GRC', employee: 'Employee' }`) defined inline near the top of the file.
  - **Where:** `ui/src/pages/TokenTracking.jsx` line 146 (KPI strip).
  - **Why:** Spec §4.8 — make combined-cost framing explicit.
  - **Verify:** In mock mode with persona filter `all`, KPI subtitle reads `Across all personas · Nova 2 Lite list pricing`; flip to CISO and it reads `CISO · Nova 2 Lite list pricing`.

- [x] **15. Add per-persona cost to the "Tokens by persona" chart in `TokenTracking.jsx`.** — Extracted reducer as exported named helper `byPersonaWithCost(records)` returning `[{ persona, total, cost }]`; chart `<Bar dataKey="total">` unchanged. Cost surfaces via a `<PersonaTooltip>` custom content-prop tooltip (sidesteps the per-dataKey `formatter` limitation noted in the plan's architect risk).
  - **What:** Extend the `byPersona` reducer (lines 101-106) so each entry carries both `total` and `cost`. Add `acc[r.persona] = { tokens: prev.tokens + r.total_tokens, cost: prev.cost + (r.estimated_cost || 0) }` then map to `{ persona, total, cost }` in the returned array. In the chart's `<Tooltip>` (line 240) add a `formatter` that returns both `formatTokens(tokens)` and `formatCost(cost)` rows, or supply a `content` prop with a custom tooltip element. The visible bar still uses `dataKey="total"`; the cost appears in the tooltip and/or as a small `<text>` label inside the bar. Sum of the four per-persona costs must equal `summary.totalCost` (modulo rounding) when persona filter is `all`.
  - **Where:** `ui/src/pages/TokenTracking.jsx` lines 101-106 (reducer) and lines 234-247 (chart card).
  - **Why:** Spec §4.8.
  - **Verify:** Hover any persona bar in mock mode and see a cost figure; four bars' costs add up to KPI strip cost when filter is `all`.

- [x] **16. Add `<UserBreakdownCard />` between charts and records table in `TokenTracking.jsx`.** — New inline component `UserBreakdownCard({ records })` defined alongside `KpiCard`/`ChartCard`; `useMemo` groups records by `user_email` (empty bucketed under `(unknown)`) producing `{ email, persona, chats: distinct session count, tokens, cost, latestTs }`, persona = most-recent row's persona, sorted desc by tokens. Card styled like the records table (rounded-xl, header, hover rows) with columns User · Persona (uses `PERSONA_COLORS`) · Chats · Tokens · Cost. Empty state: "No usage records in this range." Slotted at JSX line 285 between the charts grid and records table, gated on `!loading` so the empty-state row does not satisfy DOM waits for the records table while data is still in flight. **Departure:** column headers shortened from "Total tokens"/"Estimated cost" to "Tokens"/"Cost" to (a) match the existing records-table column-naming pattern below it and (b) avoid colliding with the KPI strip's "Estimated cost" label under the existing `getByText(/Estimated cost/i)` assertion in `tokenTracking.test.jsx` (test edits are out of scope for this phase). Cell formatters (`formatTokens`, `formatCost`) unchanged.
  - **What:** Define an inline component `UserBreakdownCard({ records })` (placed near the existing `KpiCard` / `ChartCard` helpers at the bottom of the file). It does a `useMemo` over `records` grouping by `user_email` (bucket empty emails under `(unknown)`). For each user it produces `{ email, persona, chats: distinct session_id count, tokens, cost }`. `persona` is the persona on the user's most-recent row in the window (track `latestTs` per user). Sort the array descending by `tokens`. Render a `<table>` matching the existing records-table styling (rounded-xl card, header row, hover row class). Columns: User, Persona (styled with `PERSONA_COLORS`), Chats, Total tokens (`formatTokens`), Estimated cost (`formatCost`). When the array is empty, show "No records in range" empty state matching the per-record table. Insert `<UserBreakdownCard records={records} />` between the charts grid closing tag (line ~247) and the records-table conditional block (line ~250).
  - **Where:** `ui/src/pages/TokenTracking.jsx` — new component at bottom of file, new render slot between lines 247-250.
  - **Why:** Spec §4.7 — design-review surface.
  - **Verify:** Mock mode renders a card titled (e.g.) "Token usage by user" with ≥ 4 distinct email rows, sorted descending by total tokens, each row showing email, persona, chat count, total, cost.

### Phase 5 — Tests

- [x] **17. Append "user_email distinct from user_id" assertion to `tokenTracking.test.jsx`.** — Added describe block asserting `getAllByText(/@/)` finds email-shaped cells after mock CISO render; sidesteps the missing-`user_id` mock-mode reality.
  - **What:** Add a new `describe` block "TokenTracking — user_email distinct from user_id" with one `it` that renders the page in CISO mock mode, waits for table rows, then asserts at least one cell in the User column contains `@` (email regex). Since mock rows do not carry a `user_id` field in `mockData.js`, the assertion is that the email column is populated with email-shaped strings (the inverse of the bug we are fixing).
  - **Where:** `ui/src/__tests__/tokenTracking.test.jsx` append after the existing CSV export block (after line 213).
  - **Why:** Spec §6 test plan.
  - **Verify:** `cd ui && npx vitest run src/__tests__/tokenTracking.test.jsx -t "user_email distinct"` passes.

- [x] **18. Append "per-user breakdown card" Vitest case.** — Locates the card via the literal heading "Token usage by user", climbs to its `div.rounded-xl`, asserts ≥ 5 rows, parses the Tokens column (4th cell) with K/M-suffix-aware parser, confirms descending order and every User cell is email-shaped.
  - **What:** New `describe` "TokenTracking — per-user breakdown card" with one `it`: render mock CISO, wait for data, find the card by its heading text (e.g. "Token usage by user"), then within that card assert `screen.getAllByRole('row')` length ≥ 5 (header + 4 data rows). Pull the first two data rows' "Total tokens" cells; parse to numbers (strip formatting); assert `first >= second` to confirm descending order. Confirm each visible email cell matches `/@/`.
  - **Where:** `ui/src/__tests__/tokenTracking.test.jsx` append after task 17's block.
  - **Why:** Spec §6 test plan.
  - **Verify:** `cd ui && npx vitest run src/__tests__/tokenTracking.test.jsx -t "per-user breakdown"` passes.

- [x] **19. Append "KPI subtitle reflects persona filter" Vitest case.** — Two `it` blocks: default render asserts `/Across all personas/i`; second flips the persona `<select>` to `ciso` via `fireEvent.change` and asserts `findByText(/CISO · Nova 2 Lite/i)`.
  - **What:** New `describe` "TokenTracking — KPI subtitle" with two `it` cases. (a) Default render asserts the text `Across all personas` appears in the document. (b) Change the persona `<select>` to `ciso` (existing pattern at line 168-170), wait for re-render, assert the text `CISO` appears as part of the cost-card subtitle (use `findByText` with a partial-match regex, e.g. `/CISO · Nova 2 Lite/`).
  - **Where:** `ui/src/__tests__/tokenTracking.test.jsx` append after task 18's block.
  - **Why:** Spec §6 test plan.
  - **Verify:** `cd ui && npx vitest run src/__tests__/tokenTracking.test.jsx -t "KPI subtitle"` passes.

- [x] **20. Append a `byPersonaWithCost` reducer smoke Vitest case.** — Replaced the plan's original `_computeTokenSummary` task with the architect-recommended `byPersonaWithCost` direct reducer test (per Risks §6). Imported the named export from `../pages/TokenTracking`, hand-computed a 6-row fixture spanning all four personas, asserted canonical order, summed-cost equality within 1e-6, and per-persona `total` matches summed `total_tokens`.
  - **What:** Import `_computeTokenSummary` from `ui/src/hooks/useApi.js` (export it if not already exported — check first; if not exported, skip this task and document in PR description; the JS helper does not produce breakdown maps in v1, only the Python summary endpoint does). If exportable, smoke-test that it returns objects with the existing keys and tolerates records carrying a non-numeric `estimated_cost`. The spec marks the JS-side breakdown shapes as not required for mock mode.
  - **Where:** `ui/src/__tests__/tokenTracking.test.jsx`.
  - **Why:** Spec §6 test plan (smoke only).
  - **Verify:** Test passes or task is marked N/A in PR description with reason.

### Phase 6 — Version + Infra (deploy-side last)

- [x] **21. Bump `APP_VERSION` to `1.4.0-poc`.** — Replaced `'1.3.0-poc'` with `'1.4.0-poc'` on line 59 of `ui/src/config.js`. No other call sites consume the literal.
  - **What:** Change the string at `ui/src/config.js` line 59 from `'1.3.0-poc'` to `'1.4.0-poc'`.
  - **Where:** `ui/src/config.js` line 59.
  - **Why:** Project convention; spec §5 acceptance criterion.
  - **Verify:** `grep -n "APP_VERSION" ui/src/config.js` reads `1.4.0-poc`; sidebar footer reflects new version after `npm run build`.

- [x] **22. Add `TokenUsageGet` + `TokenUsageSummaryGet` Events to `06-api.yaml`.** — Inserted both Event blocks immediately after `AuditList` on `ApiHandlerFunction.Properties.Events`, exact-mirroring the `AuditList` shape (no `Auth:` override → inherits the Cognito JWT authorizer). `aws cloudformation validate-template` returned success.
  - **What:** Under `ApiHandlerFunction.Properties.Events` (between the `AuditList` block at lines 309-314 and the `ChatPost` block at lines 315-320), insert two new Event blocks mirroring `AuditList`'s shape exactly:
    ```yaml
        TokenUsageGet:
          Type: Api
          Properties:
            RestApiId: !Ref ArbiterApi
            Path: /token-usage
            Method: GET
        TokenUsageSummaryGet:
          Type: Api
          Properties:
            RestApiId: !Ref ArbiterApi
            Path: /token-usage/summary
            Method: GET
    ```
    No `Auth:` block — both inherit the API's Cognito JWT authorizer. The Lambda's `_require_ciso` guard enforces in-handler RBAC.
  - **Where:** `Infra/templates/06-api.yaml` line ~314 (immediately after `AuditList`).
  - **Why:** Spec §4.1 — without this, API GW returns 404 before the request reaches the Lambda router.
  - **Verify:** `aws cloudformation validate-template --template-body file://Infra/templates/06-api.yaml --region us-east-1` returns success; `grep -n "TokenUsageGet\|TokenUsageSummaryGet" Infra/templates/06-api.yaml` confirms both blocks present.

- [x] **23. Widen the AgentCore IAM `SessionsTableWrite` Resource in `09-agentcore.yaml`.** — Renamed `Sid: SessionsTableWrite` → `Sid: AgentCoreTablesWrite`; updated the inline comment to mention both `sessions` (master conv-index/last_message_at) and `token-usage` (every agent records); added the `-token-usage` table ARN as a second `!Sub` entry under `Resource`. Actions unchanged (`PutItem`/`UpdateItem`/`GetItem`). `KMSDecrypt` left alone. `aws cloudformation validate-template` returned success.
  - **What:** Modify the statement at lines 106-116. Rename `Sid: SessionsTableWrite` to `Sid: AgentCoreTablesWrite`, update the comment block above it to mention both tables, and add a second `!Sub` line to the `Resource` list for `arn:aws:dynamodb:${AWS::Region}:${AWS::AccountId}:table/${Environment}-${ProjectName}-token-usage`. Keep `dynamodb:PutItem` / `UpdateItem` / `GetItem` actions unchanged. (KMS side is already correct — the `KMSDecrypt` block at line 166-178 already includes `DynamoDBKeyArn`.)
  - **Where:** `Infra/templates/09-agentcore.yaml` lines 106-116.
  - **Why:** Spec §4.1 — without this, every agent `PutItem` to `-token-usage` returns `AccessDenied`.
  - **Verify:** `aws cloudformation validate-template --template-body file://Infra/templates/09-agentcore.yaml --region us-east-1` returns success; `grep -n "token-usage" Infra/templates/09-agentcore.yaml` shows the new Resource line.

- [x] **24. Final test sweep.** — `cd ui && npm test` → Vitest v2.1.9, 8 test files / 155 tests passed (Duration 2.56s).
  - **What:** Run the full Vitest suite to confirm nothing regressed.
  - **Where:** `ui/`.
  - **Why:** Definition of done.
  - **Verify:** `cd ui && npm test` (or `npx vitest run`) — all suites green.

## Sequencing rules and gotchas

- **Do not deploy.** This plan only produces code changes. Rollout (`Infra/deploy.sh`, `scripts/deploy_agents.py`, S3 sync) is human-gated per spec §9 and is not a task in this plan.
- **Lockstep:** tasks 8-12 form one chain. The implementer should not stop in the middle. Each individual task is harmless if a downstream task is missing (defaults to `""`), but the user-visible behavior only lands when all five are in.
- **Shared-code copy:** `agents/_shared/token_usage.py` is `COPY`-ed into each agent image by each agent's `Dockerfile`. The implementer does not need to edit Dockerfiles for the kwarg change.
- **06-api redeploy wipes env vars.** Per the project's MEMORY.md, redeploying 06-api resets `MASTER_AGENT_RUNTIME_ARN` and `MEMORY_ID` on the api_handler Lambda. This is the operator's problem at rollout time, not the implementer's — the rollout step in spec §9 re-runs `deploy_agents.py` after the infra deploy, which re-patches both env vars. The implementer should not touch these env vars in this PR.
- **APP_VERSION bump (task 21)** must be one of the last tasks. It runs after all UI tasks (13-16) and after the Vitest additions (17-20) so a mid-build commit does not accidentally publish a half-built feature under the new version. Tasks 22-24 follow.
- **Editing the spec doc (tasks 1-5)** should be the very first work. The doc edits unblock the implementer's mental model and remove the risk of copy-pasting stale code blocks from the original spec into new code.

## Risks the architect surfaces beyond the spec

- **Vitest mock for `useAuth` already exports `getEmail` (line 18 of `tokenTracking.test.jsx`), so the test mock already understands the email concept.** Good — the new assertions in tasks 17-19 do not need to widen the mock surface. Confirmed by reading the test file at lines 11-31.
- **`useApi.js::useTokenUsage` already handles the live-mode summary as `sum || _computeTokenSummary(rs)` (line 569).** Adding `by_agent` / `by_persona` / `by_user` to the live summary therefore propagates straight to the page without a hook change. No edit to `useApi.js` is needed for this PR.
- **Mock records carry no `user_id` field today** (`mockData.js::_buildRecord` at lines 670-688 only writes `user_email`). The "user_email distinct from user_id" assertion in task 17 should therefore only assert the User cell shows an email — not that `user_id` is a UUID — because the mock path never had a UUID. The live-mode acceptance criterion (spec §5: "the two fields are distinct") is covered by manual backend smoke, not by a unit test.
- **`_computeTokenSummary` may not currently be exported from `useApi.js`.** Task 20 hedges accordingly. The implementer should grep before writing the test and skip task 20 with a PR-description note if it is not exportable. The JS-side breakdown maps are not on the spec's acceptance critical-path.
- **Recharts `<Tooltip>` `formatter` callback** in task 15 will fire per-`dataKey`. If the implementer uses the per-key formatter pattern, only the `total` key triggers it and cost will need to come from a `content`-prop custom tooltip. Either approach works; the implementer picks the smaller diff. The cost can also be rendered as a small `<text>` label inside each `<Bar>` using a `LabelList` — this is the simplest path if tooltip plumbing proves fiddly.
- **The `tokenTracking.test.jsx` `recharts` stub** (lines 37-48) returns `null` for `<Tooltip>`, so the per-persona cost in the tooltip will not render in tests. The acceptance is that the reducer is correct (sum of four costs equals KPI), which the implementer can assert by reading the chart's `data` prop via a Recharts mock that captures it — or by exporting the reducer and testing it directly. Recommended: export `byPersonaWithCost(records)` from `TokenTracking.jsx` as a named helper and unit-test the reducer in isolation. Cheap and removes the recharts-stub coupling.
- **`api_handler.py::_compute_token_summary`** currently returns `totalCost` as a `float` rounded to 6 dp (line 594). The new `by_user` / `by_agent` / `by_persona` maps should follow the same pattern (`float`, rounded). Decimals leak out otherwise and trip `json.dumps` downstream. Implementer should grep the file for an existing `_decimal_default` JSON encoder before assuming `float()` casting is required — `api_handler.py` already serializes Decimals via a default encoder in `_ok()`, but the breakdown maps should cast for consistency with `totalCost`.

## Out of plan

- Custom date range (`from`/`to` inputs in the filter bar) — spec §4.6 non-goal.
- CSV cursor pagination beyond 5000 rows — spec §4.6 non-goal.
- Multi-tenant chargeback views or per-org rollups — spec §4.6 non-goal.
- DDB schema migration to `usage_id` flat HASH key — spec §4.6 non-goal; pk/sk composite is canonical.
- Backfill of historical rows that were missed before this PR ships — spec §4.6 non-goal.
- Per-user trend lines / time-series chart per email — spec §4.6 non-goal (v1 ships an aggregate ranked table).
- Adding breakdown maps to the JS-side mock summary in `_computeTokenSummary` — spec §8 open question, explicitly not required for acceptance.
- Hard-truncating the per-user card at top-N — spec §8 open question, v1 ships uncapped.
- Any change to the `Sidebar.jsx` / `PersonaContext.jsx` / `App.jsx` gating — already shipped in PR #18, no regression expected.
- Any change to `04-storage.yaml` (TokenUsageTable exists and is correctly schemed) or `02-security.yaml` (api_handler wildcard already covers the table) — no edits required.
- Any change to agent `Dockerfile`s or `scripts/deploy_agents.py` — `_shared/` copy and `TOKEN_USAGE_TABLE` env var already wired.
- Deployment, rebuild, and CloudFront invalidation — operator-gated, not implementer-gated.
