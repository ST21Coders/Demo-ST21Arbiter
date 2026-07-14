"""Reusable ingest pipelines: files -> chunk/serialize -> embed (Titan) -> S3 Vectors.

One place for the bulk-ingest orchestration so the "notebook == production" invariant
extends to ingestion. Called by:
  * the offline scripts (scripts/ingest_*_vectors.py) and notebooks, and
  * (roadmap, Phase 2) the async `data_ingest` worker Lambda behind the DocuSearch /
    Structured Analytics UI paths.

Ingest-time only: pulls in the loaders/serialization code, which need the `data` extra
(pandas/pypdf/python-docx/pyarrow). The query path (agents) never imports this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import chunking, embeddings, loaders, serialization, vectors
from .config import Settings


def ingest_unstructured(
    folder: str | Path,
    bucket: str,
    index: str,
    settings: Settings,
    *,
    region: str | None = None,
    non_filterable_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Chunk + embed every supported document under `folder` into an S3 Vectors index.

    Uses loaders.iter_documents (pdf/docx/txt/md/json). The full chunk text is stored as
    `chunk_text` metadata so hybrid (BM25) retrieval can rebuild a lexical index from the
    same vectors. Idempotent by deterministic chunk key (bump settings.chunking_version).
    """
    region = region or settings.region
    docs = loaders.iter_documents(folder)
    chunks = []
    for d in docs:
        base_meta = {
            "title": d["title"],
            "source_uri": d["source_uri"],
            "doc_type": d.get("doc_type", ""),
        }
        chunks += chunking.chunk_document(
            d["text"], d["doc_id"], strategy=settings.chunk_strategy,
            max_chars=settings.chunk_max_chars, overlap_chars=settings.chunk_overlap_chars,
            chunking_version=settings.chunking_version, metadata=base_meta,
        )

    vx = vectors.make_client(region)
    vectors.ensure_vector_bucket(vx, bucket)
    vectors.ensure_index(
        vx, bucket, index, settings.embedding_dim, settings.distance_metric,
        non_filterable_keys or vectors.HR_NON_FILTERABLE_KEYS,
    )
    vecs = embeddings.embed_texts([c.text for c in chunks], settings)
    recs = [
        {"key": c.id, "embedding": v, "metadata": {**c.metadata, "chunk_text": c.text}}
        for c, v in zip(chunks, vecs)
    ]
    written = vectors.put_records(vx, bucket, index, recs, batch_size=settings.ingest_batch_size)
    return {
        "documents": len(docs), "chunks": len(chunks), "vectors": written,
        "bucket": bucket, "index": index,
    }


def ingest_tabular(
    folder: str | Path,
    bucket: str,
    index: str,
    settings: Settings,
    *,
    dataset_id: str,
    grain: list[str] | None = None,
    region: str | None = None,
    source_uri: str = "",
) -> dict[str, Any]:
    """Serialize csv/excel/parquet rows to NL facts, embed, and upsert into S3 Vectors.

    This is the SEMANTIC half of the Structured Analytics path; the Glue-catalog (Athena)
    half is registered separately (api_handler._ensure_glue_csv_table / the ingest scripts).
    """
    region = region or settings.region
    df = loaders.load_tabular(folder)
    facts = serialization.build_row_facts(
        df, dataset_id, grain=grain, source_uri=source_uri,
        chunking_version=settings.chunking_version,
    )

    vx = vectors.make_client(region)
    vectors.ensure_vector_bucket(vx, bucket)
    vectors.ensure_index(
        vx, bucket, index, settings.embedding_dim, settings.distance_metric,
        vectors.SALES_NON_FILTERABLE_KEYS,
    )
    vecs = embeddings.embed_texts([f["text"] for f in facts], settings)
    recs = [{"key": f["key"], "embedding": v, "metadata": f["metadata"]} for f, v in zip(facts, vecs)]
    written = vectors.put_records(vx, bucket, index, recs, batch_size=settings.ingest_batch_size)
    return {
        "rows": int(len(df)), "facts": len(facts), "vectors": written,
        "bucket": bucket, "index": index,
    }
