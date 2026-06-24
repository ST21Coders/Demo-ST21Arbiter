# ServiceNow Specialist — ITSM/ITAM Features & Connection Guide

This document is the functional spec, connection guide, and end-to-end test plan for the
ARBITER **ServiceNow specialist** agent. It covers connecting the agent to a live ServiceNow
instance and the full ITSM/ITAM capability set: CMDB read/write, Incident/Problem/Change
read–update–comment, Asset Management read/write, and an **AI-Scan drift** capability that
reconciles the CMDB/Assets against live AWS reality.

> Status: the ServiceNow specialist already existed (CMDB read + Change create + change-impact
> analysis). This release **extends** it. The "static placeholder" note in `CLAUDE.md` is stale.

---

## 1. What was added

| Area | Before | Now |
|---|---|---|
| Auth | basic, OAuth2 client-credentials | + **API Key** (`x-sn-apikey`) + **OAuth2 JWT bearer** (auto-detected from the secret) |
| CMDB | read (resolve CI, blast radius, owner) | + **create CI**, **update CI** (status/owner/attrs), full CI details |
| Incident | — | **query / create / update / comment** (work notes + customer comments) |
| Problem | — | **query / create / update / comment**, CI linkage |
| Change | query, create, attach CIs | + **update**, + **comment / work note** |
| Asset (ITAM) | — | **query / create / update** (`alm_asset`/`alm_hardware`), asset↔CI link |
| AI-Scan drift | — | **CMDB/Asset drift vs AWS** — in the Findings pipeline **and** a dedicated Drift Scan dashboard |

Auth choice for this deployment: **Basic** (admin user/password) — works immediately on a fresh
Personal Developer Instance. API-key and JWT are also supported and auto-detected, so you can
switch later without code changes.

---

## 2. Functional capability catalog

The agent exposes two surfaces, mirroring `jira_specialist`:

* **Chat tools** — registered on the Strands `Agent`, used conversationally from the ServiceNow
  card on the **MCP** page (`target: "servicenow"` → `/chat`).
* **Deterministic actions** — `payload.action`, no LLM, invoked by `api_handler` routes and the
  master orchestrator. Precise multi-field writes go here. Both surfaces share one helper layer.

| Module | Operation | Table(s) | Chat tool | Action |
|---|---|---|---|---|
| CMDB | resolve CI / blast radius / owner | `cmdb_ci`, `cmdb_rel_ci` | `query_ci`, `get_affected_cis`, `get_ci_owner`, `get_ci_details` | — |
| CMDB | create CI | `cmdb_ci(_*)` | — | `create_ci` |
| CMDB | update CI (status/owner/env/attrs) | `cmdb_ci(_*)` | — | `update_ci` |
| Incident | query | `incident` | `query_incident` | — |
| Incident | create | `incident` | — | `create_incident` |
| Incident | update (state/assign/priority) | `incident` | `update_incident` | `update_incident` |
| Incident | comment / work note | `incident` | `comment_incident` | `comment_incident` |
| Problem | query | `problem` | `query_problem` | — |
| Problem | create / update / comment | `problem` | `comment_problem` | `create_problem`, `update_problem`, `comment_problem` |
| Change | query | `change_request` | `query_change` | — |
| Change | create + attach CIs | `change_request`, `task_ci` | — | `create_change`, `add_affected_cis` |
| Change | update / comment | `change_request` | — | `update_change`, `comment_change` |
| Asset | query (tag/serial/model/state) | `alm_asset` | `query_asset` | — |
| Asset | create / update (lifecycle/assignee/link CI) | `alm_hardware`, `alm_asset` | — | `create_asset`, `update_asset` |
| Drift | CMDB+asset snapshot | all | — | `cmdb_snapshot` |
| Drift | CMDB/asset hygiene | all | `detect_drift` | — |
| Impact | resolve→blast→owner→draft CHG | `cmdb_ci`, `cmdb_rel_ci`, `change_request` | (impact phrasing) | `impact_analysis` |

Source: [`agents/servicenow_specialist/agent.py`](../agents/servicenow_specialist/agent.py).

---

## 3. Architecture

### 3.1 Connection & auth (auto-detected from the Secrets Manager secret)

All ServiceNow reach is behind `ServiceNowClient` (Table + Change REST APIs). Auth precedence:

1. `api_key`  → header `x-sn-apikey: <key>` (ServiceNow Inbound REST API Key)
2. `client_id` + `client_secret` (+ optional `jwt_assertion`) → OAuth2 bearer via `/oauth_token.do`
   (JWT-bearer grant when an assertion is present, else client-credentials)
3. `username` + `password` → HTTP basic **(primary for this deployment)**

Secret id: `dev/<project>/servicenow` (read by env var `SERVICENOW_SECRET_ID`). The IAM role
`ServicenowAgentRuntimeRole` already permits `dev/<project>/servicenow-*`.

Secret shapes (pick one):

```json
{"instance_url":"https://devNNNNN.service-now.com","username":"admin","password":"<pwd>"}
{"instance_url":"https://devNNNNN.service-now.com","api_key":"<inbound-api-key>"}
{"instance_url":"https://devNNNNN.service-now.com","client_id":"...","client_secret":"..."}
{"instance_url":"https://devNNNNN.service-now.com","client_id":"...","client_secret":"...","jwt_assertion":"<signed-jwt>"}
```

If the secret/instance is unreachable, the agent degrades to a "(ServiceNow not configured)"
mode (mock numbers) so the demo still renders — same pattern as `jira_specialist`.

### 3.2 Drift detection — "Both" surfaces, one rule set

Drift logic lives once, in the master orchestrator's rule pack, and feeds two consumers:

```
                          ┌─ servicenow_specialist.cmdb_snapshot ─┐  (live CIs + assets)
 master_orchestrator ─────┤                                       ├─► run_servicenow_drift()
   _seed_aws_inventory() ─┘  (canonical AWS reality)              │     → DRIFT findings
                                                                  │
   /scan  ───────────────────────────────────────────────────────┴─► merged into conflicts-v2 → Findings page
   /servicenow/drift-scan ───────────────────────────────────────────► drift-only subset → Drift Scan dashboard
```

* **Findings pipeline** — every `/scan` run now also pulls the ServiceNow snapshot and emits
  `conflict_type: "DRIFT"`, `source_pair: "ServiceNow+AWS Config"` findings beside the policy
  conflicts. Safe fallback: if ServiceNow is unconfigured the snapshot is empty → zero SN findings.
* **Dedicated dashboard** — `POST /servicenow/drift-scan` invokes the master in
  `servicenow_drift_scan` mode and returns only the drift subset for the **CMDB Drift Scan** page.

Drift classes (`agents/master_orchestrator/scan_rule_pack.py::run_servicenow_drift`):

| Class | Meaning | Severity |
|---|---|---|
| `unmanaged_resource` | live AWS resource, no CI (coverage gap) | HIGH |
| `stale_ci` | operational CI with no matching live AWS resource | MEDIUM |
| `ownership_drift` | CMDB support group ≠ AWS owner tag | LOW |
| `asset_stale` | in-use asset linked to a decommissioned resource | MEDIUM |
| `asset_unlinked` | asset not linked to any CI | LOW |

Only infrastructure CI classes (`cmdb_ci_lb`, `cmdb_ci_db_instance`, `cmdb_ci_network`,
`cmdb_ci_server`, …) reconcile against AWS; business application/service CIs are exempt to avoid
false positives.

### 3.3 AWS reality source

The canonical "AWS reality" is `master_orchestrator.agent.py::_seed_aws_inventory()` — the full
inventory of AWS-backed resources the CMDB is expected to mirror. (Distinct from the curated
policy-UC `awsconfig` observations, which are a subset and would over-flag.) When the awsconfig
specialist ships a structured inventory tool, replace this fixture; the drift code is unchanged.

---

## 4. Connect the agent to your instance

```bash
# 1. Store the instance creds (basic auth shown; see §3.1 for api_key/JWT shapes).
aws secretsmanager create-secret --region us-east-1 \
  --name dev/st21arbiter-poc/servicenow \
  --secret-string '{"instance_url":"https://devNNNNN.service-now.com","username":"admin","password":"<pwd>"}'
# (already exists? use: aws secretsmanager put-secret-value --secret-id dev/st21arbiter-poc/servicenow --secret-string '{...}')

# 2. Seed the CMDB + assets + intentional drift fixtures.
source scripts/.venv/bin/activate
PROJECT=st21arbiter-poc python3 scripts/seed_servicenow_cmdb.py --from-secret

# 3. Rebuild the two changed runtimes (servicenow + master) and re-patch ARNs onto the Lambda.
KB_ID=<id> GUARDRAIL_ID=<id> GUARDRAIL_VERSION=1 MASTER_MEMORY_ID=<id> AWS_REGION=us-east-1 \
  python3 scripts/deploy_agents.py

# 4. Rebuild + deploy the UI for the new Drift Scan page.
cd ui && npm run build   # then sync ui/dist to S3 + invalidate CloudFront (deploy.sh does this)
```

> If you redeployed `06-api` recently, the api_handler Lambda's `*_RUNTIME_ARN` env may be blanked
> — re-run `deploy_agents.py` to re-patch (see the `deploy.sh blanks runtime ARNs` gotcha).

### Seeded drift fixtures (deterministic demo)

`scripts/seed_servicenow_cmdb.py` seeds, against `_seed_aws_inventory()`:

* `ec2-mig-prod-legacy-batch-009` — operational CI, **absent from AWS** → `stale_ci`.
* `rds-mig-prod-reporting-replica-003` — CMDB owner *Network Engineering*, AWS owner *Data
  Governance* → `ownership_drift`.
* `lambda-mig-prod-claims-processor-007` — in AWS inventory, **no CI** → `unmanaged_resource`.
* Asset `P1000099` (in-use, linked to the stale EC2 CI) → `asset_stale`.
* Asset `P1000100` (no CI link) → `asset_unlinked`.
* Asset `P1000050` (healthy, linked to a live ALB) → control, must **not** flag.

Expected drift scan output: **5 items** (1 HIGH, 2 MEDIUM, 2 LOW).

---

## 5. End-to-end test scenarios

Run conversational scenarios from the **MCP** page (ServiceNow card); deterministic ones from
the named UI page or via `curl`. Each lists the expected result.

### 5.1 Connectivity smoke (local)

```bash
cd agents/servicenow_specialist && pip install -r requirements.txt
SERVICENOW_SECRET_ID=dev/st21arbiter-poc/servicenow AWS_REGION=us-east-1 python agent.py   # serves :8080
curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' \
  -d '{"prompt":"resolve CI alb-mig-prod-claims-api-001"}'
```
**Expect:** a CI summary with class `cmdb_ci_lb`, a `sys_id`, the ARN, and owning team
*Cloud Infrastructure*.

### 5.2 CMDB write
* Chat: *"Create a CI named svc-payments-api, class cmdb_ci_appl, owned by Application Development."*
* Chat: *"Set operational_status of ec2-mig-prod-legacy-batch-009 to non-operational."*
**Expect:** "Done — …"; verify the record in the ServiceNow UI (new CI / changed status).

### 5.3 Incident lifecycle
* *"Create an incident for the Claims API CI: intermittent 5xx on /claims, priority 2."* → returns `INC…`.
* *"Add a work note to INC…: investigating with the cloud team."*
* *"Update INC… to In Progress and assign Cloud Infrastructure."*
* *"Resolve INC… with close note: ALB target group health restored."*
**Expect:** journal entries + state transitions visible on the incident; `cmdb_ci` = Claims API.

### 5.4 Problem
* *"Open a problem for repeated Claims API 5xx and link the Claims API CI."* → returns `PRB…`.
* *"Comment on PRB…: root cause is the legacy batch host saturating the DB."*
**Expect:** problem created, CI linked, work note added.

### 5.5 Change (extends impact analysis)
* From **Impact Analysis** page: analyze `alb-mig-prod-claims-api-001`, PROD/HIGH, *Draft a change request* → returns `CHG…`.
* Chat: *"Update CHG… state to Scheduled and add a work note: CAB approved for Saturday window."*
**Expect:** the drafted CHG updates; work note recorded.

### 5.6 Asset Management
* *"Look up asset P1000099."* → in-use hardware asset linked to the legacy batch CI.
* *"Create a hardware asset tag P1000200 named 'Claims DB spare', in stock."* → created.
* *"Link asset P1000200 to CI mig-prod-claims-data-primary and set it to In use."*
**Expect:** asset created, linked (`alm_asset.ci`), lifecycle updated.

### 5.7 Drift scan — dedicated dashboard
Open **CMDB Drift Scan** → *Run drift scan*.
**Expect:** the 5 seeded items (§4): unmanaged `lambda-…-007` (HIGH), stale `ec2-…-009` (MEDIUM),
ownership `rds-…-003` (LOW), `asset_stale` P1000099 (MEDIUM), `asset_unlinked` P1000100 (LOW).
`P1000050` and business-service CIs must **not** appear.

### 5.8 Drift scan — Findings pipeline
Trigger a full scan (**dashboard → Run AI Scan**, or `POST /scan`). Open **Findings**, filter
`conflict_type = DRIFT`.
**Expect:** the same ServiceNow drift items appear as `DRIFT` findings
(`source_pair: ServiceNow+AWS Config`) alongside the policy DRIFT findings (UC07/08/09).

### 5.9 Auth fallback (optional)
Swap the secret to `{"instance_url":"…","api_key":"<key>"}` and re-run §5.1. **Expect:** identical
result via the `x-sn-apikey` header — proving auto-detection. (Create the API Key in ServiceNow:
*System Web Services → REST API Key*, plus a *REST API Access Policy*.)

### 5.10 Regression
```bash
cd ui && npm test                                   # 215 tests, all green
python3 -c "import sys; sys.path.insert(0,'agents/master_orchestrator'); \
  from scan_rule_pack import run_servicenow_drift; print('drift import OK')"
```

---

## 6. File map

| Concern | File |
|---|---|
| Agent (client, tools, actions, snapshot) | [`agents/servicenow_specialist/agent.py`](../agents/servicenow_specialist/agent.py) |
| Drift correlation + matchers | [`agents/master_orchestrator/scan_rule_pack.py`](../agents/master_orchestrator/scan_rule_pack.py) |
| AWS inventory + snapshot fetch + drift mode | [`agents/master_orchestrator/agent.py`](../agents/master_orchestrator/agent.py) |
| Drift-scan API route | [`Infra/functions/api_handler/api_handler.py`](../Infra/functions/api_handler/api_handler.py) (`/servicenow/drift-scan`) |
| CMDB + asset + drift seed | [`scripts/seed_servicenow_cmdb.py`](../scripts/seed_servicenow_cmdb.py) |
| Drift dashboard page | [`ui/src/pages/CmdbDrift.jsx`](../ui/src/pages/CmdbDrift.jsx) |
| API client + mock | [`ui/src/hooks/useApi.js`](../ui/src/hooks/useApi.js) (`runDriftScan`), [`ui/src/mockData.js`](../ui/src/mockData.js) (`mockDriftScan`) |
| Route / nav / RBAC | [`ui/src/App.jsx`](../ui/src/App.jsx), [`ui/src/components/Sidebar.jsx`](../ui/src/components/Sidebar.jsx), [`ui/src/contexts/PersonaContext.jsx`](../ui/src/contexts/PersonaContext.jsx) |
| ServiceNow chat card + prompts | [`ui/src/pages/MCPChat.jsx`](../ui/src/pages/MCPChat.jsx) |
