# Sunset OpenSearch KB → S3-Vectors Bedrock KB — Runbook

Replace the OpenSearch-Serverless-backed Bedrock Knowledge Base with a new **Amazon S3 Vectors**–backed
KB, provisioned as reusable CloudFormation, then repoint the 4 compliance agents and decommission
OpenSearch.

| | |
|---|---|
| **Account / region / env** | `669810405473` · `us-east-1` · `dev` (project `st21arbiter-poc`) |
| **Old KB (sunset)** | KB `2ADHACW6LB`, data source `KLUEZ1RNM5`, OSS collection `dev-st21arbiter-poc-kb` |
| **New KB (create)** | CFN stack `dev-st21arbiter-poc-07-bedrock` → S3 Vectors index `policy-vectors` (1024/float32/cosine) over `dev-st21arbiter-poc-unstructured` |
| **Affected agents** | `sharepoint`, `awsconfig`, `zscaler`, `paloalto` (they call `retrieve(knowledgeBaseId=KB_ID)`) |
| **Embedding** | Titan Text v2 `amazon.titan-embed-text-v2:0`, 1024 dims (unchanged) |
| **Guardrail** | `e0axl6y90il0` (unchanged — still owned by `setup_bedrock_kb.py`) |

**Why:** OpenSearch Serverless carries a standing OCU cost and forced the KB to be built out-of-band by
[`scripts/setup_bedrock_kb.py`](../scripts/setup_bedrock_kb.py) (the OSS index must pre-exist). S3 Vectors is
GA, cheaper, and fully IaC-able — the whole KB now lives in CloudFormation. The runtime `retrieve` API is
identical, so **agents need no code change — only the new `knowledgeBaseId`**. There is **no in-place
migration**; you build a new KB and repoint (this runbook).

**Strategy:** phased. Phase 1 builds + validates + repoints (fully reversible — the old KB stays live).
Phase 2 decommissions OpenSearch only after validation soaks.

> ⚠️ **Confirm before destructive AWS actions** (`delete-knowledge-base`, `delete-data-source`,
> `delete-stack`, `s3 rm`, removing OSS resources). Always deploy CFN via change-sets (`deploy.sh`), never
> bare `update-stack`.

---

## Phase 0 — IaC changes (already committed in this branch)

No AWS calls. These files are the reusable artifacts:

| File | Change |
|---|---|
| [`Infra/templates/07-bedrock.yaml`](../Infra/templates/07-bedrock.yaml) | **Rewritten** as the S3-Vectors KB stack: `PolicyVectorBucket` (`AWS::S3Vectors::VectorBucket`, SSE-S3), `PolicyVectorIndex` (`AWS::S3Vectors::Index` 1024/float32/cosine, non-filterable keys `AMAZON_BEDROCK_TEXT`/`AMAZON_BEDROCK_METADATA`), `KnowledgeBaseRole` (name `-kb-s3v-role`), `PolicyKnowledgeBase` (`S3_VECTORS`, name `-policy-kb-s3v`), `PolicyKBDataSource` (S3, FIXED_SIZE 512/20%). The `dev-st21arbiter-poc-unstructured` docs bucket is **pre-existing** — referenced by deterministic ARN, **not created** by this stack. No Guardrail (stays with the script). Outputs use `-S3V…` export names. |
| [`Infra/deploy.sh`](../Infra/deploy.sh) | Uncommented `"07-bedrock"` in `CF_STACKS_POST` (deploys before `09-agentcore`). |
| [`Infra/destroy.sh`](../Infra/destroy.sh) | Uncommented `"07-bedrock"` in `STACKS`. The `-unstructured` bucket is pre-existing (not stack-owned) → intentionally left untouched by teardown. |
| [`Infra/functions/api_handler/api_handler.py`](../Infra/functions/api_handler/api_handler.py) | `_handle_uploads_presign` reads `destination`; `destination=unstructured` (+ `UNSTRUCTURED_BUCKET` env set) presigns into the unstructured bucket with no SSE-KMS header (bucket default), else RAW as before. |
| [`Infra/functions/processing_pipeline/processing_pipeline.py`](../Infra/functions/processing_pipeline/processing_pipeline.py) | New `UNSTRUCTURED_BUCKET` env + `_handle_unstructured_kb_object`: an ObjectCreated event on the unstructured bucket skips the raw→processed copy (and the .csv→Glue split) and just `StartIngestionJob` on the new KB + invokes the scanner. |
| [`Infra/templates/06-api.yaml`](../Infra/templates/06-api.yaml) | `UNSTRUCTURED_BUCKET` env on `api_handler` (`${Environment}-${ProjectName}-unstructured`). |
| [`Infra/templates/05-compute.yaml`](../Infra/templates/05-compute.yaml) | `UNSTRUCTURED_BUCKET` env on `processing_pipeline` + `UnstructuredBucketObjectCreatedRule` (Events::Rule) + its Lambda permission. |
| [`Infra/templates/02-security.yaml`](../Infra/templates/02-security.yaml) | `ApiHandlerRole` `RawProcessedBucketUploads` extended with the `-unstructured` bucket ARNs so the presigned PUT is authorized. |
| [`ui/src/pages/DataPipeline.jsx`](../ui/src/pages/DataPipeline.jsx) / [`ui/src/hooks/useApi.js`](../ui/src/hooks/useApi.js) | `uploadDestinationForMix` routes the **Text / CSV+Text / CSV+Text+docs** group mixes to `destination=unstructured`; Policy Documents card shows **Raw → Unstructured → KB ingest → Scan** (KB `SQCLG3W09Y` / DS `NM2FVXL5T6`). Other mixes unchanged. |

**Local validation already run:** `cfn-lint templates/07-bedrock.yaml` → clean; `bash -n deploy.sh destroy.sh` → OK.

Before deploying, validate against the live account and dry-run the change-set to confirm the
`AWS::S3Vectors::*` resource types are registered in this region:

```bash
cd Infra
aws cloudformation validate-template \
  --template-body file://templates/07-bedrock.yaml --region us-east-1
```

---

## Phase 1 — Build, populate, ingest, validate, repoint (reversible)

### 1.1 Deploy stack 07

```bash
cd Infra
./deploy.sh          # deploys 07-bedrock in the CF_STACKS_POST pass (change-set flow)
```

> `deploy.sh` runs the full ordered set; it skips stacks with no diff, so re-running is safe. To deploy
> only 07 the first time, you can still run the whole script — the other stacks report "No changes".
>
> ⚠️ **If 06-api has any diff (e.g. `api_handler.py` is modified on the branch), `deploy.sh` redeploys it
> and BLANKS every `*_RUNTIME_ARN`/`MEMORY_ID` on the api_handler Lambda** — the MCP Admin page then shows
> all agents **NOT DEPLOYED** and the Analyst chat returns **503** until you run Step 1.5. This is expected:
> `deploy_agents.py` re-patches the complete ARN set (run agents + backfill of the rest via `find_runtime`).
> **Do not skip Step 1.5**, even if you only meant to deploy the KB.

Capture the new ids from stack outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name dev-st21arbiter-poc-07-bedrock --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='KnowledgeBaseId' || OutputKey=='DataSourceId' || OutputKey=='UnstructuredBucketName'].[OutputKey,OutputValue]" \
  --output table
```

Export them for the rest of Phase 1 (replace with the real values):

```bash
export NEW_KB_ID=<KnowledgeBaseId-from-outputs>
export NEW_DS_ID=<DataSourceId-from-outputs>
export DOC_BUCKET=dev-st21arbiter-poc-unstructured
```

### 1.2 Populate the unstructured bucket (curated, no tabular)

The `dev-st21arbiter-poc-unstructured` bucket already exists (created out-of-band). If it is already
populated with the unstructured corpus, **skip to 1.3**. Otherwise the KB ingests whatever is in
`DOC_BUCKET` — the "unstructured only" guarantee is the copy filter. Copy the existing corpus,
**excluding structured/tabular**:

```bash
# a) Local baseline source docs (pdf/txt/json), skipping any csv/xlsx
aws s3 cp BaselineFiles/ "s3://${DOC_BUCKET}/baseline/" --recursive \
  --exclude "*.csv" --exclude "*.xlsx" \
  --exclude "_source/*" --exclude "_archive/*" --region us-east-1

# b) The processed baseline corpus (markdown + json synced by generate_baseline_corpus.py)
aws s3 cp "s3://dev-st21arbiter-poc-processed/baseline/" "s3://${DOC_BUCKET}/baseline/" --recursive \
  --exclude "*.csv" --exclude "*.xlsx" --region us-east-1
```

> Do **not** copy `processed/structured/` or `processed/athena-results/` — those are the Glue/Athena
> (structured) path and must stay out of the KB. Verify the bucket holds only unstructured formats:
> `aws s3 ls "s3://${DOC_BUCKET}/" --recursive | grep -iE '\.(csv|xlsx)$'` should return **nothing**.

### 1.3 Ingest

```bash
JOB_ID=$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "$NEW_KB_ID" --data-source-id "$NEW_DS_ID" \
  --region us-east-1 --query 'ingestionJob.ingestionJobId' --output text)
echo "ingestion job: $JOB_ID"

# Poll until COMPLETE
aws bedrock-agent get-ingestion-job \
  --knowledge-base-id "$NEW_KB_ID" --data-source-id "$NEW_DS_ID" \
  --ingestion-job-id "$JOB_ID" --region us-east-1 \
  --query 'ingestionJob.{status:status,stats:statistics}'
```

**Pass criteria:** `status = COMPLETE`, `numberOfNewDocumentsIndexed > 0`, `numberOfDocumentsFailed = 0`.
If any docs fail, inspect the job's `failureReasons` (S3 Vectors caps per-vector metadata — FIXED_SIZE/512
is well within limits, but confirm the stats).

### 1.4 Retrieve smoke test (before touching agents)

```bash
# New KB
aws bedrock-agent-runtime retrieve --knowledge-base-id "$NEW_KB_ID" \
  --retrieval-query '{"text":"acceptable use policy remote access"}' --region us-east-1 \
  --query 'retrievalResults[].{score:score,src:location.s3Location.uri}' --output table

# Old KB — eyeball that top hits are comparable (guards the L2 → cosine metric change)
aws bedrock-agent-runtime retrieve --knowledge-base-id 2ADHACW6LB \
  --retrieval-query '{"text":"acceptable use policy remote access"}' --region us-east-1 \
  --query 'retrievalResults[].{score:score,src:location.s3Location.uri}' --output table
```

Expect non-empty `retrievalResults` with grounded chunks. If the new KB returns nothing, **stop** and
recheck ingestion before repointing.

### 1.5 Repoint the 4 agents

> **Important:** `dev.json` is **not** the source of the agents' `KB_ID`. It flows only via the
> `KB_ID` env var read by [`deploy_agents.py`](../scripts/deploy_agents.py) (`:66` → injected at `:816`).
> So repointing = set the env var and redeploy the 4 specialists. No IAM change (the Retrieve grant in
> `09-agentcore.yaml` is `Resource:"*"`).

```bash
cd scripts && source .venv/bin/activate     # PEP 668 — always use the venv
KB_ID="$NEW_KB_ID" \
  python3 deploy_agents.py \
    --agents sharepoint-specialist awsconfig-specialist zscaler-specialist paloalto-specialist
```

- **Agent names use dashes** (`sharepoint-specialist`, …) — they must match `agent["name"]` in
  `deploy_agents.py` exactly. Underscores match nothing and silently skip every agent.
- **Do NOT pass `--skip-build`.** These ECR repos tag images with a Unix timestamp and have **no
  `:latest`** tag, so `--skip-build` makes `UpdateAgentRuntime` fail with "image identifier does not
  exist." A normal run rebuilds the 4 images via CodeBuild (arm64, a few min) and updates the runtimes.
- `GUARDRAIL_ID`/`GUARDRAIL_VERSION`/`MASTER_MEMORY_ID` auto-fill from `params/dev.json`; **`KB_ID` does
  not** (no dev.json fallback) — pass it explicitly.
- This run also re-patches the **complete** api_handler ARN set (backfill via `find_runtime`), repairing
  the 06-api blanking from Step 1.1.
- `servicenow-specialist` is intentionally excluded (it reads `KB_ID` but never calls retrieve).
- **Restore-only shortcut (no rebuild):** to clear a 503 / NOT-DEPLOYED without repointing the KB, run
  `python3 -c "import deploy_agents as d; d._patch_api_handler_lambda({})"` — it backfills all live
  runtime ARNs onto the api_handler.

### 1.6 End-to-end validation

Drive chat/scan flows that exercise each specialist and confirm grounded, cited answers:
- Analyst page → a compliance question that fans out to `sharepoint_lookup` / `awsconfig_lookup` /
  `zscaler_lookup` / `paloalto_lookup`.
- Optionally point [`tests-adversarial/features/test_kb_retrieval.py`](../tests-adversarial/features/test_kb_retrieval.py)
  at the new id and assert KB-grounding markers.

Confirm the 4 runtimes are `READY`:

```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query "agentRuntimes[?contains(agentRuntimeName, 'st21arbiter_poc')].[agentRuntimeName,status]" --output table
```

### 1.7 Housekeeping (after validation passes)

Update the literal-id references so the repo reflects the new KB:
- [`scripts/generate_baseline_corpus.py`](../scripts/generate_baseline_corpus.py) `:46-47` (defaults) + docstring `:25-26`
- [`instructions/DEPLOYMENT.md`](../instructions/DEPLOYMENT.md) `:471`
- [`Documents/roadmap.md`](roadmap.md) `:27`
- [`tests-adversarial/features/test_kb_retrieval.py`](../tests-adversarial/features/test_kb_retrieval.py) `:7` (comment)

> The pipeline params `KbId`/`KbDataSourceId` in [`dev.json`](../Infra/params/dev.json) now point at the
> **new** KB (`SQCLG3W09Y` / `NM2FVXL5T6`), so both `api_handler` and `processing_pipeline` ingest into
> the S3-Vectors KB once 05/06 are redeployed. See § 1.8 for the Data Pipeline UI cutover.

---

## 1.8 — Data Pipeline UI → new KB via the unstructured bucket

The Data Pipeline page now stores the **policy-document** group mixes (**Text / CSV+Text /
CSV+Text+docs**) in the `dev-st21arbiter-poc-unstructured` bucket, which the new KB's data source
reads whole. A new EventBridge rule on that bucket runs KB ingest + scan automatically. Other mixes
(`CSV only`, `Unstructured + Vector`, `Structured + Vector + Glue`) are untouched.

**One-time (a): CORS on the unstructured bucket.** Browser PUTs are cross-origin, so without a CORS
policy the presigned PUT fails with `S3 PUT failed: Failed to fetch`. The raw bucket's CORS is applied
out-of-band (see DEPLOYMENT.md) — copy it verbatim so the same origins (localhost + CloudFront) are
whitelisted:

```bash
# Copy the raw bucket's working CORS onto the unstructured bucket
aws s3api get-bucket-cors --bucket dev-st21arbiter-poc-raw --region us-east-1 \
  --query 'CORSRules' --output json > /tmp/raw-cors.json
aws s3api put-bucket-cors --bucket dev-st21arbiter-poc-unstructured --region us-east-1 \
  --cors-configuration "{\"CORSRules\": $(cat /tmp/raw-cors.json)}"
# verify:
aws s3api get-bucket-cors --bucket dev-st21arbiter-poc-unstructured --region us-east-1
```

If the raw bucket has no CORS to copy, apply an explicit rule instead (swap in your CloudFront domain):

```bash
aws s3api put-bucket-cors --bucket dev-st21arbiter-poc-unstructured --region us-east-1 \
  --cors-configuration '{"CORSRules":[{"AllowedOrigins":["http://localhost:5173","https://<cloudfront-domain>"],"AllowedMethods":["PUT","GET","HEAD"],"AllowedHeaders":["*"],"ExposeHeaders":["ETag"],"MaxAgeSeconds":3000}]}'
```

**One-time (b): enable EventBridge on the pre-existing unstructured bucket** (it is not CFN-managed, so the
`UnstructuredBucketObjectCreatedRule` in 05-compute only fires once the bucket emits events):

```bash
aws s3api put-bucket-notification-configuration \
  --bucket dev-st21arbiter-poc-unstructured \
  --notification-configuration '{"EventBridgeConfiguration":{}}' \
  --region us-east-1
# verify:
aws s3api get-bucket-notification-configuration \
  --bucket dev-st21arbiter-poc-unstructured --region us-east-1
```

**Redeploy the three stacks** (change-set flow via `deploy.sh`, or targeted):
`02-security` (adds the `-unstructured` PutObject grant to `api_handler`), `05-compute`
(processing_pipeline env + the new rule), `06-api` (api_handler `UNSTRUCTURED_BUCKET` env).

```bash
cd Infra && ./deploy.sh          # provisions/updates the set in order
```

> ⚠️ Redeploying `06-api` blanks the api_handler `*_RUNTIME_ARN`/`MEMORY_ID` env → re-run
> `scripts/deploy_agents.py` (no `--skip-build`) afterward or `/chat` 500s. Same gotcha as § 1.5.

**Verify end-to-end:** on the Data Pipeline page, create a group with content mix **Text** (or
CSV+Text), upload a `.pdf`/`.txt`; confirm the object lands in `s3://dev-st21arbiter-poc-unstructured/`,
a KB ingestion job runs on `SQCLG3W09Y`, and a scan-run row appears (`triggered_by=auto-ingest:<key>`).

```bash
# object landed in the unstructured bucket (not raw)
aws s3 ls s3://dev-st21arbiter-poc-unstructured/users/ --recursive --region us-east-1 | tail
# most recent ingestion job on the new KB
aws bedrock-agent list-ingestion-jobs --knowledge-base-id SQCLG3W09Y \
  --data-source-id NM2FVXL5T6 --region us-east-1 \
  --query 'ingestionJobSummaries|sort_by(@,&startedAt)[-1]'
```

> **Assumption — bucket encryption:** the presigned PUT to the unstructured bucket sends **no** SSE
> header, relying on the bucket's default encryption. `api_handler`'s role has `kms:*` on `*`, so this
> works whether the bucket defaults to SSE-S3 or an account CMK. If the bucket policy *denies* PUTs
> lacking a specific SSE header, tell me and I'll pin the header.
>
> **Note — CSV in CSV+Text mixes:** those `.csv` files land in the unstructured bucket and are ingested
> into the KB as text (not routed to Glue). That is the intended behavior for these mixes; `CSV only`
> still takes the Glue/Athena path unchanged.

---

## Phase 2 — Decommission OpenSearch (confirmation-gated, after soak)

Only proceed once the new KB has been validated in production for an agreed window.

### 2.1 Delete the old KB + data source (out-of-band → CLI)

They were created by `setup_bedrock_kb.py`, so CloudFormation can't manage them. **Delete the data
source first, then the KB.**

```bash
aws bedrock-agent delete-data-source \
  --knowledge-base-id 2ADHACW6LB --data-source-id KLUEZ1RNM5 --region us-east-1

aws bedrock-agent delete-knowledge-base \
  --knowledge-base-id 2ADHACW6LB --region us-east-1
```

### 2.2 Confirm nothing still imports the OSS exports

```bash
aws cloudformation list-imports --export-name dev-st21arbiter-poc-OpenSearchCollectionArn --region us-east-1
aws cloudformation list-imports --export-name dev-st21arbiter-poc-OpenSearchEndpoint       --region us-east-1
```

Both must return **no importers** (the Phase-1 07 rewrite dropped the only import). If either lists a
stack, **stop** and resolve it first.

### 2.3 Remove the OSS resources from 04-storage

Edit [`Infra/templates/04-storage.yaml`](../Infra/templates/04-storage.yaml) and delete:
- Resources: `OpenSearchCollection` (L504-515), `OpenSearchEncryptionPolicy` (L517-533),
  `OpenSearchNetworkPolicy` (L535-550), `OpenSearchDataAccessPolicy` (L554-571),
  `OpenSearchVPCEndpoint` (L573-582)
- Outputs: `OpenSearchCollectionArn` (L715-718), `OpenSearchCollectionEndpoint` (L719-722)

Then validate + deploy via the change-set flow:

```bash
cd Infra
aws cloudformation validate-template --template-body file://templates/04-storage.yaml --region us-east-1
./deploy.sh          # 04-storage update via change-set; OSS collection deletes (VPCE delete takes a few min)
```

### 2.4 Cleanup (non-blocking)

- Delete the now-orphaned imperative role created by the script (safe once the old KB is gone — confirm it's unused):
  ```bash
  aws iam list-attached-role-policies --role-name dev-st21arbiter-poc-kb-role --region us-east-1   # verify empty/unused
  # then delete inline policies + role (pass the CFN service role if the deploy user lacks iam:DeleteRole)
  ```
- Remove the stale `OpenSearchIndexName` param from [`dev.json`](../Infra/params/dev.json) `:60-62`.
- Mark [`scripts/setup_bedrock_kb.py`](../scripts/setup_bedrock_kb.py) obsolete for the KB/OSS path — its
  `cf_export("…OpenSearch…")` calls will now fail. Its `ensure_guardrail` logic is still useful; split it
  into a standalone guardrail script as a follow-up (the guardrail is still active and owned there).

---

## Rollback (if Phase-1 validation fails)

The old KB `2ADHACW6LB`, its role, and the ingest pipeline are untouched through Phase 1 — rollback is clean:

1. **Revert the agents** to the old KB:
   ```bash
   cd scripts && source .venv/bin/activate
   KB_ID=2ADHACW6LB \
     python3 deploy_agents.py \
       --agents sharepoint-specialist awsconfig-specialist zscaler-specialist paloalto-specialist
   ```
   (No `--skip-build` — these repos have no `:latest` tag; a normal run rebuilds the images.)
2. **Optionally remove the new stack.** The vector bucket + index are stack-owned and deleted with it;
   the pre-existing `-unstructured` docs bucket is **not** a stack resource, so it (and its data) survive
   the delete untouched:
   ```bash
   aws cloudformation delete-stack --stack-name dev-st21arbiter-poc-07-bedrock --region us-east-1
   ```
   Or leave it deployed idle (storage-only cost) pending a fix.
3. Revert any Phase-1.7 housekeeping edits already applied. `dev.json` `KbId`/`KbDataSourceId` were never
   changed, so nothing else needs unwinding.

---

## Verification checklist

- [ ] `cfn-lint templates/07-bedrock.yaml` clean; `validate-template` + change-set dry run succeed
- [ ] Ingestion `status = COMPLETE`, `numberOfNewDocumentsIndexed > 0`, `numberOfDocumentsFailed = 0`
- [ ] `retrieve` on the new KB returns non-empty, grounded results comparable to the old KB
- [ ] `s3 ls` on `-unstructured` shows no `.csv`/`.xlsx`
- [ ] 4 specialist runtimes `READY` after repoint; chat/scan returns cited compliance answers
- [ ] (Phase 2) `list-imports` empty for both OSS exports before removing them
- [ ] (Phase 2) 8-stack health check still passes; old KB `2ADHACW6LB` no longer exists

---

## Risks / notes

1. **KMS on the vector bucket** — the template uses SSE-S3 (AES256) on the vector bucket to avoid editing
   the shared CMK policy in [`02-security.yaml`](../Infra/templates/02-security.yaml) (its `ServiceUsage`
   names only `s3`/`secretsmanager`, not `s3vectors`). To force SSE-KMS on vectors instead, add
   `EncryptionConfiguration` to `PolicyVectorBucket` **and** add `s3vectors.amazonaws.com` to the
   `DataAtRestKey` `ServiceUsage` statement.
2. **Encryption of the pre-existing docs bucket** — `dev-st21arbiter-poc-unstructured` was created
   out-of-band, so confirm its encryption before ingesting. The KB role grants `kms:Decrypt` on the
   project CMK (`S3KeyArn`) only. If the bucket uses a **different** KMS key, ingestion fails with
   AccessDenied on the objects — add that key's ARN to the `KMSDecryptSourceDocs` statement in
   `07-bedrock.yaml`. (SSE-S3 or the project CMK need no change.) Check with:
   `aws s3api get-bucket-encryption --bucket dev-st21arbiter-poc-unstructured --region us-east-1`.
3. **Name collisions** — the new stack deliberately uses `-kb-s3v-role` / `-policy-kb-s3v`; the old
   `-kb-role` / `-policy-kb` names remain in use by the OpenSearch KB throughout Phase 1.
4. **`AWS::S3Vectors::*` availability** — confirmed GA/CFN-supported in us-east-1; the `validate-template` +
   change-set dry run is the final gate. Fallback if a type isn't registered: create the vector bucket +
   index imperatively (the [`rag_src/arbiter_rag/vectors.py`](../rag_src/arbiter_rag/vectors.py)
   `ensure_vector_bucket`/`ensure_index` pattern) and pass `IndexArn` into the KB via a parameter.
5. **Distance metric L2 → cosine** — low risk with normalized Titan v2 vectors; the §1.4 side-by-side
   retrieve is the guard.
6. **Deferred — pipeline cutover:** the live F1 auto-ingest (05-compute `processing_pipeline`) + the
   Data-Grouping sync (`api_handler`) keep writing to the **old** KB during/after Phase 1. Cutting them
   over is a separate change: point `dev.json` `KbId`/`KbDataSourceId` at the new ids, route unstructured
   uploads into `-unstructured`, and redeploy 05/06.
