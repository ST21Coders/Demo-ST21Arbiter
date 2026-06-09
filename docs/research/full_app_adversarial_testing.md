# Research brief — Full-app adversarial testing harness for ARBITER (ST21)

**Status:** Research only. No spec, no plan, no implementation.
**Audience:** the designer/spec-writer who will turn this into an acceptance-criteria doc next.
**Scope:** what "fail the application" means here, what exists today, what does not, what tools fit, and what the user has to decide before a spec can be written.

---

## Problem framing

The user wants a polished, automated harness that **tries to break the deployed ARBITER application end to end** — every page, every button, every API route, every agent — across all four Cognito personas. They are explicit that this is the *deployed* (physical) app on AWS, not local mock mode, and that "polished" means production-grade reporting, not a one-off script. "Past its limits" means load, stress, and abuse, not just functional happy-path coverage.

This is a *meta-feature*: the artifact is a test harness, not a product change. So the design tradeoffs are mostly about what layers to cover, what tools to use, and what guardrails to put on cost and blast radius (this is a single demo AWS account with no WAF and no MFA, so a sloppy harness can rack up real Bedrock spend and lock real users out).

---

## 1. What "fail the application" means here

There are at least eight distinct testing layers that fit the ask. Listed in rough order of bang-for-buck for ARBITER specifically.

### 1.1 Functional E2E (every page, every persona)
The SPA has 15 pages under [`ui/src/pages/`](../../ui/src/pages/) and four personas with different `access` arrays in [`ui/src/contexts/PersonaContext.jsx:5-58`](../../ui/src/contexts/PersonaContext.jsx). A real coverage matrix is `15 pages x 4 personas = 60 cells`, of which roughly half should render the page and half should redirect to `AccessDenied`. Today this is the strongest existing layer (Playwright) but it only covers the CISO superset, not the negative cases.

### 1.2 Adversarial input fuzzing against the API
[`Infra/functions/api_handler/api_handler.py`](../../Infra/functions/api_handler/api_handler.py) exposes ~20 routes (see inventory below). Each is an abuse target: oversize JSON, malformed JSON, missing required fields, type confusion (`"prompt": {...}` instead of a string), CRLF injection in audit `details`, control characters, unicode normalization tricks, path traversal in `session_id`, IDOR on `/conversations/{id}` and `/actions/{id}/*`. The DDB-backed Scan handlers (`/findings`, `/actions`, `/audit`, `/scan-runs`) are also good targets for filter-injection (`?severity=`/`?status=`/`?domain=` are upper-cased and compared, see `_handle_list_findings` at [`api_handler.py:385-416`](../../Infra/functions/api_handler/api_handler.py)).

### 1.3 Auth abuse (Cognito + the JWT trust model)
The `/chat` route is on a Lambda Function URL with `AuthType=NONE` and the Lambda **decodes the IdToken without verifying the signature** (the existing test [`tests/security/test_auth_and_authorization.py:47-68`](../../tests/security/test_auth_and_authorization.py) documents this as a known unsafe design). Adversarial probes that fit:
- Forge a JWT with arbitrary `sub` → confirm IDOR boundary (today: forged sub is accepted).
- Forge `cognito:groups: ["ciso"]` → call `/token-usage` → expect 403 if hardened, 200 if not.
- Expired token replay (the SPA's `refresh()` path).
- Swap a SOC user's IdToken into a CISO session.
- Token reuse after `signOut`.
- Cross-persona privilege escalation: every page that gates on `_require_ciso` or `usePersona().hasAccess()` is a target.

### 1.4 LLM-specific abuse (prompt injection, jailbreak, cost DoS)
This is the layer with the biggest delta between "we look polished" and "we actually tested it." The four agents (`master_orchestrator` + 3 specialists) each expose `@tool` functions to Strands' tool-calling loop. Attack surface:
- **Prompt injection through `/chat`** — get the master to call a specialist it shouldn't, leak the system prompt, or return content from the KB that the persona shouldn't see.
- **Indirect injection through the KB** — upload a poisoned policy doc through `/uploads/presign` so the next ingestion job inserts attacker text into the KB; subsequent chats may surface it via `retrieve_policies`. The auto-detect chain (EventBridge → processing_pipeline → KB ingestion → scanner) runs unattended.
- **Tool-call abuse** — coax the master into calling `zscaler_lookup` with a URL that triggers the live ZIA category API in a loop.
- **Cost DoS** — flood `/chat` with prompts that force long completions. Each turn fans out to 4 runtimes, so 100 attacker prompts = 400+ Bedrock invocations.
- **Guardrail bypass** — probe `GUARDRAIL_ID` (the master applies one via Bedrock guardrails) with character substitution, role-play framings, and prompt-leak attacks.
- **Hallucinated tool output handling** — when a specialist returns garbage, does the master surface it as fact? Today `master_orchestrator/agent.py` aggregates specialist replies as strings into a final prompt without provenance.

### 1.5 Load / stress
Two distinct shapes matter:
- **API Gateway side** — `/findings`, `/actions`, `/audit`, `/dashboard` are DDB scans capped at 200 items. Concurrent reads will throttle DDB before they break the Lambda. CLAUDE.local.md flags that GSI queries silently return empty without the `/index/*` IAM resource — load testing might surface that as a phantom empty-result bug.
- **Chat side** — `/chat` is on a Function URL specifically because it bypasses APIGW's 29s timeout. That means the only limit is the Lambda's 15-minute max and Bedrock concurrency quotas. A burst here is the most expensive thing you can do to this account, by orders of magnitude.

### 1.6 Chaos
What "kill it" means in this stack:
- Kill the master AgentCore Runtime mid-chat (AWS does not give you `StopRuntime`; the realistic chaos is throttling the role).
- Throttle DDB writes — the `sessions` table is on the chat-critical path, but the agent's writes are best-effort (see [`agents/_shared/token_usage.py`](../../agents/_shared/token_usage.py) pattern and the `master_orchestrator` memory write). Verify they fail silently as designed.
- Drop network from a specialist runtime — confirm the master gracefully degrades (today the master continues with what it has).
- Kill the api_handler Lambda's role to confirm 5xx propagation and SPA error handling.
- Suspend the Cognito User Pool — confirm refresh flow exits cleanly.

### 1.7 Frontend resilience
- Network throttling (Chrome devtools Slow 3G) → does the loading state on every hook actually render?
- Offline → the SPA's `refresh()` path needs to fail closed, not hang.
- Race conditions — `useEffect` double-fire under React `StrictMode` is called out specifically in [`CLAUDE.local.md`](../../CLAUDE.local.md) and the `useAuth.handleCallback` already has an in-flight guard. Other one-shot effects (token exchange, idempotent POSTs) need the same check; an adversarial harness can intentionally double-fire to catch regressions.
- The DELETE-conversation race ([`useApi.js:268-278`](../../ui/src/hooks/useApi.js)) optimistically removes from local state — if the network call fails, the next `list()` restores it. Worth a chaos test.
- 5xx and CORS preflight failures from the API.

### 1.8 Visual regression / accessibility
The existing `accessibility.spec.ts` is shallow (one img-alt check, one Tab focus check). True axe-core sweeps and visual diffing across all 15 pages × 4 personas would be a meaningful uplift.

---

## 2. Current testing inventory

What exists today. Counted, not assumed.

### Vitest unit tests at [`ui/src/__tests__/`](../../ui/src/__tests__/)
9 files. Coverage by file:
- `detectProblem.test.js` — page-render guard helpers.
- `edgeCases.test.js` — misc edge handlers.
- `helpers.test.js`, `mockData.test.js` — pure-JS utilities.
- `preferences.test.js` — `usePreferences` store.
- `settings.test.jsx` — Settings page render + persona gating (uses `vi.hoisted({ groups })` pattern).
- `theme.test.js` — dark/light theme manager.
- `tokenTracking.test.jsx` — Token Tracking page render, sidebar gating, filter, CSV.

Notable gaps: nothing for `Findings`, `Dashboard`, `ActionCenter`, `AnalystView`, `MCPChat`, `Governance`, `HeatMap`, `AuditLogs`, `DataPipeline`, `LLMControl`, `Personas`, `SignIn`, `FindingDetail`. Most data hooks in `useApi.js` are untested at the unit layer.

### Playwright E2E at [`tests-e2e/`](../../tests-e2e/)
7 spec files, all run in **mock mode by default**:
- `accessibility.spec.ts` — 2 shallow checks.
- `action-request-modal.spec.ts` — open/close + empty-submit on the only modal.
- `auth-live.spec.ts` — the **one** test that hits real Cognito; gated by `TEST_MODE=live` + 5 env vars; covers only "the Personas link is visible".
- `interactions.spec.ts` — Findings filter, expandable rows, sidebar nav, AuditLogs render.
- `navigation.spec.ts` — 8 routes load without console errors, back/forward, deep link to `/findings?severity=HIGH`.
- `pages-deep.spec.ts` — one describe block per page: Dashboard, ActionCenter, Governance, HeatMap, AuditLogs, DataPipeline, LLMControl, Personas, AnalystView, MCPChat. Mostly "page renders something."
- `performance.spec.ts` — 7 routes under a `SLOW_PAGE_MS=3000` budget.

The fixture at [`fixtures.ts:32-55`](../../tests-e2e/fixtures.ts) auto-injects a mock CISO IdToken into `sessionStorage` so the SPA's `RequireAuth` wrapper passes. Persona-specific fixtures (`asSoc`, `asGrc`, `asEmployee`) exist but are **not used by any spec**.

Gaps:
- All persona-gating negative tests (an `employee` hitting `/findings` should redirect to `AccessDenied`).
- All adversarial input shapes.
- All chat-turn flows (the master orchestrator path).
- No load, no chaos, no fuzzing, no LLM red-teaming.
- Settings, TokenTracking, FindingDetail, SignIn pages.

### Backend pytest at [`tests/`](../../tests/)
- `unit/test_api_handler.py`, `unit/test_api_handler_edge_cases.py`, `unit/test_agents.py` — moto-backed handler tests.
- `security/test_auth_and_authorization.py` — IDOR coverage on `/conversations` paths, plus the documented "JWT signature not verified" finding.
- `security/test_input_validation.py` — mass assignment on `/chat`, wrong-method routes, 100KB prompt, unicode probes.
- `smoke/test_live.py` — guarded by `TEST_MODE=live`.

The security suite is the highest-quality artifact in the repo for adversarial work — it documents known-unsafe paths in named tests so regressions can re-tighten them. Worth using as the model for the new harness's API-side tests.

---

## 3. The full surface that needs coverage

### Pages and persona access
From `PersonaContext.jsx:5-58` (`PERSONAS[*].access`) and the `ROUTE_ACCESS` map. `Y` = page renders, `-` = `AccessDenied`.

| Route | File | employee | grc | soc | ciso |
|---|---|---|---|---|---|
| `/` (Dashboard) | `Dashboard.jsx` | - | Y | Y | Y |
| `/findings` | `Findings.jsx` | - | Y | Y | Y |
| `/findings/:id` | `FindingDetail.jsx` | - | Y | Y | Y |
| `/heatmap` | `HeatMap.jsx` | - | Y | Y | Y |
| `/actions` | `ActionCenter.jsx` | - | - | Y | Y |
| `/governance` | `Governance.jsx` | - | Y | - | Y |
| `/audit` | `AuditLogs.jsx` | - | Y | Y | Y |
| `/analyst` | `AnalystView.jsx` | Y | Y | Y | Y |
| `/llm-control` | `LLMControl.jsx` | - | - | - | Y |
| `/pipeline` | `DataPipeline.jsx` | - | - | - | Y |
| `/mcp-chat` | `MCPChat.jsx` | - | - | - | Y |
| `/token-usage` | `TokenTracking.jsx` | - | - | - | Y |
| `/personas` | `Personas.jsx` | Y | Y | Y | Y (unguarded) |
| `/settings` | `Settings.jsx` | Y | Y | Y | Y (unguarded) |
| `/signin` + `/callback` | `SignIn.jsx`, callback handler | n/a | n/a | n/a | n/a |

Total functional cells: 60 positive + negative. Today's E2E exercises ~10 of them, all positive, all as CISO.

### Data hooks in `useApi.js` and the API routes they hit
Every hook is also a place to assert "loading state renders," "error 5xx renders user-facing text," and "401 triggers refresh."

| Hook | Route(s) | Method |
|---|---|---|
| `useFindings` | `/findings`, `/scan` | GET, POST |
| `useChangeRequests` | `/actions`, `/actions/{id}/{approve\|reject\|execute\|escalate}` | GET, POST |
| `useConversations` | `/conversations`, `/conversations/{id}/messages`, `/conversations/{id}` | GET, DELETE |
| `sendChat` | `${CHAT_URL}chat` (Function URL) | POST |
| `useDashboard` | `/dashboard` | GET (polled 60s) |
| `triggerScan`, `getScanRun`, `listScanRuns` | `/scan`, `/scan-runs/{id}`, `/scan-runs` | POST, GET |
| `useFindingDetail` | `/findings/{id}` | GET |
| `useMcpHealth` | `/mcp-health` | GET (polled 30s) |
| `presignUpload`, `uploadToPresignedUrl` | `/uploads/presign` → S3 PUT | POST, PUT |
| `listScanRuns` | `/scan-runs` | GET |
| `createJiraTicket` | `/jira/tickets` | POST |
| `useNavCounts` | `/findings`, `/actions` | GET (polled 60s) |
| `useAudit` | `/audit` | GET |
| `useTokenUsage` | `/token-usage`, `/token-usage/summary` | GET (CISO-only) |

### Lambda routes
From [`api_handler.py:114-216`](../../Infra/functions/api_handler/api_handler.py):
- Public: `GET /health`.
- Chat: `POST /chat` (on the Function URL; the only route that touches Bedrock).
- Findings: `GET /findings`, `GET /findings/{id}`, `POST /scan`, `GET /scan-runs`, `GET /scan-runs/{id}`.
- Conversations: `GET /conversations`, `GET /conversations/{id}`, `DELETE /conversations/{id}`, `GET /conversations/{id}/messages`.
- Change requests: `GET /actions`, `POST /actions`, `POST /actions/{id}/approve|reject|execute|escalate`.
- Audit: `GET /audit`.
- Token usage (CISO-only): `GET /token-usage`, `GET /token-usage/summary`.
- Dashboard / health: `GET /dashboard`, `GET /mcp-health`.
- Uploads: `POST /uploads/presign`, `GET /uploads/list`.
- Jira stub: `POST /jira/tickets`.

### AgentCore runtimes and their tools
- `master_orchestrator/agent.py` — tools `sharepoint_lookup`, `awsconfig_lookup`, `zscaler_lookup` (each invokes the corresponding specialist runtime).
- `sharepoint_specialist/agent.py` — tool `retrieve_policies` (KB query).
- `awsconfig_specialist/agent.py` — tools `list_config_rules`, `get_rule_compliance`, `list_noncompliant_resources`, `retrieve_awsconfig_docs`.
- `zscaler_specialist/agent.py` — tools `retrieve_zscaler_policy`, `lookup_url_category` (live ZIA API call).
- A `jira_specialist` runtime is deployed in this account but **has no source in this repo** — [`CLAUDE.md`](../../CLAUDE.md) flags it explicitly. Any harness that prompts the master to call jira tooling is exercising code the team can't read; treat it as a black box and coordinate before testing.

---

## 4. Tooling tradeoffs

For each layer, the credible options and a one-line lean. The user has to make the call; these are recommendations not decisions.

### Functional E2E
- **Playwright** — already in the repo, fixture infrastructure exists, persona fixtures wired but unused. Adding 50 more specs is the lowest-friction path.
- **Cypress** — comparable feature set; would require dual-installing test infra and rewriting fixtures.
- **WebdriverIO** — Selenium-based, heavier than Playwright for this stack.
- *Lean: Playwright. Continue the existing pattern; the cost of switching is high and gains are marginal.*

### Load / stress
- **k6** — JavaScript, Grafana-native dashboards, AWS Cognito auth flow documented (the AWS Distributed Load Testing solution supports k6 directly). Lowest learning curve for a JS shop.
- **Locust** — Python, good for testing LLM workloads specifically (some teams prefer it for Bedrock load).
- **Artillery** — YAML-first, simplest; weaker reporting.
- **JMeter** — heavyweight; rarely the right call for a greenfield harness.
- *Lean: k6 for the API layer, possibly Locust for the chat layer if Python feels more natural for crafting LLM-flooding prompts. Either way, AWS Distributed Load Testing can host the runners.*

References: [Vervali load-testing comparison 2026](https://www.vervali.com/blog/best-load-testing-tools-in-2026-definitive-guide-to-jmeter-gatling-k6-loadrunner-locust-blazemeter-neoload-artillery-and-more/), [AWS Distributed Load Testing docs](https://docs.aws.amazon.com/solutions/distributed-load-testing-on-aws/).

### API fuzzing
- **Schemathesis** — OpenAPI-driven; the project does not have an OpenAPI spec, so this is non-trivial. Worth generating one from the api_handler routes anyway.
- **RESTler** — stateful API fuzzer; learns sequences. Heavier setup.
- **OWASP ZAP** — broader web scanner; not great for JSON-only APIs.
- **Hand-rolled hypothesis-based fuzzer in pytest** — fits the existing `tests/security/` pattern and lets you keep the JWT-forging helpers from `conftest.py`.
- *Lean: hand-rolled hypothesis under pytest. The existing security suite is already the right shape and the team has the JWT forging tools.*

### LLM red-teaming
- **Garak (NVIDIA, Apache 2.0)** — static probe library, ~120 probes, good for prompt injection / toxicity / data leakage scans at the model layer. Works with Bedrock.
- **PyRIT (Microsoft, MIT)** — dynamic orchestrator that generates adversarial prompts against a live target; supports multi-turn (Crescendo) attacks. Better for agent-level testing.
- **Promptfoo** — application-layer test framework; integrates with CI/CD; weaker on multi-turn.
- **Custom harness** — full control, but reinvents the probe library.
- *Lean: pair Garak for breadth (single-turn vulnerability scan) with PyRIT for depth (multi-turn against the master orchestrator). Promptfoo is a third complementary option if "assertions in CI" matters more than "exploratory probing."*

References: [BeyondScale comparison 2026](https://beyondscale.tech/blog/ai-red-teaming-tools-comparison-2026), [General Analysis tools guide](https://generalanalysis.com/guides/best-ai-red-teaming-tools), [QAwerk on coverage gaps](https://qawerk.com/blog/llm-red-teaming-tools/).

### Chaos
- **AWS FIS (Fault Injection Service)** — native, can throttle DDB, kill EC2, terminate ECS tasks. Cannot natively kill AgentCore Runtimes (no FIS action exists for them as of cutoff). Requires the FIS IAM role and experiment templates.
- **Chaos Toolkit** — open-source; AWS extension exists; covers similar ground via API calls.
- **Gremlin** — commercial; overkill for a demo.
- **Hand-rolled boto3 scripts** — for the AgentCore-specific cases (delete runtime + recreate, etc.), this is the only path.
- *Lean: AWS FIS for what it supports, hand-rolled boto3 for AgentCore-specific chaos. Both are gated on the IAM the dev account does or does not already have for FIS.*

### Reporting
- **Playwright HTML report** — already in `tests-e2e/playwright.config.ts` (`test-reports/playwright-html`). Good for the E2E layer.
- **Allure** — multi-tool aggregator; one dashboard across Playwright + pytest + k6. More setup.
- **Grafana** — natural for k6 metrics + CloudWatch overlays.
- **GitHub PR comments via Actions** — for the CI-blocking subset.
- *Lean: keep Playwright HTML for the E2E layer; add an Allure or simple static HTML aggregator if cross-layer rollup matters to "polished".*

---

## 5. Where this runs and what it costs

### Run targets
The user said "physical application." That can mean:
- **The deployed dev env at the CloudFront URL** (the only "physical" surface that exists today; account `669810405473`).
- **A short-lived scratch env spun up per CI run** (closer to production hygiene; expensive at ~2-3 hours of stack deploy per run and the AgentCore image rebuild).
- **A long-lived staging env** — does not exist in this account.

The dev env is the only realistic target without a scratch-env story. Adversarial tests against it will pollute its DDB tables (audit-log, conflicts-v2, scan-runs, change-requests, sessions, token-usage). Some of that pollution is on TTL; some is not. A test-isolation strategy (synthetic personas? reserved `actor_id` prefix? a TTL nuke step?) is part of the design space.

### Cost order-of-magnitude
- **Bedrock per chat turn** — 1 master + 1–3 specialists × Nova 2 Lite. Nova 2 Lite is the cheapest model; per-turn cost is fractions of a cent. A 100-concurrent-user × 10-minute load test that achieves even 1 turn/user/minute is ~1000 turns ≈ ~4000 invocations. At Nova 2 Lite pricing this is single-digit dollars per run.
- **If the runtimes ever flip to Claude Sonnet 4.6** (the `MASTER_MODEL_ID` override path exists), multiply by ~10–30x. A bad LLM-flooding test against Claude can hit $50–$200/run.
- **DDB writes** during chaos — `token-usage` writes ~4 rows/turn; at 4000 invocations that is 16,000 writes ≈ pennies on PAY_PER_REQUEST.
- **CloudWatch logs** — `aws-opentelemetry-distro` is auto-instrumented on every agent. A high-volume test can fill log groups; check the retention.
- **AWS FIS** — sub-dollar per experiment.

### Permissions the dev account likely does not have
- **AWS FIS** is not in any current CFN template; deploying it requires a new IAM role and a one-time CFN add.
- **Bedrock concurrency quotas** are the silent throttle ceiling. Hitting them is itself a useful test result, but it also blocks legitimate demo traffic during the run.
- **CloudWatch Synthetics** (an alternative live-monitoring tool) is similarly not provisioned.
- **DDB scaling** is PAY_PER_REQUEST so no provisioned-capacity throttle to relax, but the API-handler IAM role's wildcards may need expansion for FIS-injected paths.

---

## 6. Risks and footguns specific to this codebase

What a naive test harness will get wrong if it does not read the project docs.

- **Don't touch [`Infra/params/dev.json`](../../Infra/params/dev.json).** A harness that re-pins `KbId`, `MasterAgentRuntimeArn`, or `PrivateSubnet2AZ` will retarget infra during the next deploy and break the dev account. Off-limits.
- **AgentCore subnet AZ is account-specific** (only `use1-az1`, `use1-az2`, `use1-az4` are supported). A chaos test that "moves the runtime to another AZ" will fail to recreate. Treat the AZ pin as immutable.
- **The `/chat` Function URL has `AuthType=NONE`.** The Lambda decodes the IdToken **without verifying its signature** (documented at `tests/security/test_auth_and_authorization.py:47-68`). A harness must either (a) match this trust model when crafting tokens, which makes forging trivial, or (b) explicitly test that signature validation, when added, breaks forgery. Both have value.
- **GSI queries return empty without `/index/*` IAM.** A naive fuzz that scrambles the SAM `Resource:` line and re-deploys could produce a confusingly silent empty list, not an error. Flag in pre-flight.
- **`DynamoDBKeyArn` must be in the agentcore role's `KMSDecrypt`.** A chaos test that removes/rotates the CMK without coordination silently breaks every agent's `PutItem`.
- **WAF off, MFA off, demo passwords on.** [`CLAUDE.md`](../../CLAUDE.md) is explicit. A harness that tries to enable WAF "for realism" violates the project posture. Test against the actual stance.
- **The `jira_specialist` runtime has no source in this repo.** Coordinate before any test that triggers it.
- **MCP sidebar in `MCPChat.jsx` is cosmetic.** A test that asserts "selecting Atlassian MCP changes routing" is asserting a bug. Match the docs.
- **React `StrictMode` double-fires effects in dev.** Tests that count "GET /conversations was called exactly once on mount" are wrong unless they run against the prod build. Read the `handleCallback` in-flight guard pattern in [`useAuth.js`](../../ui/src/hooks/useAuth.js).
- **Vite dev server must be on port 5173** (Cognito callback whitelist). A harness that spins up `npm run dev -- --port 5174` will fail the auth flow with an opaque Hosted UI error.
- **Region is hard-pinned to `us-east-1`.** Cross-region anything is out of scope.
- **Mock mode vs live mode** — `USE_MOCK = !API_URL`. A harness that forgets to set `VITE_API_URL=` will appear to "pass" everything by hitting mock data and never the real backend. The current Playwright config makes this exact mistake the default, so any "physical app" suite needs an explicit `BASE_URL=<cloudfront>` + `TEST_MODE=live` mode.
- **`MODEL_PRICING` is duplicated** in `agents/_shared/token_usage.py` and `ui/src/mockData.js`. A test that asserts "UI cost equals backend cost" must read both and pass when they agree, fail loudly when they drift.
- **The CI buildspec runs each `commands:` item in its own POSIX shell.** A "polished" test runner that uses bash-isms (`${V:0:N}`, multi-step flow control across commands) will pass locally and fail in CI. See [`buildspec.yml`](../../buildspec.yml) comments.

---

## 7. Open questions for the user

These are decisions only the user can make. The designer cannot start a spec until at least the top 3 are answered.

1. **Target environment.** Is "the physical application" the existing deployed dev env at `https://d5u0vv1zl3eqd.cloudfront.net/`, a freshly-spun-up scratch env per run, or both? (Pollution of the dev DDB tables is real; a scratch env costs 2–3 hours of CFN per run.)

2. **Test-user identity.** Are you OK with the harness using the four demo Cognito users (`ciso_diana`, `soc_marcus`, `grc_priya`, `emp_sarah`) with their demo passwords, or do we need to provision four parallel `test_*` users so adversarial runs don't poison the demo accounts' session/audit history?

3. **Cost ceiling per run.** Set a dollar ceiling. A heavy LLM-flooding run on Nova 2 Lite is single-digit dollars; on Claude (if the team ever flips) it can be $50–$200. The harness should refuse to start a stress run above the ceiling.

4. **CI policy.** Should adversarial tests block PR merge (CI fails on a single LLM jailbreak), or just produce a dashboard the user reviews? They have very different shapes — gating means budgeting for occasional flaky-block; dashboard means writing a triage workflow.

5. **"Polished" means what, concretely.** Pick one or more: (a) Allure dashboards on every run, (b) Slack/email summary per run, (c) PR comments with diffs from last green, (d) a hosted Grafana with rolling 7-day trends, (e) plain Playwright HTML in `test-reports/` like today. The answer drives 20+% of the implementation effort.

6. **Single mega-harness vs layered suites.** One `npm run adversarial` button that runs everything, or independent commands (`run-e2e`, `run-fuzz`, `run-llm-red-team`, `run-load`, `run-chaos`) that compose for the mega-run? Layered suites are easier to maintain; mega-harness is what "extremely polished" sounds like.

7. **LLM red-team budget for one run.** Garak's full probe set is hundreds of attempts per probe; PyRIT's Crescendo can spend dozens of turns on a single attack. Are we OK with a 30-minute, $5-ish single-run LLM red-team in CI nightly, or do you want it as an on-demand "I have an hour and a budget" trigger?

8. **Chaos blast radius.** Are you willing to deploy AWS FIS into the dev account (one new CFN stack, new IAM role), or should chaos be limited to what hand-rolled boto3 scripts can do without FIS (kill agent runtimes, throttle by IAM deny, etc.)?

9. **Reporting retention and PII.** Test reports will contain full chat transcripts (including any inputs that hit guardrails) and the JWTs of test users. Where do they live (S3 bucket? CodeBuild artifacts? committed to the repo?) and what is the retention?

10. **Out-of-scope confirmation.** Is the deployed `jira_specialist` (no source in repo) in or out of scope? Same question for the SignIn / Cognito Hosted UI flow itself — that one is partially out-of-our-control (AWS hosted page) but is a legitimate part of the "every damn thing" surface.

---

## Recommended direction (suggestion, not a decision)

If forced to lean: **layered suites** (#6), targeting the **existing dev env** (#1) with **dedicated test users** (#2), **Playwright + k6 + Garak + PyRIT + pytest-hypothesis** as the toolchain, a **cost ceiling of $10/run for stress, $50/week for nightly LLM red-team** (#3, #7), **PR-blocking only for the deterministic security and functional layers, dashboard-only for LLM red-team and load** (#4), **Allure aggregator + Slack summary** as "polished" (#5), **AWS FIS deferred** unless the user signs off on the IAM expansion (#8).

That gets you 80% of the value, costs single-digit dollars per CI run, runs nightly without supervision, and reuses every artifact the project already has.

---

## References

- Project docs read: [`CLAUDE.md`](../../CLAUDE.md), [`CLAUDE.local.md`](../../CLAUDE.local.md), [`Documents/token_tracking_spec.md`](../../Documents/token_tracking_spec.md), [`Documents/settings_spec.md`](../../Documents/settings_spec.md), [`instructions/DEPLOYMENT.md`](../../instructions/DEPLOYMENT.md), [`Infra/params/dev.json`](../../Infra/params/dev.json).
- Codebase: [`ui/src/pages/`](../../ui/src/pages/), [`ui/src/hooks/useApi.js`](../../ui/src/hooks/useApi.js), [`ui/src/contexts/PersonaContext.jsx`](../../ui/src/contexts/PersonaContext.jsx), [`Infra/functions/api_handler/api_handler.py`](../../Infra/functions/api_handler/api_handler.py), [`agents/*/agent.py`](../../agents/), [`tests-e2e/`](../../tests-e2e/), [`tests/`](../../tests/), [`ui/src/__tests__/`](../../ui/src/__tests__/).
- External:
  - [AI Red Teaming Tools Comparison 2026 — BeyondScale](https://beyondscale.tech/blog/ai-red-teaming-tools-comparison-2026)
  - [Best AI Red Teaming Tools 2026 — General Analysis](https://generalanalysis.com/guides/best-ai-red-teaming-tools)
  - [LLM Red Teaming Tools Compared — QAwerk](https://qawerk.com/blog/llm-red-teaming-tools/)
  - [Garak vs PyRIT — AI Safety Directory](https://aisecurityandsafety.org/en/compare/garak-vs-pyrit/)
  - [Best Load Testing Tools 2026 — Vervali](https://www.vervali.com/blog/best-load-testing-tools-in-2026-definitive-guide-to-jmeter-gatling-k6-loadrunner-locust-blazemeter-neoload-artillery-and-more/)
  - [Distributed Load Testing on AWS](https://docs.aws.amazon.com/solutions/distributed-load-testing-on-aws/)
  - [Microsoft PyRIT GitHub](https://github.com/Azure/PyRIT)
  - [NVIDIA Garak GitHub](https://github.com/NVIDIA/garak)
