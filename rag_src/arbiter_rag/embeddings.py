"""Amazon Titan Text Embeddings v2 wrapper.

CRITICAL RULE: the SAME embedding model + dimension must be used to embed documents
at ingest time AND to embed the query at search time. get_settings() guarantees this
because both paths read embedding_model_id / embedding_dim from one config.

Titan embeds one input per InvokeModel call, so embed_texts fans out with a thread
pool and retries throttling with exponential backoff.
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import Settings, get_settings

_RETRYABLE = {"ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException"}


def make_runtime_client(region: str) -> Any:
    """bedrock-runtime client with adaptive retries (good default for embed fan-out)."""
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=BotoConfig(retries={"max_attempts": 4, "mode": "adaptive"}),
    )


def _invoke_with_backoff(client: Any, body: dict, model_id: str, max_retries: int = 6) -> dict:
    delay = 0.5
    for attempt in range(max_retries):
        try:
            resp = client.invoke_model(
                modelId=model_id,
                body=json.dumps(body),
                accept="application/json",
                contentType="application/json",
            )
            return json.loads(resp["body"].read())
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _RETRYABLE and attempt < max_retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
                continue
            raise
    raise RuntimeError("unreachable")


def embed_text(text: str, settings: Settings | None = None, client: Any | None = None) -> list[float]:
    """Return the embedding vector for a single string."""
    settings = settings or get_settings()
    client = client or make_runtime_client(settings.region)
    body = {
        "inputText": text,
        "dimensions": settings.embedding_dim,
        "normalize": True,  # unit-normalized embeddings pair well with cosine distance
    }
    out = _invoke_with_backoff(client, body, settings.embedding_model_id)
    return out["embedding"]


def embed_texts(
    texts: list[str],
    settings: Settings | None = None,
    client: Any | None = None,
    max_workers: int = 8,
) -> list[list[float]]:
    """Embed many strings concurrently, preserving input order."""
    settings = settings or get_settings()
    client = client or make_runtime_client(settings.region)
    results: list[list[float] | None] = [None] * len(texts)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(embed_text, text, settings, client): i for i, text in enumerate(texts)
        }
        for future in concurrent.futures.as_completed(future_to_idx):
            results[future_to_idx[future]] = future.result()

    return [r for r in results if r is not None]  # order preserved; all filled on success
