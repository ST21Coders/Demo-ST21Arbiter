# Reusable RAG Modules (Unstructured + Structured) — Incremental Enhancement Plan

> Companion to [`Documents/rag_enhancements_prompt.txt`](rag_enhancements_prompt.txt). Immediate scope = points 1 & 2; points 3–8 are the designed-for-now roadmap.

## Requirements (verbatim from `Documents/rag_enhancements_prompt.txt`)

> Think like a AWS Principle Solution Architect who is expert is desigining large scale Gen AI Systems+RAG using Amazon Bedrock Agentcore with S3 vectors

1. **Enhance the `hr_rag_lab` code to a reusable module** for handling future scenarios like
   a. Unstructured Specialist which accepts pdfs, word, text, json
   b. Generate Chunks, Embeddings and Vector indexes. Store in S3 Vector bucket
   c. Allow Common text based search like BM25 lexical
   d. For a given prompt, the agent should able to retrieve from S3 vector bucket & BM25 text search, merge results, pass to LLM to format the output
   e. Build this as a End to End system with redeployable solution. Include Observability, Evals for improvements
2. **Enhance the `sales_rag_lab` as a reusable module** for handling future scenarios like
   a. Accepts .csv, excel, parquet, flat files with columns & rows
   b. Create new catalogs in Glue. Use this for query based search (Athena SQL)
   c. Create chunks, embeddings, vectors and store in S3 Vector bucket. Use this for semantic search
   d. For a given prompt, the agent should able to retrieve from S3 vector bucket and/or run Athena Query, pass results to LLM to format the output
   e. Build this as a End to End system with redeployable solution. Include Observability, Evals for improvements
3. Keep Data Project, Group related segmentation as is. Under Group Contents drop down add new options: **Unstructured+Vector**, **Structured+Vector+Glue**
4. In UI, Data Pipeline page, Add new Processing Path with name **"DocuSearch"**. Use this when group content choice as "Unstructured+Vector"
5. In UI code, add below for DocuSearch processing path:
   a. Display short message: "Use this for semantic search from s3 vector"
   b. User should be able to create a folder in S3 bucket. upload selected files to it. **200 files maximum** from browser.
   c. Use `hr_rag_lab` code modules to process files from s3 folder, chunk, embed, vectors and store in S3 vector bucket
   d. Once User clicks on submit, run it as a **backend job**. Notify user when this job is done and ready to accept the prompts.
   e. Users can add more files & reingest to the same project as needed in future.
6. In UI code, enhance Structured Exports path when group content is Structured+Vector+Glue:
   a. Rename "Structured Exports" to **"Structured Analytics"**. Display short message: "Use this for Semantic + Analytics search from s3 vector"
   b. Accept .csv, excel, parquet, flat files with columns & rows (Tabular format files)
   c. User should be able to create a folder in S3 bucket. upload selected files to it. 200 files maximum from browser.
   d. Use `sales_rag_lab` code modules to process files from S3 folder:
      i. create new catalog in Glue for query search
      ii. chunks, embeddings, vectors and store in S3 Vector bucket for semantic search
   e. Once User clicks on submit, run it as a backend job. Notify user when this job is done and ready to accept the prompts.
   f. Users can add more files & Reingest to the same project as needed in future.
6. (bis) **Do Not change existing path "Policy Documents".** Need this path for KB related search feature. Adjust or modify the screen structure accordingly.
7. Add section or sub page to maintain a **list of all data processing jobs and show their status**.
8. **Reuse existing file upload code modules.**

---

## Context

The two modules being asked for **already exist in embryonic form** and the correct move is to **generalize what's there, not rebuild**. The repo contains a shared `rag_src/arbiter_rag/` library (chunking, Titan embeddings, S3 Vectors, retrieval, rerank, generation, guardrails, `athena_sql`, `serialization`, `evaluation`, `observability`), a working `agents/hr_specialist/` (PDF → S3 Vectors semantic RAG), a working `agents/sales_specialist/` (S3 Vectors semantic **+** Athena text-to-SQL hybrid), ingest scripts, an eval harness (`rag_src/eval/`), OTel observability, and full deploy wiring. The `arbiter_rag` invariant — **notebook == production, single source of truth** — must be preserved (no forked logic in the deployed agents).

Because points 3–8 are labeled "future requirements," the deliverable is split: **implement points 1–2 now** as redeployable modules, and design them so points 3–8 (a UI-driven bulk-upload → async ingest job → live-chat flow) land cleanly on top.

## What already exists (build on, don't rebuild)

| Capability | Where | Status |
|---|---|---|
| Chunk → Titan embed → S3 Vectors put/query/delete | [vectors.py](../rag_src/arbiter_rag/vectors.py), [embeddings.py](../rag_src/arbiter_rag/embeddings.py), [chunking.py](../rag_src/arbiter_rag/chunking.py) | ✅ |
| Full RAG turn (guardrail→retrieve→generate→cite) | [retrieval.py](../rag_src/arbiter_rag/retrieval.py) `answer()` | ✅ |
| Text-to-SQL over Athena/Glue with safety validator | [athena_sql.py](../rag_src/arbiter_rag/athena_sql.py) | ✅ |
| Unstructured agent (PDF) | [agents/hr_specialist/agent.py](../agents/hr_specialist/agent.py) | ✅ |
| Structured hybrid agent (vector + SQL router) | [agents/sales_specialist/agent.py](../agents/sales_specialist/agent.py) | ✅ |
| Eval harness + golden sets + quality gate | [rag_src/eval/](../rag_src/eval/), [evaluation.py](../rag_src/arbiter_rag/evaluation.py) | ✅ |
| Observability (OTel auto-instrument + JSON logs) | agent Dockerfiles, [observability.py](../rag_src/arbiter_rag/observability.py) | ✅ |
| Deploy/build/wiring | [scripts/deploy_agents.py](../scripts/deploy_agents.py), [09-agentcore.yaml](../Infra/templates/09-agentcore.yaml) | ✅ |

## The three real gaps (vs the requirements)

1. **Multi-format ingest** — HR loads only `*.pdf`; sales loads only CSV. Need **docx/txt/json** (1a) and **excel/parquet** (2a).
2. **BM25 lexical + hybrid fusion (1c, 1d)** — the biggest gap. Serving path is pure vector + optional Bedrock rerank. BM25 exists **only** as an offline eval floor. No lexical retrieval, no vector⊕lexical merge in any agent.
3. **Dataset-parameterized Glue (2b)** — sales uses one hard-wired `hawaii_sales` table + a fixed crawler; `serialization.build_sales_facts` hardcodes the Hawaii grain. "Create **new** catalogs in Glue" per arbitrary dataset is not generalized (api_handler's `_ensure_glue_csv_table` already does deterministic per-dataset `create_table` — reusable).

## Architecture decisions (Principal-SA rationale)

- **BM25 stays S3-native, no new engine.** S3 Vectors has no lexical search; do **not** introduce OpenSearch (adds OCU cost, leaves the S3-Vectors design). Instead: new `arbiter_rag/lexical.py` using `rank_bm25` over the same chunk texts (already stored as `chunk_text`/`fact_text` metadata). Merge via **Reciprocal Rank Fusion (RRF)** in `retrieval.py`. BM25 built **incrementally**: (Phase 1) rebuilt at agent cold-start from `vectors.list_vectors` — zero new infra, fine for ≤ a few-thousand chunks; (Phase 2) persisted as a **sidecar** to the general-purpose *processed* bucket at ingest (`s3://<processed>/bm25/<index>.json`) and loaded by the agent — deterministic, scalable. (Sidecar can't live in the S3-Vectors bucket — that namespace is service-managed.)
- **Ingestion runs as a dedicated async worker Lambda**, not in api_handler (300s cap; shouldn't carry pandas/pypdf/embeddings) or processing_pipeline (S3-event mover; long-inline-poll anti-pattern). New `data_ingest` worker bundles `arbiter_rag` + data extras, fired fire-and-forget (`InvocationType="Event"`) like the scanner, and owns the write-scope `s3vectors` IAM in one place.
- **Reuse the proven job pattern.** New `data-jobs` DDB table (PK `job_id`, GSI `project_id`); api_handler pre-writes `QUEUED`/`RUNNING` then async-invokes the worker; worker updates `SUCCEEDED`/`FAILED` + counts; UI polls `GET /data-jobs`. Mirrors `POST /scan` → `SCAN_RUNS_TABLE` → `GET /scan-runs` verbatim.
- **Reuse existing upload + segmentation (points 3, 8).** Keep `presignUpload` + `uploadToPresignedUrl` and the `arbiter.dataGrouping.v2.*` project/group model; add only a 200-file cap + new group-content options/paths. "Create a folder" == the existing per-group S3 prefix `projects/<projectId>/<groupName>/`.
- **Notifications:** none exists server-side (bell is derived from OPEN findings). Surface job completion via the new Jobs subpage + a poll hook; optionally extend the bell later. Don't over-build a notifications service.
- **Models unchanged:** Titan Embeddings v2 (1024-d, cosine) at ingest **and** query; generation defaults to Nova 2 Lite (Marketplace constraint). One-line swap via `MODEL_ID`.

---

## IMMEDIATE — points 1 & 2 (fully implemented, redeployable)

### Phase 0 — Generalize the `arbiter_rag` engine

- **Multi-format unstructured loader** — [loaders.py](../rag_src/arbiter_rag/loaders.py): add `iter_documents(folder)` dispatching by extension — `.pdf` (pypdf), `.docx` (python-docx), `.txt`/`.md` (plain), `.json` (flatten → text). Keep `iter_hr_documents` as a thin wrapper (HR metadata unchanged).
- **Multi-format tabular loader** — add `load_tabular(path_or_dir)`: `.csv`, `.xlsx`/`.xls` (`pd.read_excel`), `.parquet` (pyarrow); schema-inferred columns for arbitrary datasets; keep `HAWAII_SALES_COLUMNS` validation only for the demo.
- **BM25 module** — new `arbiter_rag/lexical.py`: `build_index(chunks)`, `search(query, k)`, `build_from_s3vectors(...)` (cold-start rebuild), `load_sidecar/save_sidecar` (Phase 2).
- **Hybrid fusion** — [retrieval.py](../rag_src/arbiter_rag/retrieval.py): add `hybrid_retrieve(...)` = vector top-N ⊕ BM25 top-N via RRF; `hybrid_answer(...)` (or `hybrid=True` on `answer()`) so agents opt in without forking. Log lexical hits + fused order.
- **Generic serializer** — [serialization.py](../rag_src/arbiter_rag/serialization.py): add `build_row_facts(df, dataset_id, grain=None)` (generalizes `build_sales_facts`, which stays for the demo).
- **Parameterize ingest** — factor reusable `ingest_unstructured(folder, bucket, index, ...)` and `ingest_tabular(folder, bucket, index, glue_table, ...)` out of [ingest_hr_vectors.py](../scripts/ingest_hr_vectors.py) / [ingest_sales_vectors.py](../scripts/ingest_sales_vectors.py). **This is the seam points 5c/6d consume.**
- **Verify:** `rag_src/eval` offline + notebooks run; existing golden sets pass; new keyword-heavy cases show BM25/RRF ≥ vector-only recall.

### Phase 1 — Enhance both agents; extend evals + observability; redeploy

- **HR agent** ([agent.py](../agents/hr_specialist/agent.py)) — switch to `hybrid_answer` (vector + cold-start BM25 RRF); accept multi-format corpus; add `HYBRID_ENABLED`, `BM25_TOP_K`, `RRF_K` env; requirements add `rank_bm25`.
- **Sales agent** ([agent.py](../agents/sales_specialist/agent.py)) — accept tabular formats; keep the two-tool router; optional `DATASET`/`GLUE_TABLE` for non-Hawaii datasets.
- **Evals** — [evaluation.py](../rag_src/arbiter_rag/evaluation.py) + [run_hr_eval.py](../rag_src/eval/run_hr_eval.py): report vector-only / BM25-only / RRF side by side; keep `quality_gate.sh`.
- **Observability** — structured lexical/fusion events via `observability.py`; CloudWatch retrieval-quality widget deferred (roadmap).
- **Redeploy** — [deploy_agents.py](../scripts/deploy_agents.py) hybrid `env_overrides`; **no IAM change** (query-only s3vectors already granted).
- **Verify:** `invoke_agent_runtime` both agents; `run_hr_eval.py --live` recall@k not regressed; a keyword query (exact SKU/policy code) vector-only missed now hits via BM25.

---

## ROADMAP — points 3–8 (designed-for-now, built incrementally)

### Phase 2 — Async ingest worker + jobs API + IAM (enables 5c/d, 6d/e, 7) — ✅ IMPLEMENTED (deploy pending)
- **Worker** `Infra/functions/data_ingest/` (`handler.py` + `Dockerfile` + `requirements.txt`) — a **container-image Lambda** (heavy deps + `arbiter_rag` COPY'd in; zip can't hold pandas/pyarrow and a macOS `sam build` would ship Mac wheels). Job types `docusearch` → `ingest.ingest_unstructured`, `structured_analytics` → `ingest.ingest_tabular` (the Phase 0 seams). Flips the `data-jobs` row RUNNING→SUCCEEDED/FAILED. Offline state-machine test: `test_handler.py` (4/4).
- **`data-jobs` DDB table** ([04-storage.yaml](../Infra/templates/04-storage.yaml), PK `job_id` / SK `created_at` / GSI `by-project`) + api_handler routes `POST /data-pipeline/ingest` (pre-write QUEUED + async `InvocationType=Event`) and `GET /data-jobs[/{id}]` — mirrors the scan pattern.
- **New IAM (the blocker, now granted):** worker role [13-data-ingest.yaml](../Infra/templates/13-data-ingest.yaml) gets `s3vectors:PutVectors/CreateIndex/CreateVectorBucket` (+query), `bedrock:InvokeModel` (Titan), S3 read raw+processed, DDB update jobs, KMS. api_handler ([02-security.yaml](../Infra/templates/02-security.yaml)) gets `lambda:InvokeFunction` on the worker (DDB RW already wildcarded); env in [06-api.yaml](../Infra/templates/06-api.yaml). Wired into [deploy.sh](../Infra/deploy.sh) (`--resolve-image-repos` + stack list).
- **Vector targets:** one env-scoped bucket per modality — `dev-<project>-docs-vectors` / `dev-<project>-analytics-vectors` — with a per-group/dataset index the worker creates on first run. **Glue** catalog for Structured Analytics stays with the existing `/data-grouping/materialize` flow; this route adds the semantic/vector half + job tracking. **BM25 sidecar deferred** — the HR agent's cold-start rebuild already works; a query agent over user corpora is Phase 3/4.
- **Deploy:** `sam` needs Docker running; `deploy.sh` builds the image + auto-manages its ECR repo. Live run not exercised this session (no Docker/AWS build).

### Phase 3 — UI Group Contents + processing paths (points 3, 4, 6, 6-bis)
- Add `Unstructured+Vector` and `Structured+Vector+Glue` to `GROUP_FILE_MIX_OPTIONS` ([DataPipeline.jsx:28](../ui/src/pages/DataPipeline.jsx#L28)).
- Add `PATH_DEFS` entries: **DocuSearch** ("Use this for semantic search from s3 vector") and rename **Structured Exports → Structured Analytics** ("Use this for Semantic + Analytics search from s3 vector"). **Leave Policy Documents (KB) untouched** (6-bis). Route paths off the group-content choice, not just the `.csv` extension.

### Phase 4 — UI bulk upload + submit-as-job + Jobs subpage (points 5, 6, 7, 8)
- Reuse `presignUpload`/`uploadToPresignedUrl` (point 8); add a **200-file cap** + batched concurrency; upload into the group prefix `projects/<projectId>/<groupName>/`.
- Submit → `POST /data-pipeline/ingest`; poll job; toast + surface on the new **Data Jobs** subpage (point 7). Re-ingest = same endpoint (deterministic chunk keys already idempotent — 5e/6f).

---

## Files to touch (immediate = Phase 0–1)

- Engine: [loaders.py](../rag_src/arbiter_rag/loaders.py), **new** `rag_src/arbiter_rag/lexical.py`, [retrieval.py](../rag_src/arbiter_rag/retrieval.py), [serialization.py](../rag_src/arbiter_rag/serialization.py), [config.py](../rag_src/arbiter_rag/config.py) (hybrid/BM25 fields), [pyproject.toml](../rag_src/pyproject.toml) (`python-docx`, `pyarrow`, `rank_bm25`).
- Ingest: [ingest_hr_vectors.py](../scripts/ingest_hr_vectors.py), [ingest_sales_vectors.py](../scripts/ingest_sales_vectors.py) (call reusable `ingest_*`).
- Agents: [agents/hr_specialist/agent.py](../agents/hr_specialist/agent.py) (+ requirements.txt), [agents/sales_specialist/agent.py](../agents/sales_specialist/agent.py).
- Evals: [evaluation.py](../rag_src/arbiter_rag/evaluation.py), [run_hr_eval.py](../rag_src/eval/run_hr_eval.py), golden `rag_src/eval/golden/hr_qa.jsonl` (add keyword cases).
- Deploy: [deploy_agents.py](../scripts/deploy_agents.py) (hybrid env_overrides).

## Verification (immediate scope)

1. **Unit/offline:** `cd rag_src && source .venv/bin/activate && python -m pytest` (or `rag_src/eval/run_hr_eval.py` offline) — loaders parse docx/txt/json + xlsx/parquet fixtures; RRF ranks a planted keyword doc first.
2. **Ingest:** run parameterized `ingest_hr_vectors.py` over a mixed folder (pdf+docx+txt+json) → S3 Vectors populated; `ingest_sales_vectors.py` over csv+parquet → facts + Glue table.
3. **Live retrieval:** `run_hr_eval.py --live` — recall@k not regressed vs `hr_live_latest.json`; a keyword-only query newly answered.
4. **Agents end-to-end:** redeploy via `deploy_agents.py`; `aws bedrock-agentcore ... invoke` HR + sales runtimes; confirm hybrid citations + SQL/semantic routing.
5. **Regression:** existing `hr`/`sales` golden sets still pass `quality_gate.sh`.

## Risks & notes

- **Immediate scope = Phase 0 + 1 only** (points 1 & 2). Phases 2–4 (points 3–8) are the roadmap; the Phase 0 `ingest_*` seams + generic loaders/serializer make them cheap later.
- **`s3vectors` write IAM is granted nowhere today** — the single biggest infra prerequisite for the Phase 2 job service; flagged so it isn't discovered late.
- Keep Titan v2 model+dim identical at ingest and query; preserve the `arbiter_rag` "notebook == production" single-source invariant.
- Bump `CHUNKING_VERSION` when changing chunking/serialization so re-ingest re-keys idempotently.
