"""Chunking strategies — the single biggest lever on RAG quality.

Implements the strategies the notebooks compare:
  * fixed_size  — split every N characters with overlap (simple, can break mid-sentence)
  * semantic    — split on paragraph/section boundaries, then pack up to a size cap
  * recursive   — try large separators first, split smaller only when a piece is too big

`chunk_document` returns Chunk objects carrying a DETERMINISTIC chunk_id so that
re-ingesting the same document with the same chunking_version is idempotent and a
document's vectors can be deleted later (right-to-be-forgotten).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Separators tried in order by the recursive splitter (largest structural unit first).
_RECURSIVE_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def chunk_id(doc_id: str, chunking_version: str, text: str) -> str:
    """Deterministic, collision-resistant id: {doc_id}-{16 hex of sha256}.

    Includes chunking_version so re-chunking the same doc with a new strategy
    produces new ids (old ones can be deleted) instead of silently colliding.
    """
    digest = hashlib.sha256(f"{doc_id}|{chunking_version}|{text}".encode()).hexdigest()
    return f"{doc_id}-{digest[:16]}"


@dataclass
class Chunk:
    """One retrievable unit of text plus the metadata that travels with its vector."""

    text: str
    doc_id: str
    chunk_index: int
    strategy: str
    chunking_version: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return chunk_id(self.doc_id, self.chunking_version, self.text)

    def char_len(self) -> int:
        return len(self.text)


# --------------------------------------------------------------------------- #
# Strategy implementations (operate on raw text, return list[str]).
# --------------------------------------------------------------------------- #
def fixed_size_chunks(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """Sliding window of `max_chars` characters advancing by (max_chars - overlap)."""
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be < max_chars")
    text = text.strip()
    if not text:
        return []
    step = max_chars - overlap_chars
    chunks = [text[i : i + max_chars].strip() for i in range(0, len(text), step)]
    return [c for c in chunks if c]


def semantic_chunks(text: str, max_chars: int) -> list[str]:
    """Pack whole paragraphs (blank-line separated) into chunks up to `max_chars`.

    Paragraphs longer than the cap are split on sentence boundaries so no single
    chunk exceeds the cap while boundaries still fall between sentences.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            units.append(para)
        else:
            units.extend(_split_sentences(para, max_chars))

    chunks: list[str] = []
    buffer = ""
    for unit in units:
        if not buffer:
            buffer = unit
        elif len(buffer) + 2 + len(unit) <= max_chars:
            buffer = f"{buffer}\n\n{unit}"
        else:
            chunks.append(buffer)
            buffer = unit
    if buffer:
        chunks.append(buffer)
    return chunks


def recursive_chunks(
    text: str, max_chars: int, overlap_chars: int, separators: Iterable[str] | None = None
) -> list[str]:
    """Recursively split on the largest separator that yields pieces under the cap."""
    seps = list(separators) if separators is not None else _RECURSIVE_SEPARATORS
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    sep = next((s for s in seps if s and s in text), "")
    if sep == "":
        # No separator helps — fall back to a hard fixed-size split.
        return fixed_size_chunks(text, max_chars, overlap_chars)

    pieces = text.split(sep)
    chunks: list[str] = []
    buffer = ""
    remaining = [s for s in seps if s != sep]
    for piece in pieces:
        candidate = piece if not buffer else f"{buffer}{sep}{piece}"
        if len(candidate) <= max_chars:
            buffer = candidate
        else:
            if buffer:
                chunks.append(buffer)
            if len(piece) > max_chars:
                chunks.extend(recursive_chunks(piece, max_chars, overlap_chars, remaining))
                buffer = ""
            else:
                buffer = piece
    if buffer:
        chunks.append(buffer)
    return [c.strip() for c in chunks if c.strip()]


def _split_sentences(text: str, max_chars: int) -> list[str]:
    """Greedily pack sentences up to the cap; hard-split any oversized sentence."""
    sentences = _SENTENCE_RE.split(text)
    out: list[str] = []
    buffer = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if buffer:
                out.append(buffer)
                buffer = ""
            out.extend(fixed_size_chunks(sentence, max_chars, 0))
            continue
        candidate = sentence if not buffer else f"{buffer} {sentence}"
        if len(candidate) <= max_chars:
            buffer = candidate
        else:
            out.append(buffer)
            buffer = sentence
    if buffer:
        out.append(buffer)
    return out


# --------------------------------------------------------------------------- #
# Public entry point used by notebooks + the ingestion pipeline.
# --------------------------------------------------------------------------- #
def chunk_document(
    text: str,
    doc_id: str,
    *,
    strategy: str = "semantic",
    max_chars: int = 1200,
    overlap_chars: int = 200,
    chunking_version: str = "v1",
    metadata: dict[str, Any] | None = None,
) -> list[Chunk]:
    """Chunk one document's text into Chunk objects, stamping shared metadata.

    `metadata` is copied into every chunk (e.g. policy_category, state, access_level)
    and augmented with chunk_index / strategy / chunking_version.
    """
    base_meta = dict(metadata or {})
    if strategy == "fixed":
        pieces = fixed_size_chunks(text, max_chars, overlap_chars)
    elif strategy == "semantic":
        pieces = semantic_chunks(text, max_chars)
    elif strategy == "recursive":
        pieces = recursive_chunks(text, max_chars, overlap_chars)
    else:
        raise ValueError(f"Unknown strategy {strategy!r}. Use fixed | semantic | recursive.")

    chunks: list[Chunk] = []
    for i, piece in enumerate(pieces):
        meta = dict(base_meta)
        meta.update(
            {
                "doc_id": doc_id,
                "chunk_index": i,
                "chunking_strategy": strategy,
                "chunking_version": chunking_version,
            }
        )
        chunks.append(
            Chunk(
                text=piece,
                doc_id=doc_id,
                chunk_index=i,
                strategy=strategy,
                chunking_version=chunking_version,
                metadata=meta,
            )
        )
    return chunks
