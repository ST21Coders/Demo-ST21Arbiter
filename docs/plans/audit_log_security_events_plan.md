# Plan — Audit log for security-relevant events

**Spec:** [`Documents/audit_log_security_events_spec.md`](../../Documents/audit_log_security_events_spec.md)
**Research brief:** [`docs/research/audit_log_security_events.md`](../research/audit_log_security_events.md)
**Status:** Draft (architecture only — no code yet)

---

## 1. Approach

Two independent pieces ship together. Both write to the existing
`<env>-<project>-audit-log` DynamoDB table, both set a 90-day `ttl`, and both
follow the project's best-effort "never break the response" rule that the
existing `_audit(...)` writer at `Infra/functions/api_handler/api_handler.py:1723`
already embodies.

**Piece A — API-layer `CROSS_PERSONA` write (synchronous, in-line).** Inside
`_require_ciso(event)` at `api_handler.py:542-552` we add one call to
`_audit("CROSS_PERSONA", ...)` immediately before the existing
`_err(403, ...)` return. Both call sites of `_require_ciso` —
`_handle_list_token_usage` at line 767 and `_handle_token_usage_summary` at
line 783 — inherit the write for free, with no per-route change. The forged-
token scenario (real CISO JWT with rewritten `cognito:groups`) lands on the
exact same code path, so it folds in without a separate `FORGED_TOKEN`
action_type. The write is wrapped in try/except inside `_audit(...)` itself
(existing contract); no extra wrapping in `_require_ciso` is required.

**Piece B — Cognito subscriber Lambda (asynchronous, out-of-band).** Cognito
sign-in attempts never reach `api_handler` because Cognito processes them
internally. Failed sign-ins are visible only through CloudTrail. A new Lambda,
`dev-st21arbiter-poc-audit-cognito-subscriber`, lives at
`Infra/functions/audit_cognito_subscriber/handler.py` and is triggered by an
EventBridge rule that matches the CloudTrail event shape for failed sign-ins
against the dev Cognito user pool. The Lambda extracts `username`, `sourceIP`,
`userAgent`, `errorCode`, and `userPoolId` from the event detail and writes one
`AUTH_FAILED` audit row per match. Writes are best-effort: any exception is
logged and swallowed so EventBridge does not retry on transient DDB throttles.

```
Cognito sign-in attempt
  └─ Cognito IdP
       └─ CloudTrail management event (always on, no config change)
            └─ EventBridge rule  dev-st21arbiter-poc-cognito-auth-failed
                  pattern: source=aws.cognito-idp
                           eventSource=cognito-idp.amazonaws.com
                           eventName in [InitiateAuth, AdminInitiateAuth,
                                         RespondToAuthChallenge]
                           errorCode in [NotAuthorizedException,
                                         UserNotFoundException, ...]
                           userPoolId == dev pool id
                 └─ Subscriber Lambda  dev-st21arbiter-poc-audit-cognito-subscriber
                       └─ PutItem  dev-st21arbiter-poc-audit-log
                             action_type=AUTH_FAILED  ttl=epoch+90d
```

CloudTrail-to-EventBridge latency is documented as 2-15 minutes. The harness
team owns widening the brute-force probe window separately; this plan does not
attempt to compress that latency.

## 2. Architecture decisions

Each significant choice with the alternative it beat.

**A1. `_audit(...)` call goes inside `_require_ciso`, not at the two call sites.**
Alternative: add a separate `_audit(...)` line inside `_handle_list_token_usage`
and `_handle_token_usage_summary`. Reason: any future CISO-only route that adopts
`_require_ciso` would otherwise silently skip the audit. Centralising it in the
helper is one place to maintain and one place to test.

**A2. Forged-token scenario folds into `CROSS_PERSONA`, not a new
`FORGED_TOKEN` type.** Alternative: add JWKS signature verification to
`_caller_claims` and emit `FORGED_TOKEN` when verification fails. Reason: the
demo's Function URL path deliberately skips signature verification
(`api_handler.py:1995` comment); we cannot tell a forged token from a real one
at the API layer without that change, which is a separate, deferred decision.
The forged canary value still appears in `details.caller_groups` so an auditor
can spot it.

**A3. Subscriber Lambda lives in `05-compute.yaml`, not a new template.**
Alternative: a new `Infra/templates/13-audit-pipe.yaml`. Reason:
`05-compute.yaml` already hosts the only other SAM Lambda the project owns
(`ProcessingPipelineFunction`) and the EventBridge rules + permissions next to
it. Matching that pattern keeps the deploy order unchanged. A new stack would
need to be wired into `Infra/deploy.sh` and would add deployment surface for no
gain.

**A4. EventBridge rule pattern scopes to `userPoolId`, not just by error code.**
Alternative: match on Cognito errors account-wide. Reason: this account could
host other Cognito pools in the future; scoping by `detail.requestParameters.userPoolId`
isolates the demo pool and prevents noise.

**A5. Subscriber Lambda has no VPC config.** Alternative: place it in the
private subnet like `ProcessingPipelineFunction`. Reason: the Lambda only calls
DynamoDB and KMS, both reachable over AWS public endpoints from a no-VPC
Lambda with lower cold-start cost. The api_handler is in-VPC for unrelated
reasons (Bedrock VPCe). We override the `05-compute.yaml` `Globals` VpcConfig
with `VpcConfig: { SubnetIds: [], SecurityGroupIds: [] }` on this one
function, which SAM treats as "no VPC".

**A6. `actor_id` for the forged path uses the real `sub`.** The forged token
keeps the real JWT header + signature + most of the payload; the harness only
mutates `cognito:groups`. So `_caller_user_id(event)` still returns the real
CISO user's `sub`. That is the right field to write to the audit row — it
reveals which credential was being abused. Document this in the code comment.

**A7. TTL is set on new rows only, not backfilled.** Alternative: backfill
existing rows with a `ttl` value. Reason: out of scope per spec §8; the
existing `_audit(...)` writer never set `ttl`, so existing rows live forever.
We add `ttl` only to the two new code paths. The existing writer is untouched.

**A8. Subscriber Lambda is its own IAM role, not a reuse of
`ApiHandlerRole`.** Alternative: attach the rule's target to `ApiHandlerRole`.
Reason: least-privilege. The subscriber needs `PutItem` on one table and
`GenerateDataKey` on one CMK; nothing else. Reusing api_handler's broad role
would silently widen the blast radius if the subscriber were ever compromised.

**A9. Subscriber Lambda is *not* SAM-packaged with a separate `requirements.txt`
install step.** Reason: it only uses `boto3` (already in the Lambda runtime)
and `os`, `json`, `logging`, `datetime`. We still ship a `requirements.txt`
with `boto3` listed for documentation, but SAM's default pip install of an
empty / boto3-only file is harmless and free.

## 3. Data and interfaces

### `_audit(...)` existing signature (unchanged)

```python
def _audit(action_type: str, resource: str, user: str, status: str,
           details: dict | None = None) -> None:
    # Writes Item to audit_table with event_id, timestamp, action_type,
    # resource, user, status, details (json.dumps'd). Best-effort.
```

Located at `api_handler.py:1723`. We do **not** change its signature.

### New call inside `_require_ciso`

```python
def _require_ciso(event):
    """Return None if the caller's Cognito groups include 'ciso',
    else write a CROSS_PERSONA audit row and return a 403 response."""
    if "ciso" not in _caller_groups(event):
        # Best-effort audit write. _audit(...) already swallows exceptions
        # so a DDB hiccup never blocks the 403.
        claims = _caller_claims(event)
        path = (event.get("rawPath")
                or event.get("path")
                or event.get("requestContext", {}).get("http", {}).get("path")
                or "")
        method = (event.get("httpMethod")
                  or event.get("requestContext", {}).get("http", {}).get("method")
                  or "")
        source_ip = (event.get("requestContext", {}).get("http", {}).get("sourceIp")
                     or event.get("requestContext", {}).get("identity", {}).get("sourceIp")
                     or "unknown")
        user_label = claims.get("email") or claims.get("cognito:username") or "unknown"
        _audit(
            "CROSS_PERSONA",
            path or "/token-usage",
            user_label,
            "DENIED",
            {
                "path": path,
                "method": method,
                "required_group": "ciso",
                "caller_groups": _caller_groups(event),
                "caller_sub": claims.get("sub"),
                "source_ip": source_ip,
            },
        )
        return _err(403, "Token Tracking is restricted to the CISO persona")
    return None
```

### TTL — note on `_audit(...)`

The existing `_audit(...)` writer does **not** set `ttl`. We have two options
for the `CROSS_PERSONA` write to honour AC5 (90-day TTL on new rows):

1. Add an optional `ttl_seconds: int | None = None` parameter to `_audit(...)`
   that, when provided, adds `ttl = int(time.time()) + ttl_seconds` to the
   Item. Existing call sites pass nothing and keep the no-TTL behaviour.
2. Bypass `_audit(...)` in `_require_ciso` and write the Item directly.

**Decision:** Option 1. Smaller blast radius, keeps the single writer, and the
new parameter is opt-in so we do not retroactively change the other 10+ call
sites' behaviour.

Updated `_audit(...)` signature:

```python
def _audit(action_type: str, resource: str, user: str, status: str,
           details: dict | None = None, ttl_seconds: int | None = None) -> None:
```

The `_require_ciso` call passes `ttl_seconds=7_776_000`.

### Subscriber Lambda handler

`Infra/functions/audit_cognito_subscriber/handler.py`:

```python
"""ARBITER audit subscriber — writes AUTH_FAILED rows from CloudTrail.

Triggered by an EventBridge rule that matches Cognito sign-in failures on the
dev user pool. Writes one row per event to <env>-<project>-audit-log.

Best-effort: any DDB error is logged at WARNING and the handler returns
normally so EventBridge does not retry.
"""
import json, logging, os, time
from datetime import datetime, timezone
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AUDIT_LOG_TABLE = os.environ.get("AUDIT_LOG_TABLE", "")
_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days

_ddb = boto3.resource("dynamodb")
_table = _ddb.Table(AUDIT_LOG_TABLE) if AUDIT_LOG_TABLE else None


def _extract_username(detail: dict) -> str:
    req = detail.get("requestParameters") or {}
    auth_params = req.get("authParameters") or {}
    if "USERNAME" in auth_params:
        return str(auth_params["USERNAME"])
    add = detail.get("additionalEventData") or {}
    if "sub" in add:
        return str(add["sub"])
    return "unknown"


def handler(event, _ctx):
    if not _table:
        logger.warning("AUDIT_LOG_TABLE not set; skipping write")
        return {"ok": False, "reason": "no-table"}
    detail = event.get("detail") or {}
    username = _extract_username(detail)
    src_ip = detail.get("sourceIPAddress") or "unknown"
    ua = detail.get("userAgent") or "unknown"
    err_code = detail.get("errorCode") or "Unknown"
    err_msg = detail.get("errorMessage") or ""
    event_name = detail.get("eventName") or ""
    region = detail.get("awsRegion") or ""
    pool_id = ((detail.get("requestParameters") or {}).get("userPoolId")
               or (detail.get("additionalEventData") or {}).get("userPoolId")
               or "")
    event_id = detail.get("eventID") or ""
    event_time = detail.get("eventTime") or datetime.now(timezone.utc).isoformat()

    item = {
        "event_id": f"auth_failed-{username}-"
                    f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        "timestamp": event_time,
        "action_type": "AUTH_FAILED",
        "resource": username,
        "user": username,
        "status": err_code,
        "details": json.dumps({
            "source_ip": src_ip,
            "user_agent": ua,
            "event_name": event_name,
            "error_message": err_msg,
            "aws_region": region,
            "user_pool_id": pool_id,
            "event_id_cloudtrail": event_id,
        }),
        "ttl": int(time.time()) + _TTL_SECONDS,
    }
    try:
        _table.put_item(Item=item)
    except Exception:
        logger.exception("auth_failed audit write failed (user=%s)", username)
    return {"ok": True}
```

### EventBridge rule pattern (CFN literal)

```yaml
EventPattern:
  source:
    - aws.cognito-idp
  detail-type:
    - "AWS API Call via CloudTrail"
  detail:
    eventSource:
      - cognito-idp.amazonaws.com
    eventName:
      - InitiateAuth
      - AdminInitiateAuth
      - RespondToAuthChallenge
    errorCode:
      - NotAuthorizedException
      - UserNotFoundException
      - TooManyRequestsException
      - PasswordResetRequiredException
    requestParameters:
      userPoolId:
        - Fn::ImportValue: !Sub "${Environment}-${ProjectName}-UserPoolId"
```

### New IAM role (`Infra/templates/02-security.yaml` addition)

```yaml
AuditCognitoSubscriberRole:
  Type: AWS::IAM::Role
  Properties:
    RoleName: !Sub "${Environment}-${ProjectName}-audit-cognito-subscriber-role"
    AssumeRolePolicyDocument:
      Version: "2012-10-17"
      Statement:
        - Effect: Allow
          Principal: { Service: lambda.amazonaws.com }
          Action: sts:AssumeRole
    ManagedPolicyArns:
      - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    Policies:
      - PolicyName: AuditCognitoSubscriberPolicy
        PolicyDocument:
          Version: "2012-10-17"
          Statement:
            - Sid: AuditLogPutItem
              Effect: Allow
              Action: dynamodb:PutItem
              Resource:
                - !Sub "arn:aws:dynamodb:${AWS::Region}:${AWS::AccountId}:table/${Environment}-${ProjectName}-audit-log"
            - Sid: AuditCmkUse
              Effect: Allow
              Action:
                - kms:GenerateDataKey
                - kms:Decrypt
              Resource:
                - !GetAtt DynamoDBKey.Arn

Outputs:
  AuditCognitoSubscriberRoleArn:
    Value: !GetAtt AuditCognitoSubscriberRole.Arn
    Export:
      Name: !Sub "${Environment}-${ProjectName}-AuditCognitoSubscriberRoleArn"
```

### Subscriber Lambda + EventBridge rule (`05-compute.yaml` additions)

```yaml
AuditCognitoSubscriberFunction:
  Type: AWS::Serverless::Function
  Properties:
    FunctionName: !Sub "${Environment}-${ProjectName}-audit-cognito-subscriber"
    Handler: handler.handler
    CodeUri: ../functions/audit_cognito_subscriber/
    Role:
      Fn::ImportValue: !Sub "${Environment}-${ProjectName}-AuditCognitoSubscriberRoleArn"
    Timeout: 30
    MemorySize: 256
    # Override Globals.VpcConfig — no VPC needed; DDB + KMS via public endpoints.
    VpcConfig:
      SubnetIds: []
      SecurityGroupIds: []
    Environment:
      Variables:
        AUDIT_LOG_TABLE:
          Fn::ImportValue: !Sub "${Environment}-${ProjectName}-AuditLogTableName"
    Tags:
      Name: !Sub "${Environment}-${ProjectName}-audit-cognito-subscriber"

CognitoAuthFailedRule:
  Type: AWS::Events::Rule
  Properties:
    Name: !Sub "${Environment}-${ProjectName}-cognito-auth-failed"
    Description: "Failed Cognito sign-ins on the dev user pool → audit subscriber"
    State: ENABLED
    EventPattern:
      source: [ "aws.cognito-idp" ]
      detail-type: [ "AWS API Call via CloudTrail" ]
      detail:
        eventSource: [ "cognito-idp.amazonaws.com" ]
        eventName: [ InitiateAuth, AdminInitiateAuth, RespondToAuthChallenge ]
        errorCode:
          - NotAuthorizedException
          - UserNotFoundException
          - TooManyRequestsException
          - PasswordResetRequiredException
        requestParameters:
          userPoolId:
            - Fn::ImportValue: !Sub "${Environment}-${ProjectName}-UserPoolId"
    Targets:
      - Arn: !GetAtt AuditCognitoSubscriberFunction.Arn
        Id: AuditCognitoSubscriberTarget

CognitoAuthFailedRulePermission:
  Type: AWS::Lambda::Permission
  Properties:
    FunctionName: !Ref AuditCognitoSubscriberFunction
    Action: lambda:InvokeFunction
    Principal: events.amazonaws.com
    SourceArn: !GetAtt CognitoAuthFailedRule.Arn

AuditCognitoSubscriberLogGroup:
  Type: AWS::Logs::LogGroup
  Properties:
    LogGroupName: !Sub "/aws/lambda/${Environment}-${ProjectName}-audit-cognito-subscriber"
    RetentionInDays: 30
```

### Row shapes

| Field | `CROSS_PERSONA` | `AUTH_FAILED` |
|---|---|---|
| `event_id` | `cross_persona-<path>-<microsec>` (existing format) | `auth_failed-<username>-<microsec>` |
| `timestamp` | `datetime.now(timezone.utc).isoformat()` | CloudTrail `detail.eventTime` (preserves original) |
| `action_type` | `CROSS_PERSONA` | `AUTH_FAILED` |
| `resource` | request path (e.g. `/token-usage`) | username or `unknown` |
| `user` | caller's email (claims.email → cognito:username fallback) | same as resource |
| `status` | `DENIED` | CloudTrail `errorCode` (e.g. `NotAuthorizedException`) |
| `details` (json) | `{path, method, required_group, caller_groups, caller_sub, source_ip}` | `{source_ip, user_agent, event_name, error_message, aws_region, user_pool_id, event_id_cloudtrail}` |
| `ttl` | `int(time.time()) + 7_776_000` | `int(time.time()) + 7_776_000` |

## 4. Files affected

**Modify:**

- `Infra/functions/api_handler/api_handler.py`
  - Insert best-effort `_audit("CROSS_PERSONA", ...)` call inside `_require_ciso` before the 403 return.
  - Add optional `ttl_seconds` parameter to `_audit(...)` so the new write can set the 90-day TTL.
- `Infra/templates/02-security.yaml`
  - Add `AuditCognitoSubscriberRole` (assume-role for Lambda, basic execution, PutItem on the audit-log ARN, GenerateDataKey + Decrypt on the DynamoDB CMK). Add a matching `Outputs` export.
- `Infra/templates/05-compute.yaml`
  - Add `AuditCognitoSubscriberFunction`, `CognitoAuthFailedRule`, `CognitoAuthFailedRulePermission`, `AuditCognitoSubscriberLogGroup`. No new template parameters required.

**Create:**

- `Infra/functions/audit_cognito_subscriber/__init__.py`
  - Empty package marker (for parity with other Lambda dirs).
- `Infra/functions/audit_cognito_subscriber/handler.py`
  - Lambda entrypoint. Receives EventBridge event, writes one `AUTH_FAILED` row with 90-day `ttl`, swallows all exceptions.
- `Infra/functions/audit_cognito_subscriber/requirements.txt`
  - Lists `boto3` for documentation only (the Lambda runtime already provides it).
- `tests/unit/test_audit_cross_persona.py`
  - Unit test for the new `_require_ciso` audit write. Mocks the DDB resource via moto; asserts a `CROSS_PERSONA` row lands with the expected fields and `ttl`.
- `tests/unit/test_audit_cognito_subscriber.py`
  - Unit test for the subscriber handler. Feeds a synthetic CloudTrail-shaped event; asserts the PutItem call and the row shape.

**Untouched (off-limits or out of scope):**

- `Infra/templates/04-storage.yaml` — table schema unchanged.
- `Infra/templates/09-agentcore.yaml` — agent IAM unchanged.
- `Infra/templates/03-identity.yaml` — Cognito pool unchanged (we only reference its exported ID).
- `ui/src/pages/AuditLogs.jsx` — page already handles unknown action_types via slate fallback (spec §4 / AC7).
- `agents/_shared/token_usage.py` and the `MODEL_PRICING` constants.

## 5. Risks and mitigations

1. **CloudTrail-to-EventBridge latency (2-15 min, occasionally longer).**
   Mitigation: documented in the spec and the research brief; the harness team
   widens the brute-force probe window separately. The rows still land, just
   not within 5 seconds.
2. **Audit-write fan-out under brute-force burst.** A botnet-scale flood fans
   out 1:1 to audit rows. Acceptable at demo scale (four users, no public
   sign-up). Mitigation if it ever bites: a 1-row-per-username-per-60s
   in-memory dedupe in the subscriber. Not implemented in v1.
3. **Subscriber Lambda becomes a new silent-failure surface.** A bug here
   means missed audit rows with no signal. Mitigation: CloudWatch log group
   (`RetentionInDays: 30`) and WARNING-level logs on every exception path.
   Threshold-based alarms are an explicit non-goal.
4. **CMK access misconfigured.** `PutItem` succeeds at the SDK level but the
   row never lands when the role lacks `GenerateDataKey` on the DynamoDB CMK.
   Documented gotcha. Mitigation: AC9 in the spec verifies the IAM scope; the
   role policy declares the CMK ARN explicitly, no wildcard.
5. **EventBridge rule pattern drift.** AWS occasionally renames CloudTrail
   fields. A silent mismatch would stop the rule from firing. Mitigation:
   manual smoke test after any Cognito / CloudTrail-side update (task 10).
6. **Lambda outside VPC vs project norm.** `05-compute.yaml::Globals.VpcConfig`
   places every function in `PrivateSubnet1`. We override to empty for this
   one Lambda; reviewers should not "fix" that back. The `VpcConfig` override
   block in this function definition is intentional.
7. **CloudTrail-redacted usernames.** Some Cognito errors omit the attempted
   username. The handler falls back to `additionalEventData.sub` and finally
   `"unknown"`. Accepted v1 behavior (spec Risk #5).
8. **Same-microsecond event_id collision.** Two events with identical
   `username` + microsecond stamp overwrite. Vanishingly rare at demo scale
   (spec Risk #4); same property already holds for the existing writer.

## 6. Out of scope (do not drift)

The spec is explicit and this plan honours it:

- No JWT signature verification on the Function URL `/chat` path.
- No GSI on the audit-log table — `04-storage.yaml` is untouched.
- No edit to `ui/src/pages/AuditLogs.jsx`.
- No Cognito Threat Protection / Advanced Security Features.
- No real-time alarms (CloudWatch Alarm / SNS) on `AUTH_FAILED` bursts.
- No de-duplication of audit rows.
- No backfill of `ttl` on existing rows.

## 7. Task checklist

Ordered. Each task small enough to ship and verify on its own. The system
stays runnable after every step (Piece A and Piece B are independent — Piece A
ships first because it is the smaller change and unlocks two harness scenarios
immediately).

- [x] **Task 1 — Audit the `_require_ciso` exit points.**
  Read `api_handler.py:542-552` and confirm the only 4xx return is the single
  `_err(403, ...)` at line 551. Note the call sites at lines 767 and 783.
  **Check:** A short comment in this plan (or the implementer's PR
  description) lists the exact line numbers, confirming there is only one 403
  return path to instrument.
  _Done:_ Confirmed only one 4xx return — `_err(403, ...)` at api_handler.py:551 (function spans 542-552). Call sites at lines 811 and 827 (post-token-tracking edits).

- [x] **Task 2 — Add `ttl_seconds` to `_audit(...)`.**
  Modify the writer at `api_handler.py:1723` to accept an optional
  `ttl_seconds: int | None = None`. When provided, add `ttl = int(time.time())
  + ttl_seconds` to the `Item`. Other call sites pass nothing and behave
  identically. **Check:** Existing unit tests in `tests/unit/test_api_handler.py`
  still pass (`pytest tests/unit/test_api_handler.py -q`).
  _Done:_ Added optional `ttl_seconds` kwarg + `import time`; existing callers unchanged. Existing tests still pass.

- [x] **Task 3 — Add the `CROSS_PERSONA` write inside `_require_ciso`.**
  Insert the call shown in §3 before the 403 return. Read claims, build the
  details dict, pass `ttl_seconds=7_776_000`. **Check:** A new unit test
  `tests/unit/test_audit_cross_persona.py` mounts moto, sends a SOC-token
  request to `GET /token-usage`, asserts the response is 403 and exactly one
  `CROSS_PERSONA` row is written with `status=DENIED`,
  `details.required_group="ciso"`, `details.caller_groups=["soc"]`, and a
  numeric `ttl` ≈ now + 90 days (±10 s tolerance).
  _Done:_ `_require_ciso` now writes a best-effort `CROSS_PERSONA` row (path, method, required_group, caller_groups, caller_sub, source_ip) with 90-day TTL before the 403.

- [x] **Task 4 — Add the forged-token test case.**
  In the same test file, build a JWT with `cognito:groups=["harness-canary"]`
  but a real-looking `sub` + `email`, send to `GET /token-usage`, assert one
  `CROSS_PERSONA` row with `details.caller_groups` containing
  `"harness-canary"`. **Check:** New test passes; no `FORGED_TOKEN` action_type
  appears anywhere in the codebase (`rg FORGED_TOKEN` returns nothing).
  _Done:_ Added forged-`cognito:groups` test plus a DDB-failure best-effort test. No `FORGED_TOKEN` string in production code (only in test docstring + negative assertion).

- [x] **Task 5 — Run the full backend test suite.**
  Verify nothing else regressed. **Check:** `pytest tests/ -q` exits 0 and the
  number of tests is `previous + 2`.
  _Done:_ Pass count rose 76 → 79 (+3, since I added 3 tests vs the plan's 2). Pre-existing 1 failure + 15 errors in `test_agents.py` and `test_chat_no_runtime_arn_returns_503` unchanged (unrelated to this change).

- [x] **Task 6 — Create the subscriber Lambda directory.**
  Create `Infra/functions/audit_cognito_subscriber/__init__.py` (empty) and
  `requirements.txt` (one line: `boto3`). **Check:** Files exist;
  `ls Infra/functions/audit_cognito_subscriber/` shows three entries after
  task 7.
  _Done:_ Created `__init__.py` (empty), `requirements.txt` (boto3 with docs comment), and `handler.py`. Three entries present.

- [x] **Task 7 — Implement the subscriber handler.**
  Drop the handler shown in §3 into
  `Infra/functions/audit_cognito_subscriber/handler.py`. **Check:** A new unit
  test `tests/unit/test_audit_cognito_subscriber.py` feeds a synthetic
  EventBridge event with the documented CloudTrail shape, asserts one PutItem
  call with `action_type="AUTH_FAILED"`, `status="NotAuthorizedException"`,
  `details.source_ip` populated, and `ttl` in the 90-day window.
  _Done:_ Handler reads `AUDIT_LOG_TABLE_NAME` env (fails loudly at import if missing), extracts actor_id via `additionalEventData.userIdentifier` → `requestParameters.username` → `authParameters.USERNAME` → `"unknown"`, writes one AUTH_FAILED row with 90-day TTL, returns `{statusCode:200, body:{written, action_type}}`. Happy-path test passes.

- [x] **Task 8 — Subscriber best-effort test.**
  In the same test file, add a case where the DDB resource is `None` (env var
  unset) and a case where `put_item` raises `ClientError`. The handler returns
  normally in both. **Check:** Both edge cases pass; no exception escapes the
  handler.
  _Done:_ 4 tests total in `test_audit_cognito_subscriber.py`: happy path, missing fields → `unknown` fallback, `ClientError` raise → 200 + warning log mentioning `AUTH_FAILED`, missing env var → import refuses. All pass.

- [x] **Task 9 — Add the IAM role in `02-security.yaml`.**
  Append the `AuditCognitoSubscriberRole` block from §3 plus the matching
  `Outputs` export. **Check:** `aws cloudformation validate-template
  --template-body file://Infra/templates/02-security.yaml --region us-east-1`
  exits 0.
  _Done:_ Added `AuditCognitoSubscriberRole` (assume-role lambda, AWSLambdaBasicExecutionRole managed, inline `AuditCognitoSubscriberInline` with PutItem on audit-log ARN + KMS on `!GetAtt DynamoDBKey.Arn` only). Added `AuditCognitoSubscriberRoleArn` export. `validate-template` returns clean JSON.

- [x] **Task 10 — Add the Lambda + rule + permission in `05-compute.yaml`.**
  Append the four resources from §3. Confirm the `VpcConfig` override is set
  to empty arrays so SAM does not attach the function to a VPC. **Check:**
  `aws cloudformation validate-template --template-body
  file://Infra/templates/05-compute.yaml --region us-east-1` exits 0 and
  `sam validate --template Infra/templates/05-compute.yaml --region us-east-1`
  exits 0.
  _Done:_ Added `AuditCognitoSubscriberFunction` (empty VpcConfig override, py3.13, handler.handler, Role via `Fn::ImportValue`, AUDIT_LOG_TABLE_NAME env), `AuditCognitoSubscriberRule` (EventBridge pattern matching errorCode + userPoolId), `AuditCognitoSubscriberPermission`, `AuditCognitoSubscriberLogGroup`. `validate-template` returns clean JSON.

- [ ] **Task 11 — Deploy to dev.**
  Run `Infra/deploy.sh` (or just `sam deploy` of the two affected stacks).
  Verify the new resources exist. **Check:** `aws cloudformation
  describe-stack-resources --stack-name dev-st21arbiter-poc-security` lists
  `AuditCognitoSubscriberRole`; `aws cloudformation describe-stack-resources
  --stack-name dev-st21arbiter-poc-compute` lists
  `AuditCognitoSubscriberFunction` and `CognitoAuthFailedRule`; `aws events
  describe-rule --name dev-st21arbiter-poc-cognito-auth-failed` shows
  `State=ENABLED`.

- [ ] **Task 12 — Manual CROSS_PERSONA probe.**
  Sign in as `soc_marcus@meridianinsurance.com`, copy the IdToken, curl
  `GET https://<function-url>/token-usage` with `Authorization: Bearer <jwt>`.
  Expect HTTP 403. **Check:** Within 5 s, `aws dynamodb scan --table-name
  dev-st21arbiter-poc-audit-log --filter-expression 'action_type = :t'
  --expression-attribute-values '{":t":{"S":"CROSS_PERSONA"}}' --max-items 5`
  returns at least one row with `status=DENIED` and the SOC user's email in
  `user`.

- [ ] **Task 13 — Manual AUTH_FAILED probe.**
  Run six rapid failed sign-in attempts via the Hosted UI or
  `aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH --client-id
  <id> --auth-parameters USERNAME=fake@example.com,PASSWORD=wrong`. Wait up to
  5 minutes. **Check:** `aws dynamodb scan` filtered on
  `action_type=AUTH_FAILED` returns at least one row with `status` matching the
  CloudTrail error code and `details.source_ip` populated. Inspect the
  subscriber's CloudWatch log group for warnings.

- [ ] **Task 14 — Re-run the harness logging_audit layer.**
  Run the adversarial harness's logging_audit tests. **Check:** The two
  API-layer scenarios (cross-persona, forged-token) flip to PASS. The
  brute-force scenario remains a known failure at the harness's 5-second
  window (harness team widens its window separately).

- [ ] **Task 15 — Verify IAM least-privilege.**
  `aws iam get-role-policy --role-name dev-st21arbiter-poc-audit-cognito-
  subscriber-role --policy-name AuditCognitoSubscriberPolicy`. **Check:**
  Output shows `dynamodb:PutItem` on the audit-log ARN only and KMS actions
  on the DynamoDB CMK ARN only; no `"Resource": "*"` outside the AWS-managed
  basic-execution policy.

- [ ] **Task 16 — Verify off-limits files untouched.**
  **Check:** `git diff main -- Infra/templates/04-storage.yaml
  Infra/templates/09-agentcore.yaml Infra/templates/03-identity.yaml
  ui/src/pages/AuditLogs.jsx` is empty.

- [ ] **Task 17 — Bump `APP_VERSION` per project convention.**
  Increment `APP_VERSION` in `ui/src/config.js`. **Check:** Git diff shows a
  single-line bump.

- [ ] **Task 18 — Update spec if implementation revealed minor decisions.**
  Touch up `Documents/audit_log_security_events_spec.md` only if a concrete
  field name or shape drifted during build. **Check:** Spec still reflects
  reality; no contradictions with the shipped code.
