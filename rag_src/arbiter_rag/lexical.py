"""BM25 (Okapi) lexical search — the keyword half of hybrid retrieval.

Requirement 1c/1d: "allow common text-based search like BM25 lexical" and merge it with
S3 Vectors semantic hits. S3 Vectors has no native lexical index, so this module provides
one over the SAME chunk texts already stored in the vector metadata (`chunk_text` for HR /
`fact_text` for sales facts). retrieval.py fuses the two rankings with Reciprocal Rank Fusion.

Deliberately PURE-PYTHON and dependency-free (no numpy / rank_bm25) so it runs inside the
light, boto3-only agent runtime. Built two ways: at agent cold start from the vectors
themselves (`build_index(vectors.iter_all_records(...))`), or — for larger corpora — from a
persisted sidecar (roadmap Phase 2). The tokenizer matches the offline eval floor so lexical
scores are comparable across notebook, eval, and production.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens (same rule as the offline eval lexical floor)."""
    return _TOKEN_RE.findall(text.lower())


@dataclass
class LexicalHit:
    """One BM25 result — key + score + the metadata (carrying chunk_text/fact_text)."""

    key: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.metadata.get("chunk_text") or self.metadata.get("fact_text") or ""


class BM25Index:
    """In-memory Okapi BM25 index. `search()` returns the top-k LexicalHits, best first."""

    def __init__(
        self,
        keys: list[str],
        corpus_tokens: list[list[str]],
        metadatas: list[dict[str, Any]],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.keys = keys
        self.metadatas = metadatas
        self.k1 = k1
        self.b = b
        self.doc_len = [len(t) for t in corpus_tokens]
        self.n = len(corpus_tokens)
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0

        self.tf: list[dict[str, int]] = []
        doc_freq: dict[str, int] = {}
        for tokens in corpus_tokens:
            freq: dict[str, int] = {}
            for tok in tokens:
                freq[tok] = freq.get(tok, 0) + 1
            self.tf.append(freq)
            for tok in freq:
                doc_freq[tok] = doc_freq.get(tok, 0) + 1
        # BM25 idf (the "plus 1" form keeps idf non-negative for common terms).
        self.idf = {
            tok: math.log(1 + (self.n - df + 0.5) / (df + 0.5)) for tok, df in doc_freq.items()
        }

    def search(self, query: str, k: int = 10) -> list[LexicalHit]:
        q_tokens = tokenize(query)
        if not q_tokens or self.n == 0:
            return []
        avgdl = self.avgdl or 1.0
        scored: list[tuple[int, float]] = []
        for i in range(self.n):
            freq = self.tf[i]
            dl = self.doc_len[i]
            score = 0.0
            for tok in q_tokens:
                f = freq.get(tok)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * dl / avgdl)
                score += self.idf.get(tok, 0.0) * (f * (self.k1 + 1)) / (denom or 1.0)
            if score > 0:
                scored.append((i, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [LexicalHit(self.keys[i], s, self.metadatas[i]) for i, s in scored[:k]]

    @classmethod
    def from_records(cls, records: Iterable[dict[str, Any]], **kwargs: Any) -> "BM25Index":
        """Build from `{key, text?, metadata}` records (text falls back to chunk/fact_text)."""
        keys: list[str] = []
        corpus: list[list[str]] = []
        metas: list[dict[str, Any]] = []
        for rec in records:
            meta = rec.get("metadata") or {}
            text = rec.get("text") or meta.get("chunk_text") or meta.get("fact_text") or ""
            if not text:
                continue
            keys.append(rec["key"])
            corpus.append(tokenize(text))
            metas.append(meta)
        return cls(keys, corpus, metas, **kwargs)


def build_index(records: Iterable[dict[str, Any]], **kwargs: Any) -> BM25Index:
    """Build a BM25Index from `{key, text?, metadata}` records (e.g. from S3 Vectors)."""
    return BM25Index.from_records(records, **kwargs)
