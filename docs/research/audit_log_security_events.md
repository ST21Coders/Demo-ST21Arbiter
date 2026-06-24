# Audit Log for Security-Relevant Events — Research Brief

## Problem framing

The harness reports four security-relevant scenarios produce zero new rows in
`dev-st21arbiter-poc-audit-log` within a 5-second window after the triggering
event: a burst of failed Cognito sign-ins, a cross-persona request (SOC token to
a CISO-only route), a forged-claim token request, and a legitimate CISO approve.
The Audit Logs page (`ui/src/pages/AuditLogs.jsx`) is one of the headline
governance surfaces for CISO and GRC personas, so the demo claim "we audit
everything" lands badly when none of those events leave a row behind.

We need to figure out, given the current code, where each of these events
should turn into an audit row, what shape that row takes, and how the failed
Cognito sign-ins can be observed at all since they never reach the
`api_handler` Lambda.

## 1. Current state

### Schema of the audit-log table

From `Infra/templates/04-storage.yaml:128`:

- Table name: `<env>-<project>-audit-log` (env=`dev`, project=`st21arbiter-poc`).
- Billing: `PAY_PER_REQUEST`.
- PITR: enabled.
- Encryption: SSE-KMS using the `DynamoDBKey` CMK (CMK ARN imported from
  `02-security`).
- Key schema: `event_id` (HASH, String) + `timestamp` (RANGE, String).
- TTL: `ttl` attribute, enabled. No default TTL is written by the existing
  `_audit(...)` writer, so rows currently never expire unless the writer sets it.
- GSIs: **none**.

That last point matters. The current Audit Logs page uses a `Scan(Limit=200)`
in `_handle_list_audit` (`Infra/functions/api_handler/api_handler.py:518`) and
sorts client-side. There is no path to query by `event_type` / `user` / time
without a Scan.

### The `_audit(...)` writer

Defined at `Infra/functions/api_handler/api_handler.py:1723`:

```python
def _audit(action_type, resource, user, status, details=None):
    if not audit_table:
        return
    try:
        audit_table.put_item(Item={
            "event_id": f"{action_type.lower()}-{resource}-{<microsec timestamp>}",
            "timestamp": <UTC ISO timestamp>,
            "action_type": action_type,
            "resource": resource,
            "user": user,
            "status": status,
            "details": json.dumps(details or {}),
        })
    except Exception:
        logger.exception("audit write failed (%s)", action_type)
```

Properties:

- Best-effort: wrapped in try/except, never raises, matches the project
  convention from `CLAUDE.md` ("side-effect writes are best-effort and must
  never break the chat").
- Synchronous: blocks the route until DDB acks the PutItem. Fine at demo scale.
- No `ttl` is written — rows live forever, contradicting the TTL spec on the
  table. Worth noting but out of scope here.
- `details` is stringified JSON (the SPA parses it back in
  `AuditLogs.jsx:24`).

A sibling helper `_audit_jira(...)` exists at line 1297 with the same shape
but a hand-rolled event_id (`jira-<key>`) and an `action_type` of
`JIRA_LINKED`.

### Existing call sites (api_handler.py)

Grep'd from the file:

| Line | Action type             | Trigger                                      |
| ---- | ----------------------- | -------------------------------------------- |
| 1462 | `JIRA_TRANSITIONED`     | `_handle_jira_transition`                    |
| 1496 | `JIRA_COMMENTED`        | `_handle_jira_comment`                       |
| 1563 | `SERVICENOW_IMPACT_ANALYSIS` | `_handle_servicenow_impact`            |
| 1688 | `WHATIF_RUN`            | scan dry-run                                 |
| 1803 | `CR_CREATED`            | `_handle_create_action`                      |
| 1933 | `CR_APPROVED` / `CR_REJECTED` / `CR_ESCALATED` / `CR_EXECUTED` | `_handle_action_transition` (variable `audit_action`) |
| `_audit_jira` (lines 1364, 1392) | `JIRA_LINKED`        | `_handle_jira_create`                       |

The scanner Lambda (`Infra/functions/scanner/...`) writes the
`SCAN_TRIGGERED` / `SCAN_STARTED` / `SCAN_COMPLETED` / `SCAN_FAILED` /
`INGESTION_COMPLETE` event types referenced in `AuditLogs.jsx:7-21` —
that's not in `api_handler` and not the focus here.

There are **zero call sites for any security event** today. No
`AUTH_FAILURE`, `FORBIDDEN`, `FORGED_TOKEN`, `BRUTE_FORCE`, `CROSS_PERSONA`,
or similar. Every existing call site is on a happy-path business action.

### What `AuditLogs.jsx` renders

From `ui/src/pages/AuditLogs.jsx`:

- Columns (in order): expand chevron, `timestamp` (formatted `yyyy-MM-dd HH:mm:ss`),
  `action_type` (with a color from `ACTION_COLORS` or default slate),
  `resource`, `user`, `status` (via `StatusBadge`), and `details` (rendered by
  `shortDetails(log)`).
- Sort: newest first by `timestamp` (line 99).
- Filter: a single text input that does case-insensitive substring match
  against `action_type`, `resource`, `user`, and the raw `details` string.
- Action color map (line 7) recognises `SCAN_*`, `CR_*`, `CONFLICT_RESOLVED`,
  `JIRA_LINKED`, `KB_SYNC`. Unknown action types still render — they just
  fall back to slate text. So new event types won't break the page; they just
  won't be colored.

## 2. The four scenarios in detail

### a. Six failed Cognito sign-in attempts (brute force)

- Harness trigger: 6 back-to-back `cognito-idp:InitiateAuth` calls with a
  synthetic username `harness-bf-<hex>@harness.invalid` and a known-bad
  password (`tests-adversarial/logging_audit/test_security_events_logged.py:440-451`).
- What the API handler does: nothing. The API handler does not see these calls.
  Cognito's `cognito-idp.amazonaws.com` endpoint processes them server-side
  before any IdToken is minted, and `api_handler.py` is only invoked once a
  Lambda URL or APIGW request actually arrives.
- Why no row is written: there is no code path between Cognito and the
  `audit-log` table. No `LambdaConfig` on the user pool, no EventBridge rule,
  no CloudWatch subscription filter.
- Data available at the failure point: only what Cognito records — username
  attempted, source IP, user agent, eventName (`InitiateAuth`), errorCode
  (`NotAuthorizedException` / `UserNotFoundException`), and (with threat
  protection enabled) a risk score. The harness has no way to inject anything
  here.

### b. Cross-persona request (SOC token to CISO-only route)

- Harness trigger: a legitimate SOC IdToken sent against `GET /token-usage`
  (`test_security_events_logged.py:259-265`).
- What the API handler does today: `_handle_list_token_usage` calls
  `_require_ciso(event)` at line 767, which calls `_caller_groups(event)`,
  finds `ciso` is not in the SOC user's groups, and returns
  `_err(403, "Token Tracking is restricted to the CISO persona")` from
  line 551 — with no `_audit(...)` call.
- Why no row is written: `_require_ciso` returns the 403 response object
  directly; the route handler short-circuits before any audit code runs. The
  same is true for the `_require_ciso` call in
  `_handle_token_usage_summary` at line 783.
- Data available at the failure point:
  - `claims.get("sub")` and `claims.get("cognito:username")` via
    `_caller_user_id(event)`.
  - `claims.get("email")` via `_caller_claims(event)` (the email is what the
    SPA actually shows in the User column).
  - `_caller_groups(event)` — the SOC user's actual group set.
  - The path (`/token-usage`).
  - Source IP via `event["requestContext"]["http"]["sourceIp"]` (Function URL)
    or `event["requestContext"]["identity"]["sourceIp"]` (API Gateway). Not
    used today.

### c. Forged-claim token request

- Harness trigger: take a real CISO IdToken, decode the payload, rewrite
  `cognito:groups` to a canary value (e.g. `harness-<hex>`), repack without
  re-signing, and `GET /token-usage` with that as the Bearer (see
  `test_security_events_logged.py:183-204` and the `forge_cognito_groups`
  helper it imports from `auth.test_forged_groups`).
- What the API handler does today: in `_caller_claims` (line 1978), if the
  request came through the Function URL, the code base64-decodes the JWT
  payload **without verifying the signature** (line 1995: `# (no signature
  verification — fine for the demo)`). It then reads `cognito:groups` and
  passes the forged value to `_require_ciso`. Because the forged groups
  contain `harness-<hex>` and not `ciso`, the route returns 403 from the
  cross-persona path. No `_audit(...)` is called.
- Why no row is written: same as scenario (b) — `_require_ciso` short-circuits.
  Additionally, even if it did write a row, there is no detection that the
  signature was forged — the demo Lambda intentionally does not verify it,
  so we can't tell a forged token from a real one without adding JWKS
  verification.
- Data available at the failure point:
  - The raw forged `cognito:groups` value (the canary), which is the harness's
    primary needle.
  - The `sub` and `email` from the (real, CISO) IdToken — those come through
    untouched because the harness only edits the groups claim.
  - The path (`/token-usage`).

### d. Legitimate CISO action approve

- Harness trigger: `POST /actions/{cr_id}/approve` with a real CISO IdToken
  and `{approver_email, approver_role: "ciso", comment}`
  (`test_security_events_logged.py:347-355`).
- What the API handler does today: hits `_handle_action_transition` (line 1809).
  This **does** call `_audit("CR_APPROVED", cr_id, user_id, new_status, {...})`
  at line 1933, so a row should be written.
- Why the harness still sees zero: probably the harness's needles
  (`cr_id`, `ciso_username`, `"approve"`) don't match what we write. The row
  we write has `action_type="CR_APPROVED"` and `resource=cr_id`, and the
  harness scans for any row whose stringified attributes contain one of
  those needles. The `cr_id` should match. So the most likely root cause is
  either:
    1. The harness's CR pickup at lines 322-331 fails — `/actions` returns
       `[]` in the live env right now, so the test hits `pytest.skip` and
       gets reported as a fail anyway by the adversarial harness's
       interpretation, OR
    2. The CR was not found (`return _err(404)` before the audit write),
       OR
    3. The Lambda has gone into an error path before reaching line 1933.
  Worth confirming by reading the harness's results file rather than
  assuming an audit gap. The brief should flag this as the
  least-clear-cut scenario.
- Data available at the failure point: full CISO claims, the `cr_id`,
  prior + new status, the approver role and email from the body, the actor
  comment, and the CISO override flag.

## 3. Cognito failed sign-in: special case

This is the only one of the four that cannot be solved by adding `_audit(...)`
calls in `api_handler.py`. The failed sign-in is processed inside the
Cognito service before any of our code runs. Three observable paths:

### Option A: CloudTrail → EventBridge → subscriber Lambda

Cognito user pool API calls are logged as CloudTrail **management events**
([source](https://docs.aws.amazon.com/cognito/latest/developerguide/logging-using-cloudtrail.html)).
Management events are on by default in every AWS account, so no extra
CloudTrail config is needed for the call to show up; what we need is an
EventBridge rule that matches them and routes to a Lambda. Pattern:

```json
{
  "source": ["aws.cognito-idp"],
  "detail-type": ["AWS API Call via CloudTrail"],
  "detail": {
    "eventSource": ["cognito-idp.amazonaws.com"],
    "eventName": ["InitiateAuth", "AdminInitiateAuth", "RespondToAuthChallenge"],
    "errorCode": ["NotAuthorizedException", "UserNotFoundException",
                  "TooManyRequestsException", "PasswordResetRequiredException"]
  }
}
```

The Lambda receives the CloudTrail event and writes one audit row per match,
with `action_type="AUTH_FAILED"`, `resource=<username>`, `user=<username>`,
`status=<errorCode>`, `details={sourceIPAddress, userAgent, eventName, awsRegion}`.

CloudTrail-to-EventBridge latency is "best-effort" per the AWS docs
([reference](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-service-event-cloudtrail.html))
— typically **2 to 15 minutes**, occasionally longer. **This will not make
the harness's 5-second window unless the harness window is widened or the
brute-force probe is changed to use a different signal.**

### Option B: Cognito Threat Protection (formerly Advanced Security Features)

Cognito's Threat Protection feature
([source](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pool-settings-threat-protection.html))
publishes a richer event stream (risk scores, geo / IP context, account
compromise events) and can be queried in near-real-time via CloudWatch and
ListUserAuthEvents. It is paid per active user — the published pricing tier
is roughly $0.05 / MAU on the "Plus" plan. For four demo users that is
trivial monetarily, but it requires `UserPoolAddOns: { AdvancedSecurityMode: ENFORCED | AUDIT }`
on the user pool. Latency is publication-time but still typically seconds,
not sub-second. Notable: ListUserAuthEvents would let an audit-writer Lambda
poll for unauthenticated `InitiateAuth` events with risk decisions — that is
the most production-real signal, but it's a polling model.

### Option C: Pre/Post auth Lambda triggers

Cognito supports `PreAuthentication`, `PostAuthentication`, and
`PreTokenGeneration` Lambda triggers on the user pool. `PreAuthentication`
runs **before** the password check, so it cannot distinguish success from
failure. `PostAuthentication` runs **only on success**, so it misses every
failed attempt — that's exactly the case we care about. Net: triggers do not
fit failed sign-ins cleanly. They are still useful for a
`SUCCESSFUL_SIGN_IN` audit row, which we may or may not want.

### Recommendation

Use Option A (CloudTrail → EventBridge → audit-writer Lambda) for the
following reasons:

- No extra paid features on Cognito.
- Already-on CloudTrail management events — zero config on Cognito side.
- One small Lambda, one EventBridge rule, one IAM role with PutItem on the
  audit table. Fits the project's "one stack, one purpose" templating style
  (the rule and Lambda could live in a new `05-compute.yaml` snippet or be
  added next to the existing scanner stack).

**But:** the harness's 5-second probe window is incompatible with the typical
CloudTrail latency. The brief should explicitly call this out as the
contradiction it is, and the open questions below ask the user how they want
to reconcile this. Without an answer, the brute-force scenario remains
fundamentally unsolvable inside the harness's current shape.

If the user wants near-real-time, Option B (Threat Protection) is the only
viable path inside the AWS-native toolset, at the cost of enabling a paid
feature.

## 4. Where the writes should land

- **Same audit-log table? Yes.** The Audit Logs UI page reads only this
  table. Adding a second table for security events would silently hide them
  from the demo, defeating the purpose. Mixing event types in one table
  also matches how the existing JIRA / CR / Scan events all coexist.
- **New GSI?** Probably not, on demo scale. The page's current Scan(Limit=200)
  is fine for the volumes we're dealing with. Querying by `event_type` for
  rollups (e.g. "top auth failures today") would benefit from a GSI on
  `action_type` (HASH) + `timestamp` (RANGE), but that is a
  `04-storage.yaml` edit, which is off-limits per `CLAUDE.md` without
  explicit sign-off (see Off-limits below). Recommend: skip the GSI for now,
  keep Scan-based reads, revisit if rollups become a page requirement.
- **CMK interaction:** the api_handler role's `KMSUsage` statement
  (`Infra/templates/02-security.yaml:147-153`) already permits Decrypt /
  GenerateDataKey / DescribeKey on `Resource: "*"`, so any existing
  api_handler-side writes work transparently. A **new** Cognito-subscriber
  Lambda needs its own role with the same KMS statement (or it'll fail
  silently on PutItem to a CMK-encrypted table — a known footgun called
  out in `CLAUDE.local.md`). That's a `02-security.yaml` edit.

## 5. Tooling tradeoffs

### Inline (synchronous) writes vs async fan-out

The project convention is best-effort, in-line, swallowed exceptions
(see `_audit(...)` already). For the three api_handler scenarios (cross-
persona, forged token, legitimate approve), in-line is the right answer:

- A burst of failed-auth requests is the only realistic scenario where the
  audit write volume could matter, and that case is handled outside the
  api_handler (Option A path).
- Async fan-out (SQS / SNS / direct EventBridge bus) adds complexity and a
  new IAM surface without solving any current problem.

### Uniform row shape vs per-type

The existing `_audit(...)` already imposes a near-uniform shape
(`event_id`, `timestamp`, `action_type`, `resource`, `user`, `status`,
`details`). The `details` JSON blob is the per-type variant. Recommendation:
**keep the uniform top-level shape, vary `details`**. That way the SPA
column layout doesn't change and the existing `shortDetails(log)` rendering
falls through cleanly.

Specifically, suggested per-type `details`:

- `AUTH_FAILED` (brute force, from the Cognito subscriber):
  `{username, source_ip, user_agent, event_name, error_code, aws_region}`.
- `CROSS_PERSONA` (in `_require_ciso`-like checks): `{path, required_group,
  caller_groups, persona, source_ip}`.
- `FORGED_TOKEN` (any path where signature verification fails — if we add
  it; today we don't verify): `{path, claimed_groups, source_ip}`.
- `CR_APPROVED` (existing, no change): `{cr_id, approver_email,
  approver_role, ciso_override}`.

### `action_type` controlled enum vs free string

The existing call sites already treat it as a controlled enum by convention
— `AuditLogs.jsx` color-maps known values and falls through gracefully on
unknowns. Recommendation: keep it a convention, not an enforced enum. Put a
short list of allowed values in a docstring or a `_SECURITY_EVENT_TYPES`
tuple near `_audit(...)`, so future contributors don't accidentally invent
near-duplicate names like `AUTH_FAIL` vs `AUTH_FAILED`. Document the four new
types we add: `AUTH_FAILED`, `CROSS_PERSONA`, `FORGED_TOKEN`, and the existing
`CR_APPROVED` (kept).

## 6. Off-limits

Three sensitive files are involved. None **must** be touched, but two
**would** be touched in the most thorough implementation:

- **`Infra/templates/04-storage.yaml`** — the table is here. The brief's
  recommendation above is to leave it alone (no new GSI, no schema change).
  Confirmation requested in the open-questions section.
- **`Infra/templates/02-security.yaml`** — needs a new IAM role for the
  Cognito-subscriber Lambda (PutItem on `audit-log`, KMS Decrypt on the
  DynamoDB CMK). The api_handler role does NOT need changes — its existing
  wildcard PutItem covers any new `_audit(...)` call sites we add.
- **`Infra/templates/09-agentcore.yaml`** — not impacted.
- **`Infra/templates/05-compute.yaml` (or new file)** — adding a new Lambda
  for the Cognito subscriber requires an edit here, OR a new template file.
  Both fit normal pipeline conventions, but the file is not flagged
  off-limits.
- **`Infra/templates/03-identity.yaml`** — only if Option B (Threat
  Protection) is chosen (adds `UserPoolAddOns`).

## 7. Risks

- **CloudTrail latency vs 5-second harness window.** Documented CloudTrail
  best-effort delivery to EventBridge is typically 2–15 minutes. The harness
  sleeps 5 seconds. Without a different signal source or a wider window, the
  brute-force scenario will keep failing even after we ship the
  Option A subscriber. Open question for the user.
- **Audit-log write amplification.** A real brute-force burst (think
  hundreds of requests per second from a botnet) fanning out 1:1 to
  audit-log rows could spike DDB write costs and, more importantly, drown
  legitimate events on the Audit Logs page. Mitigations: a 1-row-per-username-per-N-seconds
  dedupe in the subscriber Lambda, or rely on Cognito's own
  throttling (the AWS docs call out `TooManyRequestsException` after a few
  failed calls). Worth a note even if we don't act on it today.
- **AuditLogs.jsx unknown-action handling.** Verified: unknown
  `action_type` falls through to slate text rendering with no exception
  (`ui/src/pages/AuditLogs.jsx:185`). No code change required there.
  Filtering by action substring works for any name. So the four new
  types are safe to introduce without an SPA change.
- **The `_caller_claims` shortcut for the Function URL path.** We do not
  verify the JWT signature today (line 1995 comment). That means we have no
  way to distinguish "forged groups" from "real CISO" at the API layer
  without adding JWKS verification. Without verification, the only
  pragmatic audit-row we can write for scenario (c) is the same row we'd
  write for any cross-persona attempt — a `FORGED_TOKEN` action type would
  be aspirational and somewhat misleading. The open questions ask the user
  whether to fold scenario (c) into the `CROSS_PERSONA` event type.
- **Sub-5s scenario (d) ambiguity.** As described in §2(d), the legitimate
  approve path already calls `_audit(...)`. We should verify with one
  hand-run probe what is actually happening before assuming the writer is
  broken. Could be a stale change-request list or a 404 short-circuit.
- **TTL gap.** The audit-log table has `ttl` enabled but no writer sets it.
  All rows live forever today. Not in scope, but worth flagging — a feature
  that floods this table with hundreds of `AUTH_FAILED` rows per attack
  makes the TTL gap more pressing.

## 8. Open questions

1. Is editing `Infra/templates/04-storage.yaml` (off-limits per `CLAUDE.md`)
   acceptable for adding a GSI on `action_type` to the audit-log table, or
   should we stay Scan-only and skip the GSI entirely?
2. Are we OK adding a new Lambda (`audit_cognito_subscriber`) and a new
   EventBridge rule to the dev stack? Best home is a new template
   (`Infra/templates/13-audit-pipe.yaml`) or an addition to
   `05-compute.yaml`. Which do you prefer?
3. Editing `Infra/templates/02-security.yaml` to add an IAM role for the
   new Lambda is unavoidable if you accept Q2. Confirm OK?
4. Cognito CloudTrail → EventBridge latency is documented as 2–15 minutes.
   The harness's 5-second probe window cannot see this signal. Three ways to
   reconcile: (a) widen the harness window to 5+ minutes for the brute-force
   probe only, (b) enable Cognito Threat Protection / Advanced Security
   (paid: ~$0.05/MAU; tiny for demo, but a posture change), (c) accept that
   the brute-force scenario will remain failing in adversarial reports and
   live with it. Which?
5. For the legitimate CISO approve scenario (which already writes an audit
   row per code), should we treat this as "verify, no code change", or do
   you want extra fields added to the audit row (prior + new state,
   change-request severity, approver chain snapshot)?
6. For scenario (c) "forged-claim token": since we deliberately do **not**
   verify the JWT signature in the Function URL path (per the comment at
   `api_handler.py:1995`), we cannot distinguish a forged token from a real
   one. Two options: (a) fold scenario (c) into the `CROSS_PERSONA` event
   type and accept the harness will see *one* match (the cross-persona
   needle on the same path), (b) add JWKS signature verification to
   `_caller_claims` and emit a distinct `FORGED_TOKEN` event type when
   verification fails. (b) is the right answer for production but a
   meaningful change to the demo's auth path. Which?
7. Should the four event types appear under a unified `action_type` enum
   (i.e. just new strings: `AUTH_FAILED`, `CROSS_PERSONA`, `FORGED_TOKEN`,
   plus the existing `CR_APPROVED`), or do you want a new top-level
   `event_category` field ("security" vs "business") so the page can filter
   them apart?
8. Should the new audit writes set the table's `ttl` attribute (currently
   no writer does, so rows live forever), and if yes, what retention —
   90 days to match `token-usage`, or longer for compliance posture?

## Recommended direction

If forced to pick a single path: keep the audit-log table as-is, add four
new `action_type` values via the existing `_audit(...)` writer for the three
api_handler scenarios, and add a Cognito CloudTrail → EventBridge → small
Lambda subscriber for the brute-force scenario. Accept that the
CloudTrail-mediated path will not meet the harness's 5-second window
without either widening the window or enabling Cognito Threat Protection.
The biggest open call is question 4 — the rest of the implementation is
small and follows established project patterns.

## References

- Project conventions: `CLAUDE.md`, `CLAUDE.local.md`.
- Audit table definition: `Infra/templates/04-storage.yaml:128-156`.
- Audit IAM scope: `Infra/templates/02-security.yaml:115-153`.
- Cognito user pool definition: `Infra/templates/03-identity.yaml:31-60`.
- `_audit(...)` writer: `Infra/functions/api_handler/api_handler.py:1723-1737`.
- `_audit_jira(...)` sibling writer: `Infra/functions/api_handler/api_handler.py:1297-1324`.
- `_caller_claims`, `_caller_groups`, `_caller_user_id`:
  `Infra/functions/api_handler/api_handler.py:1955-1999`.
- `_require_ciso` short-circuit: `Infra/functions/api_handler/api_handler.py:542-552`.
- Audit Logs SPA page: `ui/src/pages/AuditLogs.jsx`.
- Harness probes: `tests-adversarial/logging_audit/test_security_events_logged.py`.
- Harness classifier: `tests-adversarial/logging_audit/classifiers.py`.
- [Amazon Cognito logging in AWS CloudTrail](https://docs.aws.amazon.com/cognito/latest/developerguide/logging-using-cloudtrail.html).
- [Amazon Cognito user pools events in EventBridge](https://docs.aws.amazon.com/eventbridge/latest/ref/events-ref-cognito-idp.html).
- [Amazon Cognito threat protection](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pool-settings-threat-protection.html).
- [AWS service events delivered via CloudTrail (latency note)](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-service-event-cloudtrail.html).
