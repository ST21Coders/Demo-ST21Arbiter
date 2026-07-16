"""HappyFeet RAG — a reusable DIY Retrieval-Augmented Generation library.

The SAME code powers the teaching notebooks and the deployed AgentCore chatbot, so
what you validate in a notebook is exactly what runs in production (no drift).

Pipeline:  chunk -> embed (Titan) -> S3 Vectors -> retrieve -> rerank -> generate (Claude Sonnet)

Public surface (import what you need):
    from happyfeet_rag.config import get_settings
    from happyfeet_rag import chunking, embeddings, vectors, retrieval, rerank, generation
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "config",
    "preflight",
    "loaders",
    "chunking",
    "embeddings",
    "vectors",
    "lexical",
    "serialization",
    "retrieval",
    "rerank",
    "generation",
    "guardrails",
    "athena_sql",
    "ingest",
    "evaluation",
    "observability",
]
