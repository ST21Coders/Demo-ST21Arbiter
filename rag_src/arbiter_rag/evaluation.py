"""Evaluation metrics — the objective feedback loop for the improvement cycle.

Two layers (see docs/evaluation.md):
  * Retrieval metrics (recall@k, MRR, hit-rate) — cheap, deterministic, no LLM; run these
    every time you touch chunking/embedding to see if the RIGHT documents come back.
  * Answer metrics — LLM-as-judge (faithfulness/correctness/relevance) plus a deterministic
    numeric check for sales aggregation answers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .config import Settings, get_settings
from .generation import generate


# --------------------------------------------------------------------------- #
# Retrieval metrics (pure functions).
# --------------------------------------------------------------------------- #
def recall_at_k(retrieved_ids: list[str], relevant_ids: list[str], k: int) -> float:
    """Fraction of relevant docs present in the top-k retrieved."""
    if not relevant_ids:
        return 0.0
    top = set(retrieved_ids[:k])
    return len(top & set(relevant_ids)) / len(set(relevant_ids))


def hit_at_k(retrieved_ids: list[str], relevant_ids: list[str], k: int) -> float:
    """1.0 if any relevant doc appears in the top-k, else 0.0."""
    return 1.0 if set(retrieved_ids[:k]) & set(relevant_ids) else 0.0


def reciprocal_rank(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    """1/rank of the first relevant doc (0 if none retrieved)."""
    relevant = set(relevant_ids)
    for i, rid in enumerate(retrieved_ids, start=1):
        if rid in relevant:
            return 1.0 / i
    return 0.0


@dataclass
class RetrievalReport:
    n: int
    k: int
    recall_at_k: float
    mrr: float
    hit_rate: float
    mean_top_distance: float | None


def aggregate_retrieval(per_case: list[dict[str, Any]], k: int) -> RetrievalReport:
    """Aggregate per-case results.

    Each case: {"retrieved_ids": [...], "relevant_ids": [...], "top_distance": float|None}.
    """
    if not per_case:
        return RetrievalReport(0, k, 0.0, 0.0, 0.0, None)
    recalls = [recall_at_k(c["retrieved_ids"], c["relevant_ids"], k) for c in per_case]
    rrs = [reciprocal_rank(c["retrieved_ids"], c["relevant_ids"]) for c in per_case]
    hits = [hit_at_k(c["retrieved_ids"], c["relevant_ids"], k) for c in per_case]
    dists = [c["top_distance"] for c in per_case if c.get("top_distance") is not None]
    return RetrievalReport(
        n=len(per_case),
        k=k,
        recall_at_k=sum(recalls) / len(recalls),
        mrr=sum(rrs) / len(rrs),
        hit_rate=sum(hits) / len(hits),
        mean_top_distance=(sum(dists) / len(dists)) if dists else None,
    )


# --------------------------------------------------------------------------- #
# Answer metrics.
# --------------------------------------------------------------------------- #
_JUDGE_SYSTEM = (
    "You are a strict evaluator. Score the assistant answer against the reference on three "
    "axes, each 1-5 (5 best): faithfulness (no claims beyond the reference/context), "
    "correctness (matches the reference), relevance (answers the question). "
    'Respond with ONLY minified JSON: {"faithfulness":n,"correctness":n,"relevance":n,"rationale":"..."}'
)


def judge_answer(
    question: str,
    answer: str,
    reference: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """LLM-as-judge scoring. Returns dict with the three scores + rationale."""
    settings = settings or get_settings()
    prompt = (
        f"Question: {question}\n\nReference answer: {reference}\n\n"
        f"Assistant answer: {answer}\n\nScore now."
    )
    raw = generate(prompt, system=_JUDGE_SYSTEM, settings=settings).text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {"faithfulness": 0, "correctness": 0, "relevance": 0, "rationale": f"unparseable: {raw[:120]}"}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"faithfulness": 0, "correctness": 0, "relevance": 0, "rationale": f"bad json: {raw[:120]}"}


_NUMBER_RE = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?")


def numeric_match(answer_text: str, expected_value: float, tolerance: float = 0.02) -> bool:
    """True if any number in the answer is within `tolerance` (relative) of expected.

    Used for sales aggregation answers, where being off by a rounding is acceptable but a
    wrong order of magnitude is not.
    """
    for token in _NUMBER_RE.findall(answer_text):
        try:
            value = float(token.replace("$", "").replace(",", ""))
        except ValueError:
            continue
        if expected_value == 0:
            if abs(value) < 1e-9:
                return True
        elif abs(value - expected_value) / abs(expected_value) <= tolerance:
            return True
    return False
