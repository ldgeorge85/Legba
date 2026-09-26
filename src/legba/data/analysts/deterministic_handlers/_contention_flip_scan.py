# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trigger class 3 — ``contention_flip`` (verified-tied fact_contention change).

Extracted from ``alert_trigger_scan`` 2026-09-24 (H17) under the module-size
gate, along the seam every other trigger class already sits on
(``_band_crossing_scan``, ``_watchlist_scan``, ``_situation_escalation_scan``,
``_production_deficit_scan``): one module per class, holding that class's gather
SQL and its scan, importing the shared alert vocabulary back from
``alert_trigger_scan`` at call time.

The class pages when a fact contention's SURFACE changes — its status or the
value it surfaces — and some non-superseded, floor-clearing finding rests on the
dispute. The bridge from a contention's FACT ids to a finding's SIGNAL ids, and
the set-based shape that makes the whole thing affordable, are documented on
:data:`_CONTENTIONS_SQL` below.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from ... import critic_fold

logger = logging.getLogger(__name__)

#: Defensive bound on the per-scan contention page (moved here with the
#: class; it bounds this gather and nothing else).
_MAX_CONTENTIONS_PER_SCAN = 500

# Every contention group + its non-junk supporting fact ids + the SIGNALS those
# facts were derived from + (when one exists) the freshest non-superseded
# finding that RESTS ON the dispute and meets the verified bar. The GIN indexes
# on analyst_outputs.derived_from and facts.derived_from carry the && probes;
# the contention table is small by construction.
#
# W1-C3 — WHY THE SIGNAL BRIDGE. Until 2026-08-03 this LATERAL matched
# `f.derived_from && v.fact_ids` alone, and the class had NEVER fired an alert
# in its life despite 1,606 watermark rows. The reason is not tuning, it is a
# type mismatch between two id populations that never meet:
#
#   * a contention group tracks FACT ids — all 7,602 non-junk
#     `supporting_fact_ids` live in `facts`;
#   * a finding's `derived_from` holds SIGNAL ids — of 229,768 refs carried by
#     findings in the trailing 7 days, 221,268 resolved to `signals`, 7,874 to
#     other `analyst_outputs`, and **0 to `facts`**. All-time it is 34 of
#     757,436 (0.0045%).
#
# So the join could not fire, and did not: exactly 1 of 2,152 groups with
# supporting facts had ANY finding citing them, verified or not. That is a
# structurally impossible predicate reported as a quiet gauge — the worst shape
# a trigger can have, because "0 alerts" reads as "nothing happened".
#
# The bridge is the substrate's own lineage, not a heuristic: a fact carries the
# signals it was derived from (`facts.derived_from`), and a finding cites those
# same signals. A finding "rests on" a contested fact when it cites evidence
# that fact was built from. Measured on the live substrate: 1,243 of 2,152
# groups (58%) acquire a live finding under the bridge, and the shipped query
# shape resolves 157 verified-bar findings over the 500-group scan window
# (vs 0), with no latency regression (9.4s bridged vs 12.5s before).
#
# Volume is NOT unbounded by this change: the trigger still fires only on a
# status/surfaced_fact_id CHANGE against the watermark, and real surface changes
# run ~40/day fleet-wide (352 all-time supersessions in `surface_history`), so
# expect low tens of medium-severity candidates/day, further bounded by
# `apply_desk_cap` (3/desk/scan) + rollup coalescing like every other class.
#
# H17 — this query was the tree's WORST instance of the correlated critique
# probe, and the measurement says why. Its bridge lateral (`vf`) asked, per
# contention, for the newest finding whose `derived_from` overlaps that
# contention's fact/evidence ids AND whose critique clears the floor. The
# `ORDER BY … LIMIT 1` made the planner want presorted rows, so it served the
# finding scan from `idx_analyst_outputs_kind` instead of the GIN index on
# `derived_from` — 43,787 rows filtered per contention, 500 contentions,
# 25 million buffer hits, 42.5 s live (EXPLAIN ANALYZE, 2026-09-24).
#
# Set-based, the per-row decision disappears: `v` aggregates each contention's
# ids ONCE, `cand` joins the findings through the GIN index ONCE, `cr` folds the
# critiques for the candidate ids ONCE through the expression index, and a
# `DISTINCT ON (cid)` picks the same newest passing finding per contention that
# the lateral's `ORDER BY produced_at DESC, id DESC LIMIT 1` picked. Same rows,
# same tie-break, 2.1 s.
#
# `COALESCE(v.fact_ids, …)` on the way out because the aggregate lateral always
# produced a row (empty arrays) while a GROUP BY produces none for a contention
# with no non-junk values.
_CONTENTIONS_SQL = f"""
    WITH c AS MATERIALIZED (
        SELECT c.id, c.subject_key, c.predicate_key, c.status,
               c.surfaced_value, c.surfaced_fact_id, c.value_count, c.updated_at
          FROM fact_contention c
         ORDER BY c.updated_at DESC
         LIMIT $3
    ), v AS MATERIALIZED (
        SELECT u.contention_id AS cid,
               COALESCE(array_agg(DISTINCT u.fid), '{{}}'::uuid[]) AS fact_ids,
               COALESCE(
                   array_agg(DISTINCT s.sid) FILTER (WHERE s.sid IS NOT NULL),
                   '{{}}'::uuid[]
               ) AS evidence_ids
          FROM (
            SELECT fcv.contention_id, unnest(fcv.supporting_fact_ids) AS fid
              FROM fact_contention_values fcv
             WHERE fcv.contention_id IN (SELECT id FROM c)
               AND NOT fcv.is_junk
          ) u
          LEFT JOIN LATERAL (
            SELECT unnest(f2.derived_from) AS sid
              FROM facts f2
             WHERE f2.id = u.fid
          ) s ON TRUE
         GROUP BY u.contention_id
    ), cand AS MATERIALIZED (
        SELECT v.cid, f.id, f.confidence, f.produced_at
          FROM v
          JOIN analyst_outputs f
            ON f.derived_from && (v.fact_ids || v.evidence_ids)
         WHERE f.kind = 'finding'
           AND f.superseded_by IS NULL
           AND f.analyst_id <> ALL($1::text[])
    ), {critic_fold.latest_critique_cte(
        "cr",
        critic_fold.FAITHFULNESS_SCORE_COLUMN,
        "SELECT id::text FROM cand",
    )}, vf AS MATERIALIZED (
        SELECT DISTINCT ON (cand.cid)
               cand.cid,
               cand.id::text AS finding_id,
               LEAST(cand.confidence, cr.faithfulness_score) AS eff_conf
          FROM cand
          JOIN cr ON cr.fid = cand.id::text
         WHERE LEAST(cand.confidence, cr.faithfulness_score) >= $2
         ORDER BY cand.cid, cand.produced_at DESC, cand.id DESC
    )
    SELECT c.id::text          AS contention_id,
           c.subject_key       AS subject_key,
           c.predicate_key     AS predicate_key,
           c.status            AS status,
           c.surfaced_value    AS surfaced_value,
           c.surfaced_fact_id::text AS surfaced_fact_id,
           c.value_count       AS value_count,
           c.updated_at        AS updated_at,
           COALESCE(v.fact_ids, '{{}}'::uuid[]) AS fact_ids,
           vf.finding_id       AS verified_finding_id,
           vf.eff_conf         AS verified_eff_conf
      FROM c
      LEFT JOIN v  ON v.cid  = c.id
      LEFT JOIN vf ON vf.cid = c.id
     ORDER BY c.updated_at DESC
"""


async def scan_contention_flips(
    conn: Any,
    *,
    floor: float,
) -> tuple[list[Any], list[tuple[str, str, dict[str, Any]]], bool]:
    """Page on a contention SURFACE change that a verified finding rests on.

    Returns ``(candidates, silent_watermarks, was_seeded)`` — the shape
    ``alert_trigger_scan.handle`` already folds in for this class.
    """
    from ...provenance.kinds import STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
    from .alert_trigger_scan import (
        TRIGGER_CONTENTION,
        AlertCandidate,
        _MAX_DERIVED_REFS,
        _load_class_watermarks,
        _uuid_or_none,
    )

    seeded, watermarks = await _load_class_watermarks(conn, TRIGGER_CONTENTION)
    rows = await conn.fetch(
        _CONTENTIONS_SQL,
        sorted(STRUCTURAL_VERIFY_EXEMPT_ANALYSTS),
        float(floor),
        _MAX_CONTENTIONS_PER_SCAN,
    )

    candidates: list[AlertCandidate] = []
    silent: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        cid = str(row["contention_id"])
        state = {
            "status": str(row["status"] or ""),
            "surfaced_fact_id": row["surfaced_fact_id"],
        }
        prev = watermarks.get(cid)
        verified_finding_id = row["verified_finding_id"]
        if not seeded:
            silent.append((TRIGGER_CONTENTION, cid, state))
            continue
        if prev is None:
            change = "new-contention"
        elif (
            str(prev.get("status") or "") != state["status"]
            or prev.get("surfaced_fact_id") != state["surfaced_fact_id"]
        ):
            change = f"{prev.get('status') or '?'}->{state['status']}"
        else:
            continue  # unchanged
        if verified_finding_id is None:
            # No verified finding rests on this dispute — record the state so
            # the SAME change can't fire later, but page nobody.
            silent.append((TRIGGER_CONTENTION, cid, state))
            continue

        refs: list[UUID] = []
        vf = _uuid_or_none(verified_finding_id)
        if vf is not None:
            refs.append(vf)
        for fid in list(row["fact_ids"] or []):
            f = _uuid_or_none(fid)
            if f is not None and f not in refs and len(refs) < _MAX_DERIVED_REFS:
                refs.append(f)
        candidates.append(
            AlertCandidate(
                trigger_class=TRIGGER_CONTENTION,
                severity="medium",
                title=(
                    f"Contention {change}: "
                    f"{row['subject_key']} / {row['predicate_key']} "
                    f"[{state['status']}]"
                ),
                body=(
                    f"contention={cid}\n"
                    f"subject={row['subject_key']} predicate={row['predicate_key']}\n"
                    f"change={change} status={state['status']} "
                    f"surfaced_value={row['surfaced_value']} "
                    f"value_count={row['value_count']}\n"
                    f"verified_finding={verified_finding_id} "
                    f"(effective_confidence={row['verified_eff_conf']}, "
                    f"floor={floor})"
                ),
                target_id=None,
                derived_from=refs,
                data={
                    "trigger_class": TRIGGER_CONTENTION,
                    "contention_id": cid,
                    "subject_key": str(row["subject_key"] or ""),
                    "predicate_key": str(row["predicate_key"] or ""),
                    "change": change,
                    "from_state": dict(prev) if prev else None,
                    "to_state": state,
                    "surfaced_value": row["surfaced_value"],
                    "value_count": int(row["value_count"] or 0),
                    "verified_finding_id": str(verified_finding_id),
                    "verified_effective_confidence": (
                        float(row["verified_eff_conf"])
                        if row["verified_eff_conf"] is not None
                        else None
                    ),
                },
                watermarks=[(TRIGGER_CONTENTION, cid, state)],
                event_at=row["updated_at"],
            )
        )
    return candidates, silent, seeded

