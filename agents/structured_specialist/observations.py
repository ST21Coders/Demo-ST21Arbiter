"""Pure row→observation mapping for the structured specialist.

No AWS / strands imports so it is unit-testable in isolation (see
test_observations.py) and importable by agent.py. The mappers convert Athena
result rows (every column a STRING) into the exact observation shape the
rule-pack matchers consume — see agents/master_orchestrator/agent.py
::_seed_zscaler_observations.
"""
from __future__ import annotations

from typing import Any


def as_bool(v: Any) -> bool | None:
    """Athena returns booleans as the STRINGS 'true'/'false'. Coerce, or None if blank.

    This is the type-coercion landmine: a raw string 'false' is truthy in Python,
    so a matcher gating on `raw.registered_exception` (UC04) would silently misfire
    without this coercion.
    """
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "t", "1", "yes"):
        return True
    if s in ("false", "f", "0", "no"):
        return False
    return None


def map_zscaler_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map zscaler_rules rows → observation dicts.

    Matchers key on rule_id (presence) and, for UC04 only, on
    raw.registered_exception (a real bool). Other raw fields are display-only
    (carried into enforcement_evidence), so category/note pass through as-is.
    """
    obs: list[dict[str, Any]] = []
    for r in rows:
        rule_id = (r.get("rule_id") or "").strip()
        if not rule_id:
            continue
        raw: dict[str, Any] = {}
        reg = as_bool(r.get("registered_exception"))
        if reg is not None:
            raw["registered_exception"] = reg
        for k in ("category", "note"):
            v = r.get(k)
            if v:
                raw[k] = str(v)
        obs.append({"rule_id": rule_id, "action": (r.get("action") or "").strip(), "raw": raw})
    return obs


# source → (Glue table name, mapper). Add paloalto/awsconfig here as they ship.
MAPPERS = {
    "zscaler": ("zscaler_rules", map_zscaler_rows),
}
