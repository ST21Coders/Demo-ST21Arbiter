# Scheduled raw → processed S3 file mover

Operational reference for the `dev-st21arbiter-poc-processing-pipeline` Lambda.

## What it does

Runs on a schedule, moves every object from the raw S3 bucket to the processed S3 bucket, and writes a CSV audit report to a folder inside the raw bucket.

- **Source bucket:** `dev-st21arbiter-poc-raw`
- **Destination bucket:** `dev-st21arbiter-poc-processed`
- **Report location:** `s3://dev-st21arbiter-poc-raw/File_Transfer_Reports/`
- **Schedule:** twice daily at **06:00 and 18:00 PST** (literal UTC-8, no DST tracking)
- **Mode:** true move — copy then delete source
- **Dedup rule:** if a file with the same key already exists in the destination, skip

---

## Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Schedule mechanism | `AWS::Events::Rule` with cron `0 14,2 * * ? *` UTC | Simple; user wanted literal PST, no DST drift |
| Copy vs move | Move (copy + `delete_object` on source) | User said "move"; keeps raw bucket from growing unbounded |
| Duplicate detection | `head_object` on destination key | Cheapest check; matches "if file already exists, skip" literally |
| Prefix preservation | Mirror source key into destination | No flatten or rename — `raw/policies/foo.pdf` → `processed/policies/foo.pdf` |
| Reports excluded from scan | Keys under `REPORTS_PREFIX` skipped in the list loop | Prevents reports from being moved to processed bucket |
| Bucket variability | Bucket names + report prefix held in Lambda env vars | Operators can repoint via Lambda Console with no redeploy |

---

## Architecture

```
   EventBridge Rule
   (06:00 PST / 18:00 PST = 14:00 / 02:00 UTC)
           │
           ▼
   processing_pipeline Lambda
   ENI in PrivateSubnet1 (LambdaSG)
           │
           │ list_objects_v2 (paginated)
           │ head_object (per key)
           │ copy_object  (raw → processed)
           │ delete_object (raw)
           │ put_object   (CSV report)
           ▼
   S3 (via S3 Gateway Endpoint)
     ├── dev-st21arbiter-poc-raw/
     │     ├── <your files>          ← source
     │     └── File_Transfer_Reports/
     │           └── run-<ts>-<id>.csv ← audit trail
     └── dev-st21arbiter-poc-processed/
           └── <your files>            ← destination
```

---

## Files changed

| File | Change |
|---|---|
| [processing_pipeline.py](processing_pipeline.py) | Full handler implementation |
| [../../templates/02-security.yaml](../../templates/02-security.yaml) | Added `s3:DeleteObject` to `ProcessingPipelineRole` |
| [../../templates/05-compute.yaml](../../templates/05-compute.yaml) | Added `REPORTS_PREFIX` env var, EventBridge rule, Lambda invoke permission |

No new CFN stacks. No bucket changes. No KB / agent changes.

---

## Lambda environment variables

Read once at module load. Override via Lambda Console → Configuration → Environment variables (no redeploy needed) or via the CFN template for permanent change.

| Variable | Default | Notes |
|---|---|---|
| `RAW_BUCKET` | `dev-st21arbiter-poc-raw` (from CFN export) | Source bucket. Required. |
| `PROCESSED_BUCKET` | `dev-st21arbiter-poc-processed` (from CFN export) | Destination bucket. Required. |
| `REPORTS_PREFIX` | `File_Transfer_Reports/` | Where CSVs land *and* what to skip when listing. Trailing slash required. |
| `AWS_REGION` | `us-east-1` | Set automatically by Lambda runtime. |

> Changing `RAW_BUCKET` or `PROCESSED_BUCKET` via the console takes effect on the next cold start. Force a cold start by editing any env var (the runtime swaps containers).

---

## IAM permissions

`ProcessingPipelineRole` in [02-security.yaml](../../templates/02-security.yaml) holds:

```yaml
- Effect: Allow
  Action:
    - s3:GetObject       # copy source
    - s3:PutObject       # copy destination + report write
    - s3:ListBucket      # enumerate raw bucket
    - s3:DeleteObject    # true move (added for this feature)
  Resource:
    - arn:aws:s3:::dev-st21arbiter-poc-raw
    - arn:aws:s3:::dev-st21arbiter-poc-raw/*
    - arn:aws:s3:::dev-st21arbiter-poc-processed
    - arn:aws:s3:::dev-st21arbiter-poc-processed/*

- Effect: Allow
  Action: [kms:Decrypt, kms:GenerateDataKey, kms:DescribeKey]
  Resource: "*"          # both buckets share the DataAtRestKey CMK
```

> `DeleteObject` is scoped to both buckets in IAM but the code itself only deletes from `RAW_BUCKET`. Tighten the IAM to raw-only later if a stricter least-privilege posture is desired.

---

## Schedule

`AWS::Events::Rule` in [05-compute.yaml](../../templates/05-compute.yaml):

```yaml
ProcessingPipelineSchedule:
  Type: AWS::Events::Rule
  Properties:
    Name: dev-st21arbiter-poc-processing-pipeline-schedule
    ScheduleExpression: "cron(0 14,2 * * ? *)"     # 14:00 + 02:00 UTC daily
    State: ENABLED
    Targets:
      - Arn: <ProcessingPipelineFunction.Arn>
        Id: ProcessingPipelineTarget
```

| Cron field | Value | Meaning |
|---|---|---|
| Minutes | `0` | Top of the hour |
| Hours | `14,2` | 14:00 UTC and 02:00 UTC |
| Day-of-month | `*` | Every day |
| Month | `*` | Every month |
| Day-of-week | `?` | Required `?` (mutually exclusive with `*` day-of-month in EventBridge) |
| Year | `*` | Every year |

**Wall-clock equivalence:** 14:00 UTC = 06:00 PST, 02:00 UTC = 18:00 PST. Because EventBridge cron is UTC-only, during Pacific Daylight Time (PDT, summer) these fire at 07:00 / 19:00 local. To pin to Pacific local-time year-round, swap `AWS::Events::Rule` for `AWS::Scheduler::Schedule` with `ScheduleExpressionTimezone: "America/Los_Angeles"`.

---

## CSV report schema

One CSV per run. Located at `s3://<RAW_BUCKET>/<REPORTS_PREFIX>run-<UTC_ISO_TIMESTAMP>-<run_id_first_8>.csv`.

Columns (header row always emitted):

| Column | Type | Notes |
|---|---|---|
| `timestamp_utc` | ISO 8601 UTC | When this row was processed |
| `source_bucket` | string | Always `RAW_BUCKET` |
| `source_key` | string | S3 key in raw |
| `destination_bucket` | string | Always `PROCESSED_BUCKET` |
| `destination_key` | string | Mirrors `source_key` |
| `action` | enum | `MOVED` · `SKIPPED_EXISTS` · `FAILED` |
| `size_bytes` | integer | From `list_objects_v2` |
| `source_etag` | string | MD5 of single-part objects; unique opaque ID for multipart |
| `error` | string | Empty unless action=FAILED. Format: `<stage>:<exception>` (e.g. `copy:ClientError`) |

A row is emitted for every file that was *considered*. Files under `REPORTS_PREFIX` and "folder marker" keys (ending in `/`) are silently skipped and produce no row — they're not transfer activity.

If the raw bucket is empty, a CSV with only the header row is still written — proves the run executed.

---

## Edge cases handled

| Case | Behavior |
|---|---|
| Report self-move | Keys under `REPORTS_PREFIX` skipped in the list loop |
| Folder markers (zero-byte keys ending in `/`) | Skipped silently |
| Empty raw bucket | Header-only CSV written |
| Partial failure (copy succeeds, delete fails) | Next run sees dest exists → skips → no double-copy; source remains in raw and surfaces as `SKIPPED_EXISTS` next time |
| Versioning enabled on both buckets | Copy creates a new version on dest; delete creates a delete-marker on source; both restorable |
| Listing error | Run aborts with 502, no report written |
| Report-write error | Run still returns 502 with counters in body, but the move work already happened |
| Object > 5 GB | `copy_object` fails; row recorded as `FAILED` with error `copy:...`; would need multipart copy if it ever happens (out of scope) |

---

## Verification

### Validate templates before deploy

```bash
cd Infra
aws cloudformation validate-template --template-body file://templates/02-security.yaml --region us-east-1
aws cloudformation validate-template --template-body file://templates/05-compute.yaml --region us-east-1
```

### Deploy

```bash
cd Infra
./deploy.sh        # change-sets for 02-security and 05-compute
```

### Smoke test — manual invoke

```bash
# 1. Drop a test file in raw
aws s3 cp ./README.md s3://dev-st21arbiter-poc-raw/smoke/test-$(date +%s).md --region us-east-1

# 2. Invoke manually (don't wait for 14:00 UTC)
aws lambda invoke --function-name dev-st21arbiter-poc-processing-pipeline \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json --region us-east-1
cat /tmp/out.json
# Expected: { "run_id": "...", "moved": 1, "skipped": 0, "failed": 0, "report_key": "File_Transfer_Reports/..." }

# 3. Confirm the move
aws s3 ls s3://dev-st21arbiter-poc-raw/smoke/       --region us-east-1   # empty
aws s3 ls s3://dev-st21arbiter-poc-processed/smoke/ --region us-east-1   # has the file

# 4. Read the report
aws s3 ls s3://dev-st21arbiter-poc-raw/File_Transfer_Reports/ --region us-east-1
aws s3 cp s3://dev-st21arbiter-poc-raw/File_Transfer_Reports/run-<latest>.csv - --region us-east-1
```

### Idempotency check

```bash
# Re-invoke immediately — should show 0 MOVED (raw is empty now)
aws lambda invoke --function-name dev-st21arbiter-poc-processing-pipeline \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out2.json --region us-east-1
cat /tmp/out2.json
```

### Schedule wiring

```bash
aws events describe-rule --name dev-st21arbiter-poc-processing-pipeline-schedule --region us-east-1
aws events list-targets-by-rule --rule dev-st21arbiter-poc-processing-pipeline-schedule --region us-east-1
```

### Watch a scheduled fire

```bash
aws logs tail /aws/lambda/dev-st21arbiter-poc-processing-pipeline --region us-east-1 --since 30m --follow
# Expect to see "processing_pipeline run started" / "run finished" lines at 14:00 and 02:00 UTC.
```

---

## Operating the function

### Repoint to different buckets without redeploy

```bash
aws lambda update-function-configuration \
  --function-name dev-st21arbiter-poc-processing-pipeline \
  --region us-east-1 \
  --environment "Variables={RAW_BUCKET=<new-raw>,PROCESSED_BUCKET=<new-processed>,REPORTS_PREFIX=File_Transfer_Reports/,S3_KMS_KEY_ARN=<existing>}"
```

> The IAM policy is hardcoded to `dev-st21arbiter-poc-raw` / `dev-st21arbiter-poc-processed` resource ARNs. If you repoint to buckets outside that naming pattern, update [02-security.yaml](../../templates/02-security.yaml) Resource ARNs too — otherwise S3 calls will get AccessDenied.

### Pause the schedule

```bash
aws events disable-rule --name dev-st21arbiter-poc-processing-pipeline-schedule --region us-east-1
# Re-enable with: aws events enable-rule --name ...
```

### Run on-demand (not on schedule)

```bash
aws lambda invoke --function-name dev-st21arbiter-poc-processing-pipeline \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/out.json --region us-east-1
```

### Change the schedule

Edit `ScheduleExpression` in [05-compute.yaml](../../templates/05-compute.yaml) and redeploy `05-compute`. Cron quick reference for common patterns:

| Want | Cron (UTC) |
|---|---|
| Every hour | `cron(0 * * * ? *)` |
| Every 6 hours starting at 00:00 UTC | `cron(0 0,6,12,18 * * ? *)` |
| Once daily at 09:00 PST literal | `cron(0 17 * * ? *)` |
| Weekdays only at 06:00 + 18:00 PST | `cron(0 14,2 ? * MON-FRI *)` |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `AccessDenied` on `DeleteObject` | IAM policy wasn't redeployed | Re-run `./deploy.sh` for `02-security`; confirm `s3:DeleteObject` shows up in the role |
| `AccessDenied` on a *different* bucket after repointing | IAM Resource ARNs hardcoded to original names | Edit [02-security.yaml](../../templates/02-security.yaml) Resource ARNs and redeploy `02-security` |
| `KMS.AccessDeniedException` on copy or PutObject | Target bucket uses a different CMK than the role's KMS grant covers | KMS statement is `Resource: "*"` so this only happens if the key policy itself excludes the role — check the new bucket's CMK key policy |
| Files keep showing as `SKIPPED_EXISTS` and never `MOVED` | Source filenames already exist in the destination from a prior run | Expected behavior. Delete or rename in the destination if you actually want to re-copy |
| `processing_pipeline` never fires on schedule | Rule disabled, target detached, or Lambda permission missing | `aws events describe-rule` → check `State: ENABLED`; `list-targets-by-rule` → check the Lambda ARN is wired; `aws lambda get-policy --function-name ...` → confirm `events.amazonaws.com` principal |
| Lambda runs but no CSV appears in `File_Transfer_Reports/` | `put_object` failed (KMS or bucket policy); check the response body for `report_write_failed` | Look at the Lambda's return value or CloudWatch log — failure is non-fatal for the move itself |
| Cron fires at 7 AM / 7 PM local during summer | Pacific Daylight Time (PDT, UTC-7) — cron is UTC-only | Acceptable per current design. To track DST, migrate to `AWS::Scheduler::Schedule` with `ScheduleExpressionTimezone: "America/Los_Angeles"` |
| `head_object` returns `403 Forbidden` instead of `404` | Caller has `s3:GetObject` but no `s3:ListBucket` on dest — head falls back to ACL-style check | The role already has ListBucket on processed; ensure CFN actually deployed and that you're testing against the right bucket |

---

## Out-of-scope (future work)

These are deliberately not implemented in this iteration:

- Multipart copy for objects > 5 GB
- Date-partitioned report folders (e.g. `File_Transfer_Reports/2026/05/26/`)
- Content-based dedup (compare ETag/MD5 across raw and processed)
- Per-extension filtering (currently moves *all* files except reports)
- Lifecycle policy on `File_Transfer_Reports/` to expire old CSVs
- Pacific Time DST tracking via `AWS::Scheduler::Schedule`
- Triggering KB re-ingestion on `bedrock:StartIngestionJob` after move (the role already has this permission)
