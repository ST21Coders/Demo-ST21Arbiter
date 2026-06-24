# Spec — Audit log for security-relevant events

**Status:** Draft (spec only — no implementation yet)
**Owner:** API (`Infra/functions/api_handler/`) + Infra (`Infra/templates/02-security.yaml`, `Infra/templates/05-compute.yaml`) + new Lambda source (`Infra/functions/audit_cognito_subscriber/`)
**Version target:** bump `APP_VERSION` in [`ui/src/config.js`](../ui/src/config.js) on ship, per project convention.

Source: [`docs/research/audit_log_security_events.md`](../docs/research/audit_log_security_events.md). The research brief enumerates the four failing harness scenarios, the existing `_audit(...)` writer, why three of the scenarios short-circuit before reaching an audit call, and the Cognito CloudTrail latency window. This spec does not repeat that material — it builds on it.

---

## 1. Summary

The adversarial harness reports zero new rows in `<env>-<project>-audit-log` after four security-relevant scenarios fire: a burst of failed Cognito sign-ins, a SOC token used against a CISO-only route, a forged `cognito:groups` token used against the same route, and a legitimate CISO approve. The "we audit everything" claim on the Audit Logs page lands badly when none of those events leave a trace. This spec closes three of the four gaps and verifies the fourth: emit a `CROSS_PERSONA` audit row when `_require_ciso` short-circuits at the API layer (covers both the cross-persona and forged-token scenarios), build a CloudTrail → EventBridge → subscriber Lambda pipe that writes an `AUTH_FAILED` row per failed Cognito sign-in, and confirm during testing that the legitimate CISO approve continues to write its existing `CR_APPROVED` row.

What is out of scope: this spec does not add JWT signature verification, does not add a GSI to the audit-log table, does not change `AuditLogs.jsx`, and does not change the harness's timing windows (the harness team owns its own probe windows). The unified `action_type` enum gains exactly two new strings — `AUTH_FAILED` and `CROSS_PERSONA` — and the existing `CR_APPROVED` stays as-is. New rows expire after 90 days via the table's existing TTL attribute.

## 2. Goals and non-goals

### Goals

- Every cross-persona request (SOC / GRC / employee token against a CISO-only route) produces one `CROSS_PERSONA` audit row, written best-effort from inside the API handler before the 403 returns.
- Every forged-`cognito:groups` request against a CISO-only route produces the same `CROSS_PERSONA` row — folded into the same code path, no separate `FORGED_TOKEN` event type.
- Every failed Cognito sign-in observed by CloudTrail produces one `AUTH_FAILED` audit row, written by a new subscriber Lambda fed from an EventBridge rule.
- Every new audit row sets `ttl = epoch_now + 7_776_000` (90 days), consistent with the `<env>-<project>-token-usage` table's TTL discipline.
- The existing `CR_APPROVED` audit row continues to fire on a successful CISO approve. The harness is the source of truth for that — testing verifies it, no code change.
- Audit writes are best-effort everywhere: an exception in the write path never breaks the underlying response (401/403, chat reply, Cognito sign-in result).
- IAM for the new subscriber Lambda is narrowly scoped — `dynamodb:PutItem` on the audit-log table ARN only, `kms:GenerateDataKey` on the DynamoDB CMK ARN only, no wildcards beyond what the AWS-managed basic-execution policy already provides.

### Non-goals

- **Not adding JWT signature verification.** The Function URL path's deliberate no-signature-verify shortcut at `Infra/functions/api_handler/api_handler.py:1995` stays as it is. Forged-claim tokens are still indistinguishable from real ones at the API layer; we just record the resulting 403 the same way we record any cross-persona attempt. JWT verification is a separate decision the user has already deferred.
- **Not adding a GSI to the audit-log table.** `Infra/templates/04-storage.yaml` is not edited. The Audit Logs page continues to use its existing `Scan(Limit=200)` read path. If rollup queries (top auth failures today) become a page requirement later, a GSI is its own spec.
- **Not changing `ui/src/pages/AuditLogs.jsx`.** The page already renders unknown `action_type` values with the literal string in the type column (fallback slate text from `ACTION_COLORS` at line 7). New types are safe to introduce without an SPA edit.
- **Not changing the harness's 5-second probe window.** The CloudTrail-mediated `AUTH_FAILED` path has a 2–15 minute typical latency. The harness team will widen the brute-force probe window separately; this spec does not own that change.
- **Not enabling Cognito Threat Protection / Advanced Security Features.** Paid feature, posture change, not needed when the CloudTrail path is acceptable for the demo's purpose.
- **Not implementing real-time alerting on `AUTH_FAILED` bursts.** No CloudWatch Alarm, no SNS topic. The rows land in the table; that is the deliverable.
- **Not de-duplicating audit writes.** A real brute-force burst of N attempts produces N rows. Acceptable at demo scale.

## 3. Personas and use cases

Three audiences. Two human, one machine.

**CISO and GRC personas (read the Audit Logs page).** They land on `/audit` and see security events alongside the existing business events. They filter by substring against `action_type` / `resource` / `user` / `details` (the page's existing text filter, no schema change), see `CROSS_PERSONA` rows when someone attempts a CISO-only route with the wrong token, and see `AUTH_FAILED` rows after a failed Cognito sign-in surfaces (within the documented latency window). They never need to know the difference between an in-line API write and an out-of-band CloudTrail-fed write.

**Compliance auditors (operate outside the SPA).** Read the table via DDB scan or a CloudWatch query when the SPA isn't enough. They get the same uniform row shape the existing `_audit(...)` writes today (`event_id`, `timestamp`, `action_type`, `resource`, `user`, `status`, `details`), which keeps existing CloudWatch Insights queries working.

**The adversarial harness (machine).** Triggers each scenario and checks that a row with the expected needle appears in the audit-log table within its probe window. The two API-layer scenarios land within seconds; the brute-force scenario lands within the CloudTrail latency window (the harness owns widening that probe).

## 4. Functional scope

### 4a. `CROSS_PERSONA` write — API handler, in-line

A new write happens inside `_require_ciso` at `Infra/functions/api_handler/api_handler.py:542-552`. The current function returns `_err(403, "Token Tracking is restricted to the CISO persona")` directly; the new behavior calls `_audit(...)` first and then returns the same 403 response. Both call sites of `_require_ciso` benefit without per-route changes:

- `_handle_list_token_usage` at line 767 (`GET /token-usage`)
- `_handle_token_usage_summary` at line 783 (`GET /token-usage/summary`)

Any future CISO-only route that adopts the same helper inherits the write for free.

**Row shape** (matches the existing `_audit(...)` writer at `api_handler.py:1723`):

| Field | Value |
|---|---|
| `event_id` | `cross_persona-<resource>-<microsec timestamp>` (the existing helper's format) |
| `timestamp` | UTC ISO8601 |
| `action_type` | `CROSS_PERSONA` |
| `resource` | the request path (e.g. `/token-usage`) |
| `user` | the caller's email (from `claims.get("email")`) or `cognito:username` as fallback |
| `status` | `DENIED` |
| `details` | JSON string with `{ "path", "method", "required_group": "ciso", "caller_groups": [...], "caller_sub": "<sub>", "source_ip": "<ip>" }` |
| `ttl` | `epoch_now + 7_776_000` (90 days) |

`source_ip` resolves from `event["requestContext"]["http"]["sourceIp"]` (Function URL path) or `event["requestContext"]["identity"]["sourceIp"]` (API Gateway path), falling back to `"unknown"` when neither is present.

**Best-effort semantics.** The write is wrapped in try/except inside the existing `_audit(...)` writer, which already catches and logs every exception without raising. `_require_ciso` does not add its own try/except — it relies on the writer's contract. The 403 response is returned regardless of write success or failure. This matches the project convention from `CLAUDE.md` ("side-effect writes are best-effort and must never break the chat").

**Forged-token folding.** The forged-`cognito:groups` scenario hits the same `_require_ciso` code path. The harness's CISO IdToken has been edited to set `cognito:groups` to a canary value, but the rest of the claims (`sub`, `email`) come through untouched. The resulting 403 produces a `CROSS_PERSONA` row whose `details.caller_groups` contains the forged canary value — that is enough for the harness's needle match and enough for an auditor to spot the anomaly. We do not invent a separate `FORGED_TOKEN` row because, without JWT signature verification, we cannot distinguish a forged token from a real one.

### 4b. `AUTH_FAILED` writes — Cognito subscriber Lambda

A new Lambda subscribes to Cognito user-pool API calls that CloudTrail logs as management events and writes one audit row per failed sign-in.

**Components:**

| Component | Identifier |
|---|---|
| Lambda function name | `dev-st21arbiter-poc-audit-cognito-subscriber` |
| Lambda source dir | `Infra/functions/audit_cognito_subscriber/` |
| Lambda runtime | Python 3.13 (matches the rest of the project) |
| EventBridge rule name | `dev-st21arbiter-poc-cognito-auth-failed` |
| IAM role | new role declared in `02-security.yaml` |
| CFN home | `Infra/templates/05-compute.yaml` |

**EventBridge rule pattern** (matches the Cognito CloudTrail event shape from the research brief):

```json
{
  "source": ["aws.cognito-idp"],
  "detail-type": ["AWS API Call via CloudTrail"],
  "detail": {
    "eventSource": ["cognito-idp.amazonaws.com"],
    "eventName": ["InitiateAuth", "AdminInitiateAuth", "RespondToAuthChallenge"],
    "errorCode": [
      "NotAuthorizedException",
      "UserNotFoundException",
      "TooManyRequestsException",
      "PasswordResetRequiredException"
    ]
  }
}
```

**Row shape:**

| Field | Value |
|---|---|
| `event_id` | `auth_failed-<username-or-unknown>-<microsec timestamp>` |
| `timestamp` | UTC ISO8601 of the CloudTrail `eventTime` (preserves the original event time, not wall-clock at write time) |
| `action_type` | `AUTH_FAILED` |
| `resource` | the username attempted, or `unknown` when CloudTrail did not log one |
| `user` | the same username (mirrors `_audit(...)` convention where `user` and `resource` often align for identity-scoped events) |
| `status` | the CloudTrail `errorCode` (e.g. `NotAuthorizedException`) |
| `details` | JSON string with `{ "source_ip", "user_agent", "event_name", "error_message", "aws_region", "user_pool_id" }` |
| `ttl` | `epoch_now + 7_776_000` (90 days) |

`source_ip` from `detail.sourceIPAddress`. `user_agent` from `detail.userAgent`. `username` from `detail.requestParameters.authParameters.USERNAME` (when present) or `detail.additionalEventData.sub` (when Cognito redacted the username). `user_pool_id` from `detail.requestParameters.userPoolId` when present, else `detail.additionalEventData.userPoolId`.

**Best-effort semantics.** The Lambda catches every exception around the PutItem, logs at WARNING, and returns success to EventBridge regardless. We do not want EventBridge retrying on transient DDB throttles — the audit row is informational, not transactional. The Lambda has no return-value contract beyond "did not 5xx during invoke."

**Environment variables on the subscriber:**

| Variable | Value |
|---|---|
| `AUDIT_LOG_TABLE` | the audit-log table name, imported from `04-storage.yaml`'s existing export (no schema change there) |
| `AWS_REGION` | hard-pinned `us-east-1` (managed automatically by Lambda) |

No KB id, no AgentCore ARN, no chat URLs — this Lambda has one job.

**IAM role** (in `02-security.yaml`):

- Trust: `lambda.amazonaws.com` only.
- Managed policy: `AWSLambdaBasicExecutionRole` for CloudWatch Logs.
- Inline policy statement 1: `dynamodb:PutItem` on `arn:aws:dynamodb:us-east-1:<account>:table/<env>-<project>-audit-log` only. No `Resource: "*"`.
- Inline policy statement 2: `kms:GenerateDataKey` and `kms:Decrypt` on the DynamoDB CMK ARN only (`ImportValue: <env>-<project>-DynamoDBKeyArn`). No `Resource: "*"`. Without `GenerateDataKey` on the CMK, `PutItem` against the CMK-encrypted table silently fails — the same gotcha documented in `CLAUDE.local.md` for the AgentCore role.

### 4c. `CR_APPROVED` verification — no code change

The existing `_handle_action_transition` writer at `Infra/functions/api_handler/api_handler.py:1933` already calls `_audit("CR_APPROVED", cr_id, user_id, new_status, {...})` on a successful approve. The research brief confirms this. The harness's failure for this scenario is not an audit gap — it is most likely the harness picking a stale `cr_id` (or the live `/actions` list returning `[]` and the test skipping, which the harness reports as a fail).

**Testing approach.** A manual probe during the testing phase: sign in as `ciso_diana@meridianinsurance.com`, open `/actions`, find an in-progress change request, approve it, and then `aws dynamodb scan --table-name dev-st21arbiter-poc-audit-log --filter-expression 'action_type = :t' --expression-attribute-values '{":t":{"S":"CR_APPROVED"}}' --max-items 5 --region us-east-1` to confirm the row landed. No spec-level code change. The acceptance criteria below include this as a non-regression check.

## 5. Data model

The audit-log table's existing schema, copied from the research brief, is unchanged:

- Table name: `<env>-<project>-audit-log`.
- Billing: `PAY_PER_REQUEST`.
- PITR: enabled.
- Encryption: SSE-KMS using the `DynamoDBKey` CMK.
- Key schema: `event_id` (HASH, String) + `timestamp` (RANGE, String).
- TTL: `ttl` attribute, enabled. New writes set it; the existing `_audit(...)` does not (out of scope for this spec).
- GSIs: none. Stays none.

**`action_type` value set after this spec ships** (the existing values plus the two new strings — kept here as a documentation reference, not an enforced enum):

| Value | Source | Status |
|---|---|---|
| `JIRA_TRANSITIONED` | `api_handler.py:1462` | existing |
| `JIRA_COMMENTED` | `api_handler.py:1496` | existing |
| `JIRA_LINKED` | `_audit_jira` at `api_handler.py:1297` | existing |
| `SERVICENOW_IMPACT_ANALYSIS` | `api_handler.py:1563` | existing |
| `WHATIF_RUN` | `api_handler.py:1688` | existing |
| `CR_CREATED` / `CR_APPROVED` / `CR_REJECTED` / `CR_ESCALATED` / `CR_EXECUTED` | `api_handler.py:1803`, `1933` | existing |
| `SCAN_TRIGGERED` / `SCAN_STARTED` / `SCAN_COMPLETED` / `SCAN_FAILED` / `INGESTION_COMPLETE` | scanner Lambda | existing |
| `CROSS_PERSONA` | `_require_ciso` (new) | **new** |
| `AUTH_FAILED` | subscriber Lambda (new) | **new** |

**New fields beyond what `_audit(...)` already writes.** Only `ttl`. The seven existing fields (`event_id`, `timestamp`, `action_type`, `resource`, `user`, `status`, `details`) are unchanged in name and type. `details` remains a stringified JSON blob — auditors and the SPA continue to parse it the same way.

## 6. Configuration and secrets

**Subscriber Lambda environment variables:**

| Variable | Source | Purpose |
|---|---|---|
| `AUDIT_LOG_TABLE` | `Fn::ImportValue: !Sub "${Environment}-${ProjectName}-AuditLogTableName"` | Write target. Already an export from `04-storage.yaml`; no edit needed there. |

**New IAM permissions** (declared in `02-security.yaml`, attached to the new subscriber role):

- `dynamodb:PutItem` on the audit-log table ARN only.
- `kms:GenerateDataKey` and `kms:Decrypt` on the DynamoDB CMK ARN only.
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` via the AWS-managed `AWSLambdaBasicExecutionRole`.

The existing api_handler role is **not** edited. Its wildcard `table/${Environment}-${ProjectName}-*` already covers the audit-log table, and its `KMSUsage` statement already covers the DynamoDB CMK. The new `CROSS_PERSONA` write is just another call site for the existing `_audit(...)` writer.

**EventBridge rule pattern** (repeated here as the canonical reference for the architect):

```json
{
  "source": ["aws.cognito-idp"],
  "detail-type": ["AWS API Call via CloudTrail"],
  "detail": {
    "eventSource": ["cognito-idp.amazonaws.com"],
    "eventName": ["InitiateAuth", "AdminInitiateAuth", "RespondToAuthChallenge"],
    "errorCode": [
      "NotAuthorizedException",
      "UserNotFoundException",
      "TooManyRequestsException",
      "PasswordResetRequiredException"
    ]
  }
}
```

No secrets. No new tokens, no new API keys, no new client IDs. CloudTrail management events are on by default in every AWS account; no CloudTrail configuration change is required.

## 7. Acceptance criteria

Numbered so the tester subagent can check each individually. A criterion that cannot be tested mechanically is rewritten until it can.

1. **AC1 — Cross-persona row appears.** A SOC IdToken used against `GET /token-usage` results in one new row in `<env>-<project>-audit-log` within 2 seconds. The row has `action_type = "CROSS_PERSONA"`, `status = "DENIED"`, `resource = "/token-usage"`, `user` equal to the SOC user's email (or `cognito:username` fallback), and `details.caller_groups` containing the SOC user's actual groups. Verified by `aws dynamodb scan` filtered on `action_type = CROSS_PERSONA`.

2. **AC2 — Forged-token row matches the same pattern.** A request with a CISO-real IdToken whose `cognito:groups` claim has been rewritten to a canary value (e.g. `harness-<hex>`) used against `GET /token-usage` results in a row with `action_type = "CROSS_PERSONA"` (not `FORGED_TOKEN`), `status = "DENIED"`, and `details.caller_groups` containing the canary value. No separate `FORGED_TOKEN` action_type is introduced.

3. **AC3 — Brute-force rows appear within the CloudTrail latency window.** Six rapid failed Cognito sign-in attempts (wrong password) produce one or more rows with `action_type = "AUTH_FAILED"` in the audit-log table within **5 minutes** of the last attempt. Each row has `status` equal to the CloudTrail `errorCode` (typically `NotAuthorizedException`), `resource` equal to the username attempted (or `unknown` when CloudTrail redacted it), and `details.source_ip` populated.

4. **AC4 — Legitimate CISO approve still writes `CR_APPROVED`.** A `POST /actions/{id}/approve` with a real CISO IdToken on a real in-progress change request continues to write a row with `action_type = "CR_APPROVED"`. Behavior is unchanged from before this spec; this AC exists so the tester verifies no regression.

5. **AC5 — TTL is set on every new row.** All `CROSS_PERSONA` and `AUTH_FAILED` rows have a numeric `ttl` attribute equal to `epoch_now + 7_776_000` (90 days), confirmed by reading the attribute from a freshly written row.

6. **AC6 — Write failures do not break responses.** With the audit-log table's `PutItem` mocked to raise (e.g. ProvisionedThroughputExceededException), the underlying responses are unchanged: cross-persona requests still return 403, the chat endpoint still returns its reply, and the Cognito sign-in flow still completes (or rejects) normally. Verified by unit test with a boto3 stubber on the DDB client.

7. **AC7 — `AuditLogs.jsx` renders new rows without change.** Both new `action_type` strings render in the type column with the literal string. No exception in the browser console. Filter input matches `CROSS_PERSONA` / `AUTH_FAILED` as substrings against the existing search box.

8. **AC8 — Harness logging_audit layer passes for the two API-layer scenarios.** Within the harness's existing 5-second probe window, the cross-persona scenario and the forged-token scenario both produce a row the classifier accepts. The brute-force scenario is documented as requiring a widened (5+ minute) harness window — this AC does not assert the harness passes that scenario at 5 seconds; the harness team owns that window change.

9. **AC9 — Subscriber Lambda IAM is narrowly scoped.** A `aws iam get-role-policy` (or `simulate-principal-policy`) against the new role confirms: `dynamodb:PutItem` is allowed only on the audit-log table ARN, `kms:GenerateDataKey` and `kms:Decrypt` are allowed only on the DynamoDB CMK ARN, and no statement uses `"Resource": "*"` outside the AWS-managed basic execution policy.

10. **AC10 — EventBridge rule matches a real failed-sign-in event.** A synthetic CloudTrail event matching the documented shape (replayable via `aws events put-events` with the same `source` / `detail-type` / `detail` keys) reaches the subscriber Lambda and produces an audit row. Verified end-to-end against the deployed dev stack.

11. **AC11 — Repeat events de-duplicate by `event_id` only by accident.** Two failed sign-ins on the same username at different timestamps produce two distinct rows (the `event_id` includes a microsecond timestamp). Two events with the same timestamp (rare) collide by primary key and the second silently overwrites the first — accepted v1 behavior, called out in §9.

12. **AC12 — No edit to `04-storage.yaml`, no edit to `09-agentcore.yaml`.** A `git diff` against `main` after the change shows zero changes to those two files. Allowed edits: `02-security.yaml`, `05-compute.yaml`, `api_handler.py`, and new files under `Infra/functions/audit_cognito_subscriber/`.

## 8. Out of scope / future

Called out so reviewers see the deliberate cut. One-line rationale each.

- **JWT signature verification** on the Function URL `/chat` path — separate decision the user has already deferred; would let us tell forged tokens from real ones and emit a true `FORGED_TOKEN` event.
- **GSI on the audit-log table** (`action_type` HASH + `timestamp` RANGE) — enables fast rollups (top auth failures today) without a Scan; defer until a page needs it.
- **`AuditLogs.jsx` improvements** — color-coding for the two new action_types in `ACTION_COLORS`, a dedicated security filter chip, server-side filtering by action_type. None of these are required for the rows to be visible and searchable today.
- **Cognito Threat Protection / Advanced Security Features** — paid feature that would give near-real-time signal and risk scores; not needed when the CloudTrail-mediated path is acceptable.
- **Real-time alerting** — CloudWatch Alarm on `AUTH_FAILED` count, SNS topic, on-call paging. The rows land in the table; alerting is a follow-up.
- **Retention extension beyond 90 days** — match the token-usage table for now; revisit if a compliance auditor asks for 365 days.
- **TTL backfill on existing audit rows** — the current `_audit(...)` writer never set `ttl`, so existing rows live forever. Out of scope.
- **De-duplication of audit rows** (1 row per username per N seconds) — would limit blast radius under a real brute-force burst; not needed at demo scale.

## 9. Risks

1. **CloudTrail-to-EventBridge latency variability.** The 5-minute typical upper bound is "best-effort." During peak AWS load it can run 10–15 minutes. The harness's brute-force probe might still time out even after widening to 5 minutes. Mitigation: the harness team owns its own timeout policy; this spec documents the constraint and accepts it.
2. **Audit-write fan-out under a real brute-force burst.** A botnet-scale burst (hundreds of failed sign-ins per second) fans out 1:1 to audit-log rows, spiking DDB write costs and drowning legitimate rows on the Audit Logs page. Acceptable at demo scale (we have four demo users and no public sign-up). Mitigation if the demo ever sees real abuse: add a 1-row-per-username-per-60s de-dupe in the subscriber Lambda.
3. **New Lambda becomes a new failure surface.** A bug in the subscriber Lambda silently drops audit rows. Mitigation: best-effort writes, CloudWatch logs on every WARNING, no return-value contract. The Lambda's only job is to write a row; if it fails, the worst outcome is a missing row.
4. **Primary-key collision on same-microsecond events.** Two failed sign-ins on the exact same `username` + `timestamp` microsecond produce the same `event_id`; the second `PutItem` overwrites the first. Vanishingly rare at demo scale, and the same property already holds for the existing `_audit(...)` writer (same `event_id` format).
5. **CloudTrail-redacted usernames.** Some Cognito error paths redact the attempted username from the CloudTrail event (e.g. `UserNotFoundException` sometimes shows `additionalEventData.sub` instead). The subscriber falls back to `unknown` for `resource` and `user` in that case. Accepted.
6. **EventBridge rule pattern drift.** AWS occasionally renames CloudTrail event fields. The subscriber Lambda parses defensively and logs an unparseable event at WARNING rather than crashing — but a renamed `errorCode` field could silently stop the rule from matching at all. Mitigation: a manual smoke test after any CloudTrail / Cognito-side update.
7. **CMK access on the new role.** If the `KMSDecrypt` / `GenerateDataKey` statement is misconfigured, `PutItem` silently succeeds at the SDK level but the row never lands. This is the documented gotcha from `CLAUDE.local.md`. Mitigation: AC9 explicitly checks the IAM scope.

## 10. Open questions

None at spec time. The major design axes (which path to build for brute-force, how to handle the forged-token case, whether to edit off-limits templates, the event taxonomy, TTL value, and the legitimate-approve verification approach) were resolved by the user before this spec was written. The spec-level ambiguities encountered during drafting were resolvable from the research brief and the existing code:

- The `event_id` collision under identical-microsecond events is accepted v1 behavior (Risk #4).
- The `unknown` fallback for CloudTrail-redacted usernames is accepted v1 behavior (Risk #5).
- The 90-day TTL matches the token-usage table — no further tuning needed at this stage.

If reviewers spot additional ambiguity, this section gets populated before architect handoff.
