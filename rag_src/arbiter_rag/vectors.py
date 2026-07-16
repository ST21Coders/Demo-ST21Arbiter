"""Amazon S3 Vectors wrapper — bucket/index lifecycle, ingest, query, delete.

Design decisions baked in (see docs/architecture.md):
  * Index schema is IMMUTABLE at creation. The non-filterable key sets below are the
    contract — large text (chunk_text/fact_text) is non-filterable; everything else
    stays filterable so the agent can scope retrieval (category, state, access_level).
  * put_vectors accepts <= 500 vectors per call and can throttle (429) -> batched + backoff.
  * A vector's `data` is {"float32": [...]}; a query's `queryVector` is {"float32": [...]}.
  * Metadata filters use MongoDB-style operators ($eq, $in, $gte, $lte, $and).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

DATA_TYPE = "float32"
PUT_BATCH_LIMIT = 500  # hard S3 Vectors limit per put_vectors call

# Keys stored but EXCLUDED from filtering (declared at create_index; cannot change later).
HR_NON_FILTERABLE_KEYS = ["chunk_text", "title", "source_uri", "effective_date"]
SALES_NON_FILTERABLE_KEYS = ["fact_text", "title", "source_uri"]

_RETRYABLE = {"TooManyRequestsException", "ThrottlingException", "ServiceUnavailableException"}
_ALREADY_EXISTS = {"ConflictException", "BucketAlreadyOwnedByYou"}


def make_client(region: str) -> Any:
    """Return an s3vectors client for the given region."""
    return boto3.client("s3vectors", region_name=region)


@dataclass
class SearchHit:
    """One retrieval result."""

    key: str
    distance: float | None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The retrievable text (chunk_text for HR, fact_text for sales facts)."""
        return self.metadata.get("chunk_text") or self.metadata.get("fact_text") or ""

    @property
    def similarity(self) -> float | None:
        """Cosine similarity (1 - cosine distance); None if distance not returned."""
        return None if self.distance is None else 1.0 - self.distance


# --------------------------------------------------------------------------- #
# Lifecycle (idempotent create).
# --------------------------------------------------------------------------- #
def ensure_vector_bucket(client: Any, name: str, kms_key_arn: str | None = None) -> None:
    """Create the vector bucket if it does not already exist."""
    encryption = (
        {"sseType": "aws:kms", "kmsKeyArn": kms_key_arn}
        if kms_key_arn
        else {"sseType": "AES256"}
    )
    try:
        client.create_vector_bucket(vectorBucketName=name, encryptionConfiguration=encryption)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in _ALREADY_EXISTS:
            raise


def ensure_index(
    client: Any,
    bucket: str,
    index_name: str,
    dimension: int,
    distance_metric: str,
    non_filterable_keys: list[str],
    kms_key_arn: str | None = None,
) -> None:
    """Create the index if absent. WARNING: schema is immutable once created."""
    kwargs: dict[str, Any] = {
        "vectorBucketName": bucket,
        "indexName": index_name,
        "dataType": DATA_TYPE,
        "dimension": dimension,
        "distanceMetric": distance_metric,
        "metadataConfiguration": {"nonFilterableMetadataKeys": non_filterable_keys},
    }
    if kms_key_arn:
        kwargs["encryptionConfiguration"] = {"sseType": "aws:kms", "kmsKeyArn": kms_key_arn}
    try:
        client.create_index(**kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in _ALREADY_EXISTS:
            raise


# --------------------------------------------------------------------------- #
# Ingest.
# --------------------------------------------------------------------------- #
def put_records(
    client: Any,
    bucket: str,
    index_name: str,
    records: list[dict[str, Any]],
    batch_size: int = PUT_BATCH_LIMIT,
) -> int:
    """Upsert records into an index. Each record: {key, embedding, metadata}.

    Returns the number of vectors written. Batches at <= 500 with 429 backoff.
    """
    batch_size = min(batch_size, PUT_BATCH_LIMIT)
    written = 0
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        vectors = [
            {
                "key": r["key"],
                "data": {"float32": list(r["embedding"])},
                "metadata": r["metadata"],
            }
            for r in batch
        ]
        _put_with_backoff(client, bucket, index_name, vectors)
        written += len(vectors)
    return written


def _put_with_backoff(client: Any, bucket: str, index_name: str, vectors: list[dict], max_retries: int = 6) -> None:
    delay = 0.5
    for attempt in range(max_retries):
        try:
            client.put_vectors(vectorBucketName=bucket, indexName=index_name, vectors=vectors)
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _RETRYABLE and attempt < max_retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
                continue
            raise


# --------------------------------------------------------------------------- #
# Query.
# --------------------------------------------------------------------------- #
def build_filter(
    equals: dict[str, Any] | None = None,
    is_in: dict[str, list[Any]] | None = None,
    gte: dict[str, Any] | None = None,
    lte: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a MongoDB-style S3 Vectors metadata filter, AND-combining all clauses.

    build_filter(equals={"policy_category": "leave"}, gte={"effective_epoch": 20000})
    -> {"$and": [{"policy_category": {"$eq": "leave"}}, {"effective_epoch": {"$gte": 20000}}]}
    """
    clauses: list[dict[str, Any]] = []
    for k, v in (equals or {}).items():
        clauses.append({k: {"$eq": v}})
    for k, vals in (is_in or {}).items():
        clauses.append({k: {"$in": vals}})
    for k, v in (gte or {}).items():
        clauses.append({k: {"$gte": v}})
    for k, v in (lte or {}).items():
        clauses.append({k: {"$lte": v}})
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def query(
    client: Any,
    bucket: str,
    index_name: str,
    query_embedding: list[float],
    top_k: int,
    metadata_filter: dict[str, Any] | None = None,
    return_metadata: bool = True,
    return_distance: bool = True,
) -> list[SearchHit]:
    """Nearest-neighbour search. Requires s3vectors:QueryVectors (+ GetVectors for metadata)."""
    kwargs: dict[str, Any] = {
        "vectorBucketName": bucket,
        "indexName": index_name,
        "topK": top_k,
        "queryVector": {"float32": list(query_embedding)},
        "returnMetadata": return_metadata,
        "returnDistance": return_distance,
    }
    if metadata_filter:
        kwargs["filter"] = metadata_filter
    resp = client.query_vectors(**kwargs)
    return [
        SearchHit(key=v["key"], distance=v.get("distance"), metadata=v.get("metadata", {}))
        for v in resp.get("vectors", [])
    ]


def get_vectors(
    client: Any, bucket: str, index_name: str, keys: list[str], return_data: bool = False
) -> list[SearchHit]:
    """Fetch specific vectors by key (e.g. to inspect stored metadata)."""
    resp = client.get_vectors(
        vectorBucketName=bucket,
        indexName=index_name,
        keys=keys,
        returnData=return_data,
        returnMetadata=True,
    )
    return [SearchHit(key=v["key"], distance=None, metadata=v.get("metadata", {})) for v in resp.get("vectors", [])]


# --------------------------------------------------------------------------- #
# Delete (right-to-be-forgotten / re-ingest cleanup).
# --------------------------------------------------------------------------- #
def delete_keys(client: Any, bucket: str, index_name: str, keys: list[str]) -> int:
    """Delete vectors by key, batched at <= 500. Returns count deleted."""
    deleted = 0
    for start in range(0, len(keys), PUT_BATCH_LIMIT):
        batch = keys[start : start + PUT_BATCH_LIMIT]
        client.delete_vectors(vectorBucketName=bucket, indexName=index_name, keys=batch)
        deleted += len(batch)
    return deleted


def iter_all_records(client: Any, bucket: str, index_name: str, page_size: int = 500):
    """Yield every vector's {key, metadata} in the index (paginated list_vectors).

    Used to rebuild the BM25 lexical index at agent cold start from the same chunk texts
    stored as metadata (chunk_text/fact_text). Requires s3vectors:ListVectors. For large
    indexes prefer a persisted BM25 sidecar (roadmap) over scanning on every cold start.
    """
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "vectorBucketName": bucket,
            "indexName": index_name,
            "maxResults": page_size,
            "returnMetadata": True,
        }
        if token:
            kwargs["nextToken"] = token
        resp = client.list_vectors(**kwargs)
        for v in resp.get("vectors", []):
            yield {"key": v["key"], "metadata": v.get("metadata", {})}
        token = resp.get("nextToken")
        if not token:
            break


def list_keys_for_doc(client: Any, bucket: str, index_name: str, doc_id: str) -> list[str]:
    """Scan the index and return all vector keys whose metadata doc_id matches."""
    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "vectorBucketName": bucket,
            "indexName": index_name,
            "maxResults": 500,
            "returnMetadata": True,
        }
        if token:
            kwargs["nextToken"] = token
        resp = client.list_vectors(**kwargs)
        for v in resp.get("vectors", []):
            if v.get("metadata", {}).get("doc_id") == doc_id:
                keys.append(v["key"])
        token = resp.get("nextToken")
        if not token:
            break
    return keys


def delete_document(client: Any, bucket: str, index_name: str, doc_id: str) -> int:
    """Delete every vector belonging to one source document (GDPR-style erasure)."""
    keys = list_keys_for_doc(client, bucket, index_name, doc_id)
    return delete_keys(client, bucket, index_name, keys) if keys else 0
