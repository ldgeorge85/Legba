"""
Confidence computation module.

Pure functions for computing composite confidence scores using a hybrid
gatekeeper formula.  No database access — independently testable.

Gate = source_reliability * classification_confidence
Modifier = 0.4 * temporal_freshness + 0.35 * corroboration + 0.25 * specificity
Confidence = Gate * Modifier

The gate ensures that unreliable sources or poorly classified signals
can never produce high confidence regardless of other factors.  The
modifier captures how fresh, corroborated, and specific the information is.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Default weights (overridable via env vars)
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: dict[str, float] = {
    "temporal_freshness": float(os.getenv("CONFIDENCE_W_TEMPORAL", "0.40")),
    "corroboration":      float(os.getenv("CONFIDENCE_W_CORROBORATION", "0.35")),
    "specificity":        float(os.getenv("CONFIDENCE_W_SPECIFICITY", "0.25")),
}

# Temporal decay breakpoints (hours -> freshness value)
_DECAY_POINTS: list[tuple[float, float]] = [
    (0.0,   1.0),
    (24.0,  0.5),
    (72.0,  0.1),
    (168.0, 0.0),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_composite_confidence(components: dict) -> float:
    """Compute a composite confidence score from component values.

    Parameters
    ----------
    components : dict
        Expected keys (all floats 0.0-1.0):
        - source_reliability
        - classification_confidence
        - temporal_freshness
        - corroboration
        - specificity

        Missing keys default to 0.5 (neutral).

    Returns
    -------
    float
        Composite confidence in [0.0, 1.0].
    """
    src_rel  = _clamp(components.get("source_reliability", 0.5))
    cls_conf = _clamp(components.get("classification_confidence", 0.5))
    tf       = _clamp(components.get("temporal_freshness", 1.0))
    corr     = _clamp(components.get("corroboration", 0.0))
    spec     = _clamp(components.get("specificity", 0.5))

    w = DEFAULT_WEIGHTS
    gate     = src_rel * cls_conf
    modifier = (w["temporal_freshness"] * tf
                + w["corroboration"] * corr
                + w["specificity"] * spec)

    return _clamp(gate * modifier)


def compute_temporal_freshness(
    event_timestamp: datetime,
    now: datetime | None = None,
) -> float:
    """Linear-interpolated freshness decay over time.

    Breakpoints:
      0 h  -> 1.0
      24 h -> 0.5
      72 h -> 0.1
      168 h (1 week) -> 0.0

    Parameters
    ----------
    event_timestamp : datetime
        When the event occurred (should be tz-aware).
    now : datetime, optional
        Reference time.  Defaults to ``datetime.now(timezone.utc)``.

    Returns
    -------
    float
        Freshness score in [0.0, 1.0].
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Ensure both are tz-aware for safe subtraction
    if event_timestamp.tzinfo is None:
        event_timestamp = event_timestamp.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    hours = max((now - event_timestamp).total_seconds() / 3600.0, 0.0)

    # Walk the decay curve and interpolate
    for i in range(len(_DECAY_POINTS) - 1):
        h0, v0 = _DECAY_POINTS[i]
        h1, v1 = _DECAY_POINTS[i + 1]
        if hours <= h1:
            t = (hours - h0) / (h1 - h0) if h1 != h0 else 0.0
            return _clamp(v0 + t * (v1 - v0))

    # Past the last breakpoint
    return 0.0


def compute_corroboration(independent_source_count: int) -> float:
    """Map independent source count to a corroboration score.

    Mapping (logarithmic-ish step curve):
      0 sources -> 0.0
      1 source  -> 0.3
      2 sources -> 0.6
      3 sources -> 0.8
      4 sources -> 0.9
      5+ sources -> 1.0

    Parameters
    ----------
    independent_source_count : int
        Number of independent sources reporting the same claim.

    Returns
    -------
    float
        Corroboration score in [0.0, 1.0].
    """
    _MAP = {0: 0.0, 1: 0.3, 2: 0.6, 3: 0.8, 4: 0.9}
    if independent_source_count >= 5:
        return 1.0
    return _MAP.get(independent_source_count, 0.0)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp *v* to [lo, hi]."""
    return max(lo, min(hi, v))
