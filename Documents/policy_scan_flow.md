# ARBITER — Policy Scan Flow: Structured vs Unstructured Ingestion (+ Step Functions hardening plan)

> Reference for how ARBITER turns policy documents **and** technical-control exports into
> conflict findings in DynamoDB — and the planned Step Functions orchestration to make that
> flow production-reliable. Written 2026-06-09.

## 1. The core principle: two ingestion planes, one comparison

A **conflict is a disagreement between what a policy *says* and what a control is *configured to do*.**
Those are two different data types with two different pipelines. **Glue/Athena belong only to the
structured plane** — they never touch policy PDFs, and nothing "extracts conflicts" from a single source.

| Plane | Data | Examples | Ingestion (right tool) | Queryable via | Answers |
|---|---|---|---|---|---|
| **Unstructured** | Policy *intent* | `MIG-POL-002.pdf`, `.docx`, `.txt` | **Textract → chunk → Bedrock KB (vectors)** — already built | RAG / semantic search | "What does the policy *say*?" |
| **Structured** | Enforcement *state* | `zscaler_rules.csv`, PAN-OS rulebase export, AWS Config snapshot, ServiceNow CMDB, Oracle tables | **Glue Crawler → Glue Data Catalog** | Athena SQL | "What is the control *actually configured* to do?" |

Pointing Glue at a PDF is the wrong tool. Policy PDFs already flow through the unstructured path
(`Infra/functions/processing_pipeline/processing_pipeline.py` → Textract → Bedrock KB).

## 2. Responsibilities — who does what

```
 Policy PDF ─► Textract ─► Bedrock KB (vectors)  ─┐
                                                  │   compare (deterministic
 zscaler_rules.csv ─► Glue Crawler ─► Catalog ─► Athena ─┤   rule pack: scan_rule_pack.py)
                                                  │        │
                                                  ▼        ▼
                                    policy citation  vs  enforcement observation
                                                  │
                                                  ▼
                                   findings ─► SCANNER Lambda ─► DynamoDB conflicts-v2
```

- **Glue / Athena** = read-only query surface over structured data. **Never write findings.**
- **`agents/master_orchestrator/scan_rule_pack.py`** (14 matchers) = the thing that *detects* a
  conflict, by comparing a policy citation against an enforcement observation. (e.g. UC04 fires only
  when policy mandates SSL inspection **and** a Zscaler row shows `registered_exception=false`.)
- **`Infra/functions/scanner/scanner_lambda.py`** = the **only** writer of DynamoDB
  (`BatchWriteItem` → `conflicts-v2`, plus `scan-runs` + `audit-log`). Unchanged by structured ingestion.

## 3. End-to-end structured flow → DynamoDB (Day 5, event-chained)

Glue's role is steps 2–4 only.

1. **Upload a structured export.** Analyst drops `zscaler_rules.csv` in the UI. `DataPipeline.jsx`
   `accept` includes `.csv`; it lands in S3 as `users/<sub>/<ts>-zscaler_rules.csv`.
2. **Classify + route by extension** (uploads are not source-prefixed). `processing_pipeline` copies
   a `.csv` to `s3://<env>-<project>-processed/structured/zscaler_rules/` instead of sending it to the KB.
   Unstructured files still go to the KB exactly as today.
3. **Glue Crawler runs** (on-demand, kicked after the copy). Infers the schema
   (`rule_id string, action string, category string, registered_exception string, …`) and
   registers/updates a **table in the Glue Data Catalog** pointing at that S3 prefix. The Catalog
   stores *metadata*, not data.
4. **Athena makes it SQL-queryable** — `SELECT * FROM zscaler_rules` — governed by an Athena
   workgroup (SSE-KMS results, `BytesScannedCutoffPerQuery`, 7-day results lifecycle).
5. **The scan reads it instead of fixtures.** A Glue *crawler-completed* EventBridge rule invokes the
   scanner → scanner invokes the **master** runtime → master's `_run_scan` calls the new
   **`structured_specialist`** runtime instead of `_seed_zscaler_observations()`:
   - `run_athena_query("SELECT … FROM zscaler_rules")` (SELECT-only, row-capped).
   - `produce_observations('zscaler')` maps each Athena row → the **exact** observation dict the
     matchers expect: `{"rule_id":…, "action":…, "raw":{"registered_exception": False, …}}`.
6. **Compare + write.** Master runs `run_rule_pack(sharepoint, zscaler_from_athena, awsconfig, paloalto)`
   → findings → **scanner enriches ownership + `BatchWriteItem` → `conflicts-v2`**, then updates
   `scan-runs` + `audit-log`. Identical write path Palo Alto's UC13/UC14 used.

### The type-coercion landmine (must-fix)
Athena returns **every column as a string**, so `registered_exception` comes back as the string
`'false'` — which is truthy and would silently *not* fire UC04. `produce_observations` must coerce
types to match the fixture shape byte-for-byte. Fixtures stay as an on-error fallback so a bad query
never blanks the scan. Add a non-zero-findings sanity log so a green scan never masks a zero write.

### The demo "money shot"
Edit one cell in `zscaler_rules.csv` — flip `registered_exception` `true` → `false` — re-upload →
crawler refreshes the Catalog → re-scan → Athena returns the new value → **UC04 (CRITICAL PCI)
appears in Findings, sourced from a live SQL query, not a fixture.** Flip it back and it clears.

## 4. Why Glue (honest tradeoff)

For *one small CSV*, Glue is heavier than reading the file from S3 directly. It earns its place because
it **generalizes**: the same Catalog + Athena surface later absorbs **ServiceNow CMDB, Oracle (via Glue
JDBC), and multi-source structured feeds** with no new parsers, and gives a governed SQL boundary for a
future analyst NL2SQL path. That is the production reason — state it plainly; don't oversell it for the
demo's single file.

---

# 5. Future hardening: orchestrate ingest→crawl→scan with AWS Step Functions

The flow above is **choreography** — services react to events with no central coordinator. Step
Functions is **orchestration** — one state machine owns the sequence, the waits, retries, and the
terminal state. For this pipeline the coordination is hard enough to justify a coordinator, because of
two awkward parts: the **async Glue crawler wait** and **guaranteeing the scan-run reaches a terminal
state**.

## 5.1 Why it helps ARBITER specifically

| Pain (real, in this repo) | Choreography today | With Step Functions |
|---|---|---|
| **Orphaned `RUNNING` scan-run rows** (the bug patched in `useScanFeed`, Days 1–3) | Scanner writes `RUNNING`; any failure/dropped event leaves it dangling forever | A `Catch` on every state guarantees the run ends `COMPLETED`/`FAILED` — the bug class disappears at the source |
| **Crawler wait** | Separate EventBridge "crawler SUCCEEDED" rule — invisible dependency; a misfire silently stalls | Explicit `StartCrawler → Wait → GetCrawler → Choice` poll loop in the graph |
| **Lambda-wait billing** (`processing_pipeline._wait_for_ingestion` polls KB ingest up to 180s inside a billed Lambda, capped at the 15-min timeout) | Lambda burns compute while sleeping | Native `Wait` states cost nothing while waiting; no timeout ceiling (Standard runs up to 1 yr) |
| **Silent stalls** | Dropped event between hops = no signal | A failed/timed-out execution is explicit, alarmable, pinpointed to the state |
| **3 scan triggers** (daily cron, "Run AI Scan", auto-after-upload) | Three code paths converging on the scanner | One `StartExecution` target for all three |

The first row is the strongest argument: **Step Functions would have *prevented* the orphaned-RUNNING-row
bug**, not merely let us defend against it in the UI.

## 5.2 Proposed state machine (Standard)

```
OpenScanRun ─► Route/Classify ─► StartCrawler ─► WaitCrawler ⟳ (poll GetCrawler)
                                                      │
                                              InvokeMasterScan (Athena→produce_observations→rule pack)
                                                      │
                                              EnrichAndWrite (BatchWrite conflicts-v2)
                                                      │
                                              CloseScanRun (COMPLETED)
   every state ──Catch──► CloseScanRunFailed (FAILED + alarm)
```

- **Standard**, not Express — crawler/ingest waits exceed Express's 5-min ceiling.
- The **deterministic comparison stays one task** (`InvokeMasterScan`). Orchestrate the *plumbing*
  around the rule pack / agent reasoning, not the reasoning itself.
- **Scalability lever:** a `Map` state fans out crawl+query across sources
  (zscaler ∥ paloalto ∥ oracle ∥ servicenow) and joins before the scan — the thing that matters once
  there are many structured feeds, not one CSV.

## 5.3 Caveats

- **Glue *Crawlers* have no native `.sync`** in Step Functions (only Glue *ETL Jobs* do, via
  `glue:startJobRun.sync`). Implement a poll loop, or swap the crawler for a small Glue Job for clean
  `.sync`. A crawler is simpler for pure schema inference → poll loop is the pragmatic choice.
- **Cost negligible** — Standard ≈ $25 / million transitions; a few scans/day × ~7 states is pennies.
- **New surface:** a state-machine template + an SFN IAM role
  (`glue:StartCrawler/GetCrawler`, `bedrock-agentcore:InvokeAgentRuntime`, `dynamodb` writes,
  `athena`/`s3`). It largely *replaces* the crawler-completed EventBridge rule rather than adding to it.

## 5.4 Sequencing recommendation

1. **Day 5 (demo):** build the structured path **event-chained** (Section 3). Prove the capability and
   land the CSV money-shot.
2. **Hardening pass (Day 6 or post-demo Phase C/F):** wrap `ingest → crawl → wait → scan → write-status`
   in the Standard state machine above. Banks the terminal-state guarantee, no-wait-billing, visible
   failures/retries, and the unified trigger — pairs naturally with the WebSocket/observability
   hardening already in the roadmap.

**Net:** Step Functions is a clear reliability/observability upgrade (and retires a bug class we hit),
and a scalability upgrade once sources multiply — but it is a hardening layer over a working
choreography, not a demo-day prerequisite.
