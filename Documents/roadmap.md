# ARBITER (ST21) — Feature Roadmap from the Meridian Demo-Prep Transcript

## Context

The Meridian Insurance call (transcript) sets up a customer demo **~6 days out**. The buyer is **security- and architecture-obsessed** and wants ARBITER positioned as a *consolidated place to manage policies across Zscaler + Palo Alto + other teams, surface contradicting policies, and double as a multipurpose tool* (document review, JIRA L1 resolution, structured + unstructured ingestion, plus an integrations "marketplace" of connectors).

Scope chosen: **production-grade** (not façade) for the real builds — real-time conflicts · team/tag ownership · structured ingestion · Palo Alto source · JIRA L1 resolution · document review · Analyst/Dev persona. The Integrations Marketplace stays a catalog façade.

**Intended outcome:** ship **5 genuinely-real production-grade slices** for the demo and present an honest, sequenced roadmap for the rest. You cannot land all 7 production-grade in 6 days, and a security buyer will see through a façade dressed as "production." Five real slices + a credible post-demo plan beats seven thin features.

This roadmap was produced by a 5-architect design panel + adversarial critique + integration pass; the corrections below are *verified against the deployed account*, not assumed.

---

## 1. Transcript features → current state (what to build on, not rebuild)

| # | Feature asked in the call | Today in ARBITER | Gap to close |
|---|---|---|---|
| 1 | **Scan policy docs** from security teams; scheduled **or** ad-hoc "Run AI Scan" | ✅ 12 deterministic matchers ([scan_rule_pack.py](../agents/master_orchestrator/scan_rule_pack.py)), daily EventBridge cron + `POST /scan` button + F1 auto-chain → upsert `conflicts-v2` | Make it feel **real-time** (live UI refresh) |
| 1b | Two+ sources (Zscaler **+ Palo Alto**) | ✅ SharePoint, Zscaler, AWS Config specialists | ❌ **Palo Alto** not a source |
| 2 | **Document reviews**; **resolve L1 JIRA tickets** | ⚠️ JIRA **create/read only** (mcp-atlassian, tier-0); no doc-review flow | JIRA **transition/resolve**; doc review sign-off |
| 3 | Ingest **structured + unstructured**; answer prompts over KBs | ✅ Unstructured → Bedrock KB; chat/MCP RAG works | ❌ **Structured** (Oracle/CSV/ServiceNow) ingestion |
| 4 | **Integrations / Marketplace** (Palo Alto, Splunk, Datadog, DBs, ServiceNow), categorized | ⚠️ Only a static ServiceNow placeholder card in [MCPChat.jsx](../ui/src/pages/MCPChat.jsx) | New catalog page (**façade**) |
| 5 | **Team/tag ownership & routing** (owner vs consumer vs platform team; tag → tool) | ❌ Findings carry `domains` only — no team/owner/tags | **Customer's #1 concern** — full ownership layer |
| 6 | **Analyst/Dev persona** + "test a policy before pushing to Zscaler" | ✅ 4 personas (ciso/soc/grc/employee); deterministic scan engine does no DB writes in `_run_scan` | Analyst persona + **what-if dry-run** scan |

**Key architectural leverage (verified live):** the F1 ingest→scan chain is already wired and the env is set — `KB_ID=2ADHACW6LB`, `KB_DATA_SOURCE_ID=KLUEZ1RNM5`, `SCANNER_LAMBDA_NAME` set, `MASTER_AGENT_RUNTIME_ARN` patched, **all 5 runtimes READY**. The scan engine (`run_rule_pack`) is a pure deterministic function. RBAC is table-driven ([PersonaContext.jsx](../ui/src/contexts/PersonaContext.jsx)). This is why several slices are real, not façade, inside the window.

### 1a. Team ownership model — Owner / Consumer / Platform

ARBITER tags every finding (and the change-request derived from it) along **three orthogonal ownership axes** plus a tag set. These are **distinct from the 4 Cognito *personas*** (ciso/soc/grc/employee), which are *functional roles* — ownership is about **which team a conflict belongs to**, an independent dimension. The biggest customer concern in the call was exactly this segregation.

| Axis | Definition | Example — UC01 *(Dropbox approved in SharePoint policy but blocked by Zscaler)* |
|---|---|---|
| **Owner team** | Owns / authors the **policy intent**; accountable for resolving the conflict. | `data-governance` — owns the AUP that approves Dropbox |
| **Consumer team** | **Affected / blocked** by the conflict; feels the pain and raises the ticket. | `app-dev` — its users are blocked from Dropbox |
| **Platform team** | Manages the **enforcing control** (Zscaler / AWS Config / Palo Alto). | `network-eng` — administers the Zscaler URL rule |
| **Tags** | Cross-cutting classification used for routing & filtering. | `application`, `network` |

**Why three axes:** a Zscaler admin (platform), the policy author (owner), and the blocked department (consumer) are usually *different teams* — the call explicitly raised "the Zscaler admin may be one team, the consumer another." A finding belongs to a team if it matches on **any** of the three axes (a team cares about what it owns, is blocked by, **or** runs the platform for).

**Team taxonomy** *(placeholder — swap for Meridian's real org chart before the demo)*: `platform-security`, `network-eng`, `cloud-infra`, `data-governance`, `app-dev`, `vendor-mgmt`.
**Tag taxonomy**: `infrastructure`, `database`, `network`, `application`, `identity`, `data-residency`, `vendor`.

**How it flows (all live as of Days 1–3 deploy):**
1. `scanner` → [enrichment.py](../Infra/functions/scanner/enrichment.py) matches each finding to an `ownership-rules` row by priority: `rule_key` → `source_pair+domain` → `domain` → wildcard default, and stamps owner/consumer/platform + tags onto `conflicts-v2`.
2. The 12 UCs + a wildcard default are seeded ([seed_mock_data.py](../scripts/seed_mock_data.py) `OWNERSHIP`), mirrored in [mockData.js](../ui/src/mockData.js) so mock == live.
3. API ([api_handler.py](../Infra/functions/api_handler/api_handler.py)) **server-derives** `owner_team` from the linked finding (never from the request body) onto the change-request and routes the JIRA ticket via `TEAM_ROUTING[owner_team]` (all → DEVARBITER for the demo, with the team annotated, so a ticket never fails on a missing project).
4. UI: **Team filter** on Findings, **per-team re-scope** on the System-Map matrix, **Ownership & Routing** block on each Action-Center CR.

**Open decision for Meridian (blocks content, not code):** confirm the real team names + the exact Owner/Consumer/Platform definitions + one-JIRA-project-per-team vs components. The mechanism is taxonomy-agnostic — only the seeded rows change. (See §8.)

---

## 2. Verified landmines (the difference between a working demo and a silent failure)

1. **Scanner IAM has NO project wildcard.** [11-scanner.yaml](../Infra/templates/11-scanner.yaml) enumerates exactly 3 table ARNs. The new `ownership-rules` table will `AccessDenied` on read — **and the scanner swallows the exception and marks the run `COMPLETED` with zero enriched findings.** IAM + `OWNERSHIP_RULES_TABLE` env **must ship in the same PR as the enrichment code.** (api_handler's role in [02-security.yaml:145-146](../Infra/templates/02-security.yaml#L145) *does* have the wildcard, so new tables/GSIs are free on the read side.)
2. **Scanner upserts, never deletes.** Resolved conflicts linger forever in `conflicts-v2`. The marquee "fix the policy, watch the conflict clear" demo **will not clear** unless `/findings` filters to the latest `COMPLETED` `scan_run_id` (the `scan_run-index` GSI already exists; zero CFN).
3. **Cognito `custom:team` is irreversible + needs re-login.** Any [03-identity.yaml](../Infra/templates/03-identity.yaml) redeploy can drop demo users into `FORCE_CHANGE_PASSWORD`. **Keep 03-identity changes off the demo days** → server-side team boundary is a post-demo fast-follow.
4. **Function URL decodes JWT WITHOUT signature verification.** `cognito:groups` and `custom:team` both come from the same unverified decode → neither is a *cryptographically* hard boundary on the `/chat` Function-URL path. Frame demo RBAC as "server-side enforced on the API Gateway path"; JWKS verification is Phase F.
5. **`deploy_agents.py` needs TWO edits per new agent** — `arn_env_map` *and* the master backfill tuple (~line 604).
6. **`run_rule_pack` is 3-arg.** Threading Palo Alto is a signature change across **all 12 matchers + `emit_compliants` + the master call site**.
7. **Two hardcoded source lists in the UI** — `buildConflictMatrix` ([mockData.js:541](../ui/src/mockData.js#L541)) **and** HeatMap `DOMAINS` ([HeatMap.jsx:553](../ui/src/pages/HeatMap.jsx#L553)) **and** `SOURCE_PAIRS` — all need Palo Alto.
8. **DataPipeline upload `accept` is hardcoded** ([DataPipeline.jsx:127](../ui/src/pages/DataPipeline.jsx#L127)) → CSV browser-rejected. Uploads namespaced `users/<sub>/...` (no source prefix) → structured pipeline classifies by extension/filename.
9. **RBAC namespace clash:** `/analyst`'s access key is `'analyst'`; the What-If gate must use a distinct `'whatif'` key. ~6 lockstep edits per new key.
10. **AgentCore reaches Athena/Glue/JIRA/PAN-OS over NAT today** — no interface VPC endpoints needed for the demo (Phase F hardening).

---

## 3. The 6-day demo plan (real slices only)

Build order is leverage-over-risk. Each slice lands and is verified before the next (all four scan-path streams touch the same hot files).

- **Day 1 — Real-time live-refresh + stale-conflict fix (the spine).** `useScanFeed()` (single-flight, StrictMode-guarded) in [useApi.js](../ui/src/hooks/useApi.js) → wire Findings/HeatMap/Dashboard re-pull; "scanning…" pill; non-destructive "N new conflicts" badge; consolidate DataPipeline's timer. **Correctness fix:** `/findings` filters to latest `COMPLETED` `scan_run_id`. **Gate:** verify classic JIRA token + DEVARBITER transition IDs. Time one live upload.
- **Day 2 — Team/tag ownership backend.** `ownership-rules` table + owner/consumer/platform/tags attrs on `conflicts-v2`; pure unit-tested `enrichment.py` wired into scanner before batch-writer (seed all 12 UCs + default). **MANDATORY same-PR IAM fix in 11-scanner.yaml.**
- **Day 3 — Ownership UI + JIRA routing; start Palo Alto build early.** Team filter + badges + tag chips on Findings; per-team HeatMap re-scope; seed mock==live; static `TEAM_ROUTING` (owner_team server-derived via GetItem, never request body). Kick Palo Alto AgentCore CREATE in background.
- **Day 4 — Palo Alto scan + UI + Integrations façade; structured infra.** `run_rule_pack` 3→4-arg; UC13/UC14; verify `POST /scan` writes Palo Alto source-pairs *before* UI; add Palo Alto to both hardcoded lists + SVG; fix vitest; Integrations.jsx façade. Glue+Athena infra.
- **Day 5 — Structured single-source (conditional) + JIRA L1 + What-If.** `structured_specialist` (SELECT-only Athena + exact-shape observations); swap only `_seed_zscaler_observations`; `.csv` pipeline + crawler-completed rule. JIRA L1 transition/comment. What-If dry-run.
- **Day 6 — Hardening, fallbacks, rehearse.** `_require_groups()` on every privileged route; USE_MOCK fallbacks; timed rehearsal. **No 03-identity/04-storage redeploys.** Descope structured if not green.

**Cut from demo (named post-demo):** WebSocket push, document review, `custom:team` server-side RBAC.

---

## 4. Sequenced demo backlog

| # | Item | Effort | Dep |
|---|---|---|---|
| 1 | **GATE:** verify classic JIRA token + DEVARBITER transition IDs | S | — |
| 2 | `latest-scan_run_id` reconcile: `/findings` filters to newest `COMPLETED` run (existing GSI, 0 CFN) | M | — |
| 3 | `useScanFeed()` (single-flight, StrictMode-guarded) → Findings/HeatMap/Dashboard + pill + badge | S | 2 |
| 4 | `ownership-rules` table + owner/consumer/platform/tags attrs on `conflicts-v2` (no GSI) | M | — |
| 5 | **MANDATORY** scanner IAM + `OWNERSHIP_RULES_TABLE` env — *same PR as #6* | S | 4 |
| 6 | `enrichment.py` (pure, unit-tested) wired into scanner before batch-writer | M | 5 |
| 7 | Seed ownership (mock==live); Findings filter+badges+tags; HeatMap per-team re-scope | M | 6 |
| 8 | Static `TEAM_ROUTING`; `owner_team` server-derived via GetItem; DEVARBITER fallback | S | 6 |
| 9 | Clone → `palo_alto_specialist`; repo in 09-agentcore; **both** deploy_agents.py edits; kick CREATE early | M | — |
| 10 | `run_rule_pack` 3→4-arg through 12 matchers + `emit_compliants` + master; UC13/UC14; ARNs+names | M | 9 |
| 11 | UI: Palo Alto in `buildConflictMatrix(541)` **and** `DOMAINS(553)` **and** `SOURCE_PAIRS` + SVG; fix vitest | S | 10 |
| 12 | `Integrations.jsx` read-only façade (config-driven via `useAgentStatus`, ciso-gated) + route + sidebar | S | 11 |
| 13 | Glue DB + Crawler + Athena workgroup (SSE-KMS, caps, lifecycle); structured ECR + perms | M | — |
| 14 | `structured_specialist` runtime (SELECT-only Athena + exact-shape `produce_observations`) — *longest pole* | L | 13 |
| 15 | Swap only `_seed_zscaler_observations`→invoke (fixtures fallback); `.csv` pipeline + crawler-completed rule | M | 14 |
| 16 | JIRA L1: widen tools + defensive transition/comment; two routes + audit; ActionCenter controls | M | 1 |
| 17 | What-If: `dry_run`/`observations` on `_run_scan`; `POST /scan/dry-run`; WhatIf page (curated presets) | M | — |
| 18 | `_require_groups()` on every privileged route; audit render; USE_MOCK fallbacks | S | 16,17 |

---

## 5. Post-demo production phases

- **Phase A (wk +1) — True server-side team boundary:** `custom:team` Cognito attr (irreversible) + re-seed + force re-login; `_caller_team`; server-side `/findings`+`/dashboard` filter.
- **Phase B (wk +1–2) — Document-review sign-off gate:** `doc-reviews` table; `POST /uploads/register` (beat the S3 race); gate in processing_pipeline. **Close the move-leak** (the raw→processed MOVE runs before the gate point).
- **Phase C (wk +2) — Real-time push (poll → WebSocket):** scan-runs stream + `ws-connections` table + `13-realtime.yaml` WebSocket API + JWT authorizer. **Requires `execute-api` interface VPC endpoint.**
- **Phase D (wk +2–3) — Structured breadth:** multi-source `produce_observations` + CI fixture-parity test; ServiceNow/Oracle connectors; NL2SQL (SELECT-only, capped, Guardrailed).
- **Phase E (wk +3) — Live Palo Alto + JIRA robustness + analyst persona:** real PAN-OS XML-API; new `POST /escalate` (CR escalate branch 404s without cr_id).
- **Phase F (wk +3–4) — Production security & observability:** **JWKS signature verification on the Function URL path**; interface VPC endpoints; conflict tombstoning; F1 latency SLO + alarms; PII masking + NAIC residency.

---

## 6. Cross-cutting concerns

- **Security-boundary honesty** — RBAC is server-side enforced on the API Gateway path; don't overclaim as cryptographically hard until Phase F JWKS verification.
- **IAM least-privilege** — scanner role has no wildcard (explicit ARNs per new table); structured_specialist needs scoped athena/glue/s3/KMS.
- **Silent empty-scan landmines** — add a non-zero-findings sanity log + fixture-parity assertion (Athena `json_extract` returns string `'false'` ≠ boolean `False`).
- **Cost** — `BytesScannedCutoffPerQuery` + results lifecycle + row caps; ONE shared `useScanFeed` timer with small `Limit`.
- **Observability** — every new agent Dockerfile keeps `opentelemetry-instrument` + `aws-opentelemetry-distro`.
- **RBAC namespace** — `'whatif'` ≠ `'analyst'`; ~6 lockstep edits per key.

---

## 7. Verification (per slice)

- **Real-time:** upload a doc → chips + pill → Dashboard/Findings/HeatMap update autonomously within the timed budget; upload a *fixing* doc → resolved conflict **disappears**.
- **Ownership:** `POST /scan` → all 12 UC rows carry non-blank `owner_team`/`tags`; Findings/HeatMap segregate by team; CR routes to the team's JIRA project (or DEVARBITER). Unit-test `enrichment.py`. Negative test: break scanner IAM → sanity log fires.
- **Palo Alto:** `list-agent-runtimes` shows `palo_alto_specialist READY`; `/scan` writes the new source-pairs; UI counts them; `npx vitest run` green.
- **Structured:** edit a CSV row → crawler refresh → re-scan → UC04 from Athena; observation shape byte-matches the zscaler fixture incl nested `raw{}`.
- **JIRA L1 / What-If:** transition a real issue → audit row renders; what-if writes nothing to `conflicts-v2`.
- **Hardening:** privileged routes 403 for unauthorized personas; USE_MOCK fallbacks render with empty `VITE_API_URL`.

---

## 8. Needs from Meridian before building ownership rules (blocks content, not code)

Real **team taxonomy** + **OWNER vs CONSUMER vs PLATFORM** semantics + **one JIRA project per team vs components** + whether an employee sees consumer-team findings + whether team membership federates from their IdP/AD (changes the Phase-A claim name). Seeding rules against placeholder teams risks showing the wrong org model to the customer whose #1 concern is exactly this.
