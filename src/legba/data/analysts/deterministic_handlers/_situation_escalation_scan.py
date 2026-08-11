# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trigger class 8 — ``situation_escalation`` (continuity Phase 2, plan D5).

WHY THIS CLASS EXISTS
---------------------
Until the trajectory ledger landed, situation dynamics could not reach an
operator at all. The alert plane read scorecards, findings, fact contention,
desk baselines and watchlists — not one of the seven classes touched
``situations``, so a frame could climb from watching to escalating and the only
way that ever surfaced was indirectly, if some finding about it happened to
clear the high-severity verified bar on its own. The thing an operator actually
wants to be told — "the situation you were already watching just escalated" — had
no path to them.

It has one now, and it rests on the ledger rather than on intensity_score:
``intensity_score`` is a recency-weighted corroboration count (it rises because
MORE was written, not because anything got worse), which is precisely the wrong
thing to page on. An ``escalates`` ledger row is a graded claim that the
situation moved, resting on named new evidence.

THE BAR (all three, and each is load-bearing)
---------------------------------------------
1. **The delta is ``escalates``.** Broadening scope and de-escalation are real
   and both land in the ledger; neither is a page.
2. **Its source ``situation_update`` cleared the floor.** The delta claim was
   graded by the faithfulness pass and ``min(confidence, faithfulness) >=`` the
   floor. An ungraded or demoted claim does not page — the same gate every other
   verified class applies, applied to the row that actually made the assertion.
3. **The situation is at or above an intensity floor.** A one-member frame that
   escalates is noise; the floor is what keeps the class from turning the
   long tail of small situations into a pager.

FIRE-ONCE, on the ledger row. The watermark key is the ``situation_events`` row
id, which is immutable and unique by construction, so re-firing is impossible
without a genuinely new escalation — no rank ladder and no recovery semantics are
needed here (unlike ``production_deficit``, whose subject persists and can worsen).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

TRIGGER_CLASS: str = "situation_escalation"

#: The class's outward verify-state prose (``alert_trigger_scan.
#: _UNVERIFIED_REASONS``). Declared HERE, beside the bar it describes, so the
#: sentence and the SQL that makes it true cannot drift into disagreeing — the
#: geo_convergence precedent of referencing a sibling's canonical value rather
#: than pasting a second copy into the owner module.
UNVERIFIED_REASON: str = (
    "verified trajectory delta (the source situation_update cleared the "
    "faithfulness floor); the escalation selection itself is deterministic"
)

#: Defensive per-scan bound. Far above any plausible hour (the tracker examines
#: at most a dozen situations a tick); a scan that hits it is reporting a real
#: anomaly, and hitting it does NOT advance watermarks, so nothing is lost.
_MAX_CANDIDATES = 25

#: How far back a scan looks. The alert cadence is every 10 minutes and the
#: tracker runs hourly, so this is ~24 ticks of slack for a paused alert scan.
_LOOKBACK_HOURS = 6

#: Minimum ``situations.intensity_score`` to page. Situation intensity is an
#: exponentially recency-weighted member count (half-life 3 days), so ~2.0 means
#: "at least a couple of recent corroborating members" — a real thread, not a
#: single stray finding that happened to cluster.
DEFAULT_INTENSITY_FLOOR = 2.0

#: The floor on the SOURCE claim's effective confidence — the house floor.
DEFAULT_FLOOR = 0.50

#: Watermark rows age out on ``updated_at`` (the upsert-style prune, per
#: _production_deficit_scan) — a ledger row id is never revisited, so its
#: watermark is pure history after this.
_WATERMARK_PRUNE_DAYS = 30

_ESCALATIONS_SQL = """
    SELECT e.id            AS event_id,
           e.situation_id  AS situation_id,
           e.occurred_at   AS occurred_at,
           e.why           AS why,
           e.state_from    AS state_from,
           e.state_to      AS state_to,
           e.derived_from  AS derived_from,
           e.source_output_id AS source_output_id,
           s.name          AS situation_name,
           s.target_id     AS target_id,
           s.intensity_score AS intensity_score,
           s.event_count   AS event_count,
           LEAST(u.confidence, v.faithfulness_score) AS effective_confidence,
           v.faithfulness_score AS faithfulness_score
      FROM situation_events e
      JOIN situations s ON s.id = e.situation_id
      JOIN analyst_outputs u ON u.id = e.source_output_id
      JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = u.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE e.delta = 'escalates'
       AND e.created_at > $1
       AND s.intensity_score >= $2
       AND LEAST(u.confidence, v.faithfulness_score) >= $3
     ORDER BY e.created_at DESC, e.id DESC
     LIMIT $4
"""


def _severity(intensity: float, state_to: str) -> str:
    """Page severity from the frame's weight and where it landed.

    Deliberately coarse, and deliberately NOT read from the model: an escalation
    into an already-escalating frame is a continuation, an escalation on a heavy
    frame is worse than one on a light frame, and nothing else here is knowable
    without asking the LLM to grade its own urgency.
    """
    if intensity >= 8.0:
        return "critical"
    if intensity >= 4.0:
        return "high"
    return "medium" if state_to == "escalating" else "low"


def build_body(row: Any) -> str:
    """The alert body — the delta, its evidence date, and where to look."""
    return (
        f"SITUATION: {row['situation_name']}\n"
        f"MOVED: {row['state_from']} -> {row['state_to']} (escalates)\n"
        f"AS OF: {row['occurred_at']} (evidence date, not scan time)\n"
        f"WHY: {row['why']}\n"
        f"EVIDENCE: {len(row['derived_from'] or [])} newly-attached verified "
        f"finding(s); the graded claim is analyst_output "
        f"{row['source_output_id']}.\n"
        f"FRAME: intensity {float(row['intensity_score'] or 0.0):.2f} over "
        f"{int(row['event_count'] or 0)} member(s).\n"
        f"Full trajectory: GET /api/v1/v3/situations/{row['situation_id']}"
        f"/trajectory"
    )


async def scan_situation_escalations(
    conn: Any,
    *,
    now: datetime | None = None,
    intensity_floor: float = DEFAULT_INTENSITY_FLOOR,
    floor: float = DEFAULT_FLOOR,
) -> tuple[list[Any], list[tuple[str, str, dict[str, Any]]], bool, dict[str, int]]:
    """Scan the trajectory ledger for verified escalations worth paging.

    Returns ``(candidates, silent_watermarks, was_seeded, stats)`` — the same
    4-tuple the other sibling scans return, so ``alert_trigger_scan.handle``
    folds it in with no special-casing.

    ``stats`` rides the receipt's ``counts_by_class`` entry: ``escalations`` (in
    window, past both floors) / ``paged`` / ``already_seen`` / ``seeded`` /
    ``candidate_bound_hit`` / ``unavailable``.
    """
    from .alert_trigger_scan import (
        SEED_KEY, AlertCandidate, _load_class_watermarks,
    )

    now = now or datetime.now(timezone.utc)
    stats = {
        "escalations": 0,
        "paged": 0,
        "already_seen": 0,
        "seeded": 0,
        "candidate_bound_hit": 0,
        "unavailable": 0,
    }
    try:
        seeded, marked = await _load_class_watermarks(conn, TRIGGER_CLASS)
        rows = await conn.fetch(
            _ESCALATIONS_SQL,
            now - timedelta(hours=_LOOKBACK_HOURS),
            float(intensity_floor),
            float(floor),
            _MAX_CANDIDATES * 4,
        )
    except Exception as exc:  # noqa: BLE001 — a broken class must SAY so
        # Degrade LOUD and empty rather than killing the other classes: a
        # not-yet-migrated substrate (situation_events absent) takes THIS class
        # offline, never the whole alert scan.
        if type(exc).__name__ != "UndefinedTableError":
            raise
        logger.warning(
            "situation_escalation_scan.unavailable — the trajectory ledger is "
            "not readable (%s); the class scanned nothing", exc,
        )
        stats["unavailable"] = 1
        return [], [], True, stats

    candidates: list[Any] = []
    silent: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        stats["escalations"] += 1
        key = str(row["event_id"])
        if key in marked:
            stats["already_seen"] += 1
            continue
        state = {
            "situation_id": str(row["situation_id"]),
            "occurred_at": str(row["occurred_at"]),
            "effective_confidence": float(row["effective_confidence"]),
        }
        if not seeded:
            # First-ever scan: adopt the standing window WITHOUT paging, so
            # turning the class on does not open with a backlog burst.
            stats["seeded"] += 1
            silent.append((TRIGGER_CLASS, key, state))
            continue
        if len(candidates) >= _MAX_CANDIDATES:
            # Over the bound: DO NOT watermark, so this escalation is a
            # candidate again next scan rather than silently adopted as
            # reported (the _production_deficit_scan ordering).
            stats["candidate_bound_hit"] = 1
            continue
        intensity = float(row["intensity_score"] or 0.0)
        severity = _severity(intensity, str(row["state_to"]))
        derived = [r for r in (row["derived_from"] or [])]
        candidates.append(
            AlertCandidate(
                trigger_class=TRIGGER_CLASS,
                severity=severity,
                title=(
                    f"Situation escalated: {row['situation_name']} "
                    f"({row['state_from']} -> {row['state_to']})"
                )[:2048],
                body=build_body(row),
                target_id=(
                    str(row["target_id"]) if row["target_id"] is not None else None
                ),
                # The graded CLAIM plus the new evidence it rests on — a reader
                # following the lineage lands on the situation_update whose
                # faithfulness verdict gated this page, not on the ledger row
                # (which is a pointer, not an argument).
                derived_from=[row["source_output_id"], *derived],
                data={
                    "trigger_class": TRIGGER_CLASS,
                    "situation_id": str(row["situation_id"]),
                    "situation_event_id": key,
                    "situation_name": str(row["situation_name"]),
                    "delta": "escalates",
                    "state_from": str(row["state_from"]),
                    "state_to": str(row["state_to"]),
                    "why": str(row["why"]),
                    "occurred_at": str(row["occurred_at"]),
                    "intensity_score": intensity,
                    "source_output_id": str(row["source_output_id"]),
                    "effective_confidence": float(row["effective_confidence"]),
                    "faithfulness_score": float(row["faithfulness_score"]),
                },
                watermarks=[(TRIGGER_CLASS, key, state)],
                effective_confidence=float(row["effective_confidence"]),
                faithfulness_score=float(row["faithfulness_score"]),
                event_at=row["occurred_at"],
            )
        )
    stats["paged"] = len(candidates)
    if stats["seeded"]:
        logger.warning(
            "situation_escalation_scan.seeded n=%d — adopted the standing "
            "escalation window WITHOUT paging (the 0091 seed contract)",
            stats["seeded"],
        )
    await conn.execute(
        """
        DELETE FROM alert_trigger_watermarks
         WHERE trigger_class = $1
           AND watermark_key <> $2
           AND updated_at < now() - make_interval(days => $3)
        """,
        TRIGGER_CLASS, SEED_KEY, _WATERMARK_PRUNE_DAYS,
    )
    return candidates, silent, seeded, stats


__all__ = [
    "DEFAULT_FLOOR",
    "DEFAULT_INTENSITY_FLOOR",
    "TRIGGER_CLASS",
    "build_body",
    "scan_situation_escalations",
]
