# Sales & HR RAG — step-by-step setup & evaluation

How to stand up two RAG agents for a fictional Hawaiian electronics-components retailer, from a
laptop with zero AWS calls all the way to deployed, evaluated agents:

- **`Sales_Specialist`** — a **hybrid** agent (§0–§10). Two answer paths:
  - **Semantic** (fuzzy questions) — Amazon **S3 Vectors** `sales-facts` index, Titan v2 embeddings.
  - **Text-to-SQL** (exact aggregation) — read-only **Athena** over the Glue `hawaii_sales` table.
- **`HR_Specialist`** — a **semantic-only** agent over the Kai Components HR **policy PDFs**
  (unstructured path), see **[§11](#11-hr-unstructured-scenario--kai-components-policy-rag)**.

Everything runs through the shared `arbiter_rag` library (`rag_src/arbiter_rag/`), so the notebook
you experiment in and the deployed agent execute the *same code*.

> **Model:** Amazon **Nova 2 Lite** everywhere, for now. It's the single changeable swap point —
> see [§9](#9-swapping-the-model). Anthropic Claude models need an AWS Marketplace subscription.

---

## 0 · Prerequisites

- Python 3.10+ (this repo was validated on 3.13/3.14). Node 18+ for the UI.
- The ARBITER stacks already deployed (`Infra/deploy.sh`) — specifically `04-storage` (processed
  bucket, Glue DB `dev_st21arbiter_poc_structured`, workgroup `dev-st21arbiter-poc-wg`, structured
  crawler), `06-api`, and `09-agentcore` (ECR repos + the shared AgentCore role).
- AWS credentials for account **669810405473** (`us-east-1`) with Bedrock model access to
  **Titan Text Embeddings v2** and **Nova 2 Lite**, plus S3 Vectors enabled in the region.
- The 100-branch dataset delivered at
  `/Users/…/Demo_arbiter_RAG_files/Hawaiian_Electronics_100_CSV_Flat` (or your own path).

```bash
cd rag_src
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[data,notebook]"      # installs arbiter_rag + pandas + jupyter/nbconvert
python -c "import arbiter_rag; print('arbiter_rag', arbiter_rag.__version__)"
```

---

## 1 · Import the large dataset (local, no AWS)

```bash
# from the repo root
python3 scripts/import_sales_data.py \
  --src /Users/…/Demo_arbiter_RAG_files/Hawaiian_Electronics_100_CSV_Flat
# → copies 100 CSVs into data/Hawaii_Electronics_100/  (7,219 rows, 100 branches, 6 islands)
```

The 10-file `data/Hawaii_Sample_Sales/` set is already in the repo for quick iteration.

---

## 2 · Explore + experiment in the notebook (offline first)

Open `rag_src/notebooks/sales_rag_lab.ipynb`. The top **Experiment config** cell is the only cell you
edit. With the default `RUN_AWS = False`, run the whole notebook — it uses local pandas only (no
creds, no cost) and walks every logical step:

1. load the data → 2. serialize rows to branch×category facts → 3. (guarded) embed + ingest to S3
Vectors → 4. the **aggregation trap** (why top-k-sum ≠ the true total, cross-checked against pandas)
→ 5. text-to-SQL generate + `validate_sql` + a pandas ground-truth cross-check → 6. the router split.

Run it headless to confirm it's green:

```bash
cd rag_src/notebooks
jupyter nbconvert --to notebook --execute --inplace sales_rag_lab.ipynb
```

To run the AWS steps live, set `RUN_AWS = True` (and `RUN_ATHENA = True` for the SQL execution cell)
after completing §3–§4. Change one knob (dataset, `TOP_K`, `USE_RERANK`, chunking, model) and re-run
a step — that's the experiment loop.

---

## 3 · Provision the two data paths (AWS — billable)

**3a. Semantic index (S3 Vectors):**

```bash
source scripts/.venv/bin/activate    # or the rag_src venv; both have boto3
AWS_REGION=us-east-1 PROJECT=st21arbiter-poc DATASET=large \
  python3 scripts/ingest_sales_vectors.py
# → builds 1,312 branch×category facts, embeds with Titan v2, creates the
#   dev-st21arbiter-poc-sales-vectors bucket + sales-facts index, upserts the vectors.
```

**3b. SQL table (Athena/Glue):**

```bash
AWS_REGION=us-east-1 PROJECT=st21arbiter-poc DATASET=large \
  python3 scripts/seed_sales_structured.py
# → uploads structured/hawaii_sales/hawaii_sales.csv + starts the structured Glue crawler.
# wait ~1-2 min, then confirm the table:
aws glue get-crawler --name dev-st21arbiter-poc-structured-crawler --query 'Crawler.State'
aws glue get-table --database-name dev_st21arbiter_poc_structured --name hawaii_sales --query 'Table.Name'
```

---

## 4 · Deploy the Sales_Specialist agent

The agent code is `agents/sales_specialist/`. It imports `arbiter_rag` (injected into the image from
`rag_src/arbiter_rag` by `deploy_agents.py`) and is configured entirely by env vars.

**4a.** If `09-agentcore` was deployed before this change, redeploy it so the new **ECR repo**
(`sales-specialist`) and the **`s3vectors` IAM** statement land:

```bash
cd Infra
aws cloudformation validate-template --template-body file://templates/09-agentcore.yaml --region us-east-1
./deploy.sh            # change-set flow; only 09-agentcore actually changes
```

**4b.** Build + deploy the agent, and rewire the master + api_handler:

```bash
GUARDRAIL_ID=<guardrail_id> GUARDRAIL_VERSION=1 \
  python3 scripts/deploy_agents.py --agents sales-specialist master-orchestrator
```

> `KB_ID` is **not** needed here — the sales agent (S3 Vectors + Athena) and the master (fan-out)
> don't read it; only the five KB-backed specialists do. `deploy_agents.py` treats `KB_ID` as
> optional (empty → a harmless warning), so omit it for this scoped run. `GUARDRAIL_ID` is optional
> too but recommended: both the sales agent and the master apply it to their model when set.

This builds an arm64 image (CodeBuild), creates the `…_sales_specialist` runtime (VPC-attached,
shared AgentCore role), waits for **READY**, backfills `SALES_RUNTIME_ARN` onto the master's env, and
patches it onto the api_handler Lambda. Deploying the master in the same run wires its `sales_lookup`
tool. Verify:

```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[?contains(agentRuntimeName, `sales`)].[agentRuntimeName,status]' --output table
```

> **Gotcha:** a fresh `06-api` SAM deploy blanks every `*_RUNTIME_ARN` on the api_handler Lambda. If
> chat 500s or the Sales card shows *NOT DEPLOYED*, re-run the `deploy_agents.py` line above (its patch
> step is what restores `SALES_RUNTIME_ARN`).

---

## 5 · Wire the UI

The Sales card is already in `ui/src/pages/MCPChat.jsx` (`id: 'sales'`) with starter prompts, and
`/agent-status` resolves it via `_AGENT_DISPLAY_NAMES` in `api_handler.py`. Rebuild + redeploy the SPA:

```bash
cd ui && npm install && npm run build
# then sync ui/dist to the SPA bucket + invalidate CloudFront (see instructions/DEPLOYMENT.md §9),
# or re-run Infra/deploy.sh which calls post_deploy_ui.py.
```

Open the **MCP** page → the **Sales Specialist** card → ask a starter prompt. Aggregation questions
route to SQL; "how did X do" questions route to semantic search. The Analyst page's master
orchestrator can also delegate to it via `sales_lookup`.

---

## 6 · Evaluate at scale + derive enhancements

The golden set (`eval/golden/sales_hawaii_qa.jsonl`) carries **real** pandas-computed ground truth.
Regenerate it if the dataset changes:

```bash
cd rag_src && DATASET=large python eval/make_sales_golden.py
```

**Offline** (no AWS — golden integrity, fact coverage, routing-heuristic preview):

```bash
DATASET=large python eval/run_sales_eval.py
```

**Live** (needs §3 provisioned — numeric accuracy over Athena + retrieval recall@k over S3 Vectors):

```bash
AWS_REGION=us-east-1 PROJECT=st21arbiter-poc DATASET=large \
  python eval/run_sales_eval.py --live --stamp $(git rev-parse --short HEAD)
```

Each run writes a timestamped JSON to `eval/results/`.

### The enhancement loop
1. Run the live eval → read `numeric_accuracy`, `retrieval_recall_at_k`, `retrieval_hit_rate`.
2. Inspect failures (which SQL answers were wrong? which semantic queries missed their facts?).
3. Tune one lever: the fact **grain** (`arbiter_rag/serialization.build_sales_facts`), the SQL
   **schema hint** (`arbiter_rag/athena_sql.TABLE_SCHEMA`), retrieval **`EVAL_TOP_K`**, rerank
   (`RERANK_ENABLED`, needs `bedrock:Rerank` IAM), or the agent's **router prompt**.
4. Re-ingest (bump `CHUNKING_VERSION` if you changed the grain) and re-run → compare `eval/results/`.
5. Gate regressions with `eval/quality_gate.sh` (recall@k threshold).

---

## 7 · What lives where

| Concern | Path |
|---|---|
| Shared RAG library (single source of truth) | `rag_src/arbiter_rag/` |
| Experiment notebook | `rag_src/notebooks/sales_rag_lab.ipynb` · **HR:** `rag_src/notebooks/hr_rag_lab.ipynb` |
| Agent (image source) | `agents/sales_specialist/` · **HR:** `agents/hr_specialist/` |
| Data import / vector ingest / SQL seed | `scripts/import_sales_data.py`, `scripts/ingest_sales_vectors.py`, `scripts/seed_sales_structured.py` · **HR corpus:** `rag_src/data_generators/gen_hr_pdfs.py` |
| Golden set + evaluator | `rag_src/eval/make_sales_golden.py`, `rag_src/eval/run_sales_eval.py`, `…/golden/sales_hawaii_qa.jsonl` · **HR:** `rag_src/eval/run_hr_eval.py`, `…/golden/hr_qa.jsonl` |
| Deploy wiring | `scripts/deploy_agents.py` (AGENTS `sales-specialist`, `hr-specialist`), `Infra/templates/09-agentcore.yaml`, `Infra/params/dev.json` |
| Routing + UI | `Infra/functions/api_handler/api_handler.py`, `agents/master_orchestrator/agent.py`, `ui/src/pages/MCPChat.jsx` |

---

## 8 · Configuration reference (agent env vars)

Set by `deploy_agents.py` `env_overrides` for `sales-specialist`; the agent builds an `arbiter_rag`
`Settings` from these (it never reads `settings.toml`):

| Env | Default | Purpose |
|---|---|---|
| `MODEL_ID` | `us.amazon.nova-2-lite-v1:0` | generation model (the swap point) |
| `EMBEDDING_MODEL_ID` / `EMBEDDING_DIM` | `amazon.titan-embed-text-v2:0` / `1024` | must match ingest + index |
| `SALES_VECTOR_BUCKET` / `SALES_VECTOR_INDEX` | `dev-st21arbiter-poc-sales-vectors` / `sales-facts` | semantic path |
| `GLUE_DATABASE` / `GLUE_TABLE` | `dev_st21arbiter_poc_structured` / `hawaii_sales` | SQL path (GLUE_TABLE is the allowlist) |
| `ATHENA_WORKGROUP` / `ATHENA_OUTPUT` | `dev-st21arbiter-poc-wg` / `…/athena-results/` | SQL execution |
| `RERANK_ENABLED` | `false` | Bedrock rerank (needs `bedrock:Rerank` IAM) |

The **`hr-specialist`** agent uses a smaller set (semantic-only):

| Env | Default | Purpose |
|---|---|---|
| `MODEL_ID` | `us.amazon.nova-2-lite-v1:0` | generation model (`HrModelId` in `dev.json`) |
| `EMBEDDING_MODEL_ID` / `EMBEDDING_DIM` | `amazon.titan-embed-text-v2:0` / `1024` | must match ingest + index |
| `HR_VECTOR_BUCKET` / `HR_VECTOR_INDEX` | `dev-st21arbiter-poc-hr-vectors` / `hr-policies` | its own vector bucket (isolated from sales) |
| `RETRIEVAL_TOP_K` | `4` | passages returned per query |
| `RERANK_ENABLED` | `false` | Bedrock rerank (needs `bedrock:Rerank` IAM) |

---

## 9 · Swapping the model

Nova 2 Lite is the only model in use right now. To change it later, edit **one** value:

- Notebook / scripts / eval: `generation_model_id` in `rag_src/config/settings.toml` (or export
  `BEDROCK_GENERATION_MODEL_ID`).
- Deployed agent: `SalesModelId` in `Infra/params/dev.json` (or `SALES_MODEL_ID` env on the
  `deploy_agents.py` run) → becomes the agent's `MODEL_ID`.

Claude ids (e.g. `us.anthropic.claude-sonnet-4-6`) need the `us.` inference-profile prefix **and** an
accepted AWS Marketplace subscription, and a pricing row in `agents/_shared/token_usage.py`. Embeddings
(Titan v2) are independent of this swap.

---

## 10 · Troubleshooting

- **`ModuleNotFoundError: arbiter_rag` in the running agent** → the Dockerfile must
  `COPY arbiter_rag ./arbiter_rag`; the build injects it via the `extra_pkgs` key in `deploy_agents.py`.
- **`FileNotFoundError: …/config/settings.toml` in the agent** → something called `get_settings()`.
  The agent must build `Settings` from env (`_settings()`), never call `get_settings()`.
- **SQL tool returns "refused unsafe SQL" / "non-allowlisted table"** → `GLUE_TABLE` must equal the
  real Athena table (`hawaii_sales`); `validate_sql` allowlists exactly that one table.
- **Semantic tool AccessDenied** → `s3vectors:QueryVectors/GetVectors/ListVectors` on the shared role
  (added in `09-agentcore.yaml`); redeploy that stack. During bring-up you can widen the resource ARN
  to `"*"` then tighten.
- **Numbers look wrong** → never trust the semantic path for totals; those must come from
  `query_sales_sql`. The notebook's §4 trap demonstrates why.

---

## 11 · HR (unstructured) scenario — Kai Components policy RAG

The **`HR_Specialist`** is the semantic-only sibling of the sales agent: it answers employee
questions about HR **policy** (leave, benefits, compensation, conduct, payroll, perks) by
retrieving from PDF policy documents. Same library, same S3 Vectors mechanics — no SQL path.
It lives in its **own** vector bucket (`dev-st21arbiter-poc-hr-vectors`) for least-privilege
isolation from sales.

It also differs in *how* it generates: instead of the Strands tool-loop the other agents use,
it answers through the shared **streaming Converse** helper —
`retrieval.answer(..., stream=on_delta)` → `generation.generate_stream` (Bedrock
`converse_stream`). That gives (a) partial-display text/metadata deltas as the answer forms and
(b) **inline source citations** (`[1](HR-LEAVE-001)`) injected next to the text, plus a compact
`Sources:` footer. Prompt-based citations (Nova-compatible). The `hr_rag_lab` notebook §7
demonstrates both offline; `generation.inject_citations` is a pure, unit-tested function.

### 11.1 · Generate the corpus (local, no AWS)

```bash
# needs the [data] extra (reportlab + pypdf):  pip install -e "rag_src[data]"
python3 rag_src/data_generators/gen_hr_pdfs.py
# → writes 6 deterministic policy PDFs into data/Hawaii_HR_Policies/
```

The content is fixed (no randomness) so the golden set stays valid. Edit `HR_POLICIES` in
`gen_hr_pdfs.py` and re-run to change the corpus (then re-check the golden set).

### 11.2 · Explore in the notebook (offline first)

Open `rag_src/notebooks/hr_rag_lab.ipynb`. It defaults to `RUN_AWS = False` and runs top-to-bottom
with local cross-checks (load → chunk → retrieve via a lexical stand-in → eval). Flip
`RUN_AWS = True` (with creds) to exercise the real embed → S3 Vectors → generate path.

### 11.3 · Provision + ingest the `hr-policies` index (AWS — billable)

```bash
AWS_REGION=us-east-1 PROJECT=st21arbiter-poc python3 scripts/ingest_hr_vectors.py
# → chunks the 6 PDFs, embeds with Titan v2, creates dev-st21arbiter-poc-hr-vectors
#   + the hr-policies index, and upserts the chunk vectors. Idempotent.
```

### 11.4 · Deploy the HR_Specialist agent

First deploy the ECR repo + IAM (the `HrSpecialistRepo` and `HrS3VectorsQuery` statement added to
`09-agentcore.yaml`):

```bash
cd Infra && ./deploy.sh          # updates 09-agentcore (change-set) — or just that stack
```

Then build + register the runtime (and re-point master at it):

```bash
GUARDRAIL_ID=<guardrail_id> GUARDRAIL_VERSION=1 \
  python3 scripts/deploy_agents.py --agents hr-specialist master-orchestrator
```

`deploy_agents.py` injects `rag_src/arbiter_rag` into the image (`extra_pkgs`), sets the
`HR_VECTOR_BUCKET`/`HR_VECTOR_INDEX` env, and patches `HR_RUNTIME_ARN` onto the api_handler Lambda.
No `KB_ID` needed — the HR agent uses S3 Vectors, not the Bedrock KB. The **HR Specialist** card
then appears on the MCP page (§5 covers the UI sync).

### 11.5 · Evaluate retrieval

```bash
python3 rag_src/eval/run_hr_eval.py          # OFFLINE: golden integrity + lexical floor
python3 rag_src/eval/run_hr_eval.py --live   # LIVE: recall@k / hit-rate / MRR over hr-policies
```

Retrieval is the objective signal for the unstructured path — tune chunking (`CHUNK_STRATEGY`),
`top_k`, or rerank, re-ingest, and re-run to compare `eval/results/hr_*.json`.
