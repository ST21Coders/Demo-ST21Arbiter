"""Bedrock Guardrails — apply on the way IN (user query) and OUT (model answer).

Guardrails screen for prompt injection, denied topics, and PII. This module degrades
gracefully: if guardrails are disabled in config, apply() is a no-op pass-through, so the
same pipeline runs in a bare dev account and a locked-down prod account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings, get_settings
from .embeddings import make_runtime_client


@dataclass
class GuardrailOutcome:
    intervened: bool
    text: str          # possibly-masked text (or the original if no guardrail)
    raw: dict[str, Any] | None = None


def apply(
    text: str,
    source: str,  # "INPUT" or "OUTPUT"
    settings: Settings | None = None,
    client: Any | None = None,
) -> GuardrailOutcome:
    """Run text through the configured guardrail. No-op if guardrails are disabled."""
    settings = settings or get_settings()
    if not settings.guardrails_enabled or not settings.guardrail_id:
        return GuardrailOutcome(intervened=False, text=text)

    client = client or make_runtime_client(settings.region)
    resp = client.apply_guardrail(
        guardrailIdentifier=settings.guardrail_id,
        guardrailVersion=settings.guardrail_version,
        source=source,
        content=[{"text": {"text": text}}],
    )
    intervened = resp.get("action") == "GUARDRAIL_INTERVENED"
    outputs = resp.get("outputs", [])
    masked = outputs[0]["text"] if outputs else text
    return GuardrailOutcome(intervened=intervened, text=masked, raw=resp)
