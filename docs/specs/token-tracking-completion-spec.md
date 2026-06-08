# Spec — Token Tracking Completion (gap-closing PR)

**Status:** Draft — for human approval
**Supersedes:** N/A — this is a completion delta on top of [`Documents/token_tracking_spec.md`](../../Documents/token_tracking_spec.md) (the original 582-line spec).
**Reads alongside:** [`Documents/token_tracking_spec.md`](../../Documents/token_tracking_spec.md), [`docs/research/token-tracking-gap-analysis.md`](../research/token-tracking-gap-analysis.md).

## 1. Summary

PRs #18 and #21 landed roughly 85% of the original Token Tracking spec. The
frontend page, mock data, Vitest suite, DDB table, Lambda handlers, agent
capture helper, and Dockerfiles are all live on `main`. This PR closes the
remaining gap: two hard-failure backend wiring bugs that keep live mode from
recording any rows, a small set of code/spec reconciliations the gap analysis
surfaced, a documentation update to the original spec so future readers do
not start from a stale source, and two UI surfaces requested in design review
(per-user breakdown and explicit combined-cost framing).

## 2. Why this PR is needed

Without this PR a CISO signed into the deployed UI will see an empty Token
Tracking page even after an analyst chat completes. The API Gateway has no
route registered for `/token-usage`, so requests 404 before they reach the
Lambda router. Independently, the AgentCore IAM role only permits
`dynamodb:PutItem` on the `-sessions` table, so even if the routes were
present, the agents' best-effort `record_usage` calls would silently fail with
`AccessDenied`. Both fixes are small and well-isolated. While we are in the
file, a handful of spec divergences (schema, user email semantics, KPI cards,
date-range options, CSV cap) should be ratified so the original spec stops
contradicting reality. Design review also asked for two additional UI surfaces
to make per-user consumption and the combined cost across personas legible to
a CISO at a glance.

## 3. Decisions ratified (from gap analysis review)

- **DDB schema:** the deployed `pk` / `sk` composite schema is canonical. No
  table migration. Update spec wording to match.
- **`user_email` field:** carries the user's real email, forwarded from
  `api_handler._handle_chat` via `claims["email"]`. `user_id` continues to
  carry the Cognito `sub`. Single source for each meaning, both fields
  preserved.
- **4th KPI card:** "Guardrail-blocked" stays. The original spec said "Active
  agents"; the implementation choice is better. Spec wording updates, no code
  change.
- **Custom date range:** out of scope for v1. Ship Today / 7d / 30d only.
- **Per-user breakdown surface:** added as a new card on the Token Tracking
  page (see §4.7). Rendered as a ranked table, not a chart.
- **Combined-cost framing:** the "Estimated cost" KPI gets an explicit
  "Across all personas" subtitle, and the "Tokens by persona" chart gets a
  parallel per-persona cost reducer (see §4.8).

## 4. Scope — what this PR changes

**Source of truth: DynamoDB.** In live mode every figure on the Token
Tracking page comes from the `dev-st21arbiter-poc-token-usage` table, queried
by `Infra/functions/api_handler/api_handler.py::_query_token_usage_records`
(main partition Query when persona is fixed, GSI Query otherwise). The UI
reaches it via `apiFetch('/token-usage')` and `apiFetch('/token-usage/summary')`.
In mock mode (`USE_MOCK = true`) every figure derives from `MOCK_TOKEN_USAGE`
in [`ui/src/mockData.js`](../../ui/src/mockData.js). No other data path is
introduced by this PR.

### 4.1 Critical fixes (live mode is broken without these)

- **`Infra/templates/06-api.yaml`** — under
  `ApiHandlerFunction.Properties.Events`, add two new event blocks mirroring
  the `AuditList` block at lines 309-314:
  - `TokenUsageGet` — `Type: Api`, `RestApiId: !Ref ArbiterApi`,
    `Path: /token-usage`, `Method: GET`.
  - `TokenUsageSummaryGet` — `Type: Api`, `RestApiId: !Ref ArbiterApi`,
    `Path: /token-usage/summary`, `Method: GET`.
  No auth override — both inherit the API's Cognito JWT authorizer. The
  Lambda's `_require_ciso` guard handles in-handler RBAC.
- **`Infra/templates/09-agentcore.yaml` lines 106-116** — widen the
  `SessionsTableWrite` statement so it also covers `-token-usage`. Either:
  - (a) rename to `AgentCoreTablesWrite` and add a second `!Sub` line for
    `table/${Environment}-${ProjectName}-token-usage` to `Resource`, or
  - (b) replace the single Resource entry with a wildcard
    `table/${Environment}-${ProjectName}-*` matching the api_handler role's
    pattern in `02-security.yaml:142-143`.
  Recommendation: option (a). It keeps the principle of least privilege
  closer to the existing pattern, and the comment is easy to update.
  The KMS side is already correct — `09-agentcore.yaml:178` already includes
  `DynamoDBKeyArn` in `KMSDecrypt`, satisfying the CLAUDE.local.md gotcha for
  the new table (which is encrypted with the same key).

### 4.2 Email plumbing

- **`Infra/functions/api_handler/api_handler.py::_handle_chat`** — extract
  `claims.get("email")` (fall back to empty string when missing) and add it as
  `user_email` in the master invocation payload alongside the existing
  `actor_id`, `persona`, `session_id`, `chat_type` fields.
- **`agents/master_orchestrator/agent.py`** — read `user_email` from the
  incoming chat payload, stash in `_INVOCATION_CTX` (the dict at line 137),
  and forward it to specialists inside `_invoke_runtime`'s payload (the block
  at lines 154-157). Pass `user_email` into the master's own
  `record_from_agent_result` call (lines 529-532).
- **`agents/sharepoint_specialist/agent.py`**,
  **`agents/awsconfig_specialist/agent.py`**,
  **`agents/zscaler_specialist/agent.py`** — read `user_email` from the
  incoming specialist payload (next to the existing `actor_id` / `persona` /
  `session_id` reads) and pass it into `record_from_agent_result`.
- **`agents/_shared/token_usage.py::record_usage`** and
  **`record_from_agent_result`** — accept `user_email` as a kwarg, defaulting
  to `""`. Use it for the row's `user_email` attribute instead of the current
  fallback to `actor_id`. `user_id` continues to carry the Cognito `sub`
  (`actor_id`).

### 4.3 Timezone alignment

- **`ui/src/pages/TokenTracking.jsx::startOfRange`** — for the `"today"`
  branch, use `Date.UTC(...)` to build the boundary so the UI cutoff matches
  the backend's UTC cutoff in
  `api_handler.py::_parse_token_usage_filters`. This removes the local-vs-UTC
  mismatch between mock and live mode noted in the gap analysis.

### 4.4 Summary endpoint enrichment

- **`Infra/functions/api_handler/api_handler.py::_compute_token_summary`** —
  add three breakdown maps to the returned object:
  - `by_agent`: `{ agent_id: { tokens, cost, count } }`
  - `by_persona`: `{ persona: { tokens, cost, count } }`
  - `by_user`: `{ user_email: { tokens, cost, count, persona } }` — keyed on
    `user_email`; `persona` is whichever persona was associated with the
    user's most recent row in the window (ties broken by latest `ts`).
  One additional pass over the records is sufficient; cost stays as `Decimal`
  through aggregation, serialize to float at response time. The UI does not
  consume `by_user` from the summary in v1 (it aggregates client-side from
  the already-loaded `records` array — see §4.7), but exposing all three maps
  keeps the API symmetric and future-proofs a possible standalone
  "top spenders" page.

### 4.5 Original spec text reconciled (doc edits to `Documents/token_tracking_spec.md`)

Apply these edits inline in the same PR so future readers do not start from a
contradictory source:

- §7 (data model): replace the `usage_id` HASH example with the
  pk/sk composite shape (`pk = "persona#<id>"`,
  `sk = "ts#<timestamp>#<session>#<agent>#<rand>"`) and call out the two GSIs
  by name (`persona-time-index`, `agent-time-index`). Note that
  "all rows for persona X in range" can use the main table partition without
  a GSI.
- §5 and §6 (wireframe and KPI strip): replace "Active agents today" with
  "Guardrail-blocked" as the 4th KPI card. Reflect the change in the
  user-story callouts that mention it.
- §6 (filter bar): drop the "Custom" date-range option and any associated
  date inputs from the wireframe and prose. Keep Today / 7d / 30d only.
- §7 (user fields): define `user_id` as the Cognito `sub` and `user_email` as
  the user's email forwarded from the API handler. Both are present on every
  row.
- §13 (CSV export): explicitly note the 5000-row server cap. Accept as v1
  behavior. Larger-than-cap exports are out of scope (no `next_token` /
  cursor pagination).
- §8 / §10 (line-number drift): the gap analysis notes
  `_caller_groups` is now at `api_handler.py:1415` and the agent capture uses
  `agent_result.metrics.accumulated_usage` rather than
  `agent.last_response.metrics.accumulated_usage`. Update referenced line
  numbers and accessor names so the spec matches shipped code.

### 4.6 Non-goals for this PR

- Custom date range with explicit `from`/`to` inputs.
- Cursor / `next_token` CSV pagination beyond 5000 rows.
- Multi-tenant chargeback views or per-org rollups.
- Anything else the original spec marked out of scope remains out of scope.
- Migration of the DDB table to a `usage_id` flat key.
- Backfill of historical rows that were missed before this PR ships
  (table currently has zero live rows by definition of the bug).
- Per-user trend lines / time-series chart per email (the v1 per-user surface
  is an aggregate ranked table; trends remain out of scope).

### 4.7 Per-user (per-email) breakdown card

A new card on the Token Tracking page that surfaces how much each individual
user has consumed in the selected window. Requested in design review so a
CISO can name outliers, not just personas.

- **Location.** Inline in
  [`ui/src/pages/TokenTracking.jsx`](../../ui/src/pages/TokenTracking.jsx),
  rendered between the existing charts row (≈ lines 196-247) and the
  per-record table (≈ line 250+). Implemented as an inline component
  consistent with the existing `KpiCard` / `ChartCard` style at the bottom
  of the same file. Suggested name: `<UserBreakdownCard records={records} />`.
- **Shape: ranked table, not a chart.** Charts of ranked emails read messy;
  a small table matches how a CISO consumes the data (scan names, eyeball
  outliers). Columns:
  - `User` — `user_email` from the row.
  - `Persona` — the persona affiliation. When a user appears under more than
    one persona in the window (rare), use the persona on their most recent
    row in the window.
  - `Chats` — count of distinct `session_id` values for that user in the
    window.
  - `Total tokens` — sum of input + output tokens.
  - `Estimated cost` — sum of `estimated_cost`, formatted as USD with the
    same precision used in the KPI strip.
- **Sorting.** Default sort: descending by `Total tokens`. No
  user-controllable sort in v1.
- **Empty state.** When the filtered `records` array is empty, render the
  same "No records in range" empty state used by the per-record table.
- **Filtering.** Honors the existing range filter (Today / 7d / 30d) and
  persona filter — both already applied upstream when the parent component
  produces `records`. The card takes no filter props of its own; it derives
  from whatever `records` array is passed in.
- **Data source.** Client-side aggregation only, derived via `useMemo`
  keyed on `records` and grouped by `user_email`. No new API call. No
  mutation of the records prop. When `user_email` is empty (legacy rows
  from before §4.2 ships), bucket under a literal "(unknown)" label so the
  total still reconciles with the KPI strip.
- **Mock mode.** No changes to `MOCK_TOKEN_USAGE`. The existing generator at
  `ui/src/mockData.js` line ~677 already emits one email per persona, so the
  card will render at least four distinct rows in mock mode without further
  seeding.
- **Live mode.** Sourced from the same `apiFetch('/token-usage?...')`
  response the rest of the page uses (DynamoDB-backed per §4 preamble).

### 4.8 Combined-cost framing

The user explicitly asked for "combined token cost across all 4 personas" to
be legible. The records table already covers it when the persona filter is
"all", and the KPI strip already sums cost across whatever's in `records`,
but neither says so out loud. Two small changes resolve that:

- **KPI subtitle change.** In
  [`ui/src/pages/TokenTracking.jsx`](../../ui/src/pages/TokenTracking.jsx),
  the "Estimated cost" KPI card's subtitle currently reads
  "Nova 2 Lite list pricing". Update to
  **"Across all personas · Nova 2 Lite list pricing"** when the persona
  filter is `all`. When the persona filter is a specific persona, keep the
  subtitle as **"<Persona name> · Nova 2 Lite list pricing"** so the
  framing tracks the actual scope.
- **Per-persona cost in the "Tokens by persona" chart.** Extend the existing
  reducer that produces the bar-chart data so each persona's entry carries
  both `tokens` and `cost`. Render `cost` as a small secondary label on each
  bar (e.g. `$0.0142` under the persona name) or in the tooltip. Sum of the
  per-persona costs across all four personas must equal the "Estimated cost"
  KPI value when persona filter is `all` (modulo rounding).
- **No new chart, no layout shift.** This is a label + tooltip change on the
  existing chart card; no new card is added by §4.8.

## 5. Acceptance criteria

- [ ] `aws cloudformation validate-template --template-body file://Infra/templates/06-api.yaml --region us-east-1` returns success.
- [ ] `aws cloudformation validate-template --template-body file://Infra/templates/09-agentcore.yaml --region us-east-1` returns success.
- [ ] After running `Infra/deploy.sh` and `scripts/deploy_agents.py`, signing in
  as `ciso_daiana@…` and submitting one analyst chat results in **at least
  one new row** in `dev-st21arbiter-poc-token-usage` within 5 seconds of the
  chat response, verifiable via `aws dynamodb scan --table-name dev-st21arbiter-poc-token-usage --max-items 5`.
- [ ] On that row, `user_email` is the real Cognito email
  (`ciso_daiana@…`) and `user_id` is the Cognito `sub` UUID. The two fields
  are distinct.
- [ ] A multi-tool chat that fans out to all three specialists writes **at
  least four rows** (one master + three specialists) sharing the same
  `session_id`.
- [ ] `GET /token-usage` and `GET /token-usage/summary` return HTTP 200 when
  called with a CISO IdToken, and HTTP 403 when called with a SOC, GRC, or
  Employee IdToken. Verifiable via `curl` against the API GW invoke URL.
- [ ] `GET /token-usage/summary` response includes non-empty `by_agent`,
  `by_persona`, and `by_user` maps when records exist. `by_user` is keyed on
  `user_email`.
- [ ] In mock mode (`USE_MOCK = true`, i.e. `VITE_API_URL` empty), the Token
  Tracking page renders the KPI strip, three charts, the new per-user
  breakdown card, and a populated records table. No regressions on the
  pre-existing surfaces.
- [ ] In mock mode the per-user breakdown card renders **at least four
  distinct rows** (one per persona email seeded by `MOCK_TOKEN_USAGE`),
  sorted descending by total tokens, each row showing email, persona, chat
  count, total tokens, and estimated cost.
- [ ] In live mode after the CISO sends one chat, the per-user breakdown
  card shows the CISO's email (`ciso_daiana@…`) with non-zero total tokens
  and chat count ≥ 1.
- [ ] When the persona filter is `all`, the "Estimated cost" KPI subtitle
  reads `Across all personas · Nova 2 Lite list pricing`. When a specific
  persona is selected, the subtitle reflects that persona name instead.
- [ ] The "Tokens by persona" chart card shows a per-persona cost figure
  (label or tooltip) alongside each persona's token bar. The sum of the four
  per-persona cost figures equals the "Estimated cost" KPI value (rounded
  to the displayed precision) when the persona filter is `all`.
- [ ] In mock mode the "Today" filter selects the same window the backend
  would select for "Today" — verified by reading a Pacific-time clock and
  confirming the boundary uses UTC midnight.
- [ ] `cd ui && npx vitest run src/__tests__/tokenTracking.test.jsx` passes.
- [ ] New Vitest cases cover: (a) records carry a distinct `user_email`
  string different from `user_id`; (b) summary derivation tolerates the new
  `by_agent` / `by_persona` / `by_user` shape; (c) the Token Tracking page
  in mock mode renders a per-user breakdown card with ≥ 4 rows, sorted
  descending by total tokens.
- [ ] `APP_VERSION` in `ui/src/config.js` is bumped from `1.3.0-poc` to
  `1.4.0-poc`. Sidebar footer reflects the new version after `npm run build`.
- [ ] Sidebar tab "Token Watcher" / Governance entry remains visible only for
  the CISO persona (no regression from PR #18).

## 6. Test plan

**New Vitest cases** (`ui/src/__tests__/tokenTracking.test.jsx`, append):
- Records list rendering asserts at least one row whose `user_email` matches
  an email regex (`@`-containing) and whose `user_id` does not.
- Summary derivation (`_computeTokenSummary`) returns objects whose keys
  match the expected `by_agent` / `by_persona` / `by_user` shapes when the
  upstream API returns those maps. (Smoke; the JS helper does not need to
  fabricate these in mock mode.)
- `<TokenTracking>` in mock mode renders a per-user breakdown card
  containing **at least four rows**, each row has a non-empty email and a
  numeric `total tokens` cell, and the rendered order is descending by
  total tokens.
- "Estimated cost" KPI subtitle contains `Across all personas` when the
  persona filter is `all`, and contains the persona name when a specific
  persona is selected.

**Backend smoke** (after `deploy.sh` + `deploy_agents.py`):
1. `curl -H "Authorization: Bearer <ciso_id_token>" "$API_URL/token-usage?range=today"` returns 200 with `{"records": [...]}`.
2. Same curl with a SOC IdToken returns 403.
3. `curl -H "Authorization: Bearer <ciso_id_token>" "$API_URL/token-usage/summary?range=7d"` returns 200 with `by_agent`, `by_persona`, and `by_user` present.
4. Send a chat from the UI as `ciso_daiana@…` that triggers all three
   specialists. Within 5 seconds:
   `aws dynamodb scan --table-name dev-st21arbiter-poc-token-usage --max-items 10 --region us-east-1`
   shows ≥ 4 rows with the same `session_id`, distinct `agent_id` values
   (master + sharepoint + awsconfig + zscaler).
5. Refresh the Token Tracking page; the new chat appears in the table with
   `User` column showing the email (not a UUID), and the CISO's email
   appears as a row in the new per-user breakdown card with non-zero totals.

**Negative smoke:**
- `aws logs filter-log-events --log-group-name /aws/bedrock-agentcore/runtimes/<runtime>_*-DEFAULT --filter-pattern "AccessDenied"`
  returns no matches in the minute after the chat. (Confirms the IAM widening
  worked.)

## 7. Risks

- **06-api redeploy wipes api_handler env vars.** Per the
  `06api-redeploy-wipes-apihandler-env` memory note, `Infra/deploy.sh` of the
  api stack resets `MASTER_AGENT_RUNTIME_ARN` and `MASTER_MEMORY_ID`. The
  rollout order in §9 below re-patches these by running `deploy_agents.py`
  after the infra deploy. Skipping that step will produce 500s on `/chat`
  until the env vars are restored.
- **Agent image / api_handler lockstep.** The new `user_email` kwarg added to
  `record_usage` and the forwarded payload field mean the four agent images
  and the api_handler Lambda must be deployed together. Old agent image +
  new api_handler is harmless (the new field is ignored); new agent image +
  old api_handler means the agents see `user_email` as `""` (still works, but
  rows will show empty emails). Documented in §9.
- **AgentCore subnet constraint.** Per CLAUDE.local.md, `PrivateSubnet2`
  (AgentCore-only) must stay on physical AZ IDs `use1-az1/2/4`. This PR does
  not touch the network template, but reviewers should confirm
  `01-network.yaml` was not collateral-damage edited.
- **Semantics shift.** Any external consumer that was reading the DDB table
  directly may have been treating `user_email` as a stable opaque identifier
  (it was the Cognito `sub`). After this PR `user_email` is a real email.
  Internal callers only — call out in PR description for safety.
- **CSV cap accepted.** With a 5000-row server cap and no cursor, very busy
  30-day windows truncate silently. v1 accepts this. Document in the original
  spec § 13 inline so the next reader knows.
- **PII surface area.** The per-user breakdown card surfaces real email
  addresses to the CISO. This is by design (the whole point of the card is
  to name outliers), and the route is already CISO-gated by `_require_ciso`,
  but reviewers should confirm no screenshot/export path of the card leaks
  emails to non-CISO viewers. v1 does not add CSV export for this card.

## 8. Open questions for reviewer

None blocking. All upstream open questions were answered in §3. Optional:
- Should the by_agent / by_persona / by_user enrichment in §4.4 also be
  added to the client-side mock summary so mock-mode CISO sees identical
  shapes? (Cheap to add; not strictly required by acceptance criteria.)
- The per-user card in §4.7 caps display at the full filtered set. If a busy
  30-day window produces dozens of users, should the card paginate or hard-
  truncate at top-N (e.g. top 10)? v1 ships uncapped; revisit if real data
  proves noisy.

## 9. Rollout

1. Validate both edited templates with
   `aws cloudformation validate-template` (acceptance §5 items 1-2).
2. Run `Infra/deploy.sh` from repo root. This redeploys `06-api`
   (registering the two new routes) and `09-agentcore` (widening the IAM
   Resource).
3. Activate `scripts/.venv`, then run `scripts/deploy_agents.py` (no
   `--skip-build`). This rebuilds and redeploys all four agent images with
   the new `user_email` forwarding, **and** re-patches the api_handler env
   vars (`MASTER_AGENT_RUNTIME_ARN`, `MASTER_MEMORY_ID`) wiped by step 2.
4. Bump `APP_VERSION` in `ui/src/config.js` from `1.3.0-poc` to `1.4.0-poc`.
   `Infra/deploy.sh` invokes `post_deploy_ui.py`, which rebuilds and syncs
   the SPA, but if the version bump happens after the deploy run, rebuild
   manually: `cd ui && npm run build`, then `aws s3 sync ui/dist/ s3://<env>-<project>-ui-hosting/ --delete` and a CloudFront invalidation.
5. Smoke-test as in §6: CISO sign-in → chat → DDB scan → page render → new
   per-user card row visible → "Estimated cost" subtitle reads "Across all
   personas".
6. Mark the PR done.
