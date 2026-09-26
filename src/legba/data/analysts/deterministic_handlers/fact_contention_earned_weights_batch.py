# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""h4 — one query for a whole ``fact_contention_arbiter`` pass's worth of
earned-weight tie-breaks, instead of one per contention group.

``fact_contention_arbiter._attach_earned_weights`` (A6 P3-3, OFF by default)
issued TWO round trips per contention group needing the weighted tie-break: a
fact -> source lineage lookup, then a multi-CTE aggregate over resolved
contentions EXCLUDING the group's own id (the acyclicity guard). Measured live
2026-09-20: 3 885 past-soak groups x ~64 ms via that per-group path = 247 s of
one arbiter pass — the dominant cost behind the pass overrunning its actor
invoke timeout (see ``fact_contention_pass.py``'s module docstring for the
full incident).

The fix extracted here: fetch the WHOLE pass's inputs in ONE query —
:data:`EARNED_WEIGHTS_BATCH_SQL` — and let each group's decision be a
Python-side filter over rows already in hand (:class:`EarnedWeightsBatch`)
rather than a fresh round trip. Only the two pieces the arbiter's consumption
path (``SourceRecord.earned_signal``) actually reads survive the join —
corroboration is computed by the ORIGINAL ``source_track_record._RECORDS_SQL``
too (feeds the daily ledger's display fields), but ``earned_signal`` is a pure
function of wins/losses alone, so this batch drops corroboration entirely
rather than reproduce dead weight.

THE EQUIVALENCE ARGUMENT
-------------------------
The original per-group query computed, for contention ``G`` with sources
``S_G``: wins/losses per source over ``{resolved contentions} MINUS {G}``,
narrowed to ``source_id = ANY(S_G)`` (a pure performance narrowing — extra
rows for sources outside ``S_G`` are simply never read). This module instead
fetches, ONCE, wins/losses per source over the FULL ``{resolved contentions}``
set (cutoff-filtered only, no exclusion, no allowlist), granular at
``(contention_id, source_id)``. :meth:`EarnedWeightsBatch.earned_weights` then
computes, for a specific group: sum over rows where ``source_id in S_G AND
contention_id != G`` — the identical row set the original WHERE-clause
exclusion selected, just filtered in Python instead of SQL. "Exclude G" and
"subtract G's rows from a superset already fetched" commute; the per-source
win/loss integers — and therefore ``earned_side_weight`` — are byte-identical
either way.

The fact -> source half is the same trick: instead of one
``_FACT_SOURCES_SQL`` round trip per group scoped to that group's own
``supporting_fact_ids``, this fetches source ids for EVERY currently-open fact
the pass already read (a safe superset — the caller passes the full open-fact
id list from ``_open_triples`` / ``_open_functional_role_triples``), and each
group looks its own facts up in the resulting dict.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Sequence
from uuid import UUID

#: Row-kind discriminated by which of (contention_id, fact_id) is non-NULL:
#:   * an OUTCOME row: one (contention, source) pair from the FULL resolved
#:     set (cutoff-filtered only);
#:   * a FACT-SOURCE row: one (fact, source) pair for the arbiter's own
#:     currently-open corpus (``$2`` — every open fact id the pass already
#:     read, a safe superset of any single group's ``supporting_fact_ids``).
EARNED_WEIGHTS_BATCH_SQL = """
WITH cutoff_resolved AS (
    SELECT id AS contention_id
      FROM fact_contention
     WHERE status = 'surfaced'
       AND surfaced_fact_id IS NOT NULL
       AND surfaced_at IS NOT NULL
       AND surfaced_at < $1::timestamptz
),
cluster_facts AS (
    SELECT fcv.contention_id,
           fcv.surfaced_winner AS is_winner,
           sf.fact_id
      FROM fact_contention_values fcv
      JOIN cutoff_resolved r ON r.contention_id = fcv.contention_id
     CROSS JOIN LATERAL unnest(fcv.supporting_fact_ids) AS sf(fact_id)
     WHERE fcv.is_junk = false
),
cluster_sources AS (
    SELECT DISTINCT cf.contention_id, cf.is_winner, s.source_id
      FROM cluster_facts cf
      JOIN facts f ON f.id = cf.fact_id
     CROSS JOIN LATERAL unnest(f.derived_from) AS d(sig)
      JOIN signals s ON s.id = d.sig
),
per_contention_source AS (
    SELECT contention_id, source_id,
           bool_or(is_winner)     AS on_winning_side,
           bool_or(NOT is_winner) AS on_losing_side
      FROM cluster_sources
     GROUP BY contention_id, source_id
),
open_fact_sources AS (
    SELECT f.id AS fact_id, s.source_id
      FROM facts f
     CROSS JOIN LATERAL unnest(f.derived_from) AS d(sig)
      JOIN signals s ON s.id = d.sig
     WHERE f.id = ANY($2::uuid[])
)
SELECT contention_id, NULL::uuid AS fact_id, source_id, on_winning_side, on_losing_side
  FROM per_contention_source
UNION ALL
SELECT NULL::uuid AS contention_id, fact_id, source_id, NULL::boolean, NULL::boolean
  FROM open_fact_sources
"""


class EarnedWeightsBatch:
    """One pass's worth of earned-weight inputs, fetched in a single round
    trip via :func:`fetch_earned_weights_batch`.

    ``fact_sources`` — every open fact this pass read, resolved to its
    carrying source(s) (replaces ``fact_contention_arbiter._FACT_SOURCES_SQL``,
    called once per group on the un-batched path). ``outcomes_by_source`` —
    every RESOLVED contention's ``(contention_id, on_winning_side,
    on_losing_side)`` triple, indexed by source (replaces the
    ``source_track_record._RECORDS_SQL`` aggregate, called once per group with
    that group's own ``exclude_contention``)."""

    __slots__ = ("fact_sources", "outcomes_by_source")

    def __init__(
        self,
        fact_sources: dict[UUID, set[str]],
        outcomes_by_source: dict[str, list[tuple[UUID, bool, bool]]],
    ) -> None:
        self.fact_sources = fact_sources
        self.outcomes_by_source = outcomes_by_source

    def sources_for(self, fact_ids: Iterable[UUID]) -> set[str]:
        out: set[str] = set()
        for fid in fact_ids:
            out |= self.fact_sources.get(fid, set())
        return out

    def earned_weights(
        self, source_ids: Iterable[str], *, exclude_contention: UUID,
    ) -> dict[str, float]:
        """Per-source earned signal, EXCLUDING ``exclude_contention`` — the
        same acyclicity guard the per-group query enforced in SQL, applied
        here as a Python-side filter over rows already fetched."""
        from . import source_track_record as _str  # lazy: OFF path never imports

        out: dict[str, float] = {}
        for sid in source_ids:
            wins = losses = 0
            for cid, on_winning_side, on_losing_side in self.outcomes_by_source.get(sid, ()):
                if cid == exclude_contention:
                    continue
                if on_winning_side:
                    wins += 1
                elif on_losing_side:
                    losses += 1
            out[sid] = _str.earned_side_weight(wins, losses)
        return out


async def fetch_earned_weights_batch(
    conn: Any, fact_ids: Sequence[UUID], *, cutoff: datetime,
) -> EarnedWeightsBatch:
    """The ONE query behind :class:`EarnedWeightsBatch` — see the module
    docstring for the equivalence argument."""
    rows = await conn.fetch(
        EARNED_WEIGHTS_BATCH_SQL, cutoff, list(dict.fromkeys(fact_ids)),
    )
    fact_sources: dict[UUID, set[str]] = {}
    outcomes_by_source: dict[str, list[tuple[UUID, bool, bool]]] = {}
    for r in rows or ():
        sid = r["source_id"]
        if not sid:
            continue
        sid = str(sid)
        if r["contention_id"] is not None:
            outcomes_by_source.setdefault(sid, []).append(
                (r["contention_id"], bool(r["on_winning_side"]), bool(r["on_losing_side"])),
            )
        elif r["fact_id"] is not None:
            fact_sources.setdefault(r["fact_id"], set()).add(sid)
    return EarnedWeightsBatch(fact_sources, outcomes_by_source)


__all__ = [
    "EARNED_WEIGHTS_BATCH_SQL",
    "EarnedWeightsBatch",
    "fetch_earned_weights_batch",
]
