"""Lightweight structured logging shared by the library, notebooks, and agent.

Every retrieval logs its top distance score so you can alert when the best match is
still far away (a "no good answer in the corpus" signal — see docs/evaluation.md).
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_CONFIGURED = False


def get_logger(name: str = "arbiter_rag", level: str = "INFO") -> logging.Logger:
    """Return a process-wide logger that emits one JSON object per line."""
    global _CONFIGURED
    logger = logging.getLogger(name)
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(level.upper())
        logger.propagate = False
        _CONFIGURED = True
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit a structured event (JSON) — safe for CloudWatch Logs Insights parsing."""
    logger.info(json.dumps({"event": event, **fields}, default=str))
