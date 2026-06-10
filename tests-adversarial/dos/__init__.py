"""DoS / rate-limit layer (Block E).

Covers compliance items:
  * #51 — Lack of rate limiting / resource consumption
  * #64 — Application-layer DoS (oversized payload degradation)
  * #65 — Resource exhaustion (concurrent load on a single resource)

Every probe is intentionally small (5 routes × short burst, 3 routes × 3 sizes,
1 route × 10 concurrent requests) so a misconfigured run can't take down the
dev environment. Hard ceilings on `--dos-rps` (100) and `--dos-duration-seconds`
(30) enforce the safety bar in `conftest.py`.
"""
