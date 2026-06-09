# ARBITER Adversarial Test Harness

A four-layer adversarial test harness that runs from a developer laptop against the
deployed dev ARBITER stack (CloudFront SPA + API Gateway + Lambda Function URL +
Bedrock AgentCore runtimes). The layers are **Functional E2E** (Playwright),
**API fuzz** (pytest + hypothesis), **Auth abuse** (pytest + JWT forging), and
**LLM red-team** (pytest + a curated probe corpus). A single Python orchestrator
sequences them, enforces a Bedrock cost cap, and emits a unified report.

The deliverable is intentionally **forwardable**: every run produces a self-contained
directory under `test-reports/<UTC-ISO-timestamp>/` containing `report.html`
(standalone, openable offline), `report.json` (machine-readable, schema v1.0.0), and
`summary.md` (sanitized, paste-into-chat). The harness is **read-only against
deployed infra** — it does not run `cloudformation deploy`, it does not touch IAM, it
does not write to `Infra/params/dev.json`. Findings are evidence-backed and ranked by
severity.

---

## Quick start

```bash
cd tests-adversarial
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e .
npm install
npx playwright install chromium   # E2E layer browser
cp .env.example .env              # then fill in the required values
npm run test:all
```

Required env vars (in `.env`):

- `DEMO_PASSWORD` — password for the four demo Cognito users.
- `COGNITO_USER_POOL_ID` — from CFN export of the `03-identity` stack.
- `COGNITO_CLIENT_ID` — public SPA client (no secret).
- `CHAT_FUNCTION_URL` — Lambda Function URL for `/chat` (auth and llm layers skip
  cleanly if unset, but you'll lose ~40% of coverage).

What you'll see: a progress banner on stdout, then ~5 minutes later a
`test-reports/<UTC-ISO-timestamp>/` directory with `report.html`, `report.json`,
`summary.md`, and one subdirectory per layer holding raw transcripts and
screenshots. Exit code 0 on all-pass-or-documented-unsafe, 1 on any fail, 2 on
pre-flight rejection (cost cap, missing env, manifest drift), 3 on global timeout.

---

## The 4 layers

Each layer is independently runnable. The orchestrator (`test:all`) sequences them
in the order below.

### 1. Functional E2E

What it does: Playwright drives the deployed CloudFront SPA as each of the four
personas (CISO, SOC, GRC, employee) and walks every page in the 60-cell coverage
matrix. Verifies positive access for `Y` cells (page header + one interaction
clicks without console error), negative gating for `N` cells (`<AccessDenied />`
or redirect), the Cognito Hosted UI sign-in flow once per run, and the MCP
sidebar's cosmetic-only behavior (same prompt, two sidebar selections, identical
outbound `/chat` payload).

Command: `npm run test:e2e`

Finding criteria: a positive cell that renders 4xx or a console error is `severity:high`;
a negative cell that renders the gated content is `severity:critical`; an MCP sidebar
selection that diverges the `/chat` payload is `severity:high`.

### 2. API fuzz

What it does: pytest + hypothesis hits every route in
`Infra/functions/api_handler/api_handler.py:133-216` with both a curated corpus
(XSS, SQLi, oversized 1MB bodies, malformed JSON, control chars, path traversal,
unicode normalization, CRLF) and 8 hypothesis-generated examples per route shape.
Requests are rate-limited (one per route-shape per second) and any created rows
are deleted in teardown.

Command: `npm run test:fuzz`

Finding criteria: a 5xx from any curated payload is `severity:high`; a stack trace
in the response body is `severity:high`; reflected payload in the response is
`severity:medium`.

### 3. Auth abuse

What it does: pytest exercises token replay, cross-persona privilege escalation
(SOC IdToken on CISO-only `/token-usage`), forged `cognito:groups` claims, and
the documented-unsafe `/chat` no-signature case. JWT forging reuses the
`make_jwt` pattern from `tests/conftest.py`.

Command: `npm run test:auth`

Finding criteria: a forged-groups JWT that returns 200 from a gated route is
`severity:high`; an expired-token replay that returns 200 is `severity:medium`;
the no-signature `/chat` case is `expected: documented-unsafe` and only flags a
regression if the platform suddenly **starts** rejecting (which would break
legitimate callers, per the spec's risk register).

### 4. LLM red-team

What it does: pytest sends 30 bounded probes to the master orchestrator via the
Function URL — 20 curated (system-prompt leak, persona impersonation, KB exfil,
tool coercion, jailbreak roleplay, character substitution) and 10 generative
(deterministic transforms of curated sources, fixed seed so the diff-from-last-green
stays stable). Plus one cost-DoS probe (asserts response is bounded by the
runtime's max output tokens) and one black-box `jira_specialist` probe.

Command: `npm run test:llm`

Finding criteria: a probe that elicits the system prompt verbatim, a successful
KB exfiltration, an unbounded long-completion response, or an AWS account id
`\b\d{12}\b` echoed back in chat is `severity:high` or `severity:critical`.

---

## The report

Every run writes three sibling deliverables in `test-reports/<UTC-ISO-timestamp>/`.

- **`report.html`** — a standalone, polished HTML page that opens offline (no CDN
  references). Sections in order: run header, cost footer (top), coverage matrix
  (pages × personas, API routes, agent tools — rendered as three tables), findings
  ranked by severity with sortable headers, diff-from-last-green, cost footer
  (bottom), evidence links to raw transcripts and screenshots.
- **`report.json`** — machine-readable, schema version `1.0.0`. Conforms exactly to
  spec §6.3: `metadata`, `coverage`, `cost`, `findings[]`, `summary`,
  `diff_from_last_green`. Every `findings[].evidence` field points at a file that
  exists under the run directory (enforced before the JSON is even written).
- **`summary.md`** — ~30 lines, forwardable to a teammate. Sanitized of 12-digit
  AWS account ids (`\b\d{12}\b` → `[REDACTED-12DIGIT]`), JWT-shape tokens
  (`eyJ…\.eyJ…\.…` → `[REDACTED-JWT]`), and base64 runs over 200 chars
  (`[REDACTED-BASE64-N-CHARS]`). Includes header, cost, coverage totals, top-5
  findings, diff block, and a relative link to `report.html`.

Severity ladder: `critical` > `high` > `medium` > `low` > `info`. A
`documented-unsafe` outcome is **not** a failure — it is the contract the platform
documents and only flips to a finding if the platform's behavior changes direction.

---

## Coverage matrix

What "extremely polished" means concretely:

- **60 page × persona cells** (15 pages × 4 personas — every page under
  `ui/src/pages/`, every persona). Both positive (`Y` — should render) and
  negative (`N` — should be gated) cells are tested.
- **25 API routes** (every `if path == "/..."` in
  `Infra/functions/api_handler/api_handler.py:133-216`). Each route gets at least
  one curated row and one hypothesis-generated row.
- **12 agent tools** (every `@tool`-decorated function across `agents/*/agent.py`,
  plus one synthetic `master.chat_surface` sentinel covering the LLM red-team's
  chat-level probes). Status per tool is `tool_invoked`, `prompt_only`, or
  `not_reached` — no tool is silently omitted.
- **4 personas** — CISO (broadest surface), SOC, GRC, employee (narrowest).

Every cell is auditable in `report.html` — the coverage matrix renders one row
per surface, one column per persona where applicable, with a status and evidence
link per cell. Manifest drift (a page added to `ui/src/pages/` without a
manifest entry, or vice versa) is detected at run start by
`scripts/check_manifest_drift.py` and fails the run with a non-zero exit code
before any network call.

---

## Budget

The harness caps Bedrock spend at **$1.00 per run** by default. Override via
`BEDROCK_COST_CAP_USD` in `.env` or `--cap-usd` on `run_all.py`. The pre-flight
phase refuses to start if the estimated spend exceeds the cap — actual spend is
then tracked through every Bedrock call and surfaced in the report's cost
footer. A typical full run on Nova 2 Lite (the default foundation model) costs
**well under $0.01**.

Pricing is read from a single source of truth: `agents/_shared/token_usage.py::MODEL_PRICING`.
The harness also parses `ui/src/mockData.js::MODEL_PRICING` and raises
`PricingDriftError` at pre-flight if the two disagree. **Updating `MODEL_PRICING`
is a deliberate two-file commit** done by whoever adds a model — not by this
harness. Both files remain off-limits per `CLAUDE.md`.

---

## Diff from last green

The report shows newly failing tests, newly passing tests, and resolved findings
**versus the last promoted green baseline**. After a fully green run, promote
that run as the new baseline:

```bash
npm run test:promote-baseline
```

Promotion is always explicit so a flake never overwrites a known-good baseline.
A run with no prior baseline (first ever, or `.baseline/` deleted) renders the
diff section as "no baseline; this run will be promotable" and does not crash.

The baseline lives at `test-reports/.baseline/last-green.json` and is gitignored
along with the rest of `test-reports/`.

---

## What's out of scope

These are deferred to future work and explicitly **not** in the harness:

- **Sustained load testing** (k6, Locust) — bounded-cost demo account, would blow
  the $1 cap immediately.
- **Chaos engineering** (AWS FIS, DDB throttling) — needs IAM/CFN changes that
  are off-limits.
- **Visual regression** — no reference baseline, would produce noise on every
  Tailwind change.
- **Accessibility audits** (axe-core) — out of band; the existing
  `tests-e2e/accessibility.spec.ts` stays as it is.
- **KB poisoning end-to-end** — `/uploads/presign` is probed for the URL-generation
  defect, but the harness never actually uploads poisoned content.
- **Continuous LLM red-teaming** (full Garak/PyRIT runs) — cost ceiling blocks it;
  the 30-probe bounded run is what fits the budget.
- **PR merge gating** — the harness is informational, not a gate.

Each of these gets its own spec if they become in scope. They are not bolted onto
this one.

---

## Troubleshooting

- **`DEMO_PASSWORD required`** → set it in `tests-adversarial/.env` (copy from
  `.env.example`). The harness fails fast in pre-flight if it's unset; no
  network call is made.
- **`manifest drift detected`** → `coverage/manifest.json` is out of sync with the
  real source tree. Either edit the manifest (deliberate act, treat it as a code
  review point) or check what changed in `ui/src/pages/`,
  `Infra/functions/api_handler/api_handler.py:133-216`, or `agents/*/agent.py`.
- **`Cost preflight refused`** → estimated cost > cap. Either raise
  `BEDROCK_COST_CAP_USD`, trim `--llm-probes`, or run a single layer instead
  of `test:all`.
- **`globalSetup failed`** (Playwright) → almost always `DEMO_PASSWORD` or
  `COGNITO_*` env vars unset. The global setup calls
  `cognito-idp.InitiateAuth` for each demo user and caches a per-persona
  `storageState` to disk.
- **`CHAT_FUNCTION_URL not set`** → the auth and llm layers' `/chat` probes
  cleanly skip (`pytest.skip("CHAT_FUNCTION_URL not set")`). Set it from the
  CFN exports of the `06-api` stack to recover the missing coverage.
- **`PricingDriftError`** → `MODEL_PRICING` in
  `agents/_shared/token_usage.py` and `ui/src/mockData.js` disagree on a model's
  input or output rate. Reconcile by hand (both files together, per project
  convention). The harness will not pick a winner.
- **`port 5173` errors** → ignore. The harness targets deployed CloudFront, not
  the local Vite dev server. If you see this, you started something else by
  accident.

---

## Project layout

```
tests-adversarial/
├── e2e/                       # Playwright tests (JS, not TS per project rule)
│   ├── tests/                 # 5 spec files (pages-per-persona, negative-gating,
│   │                          #   cognito-hosted-ui, mcp-cosmetic, interactions)
│   ├── fixtures/              # route -> one-click selector
│   ├── storage-states/        # per-persona storage states (gitignored)
│   └── playwright.config.js
├── fuzz/                      # pytest + hypothesis API fuzz
│   ├── corpus/                # 8 curated corpus files (findings, conversations,
│   │                          #   actions, audit, scan, chat, uploads, jira)
│   ├── test_api_routes.py
│   └── test_hypothesis_strategies.py
├── auth/                      # auth abuse probes (token replay, cross-persona,
│   │                          #   chat no-signature, forged groups)
│   ├── corpus/
│   └── test_*.py
├── llm/                       # LLM red-team probes
│   ├── corpus/probes.yaml     # 20 curated probes
│   ├── test_curated_jailbreaks.py
│   ├── test_generative_probes.py
│   ├── test_cost_dos.py
│   └── test_jira_blackbox.py
├── src/                       # shared Python code
│   ├── cost/                  # pricing reconcile + preflight + tracker
│   ├── identity/              # cognito_auth (boto3 InitiateAuth)
│   ├── coverage/              # manifest + builder
│   └── reporting/             # report_builder + renderer + diff + Jinja2 templates
├── scripts/                   # operator entry points
│   ├── run_all.py             # orchestrator
│   ├── promote_baseline.py
│   └── check_manifest_drift.py
├── tests/                     # harness-of-the-harness unit tests
├── test-reports/              # runtime artifacts (gitignored)
│   └── .baseline/             # promoted last-green baseline
├── package.json               # Playwright + npm scripts
├── pyproject.toml             # pytest, hypothesis, jinja2, pyyaml, boto3
├── .env.example
├── .gitignore                 # local override (.env, .venv/, node_modules/,
│                              #   e2e/storage-states/, test-reports/)
└── README.md                  # this file
```

---

## Forwarding the report

When you send a run to a teammate:

- Lead with **`summary.md`** — it carries the run header, cost, coverage totals,
  top-5 findings by severity, the diff-from-last-green block, and a link to
  `report.html`. It is already sanitized of 12-digit account ids, JWT-shape
  tokens, and long base64 runs.
- Attach **`report.html`** for the full coverage matrix and findings table. It is
  fully self-contained (no CDN references); the recipient opens it offline.
- Optionally attach the layer subdirectories under the run dir if the teammate
  needs to dig into raw transcripts or Playwright screenshots.

---

## CI integration

The harness is designed for **manual once-a-day runs** from a developer laptop
or a one-shot CodeBuild project. There is no CI wiring today — that is future
work and will get its own spec.

To run it once on demand from a fresh checkout:

```bash
cd tests-adversarial
source .venv/bin/activate
DEMO_PASSWORD=$DEMO_PWD \
COGNITO_USER_POOL_ID=$POOL \
COGNITO_CLIENT_ID=$CLIENT \
CHAT_FUNCTION_URL=$URL \
  npm run test:all
```

---

## Off-limits (project rules)

The harness reads but never writes the following. Updating any of them is a
deliberate, manual commit done outside the harness:

- `MODEL_PRICING` in `agents/_shared/token_usage.py` and `ui/src/mockData.js`
  (kept in sync by hand per project convention).
- `Infra/params/dev.json`, `Infra/templates/09-agentcore.yaml`, `buildspec.yml`.
- All `agents/*/agent.py` and `Infra/functions/api_handler/api_handler.py`.

See [`CLAUDE.md`](../CLAUDE.md) for the full off-limits list. Spec §9 criterion
25: `git status` after a clean run shows no changes outside `test-reports/`.

---

## Acceptance criteria

The full list of 25 acceptance criteria lives in
[`Documents/full_app_adversarial_testing_spec.md`](../Documents/full_app_adversarial_testing_spec.md)
§9. The implementation plan in
[`docs/plans/full_app_adversarial_testing_plan.md`](../docs/plans/full_app_adversarial_testing_plan.md)
maps each criterion to a specific task. Highlights:

- **AC1, AC24** — `test:all` runs under 10 minutes wall-clock from process start
  to `report.html` write.
- **AC2** — `report.html`, `report.json`, `summary.md` are siblings in the run
  directory.
- **AC3** — `report.json.cost.actual_usd < 1.00` on a default run.
- **AC4, AC5** — pre-flight refuses to start (no network call) on cost overrun or
  missing `DEMO_PASSWORD`.
- **AC6, AC7, AC8** — the coverage matrix shows every page, every API route,
  every agent tool. Nothing is silently omitted.
- **AC9** — `auth.token-usage.soc-forbidden` exists; a 200 is `severity:high`.
- **AC10** — every `N` cell in §4.1 has a negative E2E test.
- **AC11** — `auth.chat.no-signature` is `documented-unsafe`; only flags a
  direction-change regression.
- **AC12** — `summary.md` is forwardable; no account ids, no JWTs.
- **AC13** — each `npm run test:<layer>` runs standalone and produces a
  layer-only subreport.
- **AC14, AC15** — `test:promote-baseline` updates `.baseline/last-green.json`;
  missing baseline does not crash.
- **AC16** — LLM layer caps at 30 probes (20 curated + 10 generative);
  `--llm-probes` overrides.
- **AC17** — `llm.cost-dos.long-completion` asserts response bounded by the
  runtime's max output tokens.
- **AC18** — `e2e.mcp-chat.sidebar-cosmetic` flags a `/chat` payload divergence
  as `severity:high`.
- **AC19** — `llm.jira.black-box` asserts no stack trace, no `\b\d{12}\b`
  account id in response.
- **AC20** — every finding has a non-empty `evidence` field pointing at an
  existing file under the run dir.
- **AC22** — `test-reports/` and `tests-adversarial/.env` in `.gitignore`.
- **AC23** — `MODEL_PRICING` drift raises `PricingDriftError` at pre-flight.
- **AC25** — harness writes nothing outside `test-reports/`.

---

## How to add a probe or corpus row

- **New API fuzz corpus row** — edit `fuzz/corpus/<route>.json`, add an object
  with `name`, `body` (or `query`), and `expected_status` (or
  `expected_status_range`). Re-run `npm run test:fuzz`.
- **New auth probe** — edit `auth/corpus/probes.json` with the JWT shape, target
  route, and expected outcome. Add the test id under `auth.<category>.<name>`.
- **New LLM probe** — edit `llm/corpus/probes.yaml`. Each probe needs `id`,
  `category`, `prompt`, `compliance_marker` (a substring whose presence in the
  reply means the model complied with the attack), and `severity_on_failure`.
  The 30-per-run cap is enforced; bump `--llm-probes <n>` if you intentionally
  want to exceed it.
- **New page/route/tool in source** — edit `coverage/manifest.json` to add the
  entry, then run `npm run test:check-drift` to confirm the manifest matches the
  source tree. The drift check is also the first thing `run_all.py` does, so
  the next full run catches mismatches automatically.

Every probe addition should ship with a unit test in `tests/` so the
harness-of-the-harness coverage stays clean.
