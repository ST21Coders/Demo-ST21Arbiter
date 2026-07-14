"""Ingest the Kai Components HR policy PDFs into Amazon S3 Vectors (the unstructured path).

Loads the six HR-policy PDFs (data/Hawaii_HR_Policies/), chunks them on section boundaries
(arbiter_rag.chunking), embeds each chunk with Titan v2, creates the S3 Vectors bucket +
`hr-policies` index if absent, and upserts the vectors. This is the script form of the
hr_rag_lab notebook's ingest. The deployed HR_Specialist agent then queries this exact index
(HR_VECTOR_BUCKET / HR_VECTOR_INDEX).

If the corpus is missing it is generated first (rag_src/data_generators/gen_hr_pdfs.py).
Idempotent: re-running upserts by deterministic chunk key (bump CHUNKING_VERSION to re-key).

Prerequisites:
  - boto3 with the s3vectors client, Bedrock model access to Titan v2 in the region.
  - The `arbiter_rag` library importable with the [data] extra (pip install -e "rag_src[data]").

Usage:
  source scripts/.venv/bin/activate        # or: pip install -e "rag_src[data]"
  AWS_REGION=us-east-1 PROJECT=st21arbiter-poc python3 scripts/ingest_hr_vectors.py
  # options via env:
  #   HR_VECTOR_BUCKET=...                 (default <env>-<project>-hr-vectors)
  #   HR_VECTOR_INDEX=hr-policies
  #   CHUNK_STRATEGY=semantic|fixed|recursive
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Make arbiter_rag importable without requiring an editable install.
_RAG_SRC = REPO_ROOT / "rag_src"
if (_RAG_SRC / "arbiter_rag").is_dir() and str(_RAG_SRC) not in sys.path:
    sys.path.insert(0, str(_RAG_SRC))

from arbiter_rag import chunking, embeddings, loaders, vectors  # noqa: E402
from arbiter_rag.config import Settings  # noqa: E402

REGION = os.environ.get("AWS_REGION", "us-east-1")
ENV = os.environ.get("ENVIRONMENT", "dev")
PROJECT = os.environ.get("PROJECT", "st21arbiter-poc")
PREFIX = f"{ENV}-{PROJECT}"

POLICY_DIR = REPO_ROOT / "data" / "Hawaii_HR_Policies"
BUCKET = os.environ.get("HR_VECTOR_BUCKET", f"{PREFIX}-hr-vectors")
INDEX = os.environ.get("HR_VECTOR_INDEX", "hr-policies")
EMBED_MODEL = os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))
CHUNK_STRATEGY = os.environ.get("CHUNK_STRATEGY", "semantic")
CHUNK_VERSION = os.environ.get("CHUNKING_VERSION", "v1")


def _settings() -> Settings:
    """Minimal Settings for the ingest path (embeddings read region/model/dim/chunking)."""
    return Settings(
        env=ENV, region=REGION, account="", expected_account_id="",
        generation_model_id="us.amazon.nova-2-lite-v1:0", generation_max_tokens=1024,
        generation_temperature=0.2, embedding_model_id=EMBED_MODEL, embedding_dim=EMBED_DIM,
        rerank_enabled=False, rerank_model_id="amazon.rerank-v1:0", rerank_candidates_k=20,
        rerank_top_k=4, retrieval_top_k=4, chunk_strategy=CHUNK_STRATEGY, chunk_max_chars=1200,
        chunk_overlap_chars=200, chunking_version=CHUNK_VERSION, vector_bucket=BUCKET,
        hr_index=INDEX, sales_index="sales-facts", distance_metric="cosine",
        glue_database="", glue_table="", athena_workgroup="primary", athena_output_prefix="",
        max_scanned_bytes=1024 * 1024 * 1024, guardrails_enabled=False, guardrail_id="",
        guardrail_version="DRAFT", ingest_batch_size=500, log_level="INFO",
    )


def _to_epoch_days(iso: str) -> int:
    return (dt.date.fromisoformat(iso) - dt.date(1970, 1, 1)).days if iso else 0


def main() -> int:
    if not POLICY_DIR.is_dir() or not list(POLICY_DIR.glob("*.pdf")):
        print(f"corpus missing at {POLICY_DIR} — generating it…")
        from data_generators import gen_hr_pdfs  # noqa: WPS433 (rag_src on sys.path)
        gen_hr_pdfs.write_pdfs()

    S = _settings()
    docs = loaders.iter_hr_documents(POLICY_DIR)
    print(f"corpus  : {len(docs)} policy PDFs from {POLICY_DIR}")

    chunks = []
    for d in docs:
        base_meta = {
            "policy_category": d["policy_category"], "title": d["title"],
            "source_uri": d["source_uri"], "effective_date": d["effective_date"],
            "effective_epoch": _to_epoch_days(d["effective_date"]),
        }
        chunks += chunking.chunk_document(
            d["text"], d["doc_id"], strategy=CHUNK_STRATEGY,
            max_chars=S.chunk_max_chars, overlap_chars=S.chunk_overlap_chars,
            chunking_version=CHUNK_VERSION, metadata=base_meta,
        )
    print(f"chunks  : {len(chunks)} (strategy={CHUNK_STRATEGY}, version={CHUNK_VERSION})")

    vx = vectors.make_client(REGION)
    print(f"vectors : ensuring bucket {BUCKET} + index {INDEX} ({EMBED_DIM}d, {S.distance_metric})")
    vectors.ensure_vector_bucket(vx, BUCKET)
    vectors.ensure_index(vx, BUCKET, INDEX, EMBED_DIM, S.distance_metric,
                         vectors.HR_NON_FILTERABLE_KEYS)

    print(f"embed   : {len(chunks)} chunks with {EMBED_MODEL} …")
    vecs = embeddings.embed_texts([c.text for c in chunks], S)
    recs = [{"key": c.id, "embedding": v, "metadata": {**c.metadata, "chunk_text": c.text}}
            for c, v in zip(chunks, vecs)]
    n = vectors.put_records(vx, BUCKET, INDEX, recs, batch_size=S.ingest_batch_size)
    print(f"✓ ingested {n} HR-policy chunk vectors into {BUCKET}/{INDEX}")
    print("  The HR_Specialist agent queries this index via HR_VECTOR_BUCKET/HR_VECTOR_INDEX.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
