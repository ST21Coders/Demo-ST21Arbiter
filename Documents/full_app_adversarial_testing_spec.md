# Spec — Full-app adversarial testing harness

**Status:** Draft (spec only — no implementation yet)
**Owner:** Test tooling (`tests-adversarial/` — new) + minor UI hooks (`ui/`) + minor backend test fixtures (`tests/`)
**Version target:** harness has its own semver in `tests-adversarial/package.json`. Does **not** bump `APP_VERSION` in [`ui/src/config.js`](../ui/src/config.js) — the harness ships no product code.

Source: [`docs/research/full_app_adversarial_testing.md`](../docs/research/full_app_adversarial_testing.md). The research brief enumerates the surface, the tooling tradeoffs, and the project-specific footguns. This spec does not repeat them — it builds on them.

---

## 1. Summary

A layered, manual, once-a-day **adversarial testing harness** that exercises the deployed dev ARBITER application — every page, every persona, every API route, every agent tool — and produces a single polished report the user can forward to their team. The harness runs from a developer laptop against the existing CloudFront URL ([`https://d5u0vv1zl3eqd.cloudfront.net/`](https://d5u0vv1zl3eqd.cloudfront.net/)); it does not spin up scratch environments and does not gate any CI. It is the tester's tool, not a release gate.

Four independent layers compose into one run: **Functional E2E**, **API fuzz**, **Auth abuse**, and **LLM red-team**. Each layer is runnable in isolation for debugging (`npm run test:e2e`, `test:fuzz`, `test:auth`, `test:llm`) and `npm run test:all` orchestrates them in sequence. The report is the deliverable — coverage matrix, ranked findings, diff-from-last-green, cost footer — written under `test-reports/<UTC-ISO-timestamp>/` and gitignored.

"Polished" in this spec means three things, and only these three things. First, **visible completeness**: a forwarded report must let a reader see, in one glance, that every page-persona cell, every API route, and every agent tool was touched, or marked skipped with a reason. Second, **bounded cost**: the harness must refuse to start a run whose estimated Bedrock spend exceeds a configurable cap (default $1.00) and must footer the run with actual spend. Third, **stable diffs**: every test has a stable id so the "what changed since last green" section is meaningful across runs, not a diff of timestamps.

## 2. Goals and non-goals

### Goals

- Exercise **every reachable page** under [`ui/src/pages/`](../ui/src/pages/) for **every persona** that can access it, plus a negative check that personas without access get blocked (60 cells total, see §4).
- Exercise **every API route** registered in [`Infra/functions/api_handler/api_handler.py:133-216`](../Infra/functions/api_handler/api_handler.py#L133-L216) with at least one valid call and at least one adversarial input shape.
- Exercise **every `@tool`** exposed by the four agent runtimes ([`agents/*/agent.py`](../agents/)) as a black box via `/chat` prompts that coax the master into invoking each one at least once.
- Exercise **cross-persona privilege escalation** as a first-class layer: a SOC token hitting a CISO-only route, a forged `cognito:groups` claim, and a token swap mid-session.
- Exercise **the `/chat` Function URL's no-signature-verification trust model** as a named, recorded test — not as a surprise.
- Produce a **single self-contained report** (`report.html` + `summary.md` + machine-readable `report.json` + per-layer artifacts) that the user can forward without context.
- Keep **total Bedrock spend per full run under $1.00** and **total wall-clock under 10 minutes** at the default budget.
- Make **every layer runnable in isolation** so a failing test can be re-run without paying the full-run cost.

### Non-goals

- **No CI gating.** This is not in the GitHub Actions / CodeBuild flow. PR comments are a secondary nice-to-have (§6) but never block merge.
- **No sustained load testing.** Burst probes only. The harness does not run k6, Locust, or AWS Distributed Load Testing.
- **No chaos engineering.** No DDB throttling, no killing AgentCore runtimes, no AWS FIS.
- **No visual regression.** No screenshot diffing of UI pixels (only screenshots-as-evidence inside the report).
- **No accessibility audit.** No axe-core sweep. Existing `accessibility.spec.ts` stays as-is and outside this harness.
- **No scratch environments.** Runs only against the deployed dev CloudFront.
- **No real-time dashboard / Grafana / Slack delivery.** The report file is the deliverable.
- **No new product features.** This is meta-tooling. The harness writes no product code, ships no Lambda, deploys no CFN.
- **No mutation of `Infra/params/dev.json`, no re-deploy, no IAM changes.** The harness reads, the harness does not modify infra.
- **No probing of the `jira_specialist` runtime beyond black-box `/chat` prompts.** Its source is not in this repo ([`CLAUDE.md`](../CLAUDE.md) — "off-limits, coordinate before any master rebuild").

## 3. Personas and use cases

The harness has exactly two human audiences.

**The user (runs the harness).** Once a day, manually, from their laptop. Sets `DEMO_PASSWORD` in their shell, runs `npm run test:all`, waits 5–10 minutes, opens `test-reports/<timestamp>/report.html`, reviews findings, forwards `summary.md` to the team. The user does not want CI to nag them. They want a clean local artifact they can attach to a Slack message.

**The team (reads the forwarded report).** People who do not have the harness installed and may not have AWS access. They need to see, in a single HTML or markdown file, what was tested, what passed, what failed, and what the cost was. They do not need to re-run anything to understand the report.

**Engineering reviewing a PR (secondary, lower priority).** If a PR-triggered run ever produces a report, the harness may post a "diff-from-last-green" PR comment. This is a stretch goal; the standalone report is the primary deliverable.

## 4. Functional scope — the full inventory

This is the section the user explicitly asked for: every damn thing, listed, so it is trivially auditable that nothing was left out. The acceptance criteria in §9 reference each line by id.

### 4.1 Pages × personas (60-cell coverage matrix)

Pulled from [`ui/src/contexts/PersonaContext.jsx`](../ui/src/contexts/PersonaContext.jsx) `PERSONAS[*].access` × `ROUTE_ACCESS`. `Y` = page must render under that persona. `N` = harness asserts `<AccessDenied />` (or backend 403 if probed through the API). Both Y and N cells are required coverage.

| Route | File | employee | grc | soc | ciso | Layer |
|---|---|---|---|---|---|---|
| `/` | [`Dashboard.jsx`](../ui/src/pages/Dashboard.jsx) | N | Y | Y | Y | E2E |
| `/findings` | [`Findings.jsx`](../ui/src/pages/Findings.jsx) | N | Y | Y | Y | E2E + API fuzz |
| `/findings/:id` | [`FindingDetail.jsx`](../ui/src/pages/FindingDetail.jsx) | N | Y | Y | Y | E2E + API fuzz |
| `/heatmap` | [`HeatMap.jsx`](../ui/src/pages/HeatMap.jsx) | N | Y | Y | Y | E2E |
| `/actions` | [`ActionCenter.jsx`](../ui/src/pages/ActionCenter.jsx) | N | N | Y | Y | E2E + API fuzz |
| `/governance` | [`Governance.jsx`](../ui/src/pages/Governance.jsx) | N | Y | N | Y | E2E |
| `/audit` | [`AuditLogs.jsx`](../ui/src/pages/AuditLogs.jsx) | N | Y | Y | Y | E2E + API fuzz |
| `/analyst` | [`AnalystView.jsx`](../ui/src/pages/AnalystView.jsx) | Y | Y | Y | Y | E2E + LLM |
| `/llm-control` | [`LLMControl.jsx`](../ui/src/pages/LLMControl.jsx) | N | N | N | Y | E2E |
| `/pipeline` | [`DataPipeline.jsx`](../ui/src/pages/DataPipeline.jsx) | N | N | N | Y | E2E |
| `/mcp-chat` | [`MCPChat.jsx`](../ui/src/pages/MCPChat.jsx) | N | N | N | Y | E2E + LLM (cosmetic-only routing check) |
| `/token-usage` | [`TokenTracking.jsx`](../ui/src/pages/TokenTracking.jsx) | N | N | N | Y | E2E + Auth abuse |
| `/personas` | [`Personas.jsx`](../ui/src/pages/Personas.jsx) | Y | Y | Y | Y | E2E |
| `/settings` | [`Settings.jsx`](../ui/src/pages/Settings.jsx) | Y | Y | Y | Y | E2E |
| `/signin`, `/callback` | [`SignIn.jsx`](../ui/src/pages/SignIn.jsx) + Cognito Hosted UI | login flow | login flow | login flow | login flow | E2E + Auth abuse |

Within each `Y` cell the harness asserts at least one interactive element (a filter, a row click, a chart drilldown, or a modal open/close). The exact element list per page is the architect's call, but each page must have at least one click beyond "page rendered."

The `Y` cell for `/analyst` includes one chat turn per persona (4 turns total in the E2E layer alone — see cost in §5).

### 4.2 API routes

Pulled from [`api_handler.py:133-216`](../Infra/functions/api_handler/api_handler.py#L133-L216). Every route in this table is covered.

| Route | Method | Layer(s) |
|---|---|---|
| `/health` | GET | E2E (smoke) |
| `/chat` (Function URL) | POST | LLM + Auth abuse |
| `/findings` | GET | E2E + API fuzz |
| `/findings/{id}` | GET | E2E + API fuzz (path traversal, IDOR) |
| `/scan` | POST | API fuzz |
| `/scan-runs` | GET | E2E |
| `/scan-runs/{id}` | GET | API fuzz (IDOR) |
| `/conversations` | GET | E2E + Auth abuse |
| `/conversations/{id}` | GET | API fuzz (IDOR — top priority, already a named security test) |
| `/conversations/{id}` | DELETE | API fuzz (IDOR) |
| `/conversations/{id}/messages` | GET | API fuzz (IDOR) |
| `/actions` | GET | E2E + API fuzz |
| `/actions` | POST | API fuzz (mass assignment) |
| `/actions/{id}/approve` | POST | API fuzz + Auth abuse (CISO-only transition check) |
| `/actions/{id}/reject` | POST | API fuzz |
| `/actions/{id}/execute` | POST | API fuzz |
| `/actions/{id}/escalate` | POST | API fuzz |
| `/audit` | GET | E2E + API fuzz (filter injection) |
| `/token-usage` | GET | E2E + Auth abuse (CISO-only) |
| `/token-usage/summary` | GET | Auth abuse (CISO-only) |
| `/dashboard` | GET | E2E |
| `/mcp-health` | GET | E2E |
| `/uploads/presign` | POST | API fuzz (KB poisoning via S3 PUT URL — generate URL only, do not upload poisoned content) |
| `/uploads/list` | GET | E2E |
| `/jira/tickets` | POST | API fuzz |

For each route, "covered by API fuzz" means at least one curated adversarial input from the corpus AND at least one generative input from the hypothesis strategy.

### 4.3 Agent tools

Pulled from `@tool` decorators across [`agents/*/agent.py`](../agents/). All are exercised as black box through `/chat`. No direct runtime invocation.

| Runtime | Tool | Black-box probe |
|---|---|---|
| `master_orchestrator` | `sharepoint_lookup` | Prompt: "Find our policy on remote access." |
| `master_orchestrator` | `awsconfig_lookup` | Prompt: "Are any S3 buckets non-compliant right now?" |
| `master_orchestrator` | `zscaler_lookup` | Prompt: "Is github.com allowed by our Zscaler policy?" |
| `sharepoint_specialist` | `retrieve_policies` | Reached transitively via `sharepoint_lookup`. |
| `awsconfig_specialist` | `list_config_rules` | Prompt: "List our active AWS Config rules." |
| `awsconfig_specialist` | `get_rule_compliance` | Prompt: "What is the compliance state of `s3-bucket-public-read-prohibited`?" |
| `awsconfig_specialist` | `list_noncompliant_resources` | Prompt: "Which resources fail `s3-bucket-public-read-prohibited`?" |
| `awsconfig_specialist` | `retrieve_awsconfig_docs` | Prompt: "Summarize our AWS Config conformance pack history." |
| `zscaler_specialist` | `retrieve_zscaler_policy` | Reached transitively via `zscaler_lookup`. |
| `zscaler_specialist` | `lookup_url_category` | Prompt: "Look up the Zscaler category for `https://example.com`." (Note: live ZIA API call — see Risks §11.) |
| `jira_specialist` (deployed, no repo source) | unknown tools | Prompt: "Open a Jira ticket for this finding." Recorded as **black-box only**; output is logged, no assertions on tool names — see §11. |

The harness asserts each tool was invoked by parsing the chat transcript / agent telemetry surfaced through the `/chat` response. If the response shape does not include tool calls, the harness falls back to **prompt-level coverage**: marks the tool covered if the eliciting prompt was sent and produced a non-error response. The report distinguishes these two outcomes (`tool_invoked` vs `prompt_only`) so the reader knows the difference.

### 4.4 Cognito Hosted UI flows

- **Sign in (happy path)** for each of the four demo users — covered in E2E setup (each persona logs in once per run).
- **`FORCE_CHANGE_PASSWORD` edge case** — covered by attempting to sign in with the wrong-case password and asserting the Hosted UI's "Invalid username or password" error matches the known-confusing string ([`CLAUDE.local.md`](../CLAUDE.local.md) flags this). Single probe per run, no password reset side effect.
- **Forgot password** — **not exercised.** Triggering it would email the demo accounts and pollute the inbox. Reported as `skipped: by-design` in the coverage matrix so it is visibly accounted for.
- **Sign out → token reuse** — covered in Auth abuse layer.

### 4.5 MCP sidebar cosmetic-only verification

[`MCPChat.jsx`](../ui/src/pages/MCPChat.jsx) has a hardcoded server list ([`CLAUDE.local.md`](../CLAUDE.local.md): "the chat send always goes to the master AgentCore Runtime via `sendChat()`. Don't wire sidebar selection to backend routing — it's cosmetic"). The harness asserts this contract: send the same prompt twice with two different sidebar selections, assert the network request payload is identical apart from idempotency-ish fields. If they ever differ, that is a **severity:high** finding (cosmetic surface has become load-bearing).

### 4.6 `jira_specialist` black-box

The deployed runtime exists; its source does not. The harness sends a single prompt eliciting a Jira workflow and records the response verbatim. No assertions on tool names, no assertions on response shape — only "did it respond without 5xx, and did the response not leak a stack trace or AWS account id." Recorded as `coverage: black-box, assertions: smoke-only`.

## 5. Test layers — what each one does

Each layer has: a purpose, what it exercises, its inputs, its pass/fail criteria, its budget, and the standalone command.

### 5.1 Functional E2E

**Purpose.** Prove every page-persona cell in §4.1 is reachable (positive) or correctly blocked (negative), and that the basic interactive elements on each page do not error.

**What it exercises.** All 15 routes × 4 personas (60 cells). One sign-in per persona via the Cognito Hosted UI (`SignIn.jsx` → callback). Per page, at least one click beyond "page rendered" (filter, sort, row open, modal open/close). On `/analyst`, one short chat turn per persona ("ping" — see §5.4 cost budgeting; the chat turn here is functional, not adversarial).

**Inputs / corpus.** No corpus. Hardcoded selectors and a `tests-adversarial/fixtures/interactions.json` that maps each route to its one-click element.

**Pass/fail.** Each positive cell passes if the expected page header renders within 5s and the one interactive element produces no console error. Each negative cell passes if `<AccessDenied />` renders or the page redirects to `firstAccessiblePath()`. Per-cell timeouts strict; flaky retry once, then fail.

**Budget.** 4 chat turns × ~$0.002 each on Nova 2 Lite = ~$0.01. Wall clock: ~4 minutes. Bedrock spend in this layer is from `/analyst` only.

**Command.** `npm run test:e2e` from `tests-adversarial/`.

### 5.2 API fuzz / adversarial inputs

**Purpose.** Find input-validation defects and IDOR boundaries on every API route in §4.2.

**What it exercises.** Every route in §4.2 marked "API fuzz." For each route: 1) curated payloads from `tests-adversarial/corpus/api-fuzz/<route>.json` (oversize 1MB+ body, malformed JSON, missing required fields, type confusion, CRLF in `details`, control characters, unicode normalization, path traversal in path params, filter-injection in `severity=`/`status=`/`domain=` query params); 2) a small generative batch via hypothesis strategies (8 examples per route).

**Inputs / corpus.** Two-tier: curated JSON files versioned under `tests-adversarial/corpus/api-fuzz/`, plus a hypothesis strategy per route in code. Curated is the source of truth for known-bad shapes; hypothesis discovers new ones.

**Pass/fail.** A route passes if every adversarial input returns a 4xx with a JSON body containing `error` (the project's `_err()` shape), and no input causes 5xx or returns another user's data. IDOR-specific: hitting `/conversations/{id}` with a `session_id` belonging to another user must return 404 (not 403, not 200) — this is already the documented contract in [`tests/security/test_auth_and_authorization.py`](../tests/security/test_auth_and_authorization.py).

**Budget.** Zero Bedrock spend (no `/chat`). DDB read/write: pennies per run. Wall clock: ~2 minutes.

**Command.** `npm run test:fuzz`.

### 5.3 Auth abuse

**Purpose.** Confirm the documented JWT trust model on the `/chat` Function URL and validate cross-persona gating end-to-end.

**What it exercises.** 1) `/chat` with no Authorization header. 2) `/chat` with a JWT whose signature is stripped (`header.payload.` with the signature segment removed). 3) `/chat` with a forged `cognito:groups: ["ciso"]` claim from a base persona of `employee`. 4) `/token-usage` and `/token-usage/summary` with each non-CISO IdToken — must return 403. 5) `/actions/{id}/approve` from a SOC token where the spec restricts the transition to CISO. 6) SOC IdToken pasted into a CISO browser session (token swap) — UI behavior is documented, not asserted as broken. 7) Expired IdToken replay — assert refresh path or 401.

**Inputs / corpus.** Token-forging helpers reusing the pattern from [`tests/security/test_auth_and_authorization.py`](../tests/security/test_auth_and_authorization.py). Curated probe list at `tests-adversarial/corpus/auth-abuse/probes.json`.

**Pass/fail.** Each probe has an expected outcome (e.g. `expect: 403`). A `403`-expected probe that gets a `200` is a **severity:high** finding. A `200`-expected probe that gets `401` is a **severity:medium** finding (it broke a legitimate path). The known-unsafe `/chat` signature-not-verified probe is recorded as `expected: documented-unsafe`, not as a finding — it confirms the contract, not a regression.

**Budget.** 0–2 Bedrock invocations (only if a forged token reaches a `/chat` happy path). Cap: $0.02. Wall clock: ~1 minute.

**Command.** `npm run test:auth`.

### 5.4 LLM red-team

**Purpose.** Probe the master orchestrator for prompt injection, jailbreak, tool-call abuse, KB exfiltration, and prompt-cost DoS — within a tight budget.

**What it exercises.** `/chat` with a small fixed probe set (20 curated probes from `tests-adversarial/corpus/llm-red-team/probes.json`, covering: system-prompt leak, persona-impersonation, indirect injection (no actual KB poisoning — see Risks §11), guardrail bypass via roleplay, character substitution, KB exfiltration via "repeat the document above," tool-coercion ("invoke `lookup_url_category` 50 times")). Plus a small generative batch from Garak-style probe templates (10 generated probes per run). Probes are sent as **CISO** for surface depth but the harness verifies the responses do not contain content the persona should not see.

**Inputs / corpus.** Curated YAML at `tests-adversarial/corpus/llm-red-team/probes.yaml` (versioned). Generative batch is template-expanded at run time with a fixed seed so the same probes go out across runs (stable diff requirement).

**Pass/fail.** Each probe carries an `assertion` (regex match against the response, or "no tool call to X", or "did not echo system prompt marker"). Pass = assertion holds. Fail = assertion violated. Each failure is classified by category (`prompt_injection`, `jailbreak`, `data_exfil`, `tool_abuse`, `cost_dos`) and severity. **Cost-DoS probe** specifically: send a prompt that requests a 2000-token completion, assert the response is bounded by the runtime's max_tokens (which the harness reads from the deployed model config) — pass if bounded, severity:medium fail if unbounded.

**Budget.** 30 total probes × ~$0.005 average on Nova 2 Lite (each probe = 1 master turn + up to 3 specialist fan-outs) = ~$0.15. With variance: cap at $0.50 for this layer. Wall clock: ~3 minutes.

**Command.** `npm run test:llm`.

### 5.5 The orchestrator: `npm run test:all`

Runs §5.1 → §5.2 → §5.3 → §5.4 in sequence (auth abuse before LLM so a forged-token defect surfaces before paying for chat probes). Aggregates per-layer report files into the unified report at `test-reports/<timestamp>/`. Sums per-layer Bedrock spend into the cost footer. Compares against the previous green run in `test-reports/.baseline/` and emits the diff section.

**Pre-flight check before any layer runs:** the harness estimates total Bedrock cost from the layer probe counts × model unit price (`MODEL_PRICING` constant — already a duplicated constant in [`agents/_shared/token_usage.py`](../agents/_shared/token_usage.py) and [`ui/src/mockData.js`](../ui/src/mockData.js), per [`CLAUDE.md`](../CLAUDE.md) — the harness imports from one of them and fails if they disagree). If estimated > `BEDROCK_COST_CAP_USD`, refuse to start and exit non-zero with a clear message.

**Wall-clock budget for `test:all`:** 10 minutes. **Cost budget:** $1.00 hard cap.

## 6. Report shape

One run produces one directory: `test-reports/<UTC-ISO-timestamp>/`. Three top-level files plus per-layer subdirectories.

### 6.1 Directory layout

```
test-reports/
├── .baseline/
│   └── last-green.json                  # single JSON, last fully-passing run's per-test-id status
└── 2026-06-08T14-23-01Z/
    ├── report.html                       # the polished forwardable artifact
    ├── report.json                       # machine-readable (CI consumers, diffing)
    ├── summary.md                        # short text-only summary for chat/email forwarding
    ├── e2e/
    │   ├── screenshots/<persona>-<route>.png
    │   └── playwright-report/            # raw Playwright HTML (drill-down)
    ├── fuzz/
    │   ├── transcripts/<route>.jsonl     # request/response pairs
    │   └── findings.json
    ├── auth/
    │   └── probes.jsonl
    └── llm/
        ├── transcripts/<probe-id>.jsonl  # full prompt + completion
        └── findings.json
```

### 6.2 `report.html` sections, in order

1. **Header.** Run id, timestamp, target URL, harness version, pass/fail summary chip.
2. **Cost footer (rendered at top too).** Estimated $X.XX, actual $X.XX, by-layer breakdown.
3. **Coverage matrix.** Three grids: (a) pages × personas (60 cells, green=tested-pass, red=tested-fail, gray=skipped); (b) API routes × layer-of-coverage (one row per route, columns = E2E / fuzz / auth / LLM with check or dash); (c) agent tools × invocation evidence (`tool_invoked` / `prompt_only` / `not_reached`). This is the "demonstrate nothing was left out" element.
4. **Findings list.** Ranked by severity (`critical` > `high` > `medium` > `low` > `info`). Each finding has: id, layer, title, evidence link (into per-layer subdir), severity, first-seen-run (if from baseline).
5. **Diff-from-last-green.** Three subsections: new failures since last green, newly-passing tests, newly-skipped tests. Empty if no baseline exists yet.
6. **Per-layer subreport.** One collapsible section per layer with: pass/fail count, runtime, cost, link into `<layer>/` subdir.
7. **Footer.** Harness version, run command, environment variables (with secrets redacted).

### 6.3 `report.json` shape (sketch)

```json
{
  "run_id": "2026-06-08T14-23-01Z",
  "harness_version": "0.1.0",
  "target": "https://d5u0vv1zl3eqd.cloudfront.net/",
  "started_at": "...",
  "finished_at": "...",
  "cost": { "estimated_usd": 0.42, "actual_usd": 0.38, "by_layer": { "e2e": 0.01, "fuzz": 0, "auth": 0, "llm": 0.37 } },
  "coverage": {
    "pages":   [{ "route": "/findings", "persona": "ciso", "status": "pass", "test_id": "e2e.page.findings.ciso" }, ...],
    "routes":  [{ "route": "/findings",  "method": "GET", "layers": ["e2e","fuzz"], "status": "pass" }, ...],
    "tools":   [{ "tool": "sharepoint_lookup", "evidence": "tool_invoked", "status": "pass" }, ...]
  },
  "findings": [
    { "id": "auth.forged-groups-ciso", "severity": "high", "layer": "auth", "title": "...", "evidence": "auth/probes.jsonl#L17" }
  ],
  "diff": { "new_failures": [...], "newly_passing": [...], "newly_skipped": [...] }
}
```

### 6.4 `summary.md` (forwardable, ~30 lines)

```
# ARBITER adversarial run — 2026-06-08T14:23Z

Target:  https://d5u0vv1zl3eqd.cloudfront.net/
Result:  3 failures (1 high, 2 medium), 147 passes, 2 skipped
Cost:    $0.38 of $1.00 cap
Time:    8m 12s

## Coverage
- Pages × personas: 60/60 covered (58 pass, 2 fail)
- API routes: 25/25 covered (24 pass, 1 fail)
- Agent tools: 11/11 reached (10 tool_invoked, 1 prompt_only)

## Findings (ranked)
1. [HIGH] auth.forged-groups-ciso — forged `cognito:groups` claim reached `/token-usage` ...
2. [MED]  llm.kb-exfil-policy-doc  — chat surfaced ...
3. [MED]  fuzz.findings-id-traversal — `/findings/../../etc/passwd` returned 500 not 400

## Diff from last green (2026-06-07T14-12Z)
+ NEW FAIL: auth.forged-groups-ciso
- NOW PASSING: llm.system-prompt-leak

Full report: test-reports/2026-06-08T14-23-01Z/report.html
```

### 6.5 PR comment (secondary, lower priority)

If the harness is ever invoked from a PR context (presence of `GITHUB_PR_NUMBER` env var, say), it posts `summary.md` as a PR comment, truncated to the diff-from-last-green section plus the cost footer. **Never blocks merge.** Not in scope for v1 unless the architect can do it for free.

## 7. Data model

### 7.1 Last-green baseline

Single file at `test-reports/.baseline/last-green.json`:

```json
{
  "run_id": "2026-06-07T14-12-44Z",
  "tests": {
    "e2e.page.findings.ciso":            { "status": "pass" },
    "auth.chat.no-signature":            { "status": "pass", "expected": "documented-unsafe" },
    "llm.system-prompt-leak.probe-3":    { "status": "pass" },
    ...
  }
}
```

A run becomes the new baseline iff every test passed (or skipped-by-design). The user explicitly promotes a run with `npm run test:promote-baseline` — the harness does not auto-promote, so a flake doesn't overwrite a known-good baseline.

### 7.2 Corpus directory layout

```
tests-adversarial/corpus/
├── api-fuzz/
│   ├── findings.json
│   ├── conversations.json
│   ├── actions.json
│   ├── audit.json
│   ├── scan.json
│   ├── chat.json
│   ├── uploads.json
│   └── jira.json
├── auth-abuse/
│   └── probes.json
└── llm-red-team/
    └── probes.yaml
```

Each curated corpus file is human-edited, checked into git, and reviewed in PRs. Generative probes are produced at run time from strategies in code (no pre-generation, no committed generated artifacts).

### 7.3 Test IDs

Naming convention (mandatory — the diff section breaks if ids are unstable):

```
<layer>.<surface>.<probe>[.<index>]

Examples:
  e2e.page.findings.ciso
  e2e.page.findings.ciso.filter-severity-high   (one specific interaction)
  fuzz.findings.oversized-body
  fuzz.findings-id.path-traversal
  auth.chat.no-signature
  auth.token-usage.soc-forbidden
  llm.system-prompt-leak.probe-3
  llm.tool-abuse.zscaler-loop
```

Lowercase, dot-separated. Numeric suffix only for generative probes (the seed makes them deterministic across runs).

## 8. Configuration and secrets

Environment variables, all sourced from the local shell. Never committed.

| Variable | Default | Purpose |
|---|---|---|
| `DEMO_PASSWORD` | (required) | Password for the four demo Cognito users. If unset, the harness fails fast with a one-line error. |
| `BEDROCK_COST_CAP_USD` | `1.00` | Hard cap on estimated Bedrock spend per `test:all` run. Pre-flight refuses to start above this. |
| `TARGET_BASE_URL` | `https://d5u0vv1zl3eqd.cloudfront.net/` | The deployed dev CloudFront. Override only for a staging env that does not currently exist. |
| `TARGET_API_URL` | (read from CloudFront's served `config.js` if unset) | The API Gateway base. The harness scrapes the SPA's runtime config on first load to avoid hardcoding. |
| `TARGET_CHAT_URL` | (same as `TARGET_API_URL`, derived) | The Lambda Function URL for `/chat`. |
| `AWS_REGION` | `us-east-1` | Hard-pinned by the project. |
| `REPORT_DIR` | `test-reports/` | Where the run artifacts go. Always gitignored. |
| `PROMOTE_BASELINE` | `false` | Set by `npm run test:promote-baseline`, not by users directly. |

A `tests-adversarial/.env.example` template is checked in. The real `.env` is gitignored.

The four demo Cognito users (`ciso_diana@meridianinsurance.com`, `soc_marcus@…`, `grc_priya@…`, `emp_sarah@…`) and the password are the only auth secrets — no AWS access keys, no IAM roles. The harness uses the same browser-cognito flow the SPA uses.

## 9. Acceptance criteria

Numbered so the tester subagent can check each individually. A criterion that cannot be tested mechanically is rewritten until it can.

1. Running `npm run test:all` from `tests-adversarial/` with `DEMO_PASSWORD` set and no other env overrides produces `test-reports/<UTC-ISO-timestamp>/report.html` within **10 minutes wall-clock**, exits 0 when all tests pass, exits non-zero when any test fails.
2. The same run produces sibling files `report.json` and `summary.md` in the same directory.
3. `report.json.cost.actual_usd` is **less than 1.00** for a default-budget run.
4. The harness refuses to start (exits non-zero before any network call) if estimated cost exceeds `BEDROCK_COST_CAP_USD`. Test: set `BEDROCK_COST_CAP_USD=0.01` and confirm pre-flight rejection with a clear message.
5. The harness refuses to start (exits non-zero before any network call) if `DEMO_PASSWORD` is unset.
6. The coverage matrix in `report.html` shows **every page under `ui/src/pages/`** as a row, with one column per persona. Every cell is either green (tested-pass), red (tested-fail), or gray (skipped with reason).
7. The coverage matrix shows **every API route in [`api_handler.py:133-216`](../Infra/functions/api_handler/api_handler.py#L133-L216)** as a row with at least one check across the layer columns.
8. The coverage matrix shows **every `@tool` from `agents/*/agent.py`** as a row with `tool_invoked`, `prompt_only`, or `not_reached`. No tool is silently omitted.
9. A negative auth test exists with id `auth.token-usage.soc-forbidden`: SOC IdToken hitting `GET /token-usage`. It expects HTTP 403, and a 200 result is recorded as **severity:high**.
10. A negative E2E test exists for each `N` cell in §4.1: e.g. `e2e.page.governance.soc` asserts `<AccessDenied />` renders.
11. A documented-unsafe test exists with id `auth.chat.no-signature`: `/chat` with a stripped signature. Result is recorded as `expected: documented-unsafe` and **does not** fail the run unless the platform's behavior changes (i.e. it starts rejecting, which is the regression direction).
12. The `summary.md` file is **forwardable as-is**: it includes the run header, cost, coverage totals, top-5 findings by severity, the diff-from-last-green block, and a link to `report.html`. No secrets, no AWS account ids, no IdTokens.
13. Running `npm run test:e2e`, `test:fuzz`, `test:auth`, `test:llm` standalone each produces a layer-specific subreport at `test-reports/<timestamp>/<layer>/` without invoking the other layers.
14. After a green `test:all` run, `npm run test:promote-baseline` copies the run's per-test-id status to `test-reports/.baseline/last-green.json`. The next run's `report.html` shows a diff-from-last-green section referencing that baseline.
15. A run with no prior baseline (first-ever run, or `.baseline/` missing) renders the diff section as "no baseline; this run will be promotable" and does not crash.
16. The LLM red-team layer sends **at most 30 probes per run** (20 curated + 10 generative). The harness counts probes and refuses to exceed this without an explicit `--llm-probes <n>` override.
17. The cost-DoS probe in the LLM layer (`llm.cost-dos.long-completion`) sends a request that would yield ~2000 output tokens and asserts the response's `output_tokens` (read from chat response telemetry or KB token-usage page) is bounded by the runtime's configured max.
18. The MCP cosmetic-only check (`e2e.mcp-chat.sidebar-cosmetic`) sends the same prompt twice with two different sidebar selections and asserts the outbound network payload to `/chat` is identical apart from non-load-bearing fields. A diverging payload is **severity:high**.
19. The `jira_specialist` probe (`llm.jira.black-box`) sends a Jira-eliciting prompt and asserts (a) HTTP 200, (b) no stack trace in response, (c) no AWS account id (regex `\b\d{12}\b`) in response. Tool-name assertions are intentionally absent.
20. Every failure in `report.json.findings[]` has a non-empty `evidence` field pointing at an existing file under the run's directory (transcripts, screenshots, or `findings.json`).
21. The run is **idempotent across personas**: re-running `test:all` does not delete or modify the demo users' existing conversations or audit history beyond what each individual test does (a single test that creates a session removes it in teardown).
22. `test-reports/` is in `.gitignore`. A grep for the directory in `git ls-files` returns nothing. `tests-adversarial/.env` is also in `.gitignore`.
23. The harness reads `MODEL_PRICING` from a single source (currently duplicated in two files per [`CLAUDE.md`](../CLAUDE.md)). If the two sources disagree, the pre-flight fails with a clear "pricing drift" message rather than silently picking one.
24. The total wall-clock for `test:all` at default budget is **under 10 minutes** measured from process start to `report.html` write.
25. The harness does not write to `Infra/params/dev.json`, does not `cloudformation deploy`, does not `kms`, does not modify any IAM role. A test that checks `git status` after a run shows no changes outside `test-reports/` and (if locally edited) the corpus directory.

## 10. Out of scope / future

Called out so reviewers see the deliberate cut. One-line rationale each.

- **Sustained load testing** (k6, Locust, AWS Distributed Load Testing) — bounded-cost demo account, no SLA to protect, would blow the $1 cap immediately.
- **Chaos engineering** (AWS FIS, DDB throttling, AgentCore runtime kills) — requires new IAM and CFN changes that are off-limits for the harness, and the dev account is the only environment.
- **Visual regression** — no reference baseline exists, would produce noise on every Tailwind change.
- **Accessibility audit** (axe-core on every page × persona) — out of band; `tests-e2e/accessibility.spec.ts` stays as it is.
- **KB poisoning end-to-end** — `/uploads/presign` is probed for the URL-generation defect, but the harness does **not** actually upload poisoned content. Doing so would persist into the KB and affect future demo chats.
- **Continuous LLM red-teaming** (Garak/PyRIT full runs, nightly multi-turn Crescendo attacks) — cost ceiling is the blocker; a 30-probe bounded run is what fits the budget.
- **Real-time dashboards** (Allure, Grafana) — the report file is the deliverable; no dashboard infrastructure to maintain.
- **PR merge gating** — explicit non-goal per §2. Harness is informational.

If any of these later become in scope, they get their own spec — not added to this one.

## 11. Risks

Project-specific footguns the harness will hit if naive.

1. **Polluting demo user history.** Every chat probe writes to `<env>-<project>-sessions`, `audit-log`, `token-usage`. The harness must (a) tag every test-originated session with a known marker in the prompt (e.g. `[harness]` prefix) so the dev team can identify them, and (b) delete its own created conversations in teardown via the existing `DELETE /conversations/{id}` route. This is the largest blast-radius risk because there is no scratch env.
2. **Cost cap is enforced against estimated, not actual.** If the master orchestrator silently flips to Claude Sonnet 4.6 via a `MASTER_MODEL_ID` override ([`CLAUDE.local.md`](../CLAUDE.local.md): "future deploys can override via `MASTER_MODEL_ID=...`"), per-turn cost goes up 10–30x and a single run can exceed the cap mid-flight. Mitigation: the harness reads the deployed runtime's actual `MODEL_ID` (via a probe like "what model are you?" — best-effort) and re-validates the estimate before the LLM layer. If the live model is not Nova 2 Lite, skip the LLM layer and write a `severity:info` notice into the report rather than blow the budget.
3. **`lookup_url_category` calls a live ZIA API.** The Zscaler probe (§4.3) triggers a real third-party API call. Limit to one invocation per run and document the URL probed in the report.
4. **Port 5173 / Vite dev server is irrelevant.** The harness targets the deployed CloudFront, not local dev. A test author confused about this might add a `npm run dev` step somewhere — the spec explicitly says no.
5. **`MODEL_PRICING` drift between `agents/_shared/token_usage.py` and `ui/src/mockData.js`** ([`CLAUDE.md`](../CLAUDE.md)). The harness reads from one and validates against the other (criterion #23) so a drift is caught at run start, not at report-write time.
6. **The `jira_specialist` deployed runtime has no source.** Any prompt that triggers it exercises code we cannot read. Limit to one black-box probe per run, log everything verbatim.
7. **The known-unsafe JWT signature path on `/chat`** is a documented design decision ([`tests/security/test_auth_and_authorization.py:47-68`](../tests/security/test_auth_and_authorization.py#L47-L68)), not a regression. The harness records it as `expected: documented-unsafe` and only flags a change in direction (it suddenly rejects unsigned tokens — meaning someone tightened the contract and other callers might break).
8. **Bedrock concurrency quotas** are the silent ceiling. A burst of 30 LLM probes back-to-back can hit them. The harness sequences LLM probes serially, not in parallel, even though that costs wall-clock — the budget allows it.
9. **The dev account has no WAF / no MFA / demo passwords on** ([`CLAUDE.md`](../CLAUDE.md): "Demo-only, not production"). The harness must not "enable WAF for realism" or otherwise mutate the security posture.

## 12. Open questions

The user has decided the major axes (target env, cost cap, cadence, layers, reporting bar). Two items remain that the reviewer should confirm.

1. **Test-isolation strategy for the demo users.** Should the harness use the four real demo users (`ciso_diana`, etc.) with a `[harness]` prompt prefix and a delete-in-teardown discipline, or should we provision four parallel `test_<persona>` Cognito users so harness traffic never touches the demo accounts' audit/session history? The spec assumes the former for minimum infra; the latter is cleaner if the demo accounts' history matters to the user-facing demo.
2. **Promotion of the first baseline.** The harness ships with no baseline. Should the first successful `test:all` after this spec lands be auto-promoted (one-time) or does the user always promote explicitly? Spec assumes always-explicit; flag if you want the one-time auto-promote shortcut.
