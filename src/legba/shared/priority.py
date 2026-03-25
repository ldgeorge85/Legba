"""Priority stack — ranks active situations by composite score.

JDL Level 3: Priority stack with adaptive staleness.

Provides an advisory ranking of the most important situations to focus on.
The priority stack is computed from four components plus an optional
structural instability boost:

  score = (event_velocity * 0.3) + (goal_overlap * 0.25)
        + (watchlist_trigger_density * 0.25) + (recency_penalty * 0.2)

If structural balance results are provided, situations whose linked entities
appear in unbalanced triads receive a scoring boost (capped at 0.10).
Unbalanced triads represent structurally unstable relationships that may
realign — analytically interesting.

Adaptive staleness thresholds vary by severity:
  - critical: staleness starts at 5 cycles
  - high:     staleness starts at 10 cycles
  - medium:   staleness starts at 20 cycles
  - low:      staleness starts at 30 cycles
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .situation_severity import compute_situation_severity

log = logging.getLogger(__name__)

# Adaptive staleness thresholds by severity.
# staleness_start = cycle gap at which recency score begins decaying from 1.0
# staleness_end   = staleness_start * 3 (decays linearly to 0.0)
_STALENESS_THRESHOLDS: dict[str, tuple[int, int]] = {
    "critical": (5, 15),
    "high":     (10, 30),
    "medium":   (20, 60),
    "low":      (30, 90),
}


async def compute_priority_stack(
    pool,  # asyncpg.Pool
    redis_client,
    top_n: int = 7,
    structural_balance: dict | None = None,
) -> list[dict]:
    """Compute the priority stack: ranked list of active situations.

    Args:
        pool: asyncpg connection pool.
        redis_client: Redis client (for reading current cycle and synth history).
        top_n: Maximum number of situations to return.
        structural_balance: Optional dict from compute_structural_balance().
            If provided, situations linked to entities in unbalanced triads
            get a scoring boost.

    Returns:
        Sorted list of dicts, each containing:
            - situation_id (str)
            - situation_name (str)
            - score (float, 0-1)
            - severity (str)
            - trend (str)
            - components (dict with event_velocity, goal_overlap,
              watchlist_trigger_density, recency, and optionally
              structural_instability)
    """
    try:
        return await _compute_stack(pool, redis_client, top_n, structural_balance)
    except Exception as exc:
        log.warning("Priority stack computation failed: %s", exc)
        return []


async def _compute_stack(
    pool, redis_client, top_n: int,
    structural_balance: dict | None = None,
) -> list[dict]:
    """Internal implementation — may raise."""
    import json as _json

    # Pre-compute set of entities involved in unbalanced triads
    unstable_entities: set[str] = set()
    if structural_balance:
        for triad in structural_balance.get("unbalanced_triads", []):
            unstable_entities.add(triad.get("entity_a", "").lower())
            unstable_entities.add(triad.get("entity_b", "").lower())
            unstable_entities.add(triad.get("entity_c", "").lower())
    unstable_entities.discard("")

    # --- 1. Load current cycle number ---
    current_cycle = 0
    try:
        raw = await redis_client.get("legba:cycle_number")
        if raw:
            current_cycle = int(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except Exception:
        pass

    # --- 2. Load synth history (maps situation topics -> last cycle investigated) ---
    synth_history: list[dict] = []
    try:
        raw = await redis_client.get("synthesize_history")
        if raw:
            text = raw if isinstance(raw, str) else raw.decode("utf-8")
            synth_history = _json.loads(text)
    except Exception:
        pass

    # --- 3. Load analysis snapshot cycle ---
    last_analysis_cycle = 0
    try:
        raw = await redis_client.get("analysis_snapshot")
        if raw:
            text = raw if isinstance(raw, str) else raw.decode("utf-8")
            snap = _json.loads(text)
            if isinstance(snap, dict):
                last_analysis_cycle = snap.get("cycle", 0)
    except Exception:
        pass

    async with pool.acquire() as conn:
        # --- 4. Load active situations ---
        situations = await conn.fetch("""
            SELECT s.id, s.name, s.status, s.category,
                   s.event_count, s.intensity_score,
                   s.updated_at, s.data
            FROM situations s
            WHERE s.status NOT IN ('resolved', 'historical')
            ORDER BY s.intensity_score DESC
        """)

        if not situations:
            return []

        # --- 5. Load active goals (for goal overlap) ---
        goal_rows = await conn.fetch("""
            SELECT id, data, status FROM goals
            WHERE status = 'active'
        """)
        active_goals = _parse_goals(goal_rows)

        # --- 6. For each situation, compute the 4 components ---
        now = datetime.now(timezone.utc)
        cutoff_48h = now - timedelta(hours=48)

        results: list[dict] = []

        for sit in situations:
            sit_id = sit["id"]
            sit_name = sit["name"]
            sit_data = sit["data"] if isinstance(sit["data"], dict) else {}

            # --- 6a. Event velocity: events linked in last 48h ---
            recent_event_count = await conn.fetchval("""
                SELECT count(*) FROM situation_events se
                JOIN events e ON e.id = se.event_id
                WHERE se.situation_id = $1
                  AND e.created_at > $2
            """, sit_id, cutoff_48h) or 0

            # --- 6b. Get all linked events for severity computation ---
            event_rows = await conn.fetch("""
                SELECT e.severity, e.lifecycle_status, e.created_at
                FROM situation_events se
                JOIN events e ON e.id = se.event_id
                WHERE se.situation_id = $1
            """, sit_id)
            events_list = [dict(r) for r in event_rows]

            # --- 6c. Compute severity ---
            severity_result = compute_situation_severity(events_list)
            severity = severity_result["severity"]
            trend = severity_result["trend"]

            # --- 6d. Watchlist trigger density: triggers in last 48h ---
            #     We match triggers to this situation by checking if the
            #     trigger's signal is linked to an event in this situation.
            watch_trigger_count = await conn.fetchval("""
                SELECT count(DISTINCT wt.id)
                FROM watch_triggers wt
                JOIN signal_event_links sel ON sel.signal_id = wt.signal_id
                JOIN situation_events se ON se.event_id = sel.event_id
                WHERE se.situation_id = $1
                  AND wt.triggered_at > $2
            """, sit_id, cutoff_48h) or 0

            # --- 6e. Goal overlap ---
            goal_overlap = _compute_goal_overlap(
                sit_id, sit_name, sit_data, active_goals,
            )

            # --- 6f. Recency: cycles since last analysis of this situation ---
            cycles_since = _cycles_since_analysis(
                sit_name, current_cycle, synth_history, last_analysis_cycle,
            )

            # Adaptive staleness by severity
            start, end = _STALENESS_THRESHOLDS.get(severity, (20, 60))
            recency = _compute_recency(cycles_since, start, end)

            # --- 6g. Structural instability: entities in unbalanced triads ---
            structural_instability = 0.0
            if unstable_entities:
                sit_entities = {
                    e.lower() for e in (sit_data.get("key_entities") or [])
                }
                if sit_entities:
                    overlap = sit_entities & unstable_entities
                    # Cap at 0.10 — up to 3 matching entities contribute
                    structural_instability = min(len(overlap) * 0.033, 0.10)

            # --- 6h. Normalize velocity and trigger density ---
            # Will normalize across all situations after the loop
            results.append({
                "situation_id": str(sit_id),
                "situation_name": sit_name,
                "severity": severity,
                "trend": trend,
                "raw_event_velocity": recent_event_count,
                "raw_trigger_density": watch_trigger_count,
                "goal_overlap": goal_overlap,
                "recency": recency,
                "cycles_since_analysis": cycles_since,
                "structural_instability": structural_instability,
            })

    # --- 7. Normalize event_velocity and trigger_density across all situations ---
    max_velocity = max((r["raw_event_velocity"] for r in results), default=1) or 1
    max_triggers = max((r["raw_trigger_density"] for r in results), default=1) or 1

    scored: list[dict] = []
    for r in results:
        event_velocity = r["raw_event_velocity"] / max_velocity
        trigger_density = r["raw_trigger_density"] / max_triggers
        goal_overlap = r["goal_overlap"]
        recency = r["recency"]
        structural_instability = r.get("structural_instability", 0.0)

        score = (
            event_velocity * 0.30
            + goal_overlap * 0.25
            + trigger_density * 0.25
            + recency * 0.20
            + structural_instability  # additive boost, capped at 0.10
        )

        components: dict[str, Any] = {
            "event_velocity": round(event_velocity, 3),
            "goal_overlap": round(goal_overlap, 3),
            "watchlist_trigger_density": round(trigger_density, 3),
            "recency": round(recency, 3),
        }
        if structural_instability > 0:
            components["structural_instability"] = round(structural_instability, 3)

        scored.append({
            "situation_id": r["situation_id"],
            "situation_name": r["situation_name"],
            "score": round(score, 4),
            "severity": r["severity"],
            "trend": r["trend"],
            "components": components,
        })

    # Sort by score descending, then by severity rank as tiebreaker
    _SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    scored.sort(key=lambda x: (x["score"], _SEV_RANK.get(x["severity"], 0)), reverse=True)

    return scored[:top_n]


def _parse_goals(goal_rows: list) -> list[dict]:
    """Extract goal data for overlap computation."""
    import json as _json
    goals = []
    for row in goal_rows:
        data = row["data"]
        if isinstance(data, str):
            try:
                data = _json.loads(data)
            except (ValueError, TypeError):
                data = {}
        elif not isinstance(data, dict):
            data = {}
        goals.append({
            "id": str(row["id"]),
            "data": data,
        })
    return goals


def _compute_goal_overlap(
    sit_id, sit_name: str, sit_data: dict, active_goals: list[dict],
) -> float:
    """Compute goal overlap score for a situation.

    Returns:
        1.0 if a goal is directly linked to this situation, OR if an
            operator_priority goal overlaps with this situation.
        0.5 if a goal shares key entities with this situation.
        0.0 otherwise.
    """
    sit_id_str = str(sit_id)
    sit_entities = {
        e.lower() for e in (sit_data.get("key_entities") or [])
    }

    for goal in active_goals:
        gdata = goal["data"]
        is_operator = gdata.get("operator_priority", False)

        # Direct link: goal has linked_situation_id matching this situation
        linked_sit = gdata.get("linked_situation_id")
        if linked_sit and str(linked_sit) == sit_id_str:
            return 1.0

        # Check goal description for situation name overlap
        desc = (gdata.get("description") or "").lower()
        name_lower = sit_name.lower()
        if name_lower and len(name_lower) > 5 and name_lower in desc:
            # Operator priority goals always get max overlap
            if is_operator:
                return 1.0
            return 1.0

    # Entity overlap: check if any goal description mentions entities from this situation
    if sit_entities:
        for goal in active_goals:
            gdata = goal["data"]
            is_operator = gdata.get("operator_priority", False)
            desc = (gdata.get("description") or "").lower()
            overlap = sum(1 for e in sit_entities if e in desc)
            if overlap >= 2:
                # Operator priority goals boost to 1.0 even on entity overlap
                return 1.0 if is_operator else 0.5

    # Check if any operator_priority goal has even partial relevance
    # (single entity overlap or partial name match)
    for goal in active_goals:
        gdata = goal["data"]
        if not gdata.get("operator_priority", False):
            continue
        desc = (gdata.get("description") or "").lower()
        # Any entity overlap for operator goals => max score
        if sit_entities:
            if any(e in desc for e in sit_entities):
                return 1.0
        # Partial name match for operator goals
        name_lower = sit_name.lower()
        name_words = {w for w in name_lower.split() if len(w) > 3}
        if name_words:
            desc_words = set(desc.split())
            if len(name_words & desc_words) >= 1:
                return 1.0

    return 0.0


def _cycles_since_analysis(
    sit_name: str,
    current_cycle: int,
    synth_history: list[dict],
    last_analysis_cycle: int,
) -> int:
    """Compute cycles since this situation was last analyzed.

    Checks both synthesize history (topic match) and the global analysis snapshot.
    Returns the smaller gap (most recent analysis).
    """
    # Default: assume it was never analyzed specifically
    min_gap = current_cycle  # worst case

    # Check synth history for topic match
    stop_words = {"a", "an", "the", "of", "in", "on", "at", "to", "for",
                  "and", "or", "is", "was", "by", "from", "with"}
    sit_words = {
        w.lower() for w in sit_name.split()
        if w.lower() not in stop_words and len(w) > 2
    }

    if sit_words:
        for entry in synth_history:
            topic = entry.get("topic", "")
            topic_words = {
                w.lower() for w in topic.split()
                if w.lower() not in stop_words and len(w) > 2
            }
            if not topic_words:
                continue
            overlap = len(sit_words & topic_words)
            union = len(sit_words | topic_words)
            if union > 0 and overlap / union >= 0.3:
                cycle = entry.get("cycle", 0)
                gap = max(current_cycle - cycle, 0)
                min_gap = min(min_gap, gap)

    # Also consider global analysis cycle (affects all situations equally)
    if last_analysis_cycle > 0:
        gap = max(current_cycle - last_analysis_cycle, 0)
        min_gap = min(min_gap, gap)

    return min_gap


def _compute_recency(cycles_since: int, start: int, end: int) -> float:
    """Compute recency score with adaptive staleness.

    Returns 1.0 if analyzed within `start` cycles, linearly decays to 0.0
    at `end` cycles.
    """
    if cycles_since <= start:
        return 1.0
    if cycles_since >= end:
        return 0.0
    # Linear decay
    return 1.0 - (cycles_since - start) / (end - start)


def format_priority_stack(stack: list[dict]) -> str:
    """Format the priority stack for prompt injection.

    Returns a human-readable markdown string suitable for LLM context.
    """
    if not stack:
        return ""

    lines = []
    for i, item in enumerate(stack, 1):
        comp = item.get("components", {})
        trend_indicator = ""
        if item.get("trend") == "escalating":
            trend_indicator = " [ESCALATING]"
        elif item.get("trend") == "de-escalating":
            trend_indicator = " [de-escalating]"

        lines.append(
            f"{i}. **{item['situation_name']}** "
            f"(score={item['score']:.2f}, severity={item['severity']}{trend_indicator})"
        )
        detail = (
            f"   velocity={comp.get('event_velocity', 0):.1f} | "
            f"goal={comp.get('goal_overlap', 0):.1f} | "
            f"watchlist={comp.get('watchlist_trigger_density', 0):.1f} | "
            f"recency={comp.get('recency', 0):.1f}"
        )
        instability = comp.get("structural_instability", 0)
        if instability > 0:
            detail += f" | instability={instability:.2f}"
        lines.append(detail)

    return "\n".join(lines)
