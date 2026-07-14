# CLAUDE_RAG.md — Arbiter Sales RAG guide

Guidance for AI coding agents (and humans) working in `rag_src/`. Read this before making changes.

## What this is

A production-shaped, reusable **DIY RAG** system that answers questions about **Kai Components**, a
fictional Hawaiian electronics-components retailer. It powers **two** deployed AgentCore agents from
one shared library:

- **`Sales_Specialist`** (`agents/sales_specialist/`) — **hybrid**: a semantic path (Amazon **S3
  Vectors** + **Titan v2**) for fuzzy questions, and a **text-to-SQL** path (read-only **Athena** over
  a Glue table) for exact aggregation over the structured sales data.
- **`HR_Specialist`** (`agents/hr_specialist/`) — **semantic-only** over the HR **policy PDFs**
  (unstructured): leave, benefits, compensation, conduct, payroll, perks. Its own S3 Vectors
  `hr-policies` index (separate bucket from sales, for least-privilege isolation). Unlike the
  other agents (Strands tool-loop), it answers through the shared **streaming Converse** path
  (`retrieval.answer(..., stream=…)`) — partial-display deltas + **inline source citations**.

Generation is **Amazon Nova 2 Lite** via the Bedrock Converse API. The **notebooks and the shared
library are the centerpiece**, and both deployed agents import the same library — so what you validate
in a notebook is exactly what the agent runs. End-to-end setup + evaluation:
[`rag_instructions.md`](rag_instructions.md) (Sales §0–§10, HR §11).

## The one rule that shapes everything

**`rag_src/arbiter_rag/` is the single source of truth.** Both notebooks and both agents import it.
Never fork RAG logic into a notebook or into `agents/sales_specialist/` or `agents/hr_specialist/` —
add it to the library so every path stays identical. (The deploy build injects `rag_src/arbiter_rag`
into each agent image via the `extra_pkgs` key in `scripts/deploy_agents.py`; it is not copied/forked.)

## Repo map (what actually exists here)

| Path | Role |
|---|---|
| `arbiter_rag/` | Shared library: `config`, `preflight`, `loaders`, `chunking`, `embeddings`, `vectors`, `serialization`, `retrieval`, `rerank`, `generation`, `guardrails`, `athena_sql`, `evaluation`, `observability`. |
| `config/settings.toml` | **Model-swap point** + all tunables. Env vars override; `config/<env>.yaml` adds per-env knobs. |
| `notebooks/sales_rag_lab.ipynb` | Sales (hybrid) experiment lab — offline with `RUN_AWS=False`. |
| `notebooks/hr_rag_lab.ipynb` | **HR (unstructured) experiment lab** — PDF → chunk → S3 Vectors → retrieve → answer; offline with `RUN_AWS=False`. (`02_scenario_unstructured_hr.ipynb` is a legacy stub.) |
| `data_generators/gen_hr_pdfs.py` | Generates the 6 deterministic Kai Components HR policy PDFs (reportlab) → repo-root `data/Hawaii_HR_Policies/`. |
| `eval/` | Golden Q&A + evaluators. **Sales:** `make_sales_golden.py` (pandas ground truth) → `golden/sales_hawaii_qa.jsonl`, `run_sales_eval.py`. **HR:** `golden/hr_qa.jsonl`, `run_hr_eval.py` (retrieval recall@k). Both offline + `--live`; `quality_gate.sh`. |
| `pyproject.toml` | Editable install (`pip install -e rag_src`) exposing `import arbiter_rag`. `[data]` extra adds pandas/pyarrow + pypdf/reportlab (ingest + HR-corpus generation only). |
| `rag_instructions.md` | Step-by-step runbook (generate/import data → notebook → provision → deploy → eval), Sales + HR. |

Sample data lives OUTSIDE `rag_src/`, at repo-root `data/`: `Hawaii_Sample_Sales/` (10 branches),
`Hawaii_Electronics_100/` (100 branches — via `scripts/import_sales_data.py`), and
`Hawaii_HR_Policies/` (6 policy PDFs — via `rag_src/data_generators/gen_hr_pdfs.py`). There is
**no** `infra/`, `agent/`, or `tests/` here — the deployed infra + agents live in the outer repo
(`Infra/`, `agents/sales_specialist/`, `agents/hr_specialist/`, `scripts/`).

## Conventions

- **Model:** Amazon **Nova 2 Lite** only, for now. Swap via `generation_model_id` in
  `config/settings.toml` (or `BEDROCK_GENERATION_MODEL_ID`); for the agents, `SalesModelId` /
  `HrModelId` in `Infra/params/dev.json`. Everything reads it through `arbiter_rag.config` / the
  agent's `MODEL_ID`.
- **Config access:** the notebook, scripts, and eval call `arbiter_rag.config.get_settings()` — it
  loads `config/settings.toml`, then `config/<env>.yaml`, then env vars (highest precedence). The
  **deployed agent MUST NOT call `get_settings()`** (no `settings.toml` in the container, and the
  env-suffix name properties don't match ARBITER's `dev-`-prefixed resources) — it builds a `Settings`
  from `os.environ` and passes explicit bucket/index/database/workgroup names.
- **Env suffixing:** `Settings` derives env-scoped names by appending the env, e.g.
  `vector_bucket_name = f"{vector_bucket}-{env}"`, `glue_database_name = f"{glue_database}_{env}"`.
  These suit the notebook sandbox; the ingest scripts and agents instead pass **explicit** ARBITER
  resource names (`SALES_VECTOR_BUCKET` / `HR_VECTOR_BUCKET`, `GLUE_DATABASE`,
  `run_query(database=…, workgroup=…)`, and the index name straight into `vectors.query`).
- **Two vector indexes, two buckets:** sales facts → `…-sales-vectors`/`sales-facts`; HR policy
  chunks → `…-hr-vectors`/`hr-policies`. Separate buckets keep the two agents' `s3vectors` IAM
  scoped to only their own data. Each agent passes its bucket+index explicitly (never `hr_index_name`
  / `vector_bucket_name`, which would append `-dev`).
- **Path anchors** (`config.py`): `RAG_ROOT` = `rag_src/` (config + eval), `DATA_ROOT` = repo-root
  `data/`, `REPO_ROOT` = repo root. Don't collapse these back to a single `parents[2]`.
- **Boto3:** the S3 Vectors namespace is `s3vectors:*` (not `s3:*`). A vector's data is
  `{"float32": [...]}`. `put_vectors` accepts ≤ 500 per call — use `vectors.put_records` (batches + backs off).
- **Streaming + citations (one common helper):** `generation.generate_stream(prompt, on_delta=…,
  sources=…)` wraps Bedrock `converse_stream` — it accumulates text deltas (calls `on_delta` per
  chunk for partial display), reads token usage from the `metadata` event, then rewrites the model's
  `[n]` markers inline as `[n](doc_id)` via `generation.inject_citations` (a **pure**, offline-testable
  function). `retrieval.answer(..., stream=on_delta)` uses it and returns `RagAnswer.citations`.
  Citations are **prompt-based** (the model emits `[n]` against the numbered context passages) so they
  work on **Nova 2 Lite** — NOT the Claude-oriented native Converse document-citation feature. The
  `hr_specialist` agent calls this exact path; the `hr_rag_lab` notebook §7 demonstrates it.

## Gotchas (do not relearn these the hard way)

- **S3 Vectors index schema is IMMUTABLE.** Dimension, distance metric, and the non-filterable key set
  are fixed at `create_index`. The contracts are `vectors.SALES_NON_FILTERABLE_KEYS` (sales) and
  `vectors.HR_NON_FILTERABLE_KEYS` (HR — `chunk_text`/`title`/`source_uri`/`effective_date`). To change
  the fact grain or chunking, bump `chunking_version` and re-ingest (new keys) rather than mutating the index.
- **Structured data can't aggregate in vectors.** Route any count/sum/avg/top-N to `athena_sql`, never
  to the semantic index. The agent's system prompt and the notebook's §4 "aggregation trap" enforce this.
- **`athena_sql.TABLE_SCHEMA` must match the real table.** It is fed to the LLM to generate SQL; it is
  the Hawaii 19-column schema. `athena_sql.validate_sql()` is a **security control** — read-only,
  single-statement, single-table-allowlisted (`settings.glue_table`). Never weaken it.
- **The agent builds `Settings` from env**, never `get_settings()` (see Conventions).
- **`s3vectors:*` IAM** (query-only) is added to the shared AgentCore role in
  `Infra/templates/09-agentcore.yaml` — two statements, `SalesS3VectorsQuery` (…-sales-vectors) and
  `HrS3VectorsQuery` (…-hr-vectors). Athena/Glue/S3 for the sales SQL path is already there (reuses the
  existing structured Glue DB + workgroup); the HR agent needs no Athena/Glue.

## How to verify changes (offline, no AWS)

```bash
cd rag_src && python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[data,notebook]"
# sales
python -c "from arbiter_rag import loaders, serialization; \
  df=loaders.load_hawaii_sales('../data/Hawaii_Sample_Sales'); \
  print(len(serialization.build_sales_facts(df)), 'facts')"
jupyter nbconvert --to notebook --execute --inplace notebooks/sales_rag_lab.ipynb   # RUN_AWS=False
python eval/run_sales_eval.py                                                        # offline eval
# hr
python data_generators/gen_hr_pdfs.py                                                # 6 policy PDFs
jupyter nbconvert --to notebook --execute --inplace notebooks/hr_rag_lab.ipynb      # RUN_AWS=False
python eval/run_hr_eval.py                                                           # offline retrieval eval
```

Live (AWS) verification is the `--live` eval + a chat against the deployed agent — see
`rag_instructions.md` §3–§6 (sales) and §11 (HR).

## Style

Match the surrounding code: type hints, module docstrings that explain *why*, `from __future__ import
annotations`. Keep the library dependency-light — the agent runtime imports it, so heavy deps
(`pandas`, `pypdf`) stay lazy-imported inside the ingest-only functions (`loaders`, `serialization`)
that the query path never touches.
