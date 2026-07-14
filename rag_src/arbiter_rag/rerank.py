"""Reranking via the Bedrock Rerank API (Amazon Rerank / Cohere Rerank).

Vector search optimizes for embedding similarity, which is not the same as relevance.
Reranking re-scores the top-N candidates with a cross-encoder that reads the query and
each document together, then keeps the best top_k. Classic pattern: retrieve 20, rerank
to 5. Toggle with [rerank].enabled in settings.toml.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3

from .config import Settings, get_settings


@dataclass
class RerankResult:
    index: int          # position in the input documents list
    relevance_score: float
    text: str


def make_rerank_client(region: str) -> Any:
    """Rerank lives on the bedrock-agent-runtime client."""
    return boto3.client("bedrock-agent-runtime", region_name=region)


def _model_arn(region: str, model_id: str) -> str:
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"


def rerank(
    query: str,
    documents: list[str],
    top_k: int | None = None,
    settings: Settings | None = None,
    client: Any | None = None,
) -> list[RerankResult]:
    """Rerank `documents` against `query`; return the top_k most relevant, best first."""
    settings = settings or get_settings()
    if not documents:
        return []
    client = client or make_rerank_client(settings.region)
    k = min(top_k or settings.rerank_top_k, len(documents))

    resp = client.rerank(
        queries=[{"type": "TEXT", "textQuery": {"text": query}}],
        sources=[
            {
                "type": "INLINE",
                "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": doc}},
            }
            for doc in documents
        ],
        rerankingConfiguration={
            "type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {
                "modelConfiguration": {"modelArn": _model_arn(settings.region, settings.rerank_model_id)},
                "numberOfResults": k,
            },
        },
    )
    return [
        RerankResult(
            index=r["index"],
            relevance_score=r["relevanceScore"],
            text=documents[r["index"]],
        )
        for r in resp.get("results", [])
    ]
