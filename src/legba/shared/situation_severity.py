"""Situation severity computation — pure functions, no DB access.

Computes aggregate severity metrics for a situation based on its linked events.
Used by the maintenance daemon's situation detector and agent tools.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

# Severity ranking for comparison
_SEVERITY_RANK = {
    "routine": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

_RANK_TO_SEVERITY = {v: k for k, v in _SEVERITY_RANK.items()}

# Lifecycle statuses that indicate active/escalating patterns
_ACTIVE_STATUSES = frozenset({"active", "evolving", "developing", "emerging"})
_RESOLVED_STATUSES = frozenset({"resolved", "historical", "stale"})


def compute_situation_severity(events: list[dict]) -> dict:
    """Compute situation severity from linked events.

    Args:
        events: List of event dicts. Each should have at least:
            - severity (str): routine/low/medium/high/critical
            - lifecycle_status (str): emerging/developing/active/evolving/resolved/etc.
            - created_at (str or datetime): event creation timestamp

    Returns:
        Dict with::

            {
                "severity": "high",
                "event_count": 15,
                "lifecycle_distribution": {"active": 8, "developing": 4, "emerging": 3},
                "severity_distribution": {"critical": 1, "high": 5, "medium": 7, "low": 2},
                "trend": "escalating"
            }
    """
    if not events:
        return {
            "severity": "low",
            "event_count": 0,
            "lifecycle_distribution": {},
            "severity_distribution": {},
            "trend": "stable",
        }

    event_count = len(events)

    # Build distributions
    severity_counter: Counter[str] = Counter()
    lifecycle_counter: Counter[str] = Counter()

    for evt in events:
        sev = (evt.get("severity") or "medium").lower()
        lifecycle = (evt.get("lifecycle_status") or "active").lower()
        severity_counter[sev] += 1
        lifecycle_counter[lifecycle] += 1

    severity_distribution = dict(severity_counter)
    lifecycle_distribution = dict(lifecycle_counter)

    # Overall severity
    overall_severity = _compute_overall_severity(severity_counter, event_count)

    # Trend from lifecycle + event velocity
    trend = _compute_trend(events, lifecycle_counter, event_count)

    return {
        "severity": overall_severity,
        "event_count": event_count,
        "lifecycle_distribution": lifecycle_distribution,
        "severity_distribution": severity_distribution,
        "trend": trend,
    }


def _compute_overall_severity(
    severity_counter: Counter[str], event_count: int,
) -> str:
    """Determine overall situation severity from event severities.

    Rules:
    - If any event is CRITICAL -> situation is CRITICAL
    - If >30% events are HIGH -> situation is HIGH
    - If >50% events are MEDIUM or higher -> situation is MEDIUM
    - Otherwise LOW
    """
    if severity_counter.get("critical", 0) > 0:
        return "critical"

    high_count = severity_counter.get("high", 0)
    if event_count > 0 and high_count / event_count > 0.3:
        return "high"

    medium_plus = (
        severity_counter.get("medium", 0)
        + severity_counter.get("high", 0)
        + severity_counter.get("critical", 0)
    )
    if event_count > 0 and medium_plus / event_count > 0.5:
        return "medium"

    return "low"


def _compute_trend(
    events: list[dict],
    lifecycle_counter: Counter[str],
    event_count: int,
) -> str:
    """Compute situation trend from lifecycle distribution and event velocity.

    - If most events are ACTIVE/EVOLVING/DEVELOPING -> escalating
    - If most events are RESOLVED -> de-escalating
    - Compare last 24h event count vs previous 48h for velocity signal
    """
    # Lifecycle-based trend
    active_count = sum(lifecycle_counter.get(s, 0) for s in _ACTIVE_STATUSES)
    resolved_count = sum(lifecycle_counter.get(s, 0) for s in _RESOLVED_STATUSES)

    if event_count > 0 and resolved_count / event_count > 0.5:
        return "de-escalating"

    # Velocity-based trend: compare recent 24h vs prior 48h
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_72h = now - timedelta(hours=72)

    recent_count = 0
    prior_count = 0

    for evt in events:
        created = evt.get("created_at")
        if created is None:
            continue

        if isinstance(created, str):
            try:
                # Handle both ISO formats
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue

        # Ensure timezone-aware
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        if created >= cutoff_24h:
            recent_count += 1
        elif created >= cutoff_72h:
            prior_count += 1

    # Velocity: 2x or more events in last 24h vs daily average of prior 48h
    prior_daily_avg = prior_count / 2.0 if prior_count > 0 else 0

    if recent_count >= 3 and (prior_daily_avg == 0 or recent_count >= prior_daily_avg * 2):
        return "escalating"

    if event_count > 0 and active_count / event_count > 0.5:
        return "escalating"

    return "stable"
