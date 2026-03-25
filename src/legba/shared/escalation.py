"""Escalation scoring — pure function module.

JDL Level 3: Escalation scoring for portfolio management.

Takes event cluster data and returns a recommendation for portfolio promotion.
No DB access, no side effects. Used by StatePropagator and agent tools.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("legba.shared.escalation")

# Recommendation thresholds
_THRESHOLD_IGNORE = 0.2
_THRESHOLD_MONITOR = 0.4
_THRESHOLD_SITUATION_AND_WATCHLIST = 0.7
# >= 0.7 is full_portfolio


def compute_escalation_score(
    event_count: int,
    severity_distribution: dict,  # {"critical": 1, "high": 3, "medium": 2}
    entity_overlap_with_portfolio: float,  # 0.0 = totally novel, 1.0 = fully covered
    region_novelty: bool,  # True if region has no existing situation
    time_window_hours: float,  # how quickly events accumulated
    existing_situation_count: int,  # situations already covering these entities
) -> dict:
    """Score a novel event cluster for portfolio promotion.

    Returns::

        {
            "score": 0.72,
            "recommendation": "situation_and_watchlist",
            "reasons": ["5 events in 48h", "no existing situation covers region"]
        }

    Recommendations (by score threshold):
    - ignore: score < 0.2
    - monitor: 0.2 <= score < 0.4 (create situation, no goal)
    - situation_and_watchlist: 0.4 <= score < 0.7 (situation + watchlist)
    - full_portfolio: score >= 0.7 (situation + watchlist + investigative goal + hypothesis)
    """
    score = 0.0
    reasons: list[str] = []

    # --- Base: event count weight ---
    # Minimum 3 events before any meaningful score — 1-2 event situations
    # should not generate investigative goals or watchlist items.
    if event_count >= 10:
        score += 0.7
        reasons.append(f"{event_count} events (high cluster)")
    elif event_count >= 5:
        score += 0.5
        reasons.append(f"{event_count} events in cluster")
    elif event_count >= 3:
        score += 0.3
        reasons.append(f"{event_count} events in cluster")
    else:
        # Fewer than 3 events — too early to escalate
        return {
            "score": round(event_count * 0.05, 3),
            "recommendation": "ignore",
            "reasons": [f"only {event_count} event(s) — below escalation threshold"],
        }

    # --- Severity boost ---
    critical_count = severity_distribution.get("critical", 0)
    high_count = severity_distribution.get("high", 0)
    total_events = sum(severity_distribution.values()) or 1

    if critical_count > 0:
        score += 0.2
        reasons.append(f"{critical_count} CRITICAL event(s)")

    if high_count > 0 and high_count / total_events > 0.5:
        score += 0.1
        reasons.append("majority HIGH severity")

    # --- Novelty boost ---
    if region_novelty:
        score += 0.15
        reasons.append("no existing situation covers region")

    if entity_overlap_with_portfolio < 0.3:
        score += 0.1
        reasons.append("novel entities (low portfolio overlap)")

    # --- Existing coverage penalty ---
    if existing_situation_count > 0:
        score -= 0.3
        reasons.append(
            f"{existing_situation_count} existing situation(s) already cover these entities"
        )

    # --- Time compression boost ---
    if time_window_hours > 0 and time_window_hours < 6:
        score += 0.1
        reasons.append(f"events accumulated in {time_window_hours:.1f}h (rapid development)")

    # Clamp to [0.0, 1.0]
    score = max(0.0, min(1.0, score))

    # Determine recommendation
    if score >= _THRESHOLD_SITUATION_AND_WATCHLIST:
        recommendation = "full_portfolio"
    elif score >= _THRESHOLD_MONITOR:
        recommendation = "situation_and_watchlist"
    elif score >= _THRESHOLD_IGNORE:
        recommendation = "monitor"
    else:
        recommendation = "ignore"

    return {
        "score": round(score, 3),
        "recommendation": recommendation,
        "reasons": reasons,
    }
