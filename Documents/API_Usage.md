# Invoking the Master Orchestrator via API

Two client variations are documented:

1. **Postman client on desktop** — headless sign-in via Cognito `USER_PASSWORD_AUTH`.
2. **Another SPA hosted on CloudFront in the same AWS account** — Authorization Code + PKCE via the Cognito Hosted UI.

Both variations call the **same backend entry point**: the master orchestrator is fronted by the `api_handler` Lambda's `POST /chat` route ([api_handler.py:71-72](../Infra/functions/api_handler/api_handler.py#L71-L72)), which then performs `bedrock-agentcore:InvokeAgentRuntime` against `MASTER_AGENT_RUNTIME_ARN` ([api_handler.py:120-130](../Infra/functions/api_handler/api_handler.py#L120-L130)). Only authentication and CORS differ between the two callers.

---

## Endpoints in this account

Pull the actual deployed values:

```bash
aws cloudformation list-exports --region us-east-1 \
  --query 'Exports[?contains(Name, `st21arbiter-poc`) && (contains(Name, `ApiEndpoint`) || contains(Name, `ChatFunctionUrl`) || contains(Name, `UserPoolId`) || contains(Name, `UserPoolClientId`) || contains(Name, `CognitoDomain`))].[Name,Value]' \
  --output table
```

You will use:

| Export | Use |
|---|---|
| `ApiEndpoint` → `https://<apiId>.execute-api.us-east-1.amazonaws.com/dev` | REST API Gateway, Cognito JWT authorizer, **29 s integration cap**. Fine for `/findings`, `/conversations`, etc. |
| `ChatFunctionUrl` → `https://<urlid>.lambda-url.us-east-1.on.aws/` | Lambda Function URL, `AuthType=NONE` on the URL, JWT decoded in code, **up to 15 min**. Use this for `/chat` ([api_handler.py:308-333](../Infra/functions/api_handler/api_handler.py#L308-L333)). |
| `UserPoolId`, `UserPoolClientId`, `CognitoDomain` | Required for token acquisition. |

No backend IAM/permission change is needed for either variation — the `api_handler` Lambda's role already holds `bedrock-agentcore:InvokeAgentRuntime` ([02-security.yaml:123](../Infra/templates/02-security.yaml#L123)). All you wire up is the **client** side.

---

## Variation 1 — Postman on desktop (step-by-step setup)

End state: a Postman collection with two requests — `Get IdToken` (Cognito auth) and `Chat` (calls the master orchestrator) — sharing a single set of collection variables.

The SPA Cognito client allows `USER_PASSWORD_AUTH` ([03-identity.yaml:79-82](../Infra/templates/03-identity.yaml#L79-L82)), so Postman can sign in headlessly without a browser/PKCE round-trip.

---

### Step 1 — Collect five values from AWS

Run these four commands from a shell that's authenticated to account `669810405473`. Write each value down — you'll paste them into Postman in Step 4.

**1a. Cognito User Pool ID**

```bash
aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-UserPoolId'].Value" \
  --output text
```

Example: `us-east-1_AbCdEfGhI`

**1b. Cognito App Client ID**

```bash
aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-UserPoolClientId'].Value" \
  --output text
```

Example: `4q8m9p3rj7s5tv2w1x0y6z3a4b`

**1c. Chat endpoint (Lambda Function URL — use this for `/chat`)**

```bash
aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?Name=='dev-st21arbiter-poc-ChatFunctionUrl'].Value" \
  --output text
```

Example: `https://abcdef123456.lambda-url.us-east-1.on.aws/`

**1d. Region**: `us-east-1` (hard-coded throughout this project).

**1e. Test user credentials**: one of the four seeded users — e.g. `soc_marcus@example.com` — plus the `DEMO_PASSWORD` that was exported when [`deploy.sh`](../Infra/deploy.sh) was run.

---

### Step 2 — Make sure the test user can actually sign in

New Cognito users sit in `FORCE_CHANGE_PASSWORD` state until promoted. The Hosted UI is misleading here — it returns "Invalid username or password" rather than telling you the truth. Run this once per user:

```bash
aws cognito-idp admin-set-user-password --region us-east-1 \
  --user-pool-id <UserPoolId-from-1a> \
  --username soc_marcus@example.com \
  --password '<DEMO_PASSWORD>' \
  --permanent
```

Confirm the user is now `CONFIRMED`:

```bash
aws cognito-idp admin-get-user --region us-east-1 \
  --user-pool-id <UserPoolId-from-1a> \
  --username soc_marcus@example.com \
  --query 'UserStatus' --output text
```

Expected: `CONFIRMED`.

---

### Step 3 — Create a Postman collection

In Postman:

1. **Collections** sidebar → **+** → name it `ARBITER`.
2. Click the collection → **Variables** tab → add these five rows. Leave `idToken` blank; the auth request fills it.

| Variable | Initial value | Current value |
|---|---|---|
| `userPoolRegion` | `us-east-1` | `us-east-1` |
| `cognitoClientId` | *(paste from 1b)* | *(same)* |
| `username` | `soc_marcus@example.com` | *(same)* |
| `password` | *(paste DEMO_PASSWORD)* | *(same)* |
| `chatUrl` | *(paste from 1c, keep trailing slash)* | *(same)* |
| `idToken` | *(leave blank)* | *(leave blank)* |

3. **Save** the collection.

---

### Step 4 — Add the `Get IdToken` request

1. Right-click the `ARBITER` collection → **Add request** → name it `Get IdToken`.
2. **Method**: `POST`
3. **URL**: `https://cognito-idp.{{userPoolRegion}}.amazonaws.com/`
4. **Headers** tab — add these two rows:

   | Key | Value |
   |---|---|
   | `Content-Type` | `application/x-amz-json-1.1` |
   | `X-Amz-Target` | `AWSCognitoIdentityProviderService.InitiateAuth` |

5. **Body** tab → **raw** → **JSON**:

   ```json
   {
     "AuthFlow": "USER_PASSWORD_AUTH",
     "ClientId": "{{cognitoClientId}}",
     "AuthParameters": {
       "USERNAME": "{{username}}",
       "PASSWORD": "{{password}}"
     }
   }
   ```

6. **Tests** tab — paste this script. It runs after the response arrives and stashes the IdToken into the collection variable so the next request can use it:

   ```js
   const json = pm.response.json();
   pm.test("auth succeeded", () => pm.response.to.have.status(200));
   pm.collectionVariables.set("idToken", json.AuthenticationResult.IdToken);
   console.log("IdToken cached. Expires in", json.AuthenticationResult.ExpiresIn, "seconds.");
   ```

7. **Save** the request → click **Send**.

Expected status `200`. The response JSON contains `AuthenticationResult.IdToken`, `AccessToken`, `RefreshToken`. The `Tests` script writes `IdToken` to `{{idToken}}`. Verify by opening the collection's **Variables** tab and checking that `idToken` now has a long JWT value (three dot-separated base64 segments).

> Tokens are valid for 1 hour ([03-identity.yaml:100-101](../Infra/templates/03-identity.yaml#L100-L101)). Re-run `Get IdToken` whenever `Chat` starts returning 401.

---

### Step 5 — Add the `Chat` request (calls the master orchestrator)

1. Right-click the `ARBITER` collection → **Add request** → name it `Chat`.
2. **Method**: `POST`
3. **URL**: `{{chatUrl}}chat`

   The `chatUrl` collection variable already ends with `/`, so just append `chat`. The full URL looks like `https://abcdef123456.lambda-url.us-east-1.on.aws/chat`. The Function URL forwards the request path verbatim to the Lambda as `rawPath`; the router in [api_handler.py:64-96](../Infra/functions/api_handler/api_handler.py#L64-L96) only matches `/chat` — calling the root `/` falls through to a `{"status":"stub", ...}` response.
4. **Headers** tab:

   | Key | Value |
   |---|---|
   | `Authorization` | `Bearer {{idToken}}` |
   | `Content-Type` | `application/json` |

5. **Body** tab → **raw** → **JSON**:

   ```json
   {
     "prompt": "Are SharePoint and Zscaler policies aligned on github.com access for engineering?",
     "session_id": "postman-2026-05-26-001",
     "chat_type": "analyst"
   }
   ```

6. **Save** → **Send**.

Expected response (shape from [api_handler.py:131-136](../Infra/functions/api_handler/api_handler.py#L131-L136)):

```json
{
  "reply": "…orchestrator answer with citations across SharePoint / AWS Config / Zscaler…",
  "session_id": "postman-2026-05-26-001"
}
```

First call may take 20–60 s — the master fans out to three specialist runtimes. Subsequent calls within the same `session_id` are faster and pick up conversation memory ([agent.py:139-199](../agents/master_orchestrator/agent.py#L139-L199)).

---

### Step 6 — Verify the call landed end-to-end (CloudWatch tail)

In a separate shell, tail the Lambda log and re-send the `Chat` request:

```bash
aws logs tail /aws/lambda/dev-st21arbiter-poc-api-handler \
  --region us-east-1 --since 5m --follow
```

You should see lines like:

```
api_handler invoked: path=/chat method=POST headers=[...]
Orchestrator invoked: actor=<sub> session=postman-2026-05-26-001 chat_type=analyst prompt=...
```

The `sub` claim in the log line should match the JWT sub of the user you signed in as (you can decode `{{idToken}}` at jwt.io if you want to confirm). If the sub differs, requests for that session won't be readable by your user — see the ownership note in Step 7.

---

### Step 7 — Conversation history endpoints

Three GET endpoints expose what the master persisted ([api_handler.py:200-304](../Infra/functions/api_handler/api_handler.py#L200-L304)):

| Endpoint | Returns | Source |
|---|---|---|
| `GET /conversations` | List of the caller's sessions (newest first, max 50) | DDB `sessions` table, queried by GSI `user-sessions-index` keyed on `user_id` |
| `GET /conversations/{session_id}` | Single session's metadata (title, counts, timestamps) | DDB `get_item` on `sessions` |
| `GET /conversations/{session_id}/messages` | Full chronological message list (`user` + `assistant` turns) | AgentCore Memory `list_events`, ownership-checked against DDB first |

All three:

- Require `Authorization: Bearer {{idToken}}` (same Cognito JWT as `/chat`).
- Resolve the caller via `sub` from the decoded JWT ([api_handler.py:308-333](../Infra/functions/api_handler/api_handler.py#L308-L333)).
- **Enforce ownership** — a session is only visible to the JWT `sub` that created it. Switching users in Step 4 makes prior sessions invisible.

Send `Chat` at least once in Step 5 before testing these, otherwise the list is empty.

---

#### 7a — `List conversations`

1. Right-click the `ARBITER` collection → **Add request** → name it `List conversations`.
2. **Method**: `GET`
3. **URL**: `{{chatUrl}}conversations`
4. **Headers** tab:

   | Key | Value |
   |---|---|
   | `Authorization` | `Bearer {{idToken}}` |

5. **Params** tab (optional) — filter by chat surface:

   | Key | Value | Notes |
   |---|---|---|
   | `type` | `analyst` | Or `mcp`. Omit to return everything. Matches `chat_type` set by the master at first-turn write ([agent.py:245-273](../agents/master_orchestrator/agent.py#L245-L273)). |

6. **Save** → **Send**.

Expected response:

```json
{
  "sessions": [
    {
      "session_id": "postman-2026-05-26-001",
      "title": "Are SharePoint and Zscaler policies aligned on github…",
      "created_at": "2026-05-26T13:42:11.034000+00:00",
      "last_message_at": "2026-05-26T13:42:38.221000+00:00",
      "message_count": 2,
      "chat_type": "analyst"
    }
  ]
}
```

`message_count` increments by 2 per turn (one user + one assistant message — see [agent.py:340-343](../agents/master_orchestrator/agent.py#L340-L343)).

**Optional Tests script** — captures the first session ID into a collection variable so the next two requests can reuse it:

```js
const json = pm.response.json();
pm.test("got at least one session", () => pm.expect(json.sessions.length).to.be.above(0));
pm.collectionVariables.set("sessionId", json.sessions[0].session_id);
console.log("sessionId cached:", json.sessions[0].session_id);
```

Add `sessionId` to the collection variables (Step 3) with a blank initial value.

---

#### 7b — `Get conversation metadata`

1. **Add request** → name it `Get conversation`.
2. **Method**: `GET`
3. **URL**: `{{chatUrl}}conversations/{{sessionId}}`
4. **Headers**:

   | Key | Value |
   |---|---|
   | `Authorization` | `Bearer {{idToken}}` |

5. **Save** → **Send**.

Expected response:

```json
{
  "session_id": "postman-2026-05-26-001",
  "title": "Are SharePoint and Zscaler policies aligned on github…",
  "created_at": "2026-05-26T13:42:11.034000+00:00",
  "last_message_at": "2026-05-26T13:42:38.221000+00:00",
  "message_count": 2,
  "chat_type": "analyst"
}
```

`404 "Session ... not found"` means either the session ID is wrong, or the session belongs to a different user (the ownership check in [api_handler.py:245-247](../Infra/functions/api_handler/api_handler.py#L245-L247) rejects cross-user reads with 404 to avoid leaking existence).

---

#### 7c — `Get messages`

1. **Add request** → name it `Get messages`.
2. **Method**: `GET`
3. **URL**: `{{chatUrl}}conversations/{{sessionId}}/messages`
4. **Headers**:

   | Key | Value |
   |---|---|
   | `Authorization` | `Bearer {{idToken}}` |

5. **Save** → **Send**.

Expected response (chronological, oldest first):

```json
{
  "session_id": "postman-2026-05-26-001",
  "messages": [
    {
      "role": "user",
      "content": "Are SharePoint and Zscaler policies aligned on github.com access for engineering?",
      "ts": "2026-05-26T13:42:11.034000+00:00"
    },
    {
      "role": "assistant",
      "content": "…orchestrator answer with citations…",
      "ts": "2026-05-26T13:42:38.221000+00:00"
    }
  ]
}
```

Up to 100 messages per call (no pagination implemented; see `maxResults=100` in [api_handler.py:283-289](../Infra/functions/api_handler/api_handler.py#L283-L289)). For longer conversations, the older turns get rolled into a summary record by AgentCore Memory's summarization strategy, asynchronously — they don't show here but the master agent still sees them via `retrieve_memory_records` on the next `/chat` call ([agent.py:175-188](../agents/master_orchestrator/agent.py#L175-L188)).

---

### End-to-end smoke loop

Run the collection in this order to confirm everything is wired:

1. `Get IdToken` — caches `{{idToken}}`.
2. `Chat` — creates session `postman-…-001`, returns the agent's reply.
3. `List conversations` — should include the new session; caches `{{sessionId}}`.
4. `Get conversation` — returns metadata for that session.
5. `Get messages` — returns 2 messages (the prompt + the reply).
6. `Chat` again with the **same** `session_id` — `message_count` rises by 2, master picks up the prior turn from Memory.

---

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Get IdToken` returns `NotAuthorizedException: Password attempts exceeded` | Cognito rate-limit on the username | Wait ~15 min, then retry. |
| `Get IdToken` returns `InvalidParameterException: USER_PASSWORD_AUTH flow not enabled` | App client missing the flow | Re-check [03-identity.yaml:79-82](../Infra/templates/03-identity.yaml#L79-L82) — `ALLOW_USER_PASSWORD_AUTH` must be present. |
| `Get IdToken` returns `NotAuthorizedException` even with correct password | User stuck in `FORCE_CHANGE_PASSWORD` | Re-run Step 2's `admin-set-user-password`. |
| `Chat` returns 200 with `{"status":"stub","path":"/","method":"POST"}` | URL is the Function URL root; router didn't match `/chat` | Set the request URL to `{{chatUrl}}chat` (full path `https://<id>.lambda-url.us-east-1.on.aws/chat`). |
| `Chat` returns 503 `"Master runtime ARN not configured"` | Lambda env var not populated | Re-run `scripts/deploy_agents.py` — it patches `MASTER_AGENT_RUNTIME_ARN` on the function. |
| `Chat` returns 401 | IdToken expired (1 hr TTL) | Re-send `Get IdToken`; the Tests script refreshes `{{idToken}}`. |
| `Chat` returns 502 with `AccessDeniedException` in the body | Lambda role lacks `InvokeAgentRuntime` on the master ARN | Check the policy at [02-security.yaml:123](../Infra/templates/02-security.yaml#L123). |
| Postman shows `{{idToken}}` literally in the request preview | Variable scope wrong (saved in environment instead of collection, or the Tests script didn't run) | Confirm the variable lives on the **collection**, and that `Get IdToken` returned 200 before sending `Chat`. |
| `List conversations` returns `{"sessions": []}` | No `/chat` call yet, or `session_id` was `"adhoc"` (which the master deliberately doesn't persist — [agent.py:323-329](../agents/master_orchestrator/agent.py#L323-L329)) | Send `Chat` with an explicit `session_id` in the body. |
| `Get conversation` / `Get messages` returns `404 "Session ... not found"` | JWT `sub` doesn't match the session's `user_id` — most often caused by switching `username` in Step 4 between writes and reads | Re-run `Get IdToken` as the same user that created the session. |
| `Get messages` returns `500 "MEMORY_ID not configured"` | Lambda env var not populated | Re-run `scripts/deploy_agents.py` — it patches both `MASTER_AGENT_RUNTIME_ARN` and `MEMORY_ID`. |
| `Get messages` returns `{"messages": []}` despite a successful `Chat` | Memory write failed silently (warn-logged, never raised — [agent.py:202-219](../agents/master_orchestrator/agent.py#L202-L219)) | Check Lambda + master runtime logs for `create_event failed`. Confirm AgentCore role's `KMSDecrypt` includes `DynamoDBKeyArn`. |

---

### Permissions needed — Variation 1

- **AWS side**: nothing beyond what's already provisioned. The Function URL is `AuthType=NONE` and the Lambda role already has `bedrock-agentcore:InvokeAgentRuntime`.
- **User side**: a Cognito user that exists, belongs to a persona group (`ciso`/`soc`/`grc`/`employee`), and has a permanent password.
- **Optional hardening**: switch the Function URL to `AuthType=AWS_IAM` and sign requests in Postman with **SigV4** (Postman → Auth → AWS Signature) using an IAM principal that has `lambda:InvokeFunctionUrl` on `arn:aws:lambda:us-east-1:669810405473:function:dev-st21arbiter-poc-api-handler`. Drops the Cognito flow entirely.

---

### Alternative — bypass the Lambda and call AgentCore Runtimes directly

Why you'd do this: isolate a single agent for debugging, compare specialist outputs without the master in front, or benchmark per-agent latency.

**AgentCore Runtime requires SigV4 IAM auth — a Cognito JWT will not work here.** Cognito only protects the API plane (API Gateway + Lambda); the AgentCore data plane is plain AWS IAM.

The project provisions four runtimes, each invokable independently:

| Agent | Purpose | Payload schema |
|---|---|---|
| `master_orchestrator` | Routes a question to the three specialists, aggregates findings, returns a final analysis | `{"prompt", "session_id", "actor_id", "chat_type"}` ([agent.py:309-318](../agents/master_orchestrator/agent.py#L309-L318)) |
| `sharepoint_specialist` | Looks up policy docs in the Bedrock KB | `{"prompt"}` ([sharepoint agent.py:93-95](../agents/sharepoint_specialist/agent.py#L93-L95)) |
| `awsconfig_specialist` | Looks up AWS Config compliance state | `{"prompt"}` ([awsconfig agent.py:202-204](../agents/awsconfig_specialist/agent.py#L202-L204)) |
| `zscaler_specialist` | Looks up ZIA URL allowlist / category policy | `{"prompt"}` ([zscaler agent.py:134-136](../agents/zscaler_specialist/agent.py#L134-L136)) |

All four return `{"result": "<text>"}` on success.

---

### Step A1 — Discover the four runtime ARNs

```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[?contains(agentRuntimeName, `st21arbiter_poc`)].[agentRuntimeName,agentRuntimeArn,status]' \
  --output table
```

You should see four rows, all `READY`. Note each ARN — the format is
`arn:aws:bedrock-agentcore:us-east-1:669810405473:runtime/<runtime_name>-<10charHash>`.

For repeated CLI use, stash them in shell vars:

```bash
list_arn() {
  aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
    --query "agentRuntimes[?contains(agentRuntimeName, '$1')] | [0].agentRuntimeArn" \
    --output text
}
MASTER_ARN=$(list_arn master_orchestrator)
SP_ARN=$(list_arn sharepoint_specialist)
AC_ARN=$(list_arn awsconfig_specialist)
ZS_ARN=$(list_arn zscaler_specialist)
```

---

### Step A2 — Grant your IAM principal `InvokeAgentRuntime`

Attach this inline policy to the IAM user/role you'll call from (your local CLI profile, or the Postman SigV4 credentials):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "InvokeArbiterRuntimes",
    "Effect": "Allow",
    "Action": "bedrock-agentcore:InvokeAgentRuntime",
    "Resource": [
      "arn:aws:bedrock-agentcore:us-east-1:669810405473:runtime/st21arbiter_poc_master_orchestrator-*",
      "arn:aws:bedrock-agentcore:us-east-1:669810405473:runtime/st21arbiter_poc_sharepoint_specialist-*",
      "arn:aws:bedrock-agentcore:us-east-1:669810405473:runtime/st21arbiter_poc_awsconfig_specialist-*",
      "arn:aws:bedrock-agentcore:us-east-1:669810405473:runtime/st21arbiter_poc_zscaler_specialist-*"
    ]
  }]
}
```

Verify:

```bash
aws sts get-caller-identity
# Then dry-run one invocation (see Step A3); should NOT return AccessDeniedException.
```

---

### Step A3 — Method 1: AWS CLI (recommended)

Simpler than Postman — no URL-encoding the ARN, no manual SigV4. Generic shape:

```bash
echo '<json-body>' > /tmp/payload.json

aws bedrock-agentcore invoke-agent-runtime --region us-east-1 \
  --agent-runtime-arn "$ARN" \
  --runtime-session-id "$(uuidgen)" \
  --payload fileb:///tmp/payload.json \
  --content-type application/json \
  --accept application/json \
  /tmp/response.json

cat /tmp/response.json | jq -r '.result'
```

Notes:
- `--payload` requires `fileb://` (binary file URI), not raw JSON.
- `--runtime-session-id` is the **runtime-container session** (affects warm-start affinity) — distinct from the master's `session_id` in the payload (memory key). Use a fresh UUID per call unless you want to reuse a warm container.
- The final positional arg is the output file — the CLI streams the response body there.

#### A3.1 — Master orchestrator

```bash
cat > /tmp/payload.json <<'EOF'
{
  "prompt": "Are SharePoint and Zscaler policies aligned on github.com access for engineering?",
  "session_id": "cli-direct-2026-05-26-001",
  "actor_id": "cli-direct-test",
  "chat_type": "analyst"
}
EOF

aws bedrock-agentcore invoke-agent-runtime --region us-east-1 \
  --agent-runtime-arn "$MASTER_ARN" \
  --runtime-session-id "$(uuidgen)" \
  --payload fileb:///tmp/payload.json \
  --content-type application/json \
  --accept application/json \
  /tmp/response.json

jq -r '.result' /tmp/response.json
```

This runs the full fan-out (master → 3 specialists → aggregate). Takes 20–60 s. Persists to AgentCore Memory + the DDB `sessions` table under `actor_id="cli-direct-test"`. (Don't reuse a real Cognito user's `sub` here — it would interleave with their UI sessions.)

#### A3.2 — SharePoint specialist (isolated)

```bash
cat > /tmp/payload.json <<'EOF'
{ "prompt": "What does the remote-work URL policy say about public code repositories?" }
EOF

aws bedrock-agentcore invoke-agent-runtime --region us-east-1 \
  --agent-runtime-arn "$SP_ARN" \
  --runtime-session-id "$(uuidgen)" \
  --payload fileb:///tmp/payload.json \
  --content-type application/json \
  --accept application/json \
  /tmp/response.json

jq -r '.result' /tmp/response.json
```

Returns text citing one or more policy PDFs from `s3://dev-st21arbiter-poc-processed/` via the Bedrock KB.

#### A3.3 — AWS Config specialist (isolated)

```bash
cat > /tmp/payload.json <<'EOF'
{ "prompt": "Which S3 buckets are non-compliant with encryption-at-rest rules?" }
EOF

aws bedrock-agentcore invoke-agent-runtime --region us-east-1 \
  --agent-runtime-arn "$AC_ARN" \
  --runtime-session-id "$(uuidgen)" \
  --payload fileb:///tmp/payload.json \
  --content-type application/json \
  --accept application/json \
  /tmp/response.json

jq -r '.result' /tmp/response.json
```

Returns Config rule findings (mock data in dev, sourced from `BaselineFiles/` KB ingestion).

#### A3.4 — Zscaler specialist (isolated)

```bash
cat > /tmp/payload.json <<'EOF'
{ "prompt": "Is github.com allowed for the engineering URL category?" }
EOF

aws bedrock-agentcore invoke-agent-runtime --region us-east-1 \
  --agent-runtime-arn "$ZS_ARN" \
  --runtime-session-id "$(uuidgen)" \
  --payload fileb:///tmp/payload.json \
  --content-type application/json \
  --accept application/json \
  /tmp/response.json

jq -r '.result' /tmp/response.json
```

Returns the ZIA URL-allowlist / category decision text.

---

### Step A3 — Method 2: Postman with SigV4

Use this if you want to stay in Postman. The URL needs the runtime ARN **URL-encoded into the path**, which is the only fiddly part.

#### One-time collection setup

1. Add these collection variables to `ARBITER`:

   | Variable | Value |
   |---|---|
   | `awsAccessKey` | IAM access key for the principal from Step A2 |
   | `awsSecretKey` | IAM secret key (mark as **secret**) |
   | `awsRegion` | `us-east-1` |
   | `masterArn` | full ARN from Step A1 |
   | `sharepointArn` | full ARN |
   | `awsconfigArn` | full ARN |
   | `zscalerArn` | full ARN |

   > Per-request, you'll paste the **URL-encoded** ARN into the URL — Postman variables don't auto-encode path segments. Encode each ARN once at https://www.urlencoder.org (or `python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' "$ARN"`) and store the encoded forms as `masterArnEnc`, `sharepointArnEnc`, etc.

2. For each request below: **Authorization** tab → **Type: AWS Signature** → set:
   - **AccessKey**: `{{awsAccessKey}}`
   - **SecretKey**: `{{awsSecretKey}}`
   - **AWS Region**: `{{awsRegion}}`
   - **Service Name**: `bedrock-agentcore`

3. Headers (same for all four requests):

   | Key | Value |
   |---|---|
   | `Content-Type` | `application/json` |
   | `Accept` | `application/json` |
   | `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` | any string ≤ 256 chars, e.g. `postman-direct-001` |

#### Per-agent requests

All are `POST` to `https://bedrock-agentcore.{{awsRegion}}.amazonaws.com/runtimes/<encodedArn>/invocations`.

| Request name | URL (path tail) | Body |
|---|---|---|
| `Direct - Master` | `…/runtimes/{{masterArnEnc}}/invocations` | `{"prompt":"…","session_id":"postman-direct-001","actor_id":"postman-direct","chat_type":"analyst"}` |
| `Direct - SharePoint` | `…/runtimes/{{sharepointArnEnc}}/invocations` | `{"prompt":"What does the remote-work URL policy say about public code repos?"}` |
| `Direct - AWS Config` | `…/runtimes/{{awsconfigArnEnc}}/invocations` | `{"prompt":"Which S3 buckets are non-compliant with encryption-at-rest?"}` |
| `Direct - Zscaler` | `…/runtimes/{{zscalerArnEnc}}/invocations` | `{"prompt":"Is github.com allowed for the engineering URL category?"}` |

Expected response (all four):

```json
{ "result": "…agent answer…" }
```

#### Direct-invoke troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `403 AccessDeniedException ... bedrock-agentcore:InvokeAgentRuntime` | IAM principal missing the policy from Step A2 | Re-attach the policy; confirm the `Resource` ARN pattern matches the runtime you're calling. |
| `404` or `ValidationException: ... agentRuntimeArn` | Bad ARN, or ARN not URL-encoded in the path | Recompute `*ArnEnc` — both `:` and `/` must be percent-encoded. |
| `400 InvalidSignatureException` | Postman SigV4 region/service mismatch, or clock skew | Service must be `bedrock-agentcore` (with the hyphen). Sync your machine clock. |
| Empty `result` from a specialist | KB ingestion job never ran | Run `aws bedrock-agent start-ingestion-job ...` (Step 5 of DEPLOYMENT.md). |
| Master returns `(specialist runtime not configured)` inside `result` | Specialist runtime ARN env var unset on the master container | Re-run `scripts/deploy_agents.py` to refresh `SHAREPOINT_RUNTIME_ARN`/`AWSCONFIG_RUNTIME_ARN`/`ZSCALER_RUNTIME_ARN`. |

---

## Variation 2 — Another SPA hosted on CloudFront in the same account

Cleanest path: reuse the same Cognito User Pool and add a **second app client + a CloudFront distribution** for the new UI. No new backend code, no new IAM.

### Step 1 — Add a second Cognito app client

Choose one:

- **Reuse the existing SPA client** by appending the new CloudFront callback to the `CallbackURLs` list in [03-identity.yaml:92-95](../Infra/templates/03-identity.yaml#L92-L95). Simplest, but mixes audit-log `clientId`s.
- **Add a new `AWS::Cognito::UserPoolClient`** named `…-spa-client-app2` with `GenerateSecret: false`, the same `ExplicitAuthFlows` and `AllowedOAuth*`, and callback `https://<new-distribution>.cloudfront.net/callback`. **Recommended** — clean separation, independent revoke.

Always go through `aws cloudformation validate-template` and the change-set flow in `deploy.sh` — don't hand-edit via console.

### Step 2 — Wire the CloudFront distribution to the same backend

Mirror [10-ui-hosting](../Infra/templates/) for the new UI: private S3 origin + CloudFront with OAC. The new UI's environment file points at the **existing** API:

```
VITE_API_URL=https://<apiId>.execute-api.us-east-1.amazonaws.com/dev
VITE_CHAT_URL=https://<urlid>.lambda-url.us-east-1.on.aws/
VITE_COGNITO_USER_POOL_ID=<UserPoolId>
VITE_COGNITO_CLIENT_ID=<NewClientId>
VITE_COGNITO_REGION=us-east-1
```

### Step 3 — Sign-in flow in the new UI

Use Authorization Code + PKCE through the Hosted UI (same pattern as [useAuth.js](../ui/src/hooks/useAuth.js)). Watch out for:

- **StrictMode double-fire**: gate the token-exchange POST with a module-level in-flight promise, exactly like `handleCallback` in [useAuth.js](../ui/src/hooks/useAuth.js). Otherwise the second fire reuses the auth code and Cognito returns 400.
- **No client secret** — the SPA client is public.
- **Persona binding** — `cognito:groups` from the IdToken pins the persona ([PersonaContext.jsx](../ui/src/contexts/PersonaContext.jsx)); add new users to one of `ciso`/`soc`/`grc`/`employee` if you want the page guards to behave.

### Step 4 — Call the master orchestrator

Identical to Variation 1: `POST {VITE_CHAT_URL}` with `Authorization: Bearer <IdToken>` and the JSON body shown above. `_caller_user_id` resolves the JWT's `sub` to enforce per-user session ownership ([api_handler.py:308-333](../Infra/functions/api_handler/api_handler.py#L308-L333)).

### CORS — the only piece that needs attention

- `_cors_headers` returns `Access-Control-Allow-Origin: *` ([api_handler.py:380-386](../Infra/functions/api_handler/api_handler.py#L380-L386)) and API Gateway's CORS uses the `AllowedOrigin` parameter (default `*`) ([06-api.yaml:30-33](../Infra/templates/06-api.yaml#L30-L33)). With `*` everywhere, the new CloudFront origin works out of the box.
- For prod, tighten both: set `AllowedOrigin` to the **new** CloudFront domain on the 06-api stack, and harden `_cors_headers` to echo a whitelist of origins instead of `*`. After changing API Gateway CORS, redeploy the stage (the `GatewayResponse` resources require it).

### Permissions needed — Variation 2

- **AWS side**: only the Cognito client/callback edit and the new CloudFront + S3 stack. No new IAM on the Lambda or AgentCore side.
- **CSP / CloudFront response-headers policy** on the new distribution must allow `connect-src` to all of:
  - `https://*.execute-api.us-east-1.amazonaws.com`
  - `https://*.lambda-url.us-east-1.on.aws`
  - `https://cognito-idp.us-east-1.amazonaws.com`
  - `https://<CognitoDomain>.auth.us-east-1.amazoncognito.com`
- **User side**: same Cognito users; optionally a second group set if the new app has different RBAC.

---

## Sanity checks after either path

```bash
# Master runtime is READY
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[?contains(agentRuntimeName, `master`)].[agentRuntimeName,status]' --output table

# Lambda saw the call
aws logs tail /aws/lambda/dev-st21arbiter-poc-api-handler --region us-east-1 --since 5m --follow
```

If `/chat` returns `503 "Master runtime ARN not configured"`, the Lambda env var was never populated — re-run `scripts/deploy_agents.py`, which patches `MASTER_AGENT_RUNTIME_ARN` and `MEMORY_ID` on the function.

---

# Triggering the File-Processing Lambda from a UI

The `processing_pipeline` Lambda runs twice daily on an EventBridge schedule (06:00 / 18:00 PST). It can also be invoked **on demand from any UI** via a dedicated Function URL that mirrors the `/chat` auth pattern — `AuthType=NONE` at the URL level, with the Lambda decoding the Cognito IdToken and verifying the caller's group membership.

## Design

| Aspect | Detail |
|---|---|
| Endpoint | Lambda Function URL on the `processing_pipeline` function. CFN export `dev-st21arbiter-poc-ProcessingPipelineFunctionUrl` ([05-compute.yaml](../Infra/templates/05-compute.yaml)) |
| URL-level auth | `AuthType=NONE` |
| In-code auth | `Authorization: Bearer <Cognito IdToken>` — decoded by `_caller_groups` in [processing_pipeline.py](../Infra/functions/processing_pipeline/processing_pipeline.py) |
| Authorization | Caller must belong to one of `ALLOWED_GROUPS` (CFN default `ciso,grc`). Other personas get 403. |
| Method | `POST /` (Function URL root). Body is ignored — empty `{}` is fine. |
| Timeout | 15 min (Function URL limit; Lambda itself is 900 s) — well above any realistic file-mover run |
| CORS | Lambda emits `Access-Control-Allow-Origin: *` and handles `OPTIONS` preflight |
| Schedule-driven runs | Unchanged — EventBridge events have no `requestContext.http`, so the auth gate is skipped |

## How the Lambda decides whether to authenticate

The handler distinguishes the two invocation paths by looking for `event.requestContext.http`:

```
EventBridge schedule → no http context → skip auth gate, run unconditionally
Function URL HTTP    → http context present → require JWT + group match
```

This means the same code answers both surfaces and you never have to maintain two handlers.

## Endpoints to collect (in this account)

```bash
aws cloudformation list-exports --region us-east-1 \
  --query "Exports[?contains(Name, 'st21arbiter-poc') && (contains(Name, 'ProcessingPipelineFunctionUrl') || contains(Name, 'UserPoolId') || contains(Name, 'UserPoolClientId'))].[Name,Value]" \
  --output table
```

You'll use:

| Export | Use |
|---|---|
| `dev-st21arbiter-poc-ProcessingPipelineFunctionUrl` | `POST` target — the URL the UI hits |
| `dev-st21arbiter-poc-UserPoolId` | Token acquisition |
| `dev-st21arbiter-poc-UserPoolClientId` | Token acquisition |

---

## Variation 1 — Postman on desktop

Mirrors the existing `Chat` flow. If you already have the `ARBITER` collection from earlier in this doc, you can reuse `Get IdToken` verbatim.

### Step 1 — Use a `ciso` or `grc` user

The `ALLOWED_GROUPS` env var on the Lambda defaults to `ciso,grc`. Sign in as one of those two seeded users:

| User | Group | Will it work? |
|---|---|---|
| `ciso_daiana@example.com` | `ciso` | ✅ |
| `grc_priya@example.com` | `grc` | ✅ |
| `soc_marcus@example.com` | `soc` | ❌ 403 |
| `emp_sarah@example.com` | `employee` | ❌ 403 |

If `ciso_daiana` is still in `FORCE_CHANGE_PASSWORD`, promote them:

```bash
aws cognito-idp admin-set-user-password --region us-east-1 \
  --user-pool-id <UserPoolId> \
  --username ciso_daiana@example.com \
  --password '<DEMO_PASSWORD>' --permanent
```

### Step 2 — Add the URL to the collection

Open the `ARBITER` collection's **Variables** tab and add:

| Variable | Initial value | Current value |
|---|---|---|
| `processingUrl` | *(paste from `dev-st21arbiter-poc-ProcessingPipelineFunctionUrl`, keep trailing slash)* | *(same)* |

Also update `username` to `ciso_daiana@example.com` (or `grc_priya@example.com`).

### Step 3 — Re-run `Get IdToken`

The cached `{{idToken}}` is tied to whichever user signed in last. Re-run **Get IdToken** so the cached token belongs to the CISO/GRC user — the new token's `cognito:groups` claim is what the Lambda checks.

### Step 4 — Add the `Trigger Processing` request

1. Right-click the `ARBITER` collection → **Add request** → name it `Trigger Processing`.
2. **Method**: `POST`
3. **URL**: `{{processingUrl}}` (root path — Function URL maps `/` to the handler)
4. **Headers** tab:

   | Key | Value |
   |---|---|
   | `Authorization` | `Bearer {{idToken}}` |
   | `Content-Type` | `application/json` |

5. **Body** tab → **raw** → **JSON**: `{}`
6. **Save** → **Send**.

Expected response on success (200):

```json
{
  "run_id": "abc123def456...",
  "started": "2026-05-27T18:42:11Z",
  "finished": "2026-05-27T18:42:14Z",
  "moved": 3,
  "skipped": 0,
  "failed": 0,
  "report_key": "File_Transfer_Reports/run-2026-05-27T18-42-11Z-abc123de.csv"
}
```

### Step 5 — Verify the report and the moved files

```bash
# CSV report in raw/File_Transfer_Reports/
aws s3 ls s3://dev-st21arbiter-poc-raw/File_Transfer_Reports/ --region us-east-1

# Read the latest report
aws s3 cp s3://dev-st21arbiter-poc-raw/File_Transfer_Reports/<latest>.csv - --region us-east-1

# Tail the Lambda log to confirm a manual trigger landed
aws logs tail /aws/lambda/dev-st21arbiter-poc-processing-pipeline --region us-east-1 --since 5m --follow
# Expect: "Manual HTTP trigger: sub=<sub> groups=['ciso']" then "processing_pipeline run started/finished"
```

### Permissions needed — Variation 1

- **AWS side**: none beyond what `deploy.sh` already provisions. The Function URL is created by CFN; the Lambda already has S3 read/write/delete + KMS.
- **User side**: caller is in Cognito group `ciso` or `grc`, with a permanent password.

### Variation 1 — Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 "Missing or invalid Authorization header"` | No / malformed `Authorization` header | Re-run `Get IdToken`; verify the `{{idToken}}` variable populated; confirm header value starts with `Bearer ` |
| `403 "Caller not in allowed groups ['ciso', 'grc']"` with `caller_groups: ['soc']` | Wrong persona signed in | Re-run `Get IdToken` as `ciso_daiana@` or `grc_priya@`; or widen `ALLOWED_GROUPS` env var on the Lambda |
| `403 caller_groups: []` | User belongs to no Cognito groups | `aws cognito-idp admin-add-user-to-group --user-pool-id ... --username ... --group-name ciso` |
| `200 {"status":"stub","path":"/","method":"POST"}` | You hit the **api_handler** Function URL by mistake (which routes by path); the processing-pipeline Function URL accepts root POST and there is no stub route | Confirm `{{processingUrl}}` is the export named `ProcessingPipelineFunctionUrl`, not `ChatFunctionUrl` |
| `502 list_failed` in the response body | Lambda role missing S3 ListBucket on raw | Re-deploy `02-security` (`s3:ListBucket` was already present, so this is rare — check the actual error string for the AccessDenied resource) |
| `200 moved=0 skipped=0 failed=0` | Raw bucket is empty (except for reports prefix) | Expected. Drop a test file: `aws s3 cp ./README.md s3://dev-st21arbiter-poc-raw/smoke/test-$(date +%s).md` and re-trigger |

---

## Variation 2 — Another SPA on CloudFront

Same auth model as the `/chat` Variation 2 above. The new SPA already signs in through the shared Cognito User Pool — to trigger a processing run, it just POSTs to the Function URL with its existing IdToken.

### Step 1 — Add the URL to the SPA's environment

```
VITE_PROCESSING_URL=https://<urlid>.lambda-url.us-east-1.on.aws/
```

### Step 2 — Gate the UI by group (defense in depth — the Lambda re-checks)

In the React app, read `cognito:groups` from the IdToken (same `getGroups()` helper used by `PersonaContext`). Render the "Run File Processing" button **only** when the user is `ciso` or `grc`. The Lambda will still 403 if a forged client tries anyway.

### Step 3 — Fetch hook

```js
export async function triggerProcessing(idToken) {
  const url = import.meta.env.VITE_PROCESSING_URL;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${idToken}`,
      "Content-Type": "application/json",
    },
    body: "{}",
  });
  const body = await resp.json();
  if (!resp.ok) {
    throw new Error(`Processing failed (${resp.status}): ${body.error || JSON.stringify(body)}`);
  }
  return body;   // { run_id, started, finished, moved, skipped, failed, report_key }
}
```

UX recommendation: this Lambda's run is slow-ish (S3 list + per-object head/copy/delete). Show a spinner with "Processing files…" and disable the button while pending. Surface `moved` / `skipped` / `failed` counts plus a link to the CSV report.

### CORS + CSP for the new SPA

- Lambda already emits `Access-Control-Allow-Origin: *` — works out of the box.
- For prod, tighten to the specific CloudFront domain by editing `CORS_HEADERS` in [processing_pipeline.py](../Infra/functions/processing_pipeline/processing_pipeline.py).
- The new CloudFront distribution's CSP `connect-src` must allow `https://*.lambda-url.us-east-1.on.aws` (same hostname pattern used for `/chat`).

### Permissions needed — Variation 2

- **AWS side**: only the CFN re-deploy of `05-compute` to create the Function URL. No new IAM on the Lambda role.
- **User side**: user belongs to `ciso` or `grc` Cognito group.

---

## Sanity checks specific to the file-processing Lambda

```bash
# Function URL is configured and live
aws lambda get-function-url-config --function-name dev-st21arbiter-poc-processing-pipeline --region us-east-1

# Lambda env vars include COGNITO_ISSUER_URL + ALLOWED_GROUPS
aws lambda get-function-configuration --function-name dev-st21arbiter-poc-processing-pipeline \
  --region us-east-1 --query 'Environment.Variables'

# EventBridge schedule still in place (so scheduled runs still happen)
aws events describe-rule --name dev-st21arbiter-poc-processing-pipeline-schedule --region us-east-1
```

If the Function URL returns immediately with a JSON body containing CORS headers, the auth gate is wired correctly. If it returns a raw `{"errorMessage": ...}` shape, the Lambda crashed before reaching `_resp` — check CloudWatch logs.
