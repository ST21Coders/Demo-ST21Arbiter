# ARBITER Adversarial Harness — Security Compliance Coverage Matrix

**Date:** 2026-06-10
**Harness version:** post-Block-B (2026-06-10)
**Reference standard:** internal compliance checklist (OWASP Top 10 + API Top 10 + LLM Top 10 + CWE common weaknesses)

This document maps every item on the requested compliance checklist to the current state of the adversarial test harness at `tests-adversarial/`. For each item it states:

- **✅ Covered** — at least one test exists and would catch a regression
- **🟡 Partial** — some surface tested but coverage has known gaps
- **❌ Missing** — no test in the harness today
- **⚪ Out of scope** — not testable by an automated runtime test harness against a deployed app (different tool, different team, or process control)

The end of the document has a priority-ranked build plan for closing the gaps.

---

## 1. Injection

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 1 | SQL injection | ✅ Covered | `fuzz/corpus/sqli.json` (9 curated payloads) parameterised across all 25 API routes. ARBITER uses DynamoDB so SQLi isn't the literal threat, but the harness catches any string-template logic that mishandles quoted input. | – |
| 2 | NoSQL injection | ✅ Covered | Block A: `fuzz/corpus/nosql_operators.json` (7 entries: `$ne`, `$gt`, `$where`, `$regex`, `$or`, etc.) parameterised across all 25 routes. | – |
| 3 | OS command injection | ✅ Covered | `fuzz/corpus/command_injection.json` (6 entries: backticks, `$()`, `;`, `|`, etc.) on every route. |  – |
| 4 | LDAP injection | ✅ Covered | Block A: `fuzz/corpus/ldap.json` (6 entries: `*)(uid=*`, `*)(\|(uid=*))`, null-byte truncation, etc.). | – |
| 5 | XPath injection | ✅ Covered | Block A: `fuzz/corpus/xpath.json` (6 entries: `' or '1'='1`, `or count(/*)>0`, union wildcard). | – |
| 6 | XML injection (incl. XXE) | ✅ Covered | Block A: `fuzz/corpus/xml_xxe.json` (6 entries: file://etc/passwd, billion-laughs, parameter-entity OOB, IMDS-via-XXE). | – |
| 7 | Cross-site scripting (XSS) | ✅ Covered | `fuzz/corpus/xss.json` (14 entries incl. OWASP polyglot, `<svg onload>`, javascript: URLs) + reflection-detection on every response. **Note:** classifier has a 6-char floor to avoid false positives — flagged in earlier fuzz review. | – |
| 8 | Server-Side Template Injection (SSTI) | ✅ Covered | Block A: `fuzz/corpus/ssti.json` (7 entries: `{{7*7}}`, `${{7*7}}`, `<%= 7*7 %>`, SpEL RCE, Twig dump). Evaluated-form detection (literal `49` in response) is documented as future enhancement. | – |
| 9 | Header / CRLF injection | ✅ Covered | `fuzz/corpus/header_injection.json` (5 entries including CRLF + Set-Cookie smuggling). Probes every route. | – |
| 10 | Log injection | ✅ Covered | Block A: `fuzz/corpus/log_injection.json` (5 entries: CR/LF, ANSI ESC[2K erase-line, NUL truncation, ANSI color codes). API-layer reflection check only; CloudWatch verification deferred to Block G. | – |

**Section totals:** 10 covered · 0 partial · 0 missing  *(post-Block-A)*

---

## 2. Authentication & Access Control

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 11 | Insecure Direct Object Reference (IDOR) | 🟡 Partial | Auth layer's cross-persona probes are an IDOR-style test for class-level access. Individual record IDOR (one user reading another user's conversation by guessing session_id) is not probed. | Medium (~half day) — add `auth/test_idor.py` that fetches one persona's session_ids, attempts to read them as another persona |
| 12 | Privilege escalation | ✅ Covered | `auth/test_forged_groups.py` + `auth/test_cross_persona.py`. Found CRITICAL today (CISO claim forgery is accepted). | – |
| 13 | Forced browsing | 🟡 Partial | E2E negative-gating covers UI-level forced browsing. Backend-level (unauthenticated GET on `/admin`, `/internal`, etc.) is not covered. | Small (~1 hr) — add `auth/test_forced_browsing.py` with a dictionary of common admin paths |
| 14 | Missing authorization on APIs | ✅ Covered | Auth layer's negative tests confirmed today that 9 CISO-only routes accept SOC/GRC/Employee tokens (HIGH finding). | – |
| 15 | Server-Side Request Forgery (SSRF) | ✅ Covered | Block A: `fuzz/corpus/ssrf.json` (7 entries: IMDS, loopback, IPv6 loopback, RFC1918, gopher://, file://). Probes every URL-shaped field across all routes. | – |
| 16 | Path traversal | ✅ Covered | `fuzz/corpus/path_traversal.json` (6 entries with `..`, `%2e%2e`, etc.) on every route including those with path params. | – |
| 17 | Credential stuffing / brute force | ❌ Missing | No timed sign-in attempt loop against Cognito. Cognito has built-in throttling but we don't verify the threshold. | Small (~1 hr) — add `auth/test_brute_force.py` that fires N rapid bad-password attempts and asserts throttling after K |
| 18 | Weak password storage | ⚪ Out of scope | Cognito handles password storage server-side with SRP. Not directly testable from a black-box harness. Can verify pool config via `aws cognito-idp describe-user-pool` and assert `PasswordPolicy.MinimumLength >= 12`. | Small (~30 min) — add as a config-check probe in `auth/test_pool_config.py` |
| 19 | Session fixation / hijacking | ❌ Missing | No probe that swaps a session_id across personas mid-conversation, or that reuses a stale session_id after logout. | Medium (~half day) |
| 20 | JWT vulnerabilities | ✅ Covered | `auth/test_chat_no_signature.py` (alg-none-equivalent), `auth/test_forged_groups.py` (signature manipulation), `auth/test_expired_token.py` (exp validation), `auth/test_token_replay.py` (access-token-instead-of-id). Found CRITICAL + HIGH today. | – |
| 21 | Insecure password reset | ❌ Missing | No probe of the Cognito forgot-password flow. Race condition where the reset code is guessable, or email enumeration via `ForgotPassword` returning different errors for unknown vs. known users. | Medium (~half day) — `auth/test_password_reset.py` with rate-limit + enumeration checks |
| 22 | Missing or bypassable MFA | ⚪ Out of scope | Per CLAUDE.md: "MFA off" in this demo. Documented intentional gap. If MFA is enabled later, add a probe that attempts to skip the SMS_MFA challenge. | – |

**Section totals:** 5 covered · 3 partial · 2 missing · 2 out-of-scope  *(post-Block-A)*

---

## 3. Crypto & Data Handling

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 23 | Plaintext transmission of sensitive data | ✅ Covered | Block B: `headers/test_https_only.py` — for every public host (CloudFront SPA, API, Lambda Function URL), GET on `http://` must redirect to `https://` (FAIL HIGH on a 200 or http-to-http redirect) and GET on `https://` must set HSTS with `max-age >= 1 year` (FAIL MEDIUM if missing). | – |
| 24 | Weak / outdated cryptographic algorithms | ✅ Covered | Block B: `headers/test_tls_ciphers.py` — stdlib `ssl` + `socket` only (no `nmap` / `testssl.sh` subprocess). Forces TLS 1.0 / 1.1 handshakes against CloudFront (FAIL HIGH if either succeeds), then negotiates TLS 1.2 and verifies the cipher name is not in the weak-token list (RC4 / 3DES / NULL / EXPORT / anonymous DH / MD5-based). | – |
| 25 | Hardcoded or poorly managed keys | 🟡 Partial | The harness itself has a `_test_safety_invariants` that grep-scans for AWS account IDs / forbidden boto3 clients. Doesn't scan the deployed bundle for inlined keys. | Small (~1 hr) — add `e2e/test_bundle_secrets_scan.spec.js` that fetches `/assets/*.js` and regex-scans for `AKIA`, `eyJ`, `xoxb-`, etc. |
| 26 | Weak randomness | ❌ Missing | No probe that watches for predictable session_ids or short-cycle UUIDs. ARBITER uses `crypto.randomUUID()` which is fine, but a regression to `Math.random()` wouldn't be caught. | Small (~1 hr) — collect 100 session_ids and check Shannon entropy |
| 27 | Improper certificate validation | ⚪ Out of scope | This applies to client-side code. The deployed SPA uses the browser's cert validation; ARBITER doesn't ship a desktop/mobile client. | – |
| 28 | Unnecessary retention of sensitive data | ❌ Missing | No probe of DDB TTL / retention policies. Spec'd in `04-storage.yaml` but not enforced by harness. | Small-medium (~2 hr) — `tls/test_data_retention.py` querying DDB for items past TTL or session table for old rows |

**Section totals:** 2 covered · 0 partial · 3 missing · 1 out-of-scope  *(post-Block-B)*

---

## 4. Configuration & Infrastructure

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 29 | Default credentials | ✅ Covered | Block A: `auth/test_default_creds.py` (5 pairs: admin/admin, admin/password, test/test, arbiter/arbiter, demo/demo123). PASS = Cognito returns NotAuthorizedException; FAIL severity HIGH if any unexpectedly authenticates. | – |
| 30 | Verbose errors / stack trace leakage | ✅ Covered | Fuzz layer's stack-trace marker check catches `Traceback`, `boto3`, `ClientError`, `aws_request_id` in any response body. **Note:** earlier review flagged that `"boto3"` is too broad — fixed already. | – |
| 31 | Missing security headers | ✅ Covered | Block B: `headers/test_security_headers.py` (parameterized across SPA root + 3 API routes). Per-header rows: `headers.csp.*` (FAIL MEDIUM if missing, LOW if `unsafe-eval` / `unsafe-inline` / `data:` in script-src), `headers.xfo.*` (clickjacking — FAIL MEDIUM if neither XFO nor CSP `frame-ancestors` restricts framing), `headers.xcto.*` (FAIL LOW if missing / wrong value), `headers.referrer.*` (FAIL LOW if missing or `unsafe-url`). HSTS covered separately by `test_https_only.py`. | – |
| 32 | Open cloud storage / over-permissive IAM | 🟡 Partial | Manifest-driven static check covers IAM in templates indirectly. No probe attempts unauthenticated GET on S3 bucket URLs or non-CISO access to KMS keys. | Medium (~half day) |
| 33 | Exposed admin consoles or sensitive directories | ❌ Missing | No probe of common admin paths (`/admin`, `/console`, `/.git`, `/.env`, `/swagger`, `/api-docs`). | Small (~30 min) — add to forced-browsing dictionary above |
| 34 | Directory listing / debug mode in production | ❌ Missing | No probe of CloudFront for directory indices, or of API for debug endpoints (`/debug`, `?debug=1`). | Small (~30 min) |
| 35 | Misconfigured CORS | ✅ Covered | Block B: `headers/test_cors.py` (route × attacker_origin parametrize). Sends OPTIONS preflights with `Origin: https://evil.com`, `Origin: null`, and `Origin: file://` against 5 representative API routes (15 cells). FAIL HIGH if `Access-Control-Allow-Origin: *` + `Allow-Credentials: true`, or if ACAO echoes the attacker origin back. PASS on no ACAO / fixed-allowed-origin / wildcard-without-credentials. | – |
| 36 | Vulnerable / outdated dependencies | ⚪ Out of scope | GitHub Dependabot is on (we saw the warning on push: 1 critical, 5 moderate). Pin SCA there. The harness doesn't replicate this. | Note in report |
| 37 | Dependency confusion | ⚪ Out of scope | Best handled in build pipeline + private registry config, not runtime probe. | – |
| 38 | Typosquatting | ⚪ Out of scope | Static analysis of `package.json` / `requirements.txt`. Tools like Socket.dev. | – |

**Section totals:** 4 covered · 1 partial · 2 missing · 3 out-of-scope  *(post-Block-B)*

---

## 5. Build / Supply Chain

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 39 | Compromised build pipelines | ⚪ Out of scope | Requires CI hardening (signed commits, OIDC role assumption, branch protection). Not testable from a black-box runtime harness. | – |
| 40 | Malicious maintainer / package takeover | ⚪ Out of scope | SCA + signed releases. | – |
| 41 | Compromised third-party JS / Magecart skimming | 🟡 Partial | Bundle-scan probe (planned for item #25) would catch unexpected script sources. Subresource Integrity (SRI) inspection separate. | Small (~1 hr) — add SRI check to bundle scan |
| 42 | Sensitive data in client storage / source maps / comments | ❌ Missing | No probe fetches `/assets/*.js.map` or scans comments in deployed HTML. | Small (~1 hr) |

**Section totals:** 0 covered · 1 partial · 1 missing · 2 out-of-scope

---

## 6. Errors & State

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 43 | Fail-open logic | ❌ Missing | No probe that breaks an auth check mid-flow and asserts the request fails closed (e.g. AgentCore runtime returns error → should the chat still complete?). | Medium (~half day) — would need fault-injection capability |
| 44 | Unhandled exceptions | 🟡 Partial | Fuzz layer's "no 500s" assertion catches some unhandled exceptions. Doesn't catch swallowed ones. | – |
| 45 | Swallowed errors hiding attacks | ❌ Missing | No probe that triggers a known error and verifies a CloudWatch log line + audit-trail entry. | Medium (~half day) |
| 46 | Race conditions / TOCTOU | ❌ Missing | No probe that fires concurrent requests on the same resource (e.g. two simultaneous approvals of the same change request). | Medium (~1 day) — needs concurrent-request infrastructure in the harness |
| 47 | Inconsistent state after partial failure | ❌ Missing | No probe that, e.g., uploads a file but kills the connection mid-stream and asserts the DDB row is rolled back. | Medium-large |

**Section totals:** 0 covered · 1 partial · 4 missing

---

## 7. API Top-10 Specific

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 48 | Broken Object-Level Authorization (BOLA) | 🟡 Partial | Same as IDOR (#11). Cross-persona tests cover class-level; per-object IDOR not covered. | See #11 |
| 49 | Mass assignment | 🟡 Partial | Block A: `fuzz/corpus/mass_assignment.json` (6 entries: is_admin, persona, cognito:groups, role, user_id, approved) injected as the primary writable field's value on every route. Multi-key body extension (extra top-level keys in the same JSON body) is documented future enhancement — see notes/mass-assignment-extra-keys.md. | Small (~1 hr) — extend test_api_routes.py to inject the corpus as top-level keys instead of values |
| 50 | Excessive data exposure | 🟡 Partial | Stack-trace check catches obvious leaks. No probe that walks each endpoint and asserts the response shape doesn't include `password_hash`, `secret`, `email` to non-owner. | Small-medium (~2 hr) — `fuzz/test_field_exposure.py` |
| 51 | Lack of rate limiting / resource consumption | ❌ Missing | No probe that fires N requests/sec at any endpoint and asserts a 429 response after K. AgentCore runtime + APIGW have implicit limits we never verify. | Small (~1 hr) — `dos/test_rate_limit.py` |
| 52 | Broken function-level authorization (BFLA) | ✅ Covered | Same as #14 — auth layer's per-route persona tests cover BFLA. Confirmed broken today. | – |
| 53 | Unsafe consumption of third-party APIs | ❌ Missing | The jira_specialist and zscaler integrations consume external APIs. No probe that verifies the responses are validated before being passed to the model. | Medium (~half day) — requires fault injection on the upstream |
| 54 | GraphQL-specific abuse | ⚪ Out of scope | ARBITER doesn't expose GraphQL. | – |
| 55 | Cross-Site Request Forgery (CSRF) | ✅ Covered | Block B: `headers/test_csrf.py`. For every POST / PUT / PATCH / DELETE route in the manifest, fire the request with NO `Authorization:` header but WITH a fake `Cookie: arbiter.tokens=...`. Expected: 401 / 403. FAIL HIGH if the API returns 2xx — that would mean a cookie-based auth fallback exists, exposing the surface to classic CSRF. | – |

**Section totals:** 2 covered · 3 partial · 2 missing · 1 out-of-scope  *(post-Block-B)*

---

## 8. Client-Side

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 56 | Clickjacking | ✅ Covered | Block B (two-pronged): (a) `headers/test_clickjacking.py::test_iframe_embed_blocked_via_headers` — header-side check that XFO DENY/SAMEORIGIN or CSP `frame-ancestors` restricts framing (FAIL MEDIUM if neither); (b) `e2e/tests/clickjacking.spec.js` — real-browser Playwright spec that wraps the SPA URL in an `<iframe>` and asserts the dashboard does not render inside it. | – |
| 57 | Open redirects | ✅ Covered | Block A: `fuzz/corpus/open_redirects.json` (7 entries: bare host, protocol-relative, full https, UNC, mixed slash, subdomain confusion, `@` userinfo bypass) + dedicated probe `fuzz/test_open_redirects.py` against the Cognito Hosted UI `/login?redirect_uri=` with Location-header inspection. ARBITER's own API has no redirect routes — the Hosted UI is the exposure surface. | – |
| 58 | Prototype pollution | ✅ Covered | Block A: `fuzz/corpus/prototype_pollution.json` (5 entries: `__proto__.isAdmin`, `constructor.prototype`, `__proto__.toString`, nested, array-typed). Lambda runtime is Python — these are regression detectors for a future Node Lambda. | – |
| 59 | Tabnabbing | ❌ Missing | No probe that confirms outbound links have `rel="noopener noreferrer"`. | Small (~30 min) — Playwright spec |
| 60 | Sensitive data in client storage / source maps / comments | ❌ Missing | See #42. | – |

**Section totals:** 3 covered · 0 partial · 2 missing  *(post-Block-B)*

---

## 9. Logic & Workflow

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 61 | Workflow bypass | ❌ Missing | No probe that, e.g., approves a change request without first executing it, or jumps stages in the action lifecycle. ARBITER's `/actions/{id}/approve`, `/execute`, `/reject`, `/escalate` have a state machine; the harness doesn't probe state-skip. | Medium (~half day) |
| 62 | Price / quantity manipulation | ⚪ Out of scope | Not applicable to a compliance dashboard. | – |
| 63 | Abuse of trial, referral, or exempt flows | ⚪ Out of scope | Not applicable. | – |

**Section totals:** 0 covered · 0 partial · 1 missing · 2 out-of-scope

---

## 10. Denial of Service

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 64 | Application-layer DoS | 🟡 Partial | LLM cost-DoS probe (`llm.cost-dos.long-completion`) is the canonical AC17 case. Doesn't probe API-layer DoS (large bodies, recursive payloads). | Small (~1 hr) — add as a separate test (oversized payload is already in fuzz corpus; verify response time degradation) |
| 65 | Resource exhaustion | 🟡 Partial | Cost-DoS probe is one case. Memory/file-descriptor exhaustion in the Lambda runtime is not probed. | Medium |
| 66 | Network-layer DDoS | ⚪ Out of scope | Would actually attack the dev env. CloudFront has AWS Shield Standard by default; verification is via AWS-side metrics. | – |

**Section totals:** 0 covered · 2 partial · 0 missing · 1 out-of-scope

---

## 11. Logging & Monitoring

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 67 | Insufficient logging of security events | ❌ Missing | No probe that triggers a forged-token attempt and asserts a CloudWatch log entry appears in `audit-log` table. | Medium (~half day) — needs DDB read fixture |
| 68 | Logging sensitive data | ❌ Missing | No probe that triggers a known sensitive value (a JWT, a password) and grep-scans CloudWatch for accidental logging. | Medium (~half day) |
| 69 | No alerting / monitoring | ⚪ Out of scope | CloudWatch Alarms / EventBridge rule audit. Separate from runtime probe. | – |
| 70 | Mutable or attacker-deletable logs | ⚪ Out of scope | CloudWatch immutability / S3 Object Lock configuration audit. | – |
| 71 | Log injection | 🟡 Partial | API-layer reflection check covered by Block A (see #10). CloudWatch-side verification (did the payload land in logs unsanitized?) deferred to Block G. | – |
| 72 | No incident response plan | ⚪ Out of scope | Process / runbook, not a runtime test. | – |

**Section totals:** 0 covered · 1 partial · 2 missing · 3 out-of-scope  *(post-Block-A)*

---

## 12. LLM Top-10

| # | Item | Status | Where / Gap | Effort to close |
|---|---|---|---|---|
| 73 | Prompt injection | ✅ Covered | LLM red-team: 20 curated jailbreaks + 10 generative transformations. Found today that 26 of 30 were correctly refused (Bedrock Guardrails) but classifier missed the phrasing (harness fix planned). | – |
| 74 | Insecure output handling | ❌ Missing | No probe that checks whether the chat response, rendered in the SPA, executes JavaScript (i.e. if the model is coerced to output `<script>` and the SPA renders it unescaped). | Medium (~half day) — Playwright spec that asks the model for an XSS payload and asserts the rendered DOM has it text-encoded |
| 75 | Training-data poisoning | ⚪ Out of scope | Requires access to Bedrock model training pipeline. Not testable for a hosted model. | – |
| 76 | Sensitive information disclosure | ✅ Covered | `exfil.list-other-ciso-conversations`, `exfil.cross-tenant-sharepoint`, `exfil.knowledge-base-dump` probes. Reinforced by jira black-box check (AC19) for 12-digit AWS account IDs in responses. | – |
| 77 | Excessive agency | ✅ Covered | `tool-abuse.sharepoint-path-traversal`, `tool-abuse.awsconfig-cross-account`, `tool-abuse.zscaler-crafted-url` probes. | – |
| 78 | Model / plugin supply-chain risks | ⚪ Out of scope | Bedrock model provenance + AgentCore container audit. Separate AWS-side audit. | – |

**Section totals:** 4 covered · 0 partial · 1 missing · 2 out-of-scope

---

# Summary

| Category | Covered | Partial | Missing | Out of scope | Total |
|---|---:|---:|---:|---:|---:|
| 1. Injection | 10 | 0 | 0 | 0 | 10 |
| 2. AuthN / AuthZ | 5 | 3 | 2 | 2 | 12 |
| 3. Crypto & data | 2 | 0 | 3 | 1 | 6 |
| 4. Config / infra | 4 | 1 | 2 | 3 | 10 |
| 5. Build / supply chain | 0 | 1 | 1 | 2 | 4 |
| 6. Errors & state | 0 | 1 | 4 | 0 | 5 |
| 7. API Top-10 | 2 | 3 | 2 | 1 | 8 |
| 8. Client-side | 3 | 0 | 2 | 0 | 5 |
| 9. Logic / workflow | 0 | 0 | 1 | 2 | 3 |
| 10. DoS | 0 | 2 | 0 | 1 | 3 |
| 11. Logging / monitoring | 0 | 1 | 2 | 3 | 6 |
| 12. LLM Top-10 | 4 | 0 | 1 | 2 | 7 |
| **Totals** | **30** | **12** | **20** | **17** | **79** |

**Coverage post-Block-B: 30/79 fully (38%), 42/79 at least partial (53%), 17/79 out of scope (22%).**

Block A delta: +10 fully covered, +1 partial (mass assignment + log injection re-classified), −11 missing.
Block B delta: +6 fully covered (items 23, 24, 31, 35, 55, 56), −3 partial (items 23, 55 → covered; item 41 stays partial — bundle/SRI scan still scheduled for Block D), −3 missing (items 24, 31, 35 + 56).

The 17 out-of-scope items are real items on the checklist — they just need different tools (SCA, AWS config audit, IR runbook review, CI hardening), not this runtime harness. They should be addressed and recorded as "covered by other controls" rather than ignored.

---

# Build plan to close the gaps

This groups the 34 missing + 14 partial items by effort and dependency. Each block is sized so it can land independently.

## Block A — Tiny additions ✅ Done (2026-06-10)

10 corpus files + 2 test modules + wiring. Landed 2026-06-10. Bumped coverage 18% → 30% (or 35% → 49% counting partials).

| Item | Where | Status |
|---|---|---|
| NoSQL operators | `fuzz/corpus/nosql_operators.json` (7 entries) | ✅ |
| LDAP injection | `fuzz/corpus/ldap.json` (6 entries) | ✅ |
| XPath injection | `fuzz/corpus/xpath.json` (6 entries) | ✅ |
| XML / XXE | `fuzz/corpus/xml_xxe.json` (6 entries — XXE file://, billion-laughs, parameter-entity OOB, XXE-to-SSRF on IMDS, php base64 wrapper, external DOCTYPE) | ✅ |
| SSTI | `fuzz/corpus/ssti.json` (7 entries — Jinja `{{7*7}}`, JSX/SpEL `${{}}`, ERB `<%= %>`, SpEL RCE, Twig dump, etc.) | ✅ |
| Log injection | `fuzz/corpus/log_injection.json` (5 entries — CR/LF, ANSI ESC[2K, NUL truncation, ANSI color codes) | ✅ |
| SSRF | `fuzz/corpus/ssrf.json` (7 entries — IMDS, loopback, IPv6, RFC1918, gopher://, file://) | ✅ |
| Default credentials | `auth/test_default_creds.py` (5 pairs) | ✅ |
| Mass assignment | `fuzz/corpus/mass_assignment.json` (6 entries) | ✅ (partial — single-field substitution only; multi-key body extension is a future enhancement) |
| Prototype pollution | `fuzz/corpus/prototype_pollution.json` (5 entries) | ✅ |
| Open redirects | `fuzz/corpus/open_redirects.json` (7 entries) + `fuzz/test_open_redirects.py` (Cognito Hosted UI probe with Location header inspection) | ✅ |

Wiring changes: `fuzz/test_api_routes.py::_families_for_route` now references all 10 new families, so the cross-product (route × family × payload × persona) grew from 5728 to 11680 parametrize cases (+5952). New unit tests in `tests/test_fuzz_infrastructure.py` and `tests/test_auth_abuse_infrastructure.py` enforce the corpus shape, entry counts, route-enumeration wiring, and default-credential classification rules.

## Block B — New "headers / TLS" mini-layer ✅ Done (2026-06-10)

New top-level `tests-adversarial/headers/` directory + Playwright spec under
`e2e/tests/`. Covers items 23, 24, 31, 35, 55, 56. Landed 2026-06-10.
Coverage moved 30% → 38% (or 49% → 53% counting partials).

| Item | Test | Status |
|---|---|---|
| HTTPS-only + HSTS | `headers/test_https_only.py` | ✅ |
| TLS cipher strength + min version | `headers/test_tls_ciphers.py` (stdlib `ssl` + `socket`, no subprocess) | ✅ |
| Security headers (CSP, XFO, X-Content-Type-Options, Referrer-Policy) | `headers/test_security_headers.py` | ✅ |
| CORS misconfiguration | `headers/test_cors.py` (5 routes × 3 attacker origins) | ✅ |
| CSRF resistance | `headers/test_csrf.py` (cookie-only + no Authorization header against every destructive route) | ✅ |
| Clickjacking — header side | `headers/test_clickjacking.py::test_iframe_embed_blocked_via_headers` | ✅ |
| Clickjacking — browser side | `e2e/tests/clickjacking.spec.js` (Playwright, no-auth project) | ✅ |

Wiring: `tests-adversarial/package.json::scripts.test:headers` plus a new
`"headers"` entry in `scripts/run_all.py::_LAYERS_ALL` and a zero-budget
`LayerBudget` (the layer makes no Bedrock calls). `src/coverage/builder.py`
already accepts arbitrary `layer` strings — a regression-pinning unit test
in `tests/test_headers_infrastructure.py::test_builder_accepts_headers_layer`
guards against an accidental allow-list. Manifest also gained the
`Integrations` page, the `/agent-status` API route, and the
`master.paloalto_lookup` + `master.jira_lookup` tools (drift checker is
green: 16 pages / 26 routes / 14 tools).

## Block C — Auth gaps (~1 day)

Targeted additions to the existing auth layer.

| Item | Test |
|---|---|
| IDOR / BOLA | `auth/test_idor.py` (per-object cross-persona reads) |
| Forced browsing | `auth/test_forced_browsing.py` (admin path dictionary) |
| Credential stuffing | `auth/test_brute_force.py` |
| Session fixation | `auth/test_session_swap.py` |
| Insecure password reset | `auth/test_password_reset.py` |
| Pool config | `auth/test_pool_config.py` (verifies MinimumLength, RequireUppercase, etc.) |

## Block D — Bundle / client-side scan (~half day)

| Item | Test |
|---|---|
| Hardcoded keys in bundle | `e2e/test_bundle_secrets.spec.js` (regex scan `/assets/*.js`) |
| Source maps in production | same spec, checks for `*.js.map` 200s |
| Comments containing secrets | same spec |
| Subresource Integrity | same spec |
| Tabnabbing | Playwright check on external links |

## Block E — DoS / rate limiting (~1 day)

| Item | Test |
|---|---|
| Rate limiting | `dos/test_rate_limit.py` (burst test, asserts 429) |
| Application-layer DoS | `dos/test_payload_oversize.py` (extends fuzz with timing-degradation check) |
| Resource exhaustion | already partly covered by cost-DoS; expand to memory pressure if needed |

## Block F — Logic / state (~1–2 days)

| Item | Test |
|---|---|
| Workflow bypass | `logic/test_action_state_machine.py` (try approve-without-execute, escalate-after-execute, etc.) |
| Race conditions | `logic/test_race_conditions.py` (concurrent approve on same CR) |
| Inconsistent state after partial failure | requires fault-injection primitives — defer to block H |
| Fail-open logic | requires fault injection — defer to block H |
| Excessive data exposure | `fuzz/test_field_exposure.py` walks every endpoint and validates response schema for known sensitive fields |

## Block G — Logging audit (~half day)

| Item | Test |
|---|---|
| Security-event logging | `logging/test_audit_log.py` (trigger a forged-token attempt, read DDB audit-log, assert entry exists) |
| Sensitive data in logs | `logging/test_log_redaction.py` (trigger a known sensitive value, grep CloudWatch via boto3) |

## Block H — Fault injection (~1+ day, larger)

Most ambitious. Requires extending the harness to selectively break upstream services.

| Item | Test |
|---|---|
| Fail-open | Lambda extension or boto3-side mock to fail an AgentCore call mid-flow and verify the response fails closed |
| Swallowed errors | trigger error, assert CloudWatch logged the right severity |
| Unsafe consumption of third-party APIs | inject a malformed response from a specialist into the master chain |
| LLM insecure output handling | force the model to emit an XSS payload, assert the SPA renders it text-encoded |

## Block I — Coverage report regeneration (~30 min after each block)

After every block lands, regenerate this matrix doc and the daily PDF so the team can see the percentage moving.

---

# What this is NOT going to cover

These 17 items belong in different tools or processes. Worth listing them out and noting where they're addressed so an auditor reading the compliance package sees the full picture.

| Item | Where it's actually covered |
|---|---|
| Insecure cert validation (client) | n/a — ARBITER has no thick client |
| Vulnerable / outdated deps | GitHub Dependabot (currently 6 open: 1 critical, 5 moderate) |
| Dependency confusion | Private registry config in CI |
| Typosquatting | Same |
| Compromised build pipelines | CI hardening, OIDC role assumption |
| Malicious maintainer / takeover | SCA tool + signed releases |
| GraphQL abuse | n/a — no GraphQL surface |
| Network DDoS | CloudFront + AWS Shield Standard |
| No alerting / monitoring | CloudWatch Alarms + EventBridge audit |
| Mutable / deletable logs | CloudWatch immutability + S3 Object Lock |
| No incident response plan | Process / runbook review |
| Training-data poisoning | Bedrock provider responsibility (AWS) |
| Model / plugin supply chain | Bedrock model provenance + AgentCore container scan |
| MFA bypass | Demo has MFA off intentionally — add when enabled |
| Improper cert validation (server) | CloudFront / ACM responsibility |
| Price / trial / referral abuse | n/a — not an e-commerce surface |

---

# Recommended sequencing

If we run the same daily harness cadence and each "Block" above is one focused build session, the rough timeline is:

- **Day 1:** Block A + Block B → 50%+ coverage
- **Day 2:** Block C → ~65% coverage
- **Day 3:** Block D + start Block E → ~75%
- **Day 4:** Finish Block E + start F → ~83%
- **Day 5:** Finish F + Block G → ~92%
- **Day 6+:** Block H (fault injection) brings us to ~95% — the last 5% is genuinely out of scope and gets documented as such.

After every block, the compliance matrix in this document gets updated and the daily PDF report shows the new percentages.

*Last updated: 2026-06-10 (post-Block-B). Refresh this doc after every block lands.*
