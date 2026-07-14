"""Text generation via the Bedrock Converse API.

The Converse API gives one model-agnostic interface, so swapping the generation
model (Claude Sonnet -> Nova -> Llama) is a config change with no code change. The
model id is always read from Settings.generation_model_id (the swap point).

Two call styles share the same request shape:
  * `generate`         — one-shot `converse`, returns the whole answer.
  * `generate_stream`  — `converse_stream`, accumulates text + metadata deltas as the
                         answer forms (so a caller can render partial output) and injects
                         inline source citations. See `inject_citations`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Settings, get_settings
from .embeddings import make_runtime_client

DEFAULT_SYSTEM = (
    "You are a retail sales analytics assistant for a Hawaiian electronics-components "
    "retailer. Answer ONLY from the provided context. If the context does not contain the "
    "answer, say you don't have that information. Be concise, and after each claim cite the "
    "supporting context number in square brackets, e.g. [1]."
)

# Prompt-based citation instruction appended to every RAG prompt. Nova-compatible: the
# model emits [n] markers that reference the numbered context blocks, and inject_citations
# turns them into inline source references. (Native Converse document citations are a
# separate, Claude-oriented feature; this path works on any Converse model.)
CITATION_INSTRUCTION = (
    "Cite your sources: immediately after each sentence that uses a context passage, add "
    "that passage's number in square brackets (e.g. [1], or [1][2] for several). Only cite "
    "passages you actually used."
)

# Matches a single marker or a comma-grouped one: [1], [2], [1, 2].
_CITE_MARKER_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


@dataclass
class Generation:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str


@dataclass
class StreamedGeneration:
    """Result of a streamed Converse turn.

    `text` is the raw model output (markers intact); `cited_text` is the same text with
    each `[n]` marker rewritten inline as `[n](source)`; `citations` lists the distinct
    sources actually referenced, in order.
    """

    text: str
    cited_text: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""


def inject_citations(
    text: str, sources: dict[int, dict[str, Any]] | None
) -> tuple[str, list[dict[str, Any]]]:
    """Rewrite `[n]` citation markers inline with their source, right next to the text.

    Pure function (no AWS), so the citation behaviour is unit-testable offline. `sources`
    maps a 1-based context number → a dict with at least `doc_id` (or `source`); e.g.
    `{1: {"doc_id": "HR-LEAVE-001", "title": "Paid Time Off…"}}`. Each `[1]` becomes
    `[1](HR-LEAVE-001)`; unknown markers are left untouched. Returns (cited_text, citations)
    where citations are the distinct referenced sources in first-appearance order.
    """
    sources = sources or {}
    used: dict[int, dict[str, Any]] = {}

    def _replace(match: re.Match) -> str:
        nums = [int(n) for n in match.group(1).replace(" ", "").split(",")]
        out = []
        for n in nums:
            src = sources.get(n)
            if not src:
                out.append(f"[{n}]")  # unknown marker — leave as the model wrote it
                continue
            label = src.get("doc_id") or src.get("source") or f"ctx{n}"
            out.append(f"[{n}]({label})")
            if n not in used:
                used[n] = {"marker": n, **src}
        return "".join(out)

    cited = _CITE_MARKER_RE.sub(_replace, text)
    citations = [used[n] for n in sorted(used)]
    return cited, citations


def _converse_request(prompt: str, system: str | None, settings: Settings) -> dict[str, Any]:
    """Shared Converse request body for both generate and generate_stream."""
    return {
        "modelId": settings.generation_model_id,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "system": [{"text": system or DEFAULT_SYSTEM}],
        "inferenceConfig": {
            "maxTokens": settings.generation_max_tokens,
            "temperature": settings.generation_temperature,
        },
    }


def generate(
    prompt: str,
    system: str | None = None,
    settings: Settings | None = None,
    client: Any | None = None,
) -> Generation:
    """Single-turn Converse call. maxTokens/temperature come from config."""
    settings = settings or get_settings()
    client = client or make_runtime_client(settings.region)
    resp = client.converse(**_converse_request(prompt, system, settings))
    usage = resp.get("usage", {})
    message = resp["output"]["message"]
    text = "".join(block.get("text", "") for block in message.get("content", []))
    return Generation(
        text=text.strip(),
        input_tokens=int(usage.get("inputTokens", 0)),
        output_tokens=int(usage.get("outputTokens", 0)),
        stop_reason=resp.get("stopReason", ""),
    )


def generate_stream(
    prompt: str,
    system: str | None = None,
    settings: Settings | None = None,
    client: Any | None = None,
    *,
    on_delta: Callable[[str], None] | None = None,
    sources: dict[int, dict[str, Any]] | None = None,
) -> StreamedGeneration:
    """Streamed Converse call: collect text + metadata events, then inject citations.

    Feature (a): as the model streams, each text delta is appended and, if `on_delta` is
    given, passed to it immediately — so a caller (notebook cell, SSE handler) can render
    the partial answer as it forms. The `metadata` event supplies the token usage.

    Feature (b): after the stream completes, the accumulated text's `[n]` markers are
    rewritten inline with their source via `inject_citations(sources)`.

    Model-agnostic (works on Nova 2 Lite): uses only text-delta + metadata events, not the
    Claude-oriented native citation content blocks.
    """
    settings = settings or get_settings()
    client = client or make_runtime_client(settings.region)
    resp = client.converse_stream(**_converse_request(prompt, system, settings))

    parts: list[str] = []
    input_tokens = output_tokens = 0
    stop_reason = ""
    for event in resp.get("stream", []):
        if "contentBlockDelta" in event:
            piece = event["contentBlockDelta"].get("delta", {}).get("text", "")
            if piece:
                parts.append(piece)
                if on_delta is not None:
                    on_delta(piece)
        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason", "")
        elif "metadata" in event:
            usage = event["metadata"].get("usage", {})
            input_tokens = int(usage.get("inputTokens", 0))
            output_tokens = int(usage.get("outputTokens", 0))

    text = "".join(parts).strip()
    cited_text, citations = inject_citations(text, sources)
    return StreamedGeneration(
        text=text,
        cited_text=cited_text,
        citations=citations,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason=stop_reason,
    )


def build_rag_prompt(question: str, contexts: list[dict[str, Any]]) -> str:
    """Assemble a grounded prompt from retrieved contexts.

    Each context is a dict with at least 'text'; optional 'source' / 'doc_id' are
    surfaced so the model can cite them.
    """
    blocks = []
    for i, ctx in enumerate(contexts, start=1):
        source = ctx.get("doc_id") or ctx.get("source") or f"ctx{i}"
        blocks.append(f"[{i}] (source: {source})\n{ctx['text']}")
    context_text = "\n\n".join(blocks) if blocks else "(no context retrieved)"
    return (
        "Use the following numbered context passages to answer the question.\n\n"
        f"=== CONTEXT ===\n{context_text}\n=== END CONTEXT ===\n\n"
        f"{CITATION_INSTRUCTION}\n\n"
        f"Question: {question}\n\nAnswer:"
    )
