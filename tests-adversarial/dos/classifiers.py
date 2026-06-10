"""dos/classifiers.py — pure (status, severity, reason) verdicts.

Three classifiers, one per test module:

  * ``classify_rate_limit_burst``  → test_rate_limit.py
  * ``classify_oversize_payload``  → test_payload_oversize.py
  * ``classify_concurrent_burst``  → test_resource_exhaustion.py

Severity bands match the auth/headers layer convention:

  HIGH    — service is buckling under load (500s, timeouts, monotonic
            latency growth without throttle).
  MEDIUM  — service answers but does not protect itself (no 429s on a sustained
            burst, large payloads accepted with 5x+ baseline latency,
            malformed responses under concurrency).
  LOW     — informational only (not used by the dos layer today, reserved
            for future degradation gradients).

Status is the literal CellStatus value string ``"pass"`` or ``"fail"`` so the
results writer can drop the verdict straight into a TestResult row.
"""

from __future__ import annotations

from collections.abc import Sequence

# ──────────────────────────── severity bands ─────────────────────────────────

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

# Hard ceilings on the CLI flags. Defined here (not in conftest) so the unit
# tests can import them without pulling pytest in.
DOS_RPS_HARD_CEILING = 100
DOS_DURATION_HARD_CEILING_SECONDS = 30

# Latency-degradation thresholds for the oversized-payload classifier.
# Anything > LATENCY_MEDIUM_X but < LATENCY_HIGH_X is FAIL MEDIUM. Anything
# above LATENCY_HIGH_X (or a hard timeout) is FAIL HIGH.
LATENCY_MEDIUM_X = 3.0
LATENCY_HIGH_X = 5.0

# Above this wall-clock the oversized-payload probe treats the call as a
# timeout (FAIL HIGH) regardless of whether `requests` raised. Used by the
# classifier when the caller couldn't capture an explicit timeout exception.
TIMEOUT_SECONDS_FLOOR = 30.0


# ────────────────────── rate-limit burst classifier ──────────────────────────


def classify_rate_limit_burst(
    status_codes: Sequence[int],
    latencies_ms: Sequence[float],
) -> tuple[str, str | None, str]:
    """Classify the outcome of a sustained burst against one route.

    Inputs:
      status_codes: per-request HTTP status (0 = connection error / timeout).
      latencies_ms: per-request wall-clock in milliseconds, paired by index.

    Verdicts:
      PASS         — at least one 429 observed AND no connection-error / 500.
      FAIL HIGH    — any 500 OR any zero-status (connection drop / timeout)
                     OR all 200s with monotonically increasing latency
                     (API buckling but no throttle).
      FAIL MEDIUM  — no 429 observed AND latency is flat (no rate limiting
                     in place, but the API is keeping up).

    The "monotonic increase" detector compares the mean of the last quartile
    of latencies to the mean of the first quartile; a >2x ratio is the
    "buckling" signal. Small bursts (<8 requests) fall back to first-vs-last
    comparison to avoid quartile noise.
    """
    if not status_codes:
        return "fail", SEVERITY_MEDIUM, "no responses recorded"
    if len(status_codes) != len(latencies_ms):
        raise ValueError(
            "status_codes and latencies_ms must be the same length "
            f"(got {len(status_codes)} vs {len(latencies_ms)})."
        )

    # Critical failures first — these are HIGH regardless of throttle behavior.
    if any(code == 0 for code in status_codes):
        return (
            "fail",
            SEVERITY_HIGH,
            "transport-layer failure (timeout / connection drop) under burst",
        )
    if any(500 <= code < 600 for code in status_codes):
        return (
            "fail",
            SEVERITY_HIGH,
            f"server error under burst (status {next(c for c in status_codes if 500 <= c < 600)})",
        )

    saw_throttle = any(code == 429 for code in status_codes)
    if saw_throttle:
        n_throttle = sum(1 for code in status_codes if code == 429)
        return (
            "pass",
            None,
            f"{n_throttle}/{len(status_codes)} responses throttled (HTTP 429)",
        )

    # No 429, no 500, no transport error. Now decide between MEDIUM (flat
    # latency, no protection) and HIGH (monotonic growth, buckling).
    if _latency_is_monotonic_buckling(latencies_ms):
        return (
            "fail",
            SEVERITY_HIGH,
            "no 429 observed and latency increases monotonically — API is buckling without throttling",
        )
    return (
        "fail",
        SEVERITY_MEDIUM,
        f"no 429 observed across {len(status_codes)} requests — rate limiting absent",
    )


def _latency_is_monotonic_buckling(latencies_ms: Sequence[float]) -> bool:
    """Heuristic: is the last quartile mean > 2x the first quartile mean?

    Returns False for short or empty sequences (no signal).
    """
    n = len(latencies_ms)
    if n < 4:
        # Fallback: compare last to first directly for short sequences.
        if n < 2:
            return False
        return latencies_ms[-1] > 2.0 * latencies_ms[0] and latencies_ms[0] > 0
    q = max(1, n // 4)
    first_mean = sum(latencies_ms[:q]) / q
    last_mean = sum(latencies_ms[-q:]) / q
    if first_mean <= 0:
        return False
    return last_mean > 2.0 * first_mean


# ──────────────────── oversized-payload classifier ───────────────────────────


def classify_oversize_payload(
    baseline_status: int,
    baseline_ms: float,
    oversize_status: int,
    oversize_ms: float,
    *,
    timed_out: bool = False,
) -> tuple[str, str | None, str]:
    """Classify a (baseline, oversize) request pair for one route.

    PASS:
      * oversize_status is 4xx (request rejected — the API has a body limit), OR
      * oversize latency stayed below `LATENCY_MEDIUM_X * baseline_ms`.
    FAIL MEDIUM:
      * oversize_status is 2xx AND oversize latency between MEDIUM_X * baseline
        and HIGH_X * baseline (degraded, but the request still completed).
    FAIL HIGH:
      * `timed_out=True` (the caller hit its read timeout), OR
      * oversize_status == 0 (connection error), OR
      * oversize_status is 5xx, OR
      * oversize latency >= HIGH_X * baseline.

    Inputs:
      baseline_status, baseline_ms: small-body reference call.
      oversize_status, oversize_ms: large-body call. status=0 means transport
                                    error.
      timed_out: True if the caller's read timeout fired before a response was
                 received.
    """
    if timed_out or oversize_status == 0:
        return (
            "fail",
            SEVERITY_HIGH,
            (
                f"oversized payload timed out after {oversize_ms:.0f} ms "
                f"(baseline {baseline_ms:.0f} ms) — no error response"
            ),
        )
    if 500 <= oversize_status < 600:
        return (
            "fail",
            SEVERITY_HIGH,
            f"oversized payload triggered server error (HTTP {oversize_status})",
        )

    # 4xx is the explicit refusal — that's the protective behaviour we want.
    if 400 <= oversize_status < 500:
        return (
            "pass",
            None,
            f"oversized payload rejected with HTTP {oversize_status}",
        )

    # 2xx path: degradation gradient.
    if baseline_ms <= 0:
        # No usable baseline; fall back to absolute threshold only.
        ratio = float("inf") if oversize_ms > 0 else 1.0
    else:
        ratio = oversize_ms / baseline_ms

    if ratio >= LATENCY_HIGH_X:
        return (
            "fail",
            SEVERITY_HIGH,
            (
                f"oversized payload latency {oversize_ms:.0f} ms vs "
                f"baseline {baseline_ms:.0f} ms ({ratio:.1f}x) — buckled"
            ),
        )
    if ratio >= LATENCY_MEDIUM_X:
        return (
            "fail",
            SEVERITY_MEDIUM,
            (
                f"oversized payload latency {oversize_ms:.0f} ms vs "
                f"baseline {baseline_ms:.0f} ms ({ratio:.1f}x) — degraded but not refused"
            ),
        )
    return (
        "pass",
        None,
        f"oversized payload absorbed cleanly ({ratio:.1f}x baseline latency)",
    )


# ─────────────────── concurrent-burst classifier ─────────────────────────────


def classify_concurrent_burst(
    status_codes: Sequence[int],
    malformed_response_count: int = 0,
) -> tuple[str, str | None, str]:
    """Classify a fan-out of N concurrent requests against one resource.

    PASS:
      * Every response is one of 200, 429, 503 (clean handling under load).
    FAIL HIGH:
      * Any 500 (server crash under concurrency), OR
      * Any zero-status (connection dropped mid-fan-out).
    FAIL MEDIUM:
      * Any response was malformed (truncated JSON, wrong content type) —
        the count is reported by the caller because parsing is route-shaped.
      * Any 4xx other than 429 (improper rejection — request was well-formed).

    Inputs:
      status_codes: HTTP status per concurrent request. Empty = misconfigured.
      malformed_response_count: number of responses the caller couldn't parse
                                cleanly. Counted separately from status_codes
                                so a 200-status-but-broken-body shows up.
    """
    if not status_codes:
        return "fail", SEVERITY_MEDIUM, "no concurrent responses recorded"

    if any(code == 0 for code in status_codes):
        return (
            "fail",
            SEVERITY_HIGH,
            "at least one concurrent request dropped at transport layer",
        )
    # 503 Service Unavailable is the polite "I'm overloaded, try again" reply
    # — it's a structured response, not a crash. We exempt it from the
    # generic 5xx check; every other 5xx is a server-side fault under load.
    bad_5xx = [c for c in status_codes if 500 <= c < 600 and c != 503]
    if bad_5xx:
        return (
            "fail",
            SEVERITY_HIGH,
            f"concurrent burst produced server error (HTTP {bad_5xx[0]})",
        )
    if malformed_response_count > 0:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"{malformed_response_count}/{len(status_codes)} responses malformed (truncated / wrong type)",
        )
    other_4xx = [c for c in status_codes if 400 <= c < 500 and c != 429]
    if other_4xx:
        return (
            "fail",
            SEVERITY_MEDIUM,
            f"unexpected 4xx under concurrent load: {other_4xx}",
        )
    counts = {
        "200": sum(1 for c in status_codes if c == 200),
        "429": sum(1 for c in status_codes if c == 429),
        "503": sum(1 for c in status_codes if c == 503),
    }
    return (
        "pass",
        None,
        f"{len(status_codes)} concurrent requests handled cleanly ({counts})",
    )


# ──────────────────────── CLI flag clamp helpers ─────────────────────────────


def clamp_rps(value: int) -> int:
    """Clamp `--dos-rps` to the safe interval [1, DOS_RPS_HARD_CEILING].

    Values above the ceiling silently cap (loud failure on the CLI would
    discourage operators from trying values; we want them to see "your 500 was
    silently turned into 100" in the banner instead).
    """
    if value < 1:
        return 1
    if value > DOS_RPS_HARD_CEILING:
        return DOS_RPS_HARD_CEILING
    return value


def clamp_duration_seconds(value: int) -> int:
    """Clamp `--dos-duration-seconds` to [1, DOS_DURATION_HARD_CEILING_SECONDS]."""
    if value < 1:
        return 1
    if value > DOS_DURATION_HARD_CEILING_SECONDS:
        return DOS_DURATION_HARD_CEILING_SECONDS
    return value


__all__ = [
    "DOS_DURATION_HARD_CEILING_SECONDS",
    "DOS_RPS_HARD_CEILING",
    "LATENCY_HIGH_X",
    "LATENCY_MEDIUM_X",
    "SEVERITY_HIGH",
    "SEVERITY_LOW",
    "SEVERITY_MEDIUM",
    "TIMEOUT_SECONDS_FLOOR",
    "clamp_duration_seconds",
    "clamp_rps",
    "classify_concurrent_burst",
    "classify_oversize_payload",
    "classify_rate_limit_burst",
]
