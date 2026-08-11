# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``reifier_selection`` — which candidates ``relationship_reifier`` may type (K-G2).

THE DEFECT THIS MODULE EXISTS TO FIX
------------------------------------
``relationship_reifier._read_candidates`` chose its per-run window with two
compounding defects, measured against the live substrate on 2026-08-03 and
written up in ``docs/TYPING_BAKEOFF_2026-08-03.md`` §1:

**(a) No ``status`` filter.** The query read the WHOLE ``proposed_edges`` table,
not the pending queue. Only ``pending`` rows can ever become a new edge, and
``pending`` confidence tops out at 0.750 — while ``orphaned`` / ``rejected`` /
``promoted`` rows reach 1.000. Ordered by ``confidence DESC``, the dead rows
therefore sorted ABOVE every live candidate.

**(b) The dedup guard missed its own output.** ``write_nexus`` is preceded by
:func:`~legba.data._entity_resolve.resolve_keeper`, which rewrites both
endpoints to their elected ``entity_profiles`` keeper. The ``NOT EXISTS`` guard
compared the RAW ``proposed_edges`` surfaces against ``nexuses``, which carry
the keeper-rewritten names — so a successfully-promoted pair stayed eligible
forever. Measured: the promoted pair ``Iran → US`` matched **0** open nexuses on
the raw surfaces, while its keeper-resolved form ``Iran → United States`` has
two.

Together they produced the headline finding: reproducing the reifier's exact
window against the live DB returned **0 pending rows in the top 40** (24
``orphaned``, 15 ``promoted``, 1 ``rejected``). Every one of the typer's 80 LLM
calls per day was spent on rows that are structurally incapable of producing a
new edge.

THE FIX
-------
:func:`select_candidates` is the replacement window, and it is deliberately a
TWO-STAGE selection rather than one clever query:

  1. **SQL stage** — ``status = 'pending'`` (the live queue, and only it) above
     the confidence floor, newest-and-strongest first, read with headroom
     (:data:`EXAMINE_MULTIPLIER`) so the Python stage has rows to spend.
  2. **Python stage** — resolve each pair the SAME way the WRITE path does
     (:func:`~legba.data._entity_canon.canonicalize_entity` then
     :func:`~legba.data._entity_resolve.resolve_keeper`), then ask ONE bulk
     query which of those RESOLVED pairs already carry an open nexus.

Stage 2 is why the guard is now merge-aware: it compares like with like. A pair
whose endpoints were merged away, aliased, or already reified resolves onto the
same keeper names the nexus carries, and is excluded — permanently, instead of
never.

The guard is also **bidirectional**. A ``co_occurs`` proposed edge is an
UNORDERED co-mention; both ``A→B`` and ``B→A`` can exist as distinct rows (the
``uq_proposed_edges_triple`` unique index is on the ordered triple). Typing both
would mint two nexuses for one co-mention pair, so an open nexus in EITHER
direction retires the candidate.

Nothing here writes. It reads, it resolves, it counts, and it hands the caller a
window plus a :class:`SelectionCounters` receipt.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .._entity_canon import canonicalize_entity, is_junk_entity, same_referent
from .._entity_resolve import resolve_keeper
from .edge_qualification import (
    MIN_INDEPENDENT_SOURCES,
    RECOMMENDED_BAR,
    scored_pool_sql,
)

logger = logging.getLogger(__name__)

#: Skip the thinnest co-occurrence edges — a single co-mention (confidence ~0.4
#: from entity_resolution) is too weak to reify. Pairs accrue confidence as they
#: re-co-occur; this floors the candidate set at "seen more than once". Defined
#: HERE (not in ``relationship_reifier``) because it is a SELECTION knob;
#: ``relationship_reifier`` re-exports the name for back-compat.
MIN_EDGE_CONFIDENCE: float = 0.45

#: The only ``proposed_edges.status`` that can still become an edge. ``promoted``
#: already is one; ``rejected`` was refused; ``orphaned`` lost an endpoint to the
#: entity GC. Reading any of them is pure waste — see the module docstring.
PENDING_STATUS: str = "pending"

#: How many rows to READ per row the caller wants to TYPE. Headroom for the
#: candidates the Python stage drops (junk endpoints, self-loops, and pairs an
#: open nexus already covers). 3× is measured-generous: on the live pool the
#: keeper-aware guard retires well under a third of a pending window, because
#: pending rows are by definition the ones that have NOT been reified.
EXAMINE_MULTIPLIER: int = 3

#: Absolute ceiling on the SQL stage regardless of ``limit × EXAMINE_MULTIPLIER``.
#: Bounds one run's read + keeper-resolution work; the cap is the throughput
#: dial, this is the blast radius.
MAX_EXAMINE: int = 8000


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

#: Stage 1a — SCORE the live queue. ``$1`` status, ``$2`` confidence floor,
#: ``$3`` minimum independent sources, ``$4`` row cap.
#:
#: Ordered by the QUALIFICATION SCORE, not by ``proposed_edges.confidence``.
#: That column is accumulated co-mention weight: it cannot tell nine newsrooms
#: independently reporting a relationship from one wire story syndicated to nine
#: outlets, which for a graph whose whole claim is that edges are EARNED is the
#: wrong quantity to sort on (``docs/TYPING_BAKEOFF_2026-08-03.md`` §2.1).
#:
#: The independent-source floor is pushed into SQL because it is a HARD gate,
#: not a ranking — and because it is what makes this query fast: 92.1% of the
#: pending pool rests on a single independent source, so the floor takes the
#: scan from ~176,000 rows to ~13,000 in one predicate.
#:
#: Deliberately narrow: ids and numbers only. The row payload (evidence text,
#: lineage) is fetched for the WINNERS by :data:`CANDIDATE_FETCH_SQL`, so a
#: 13,000-row scan never drags 13,000 evidence excerpts through the CTE chain.
QUALIFICATION_SCAN_SQL = (
    scored_pool_sql(status_filter="pe.status = $1 AND pe.confidence >= $2")
    + """
 WHERE e.independent_sources >= $3
 ORDER BY qual_score DESC, e.produced_at DESC
 LIMIT $4
"""
)

#: Stage 1b — the row payload for the chosen ids, in scan order.
#:
#: ``source_signal_text`` is the UNION of every backing signal's title+summary
#: (FU4) — the D14 sports gate runs over it, so a sports frame living in a
#: DIFFERENT source signal than the excerpt still gates. ``NULL`` when the edge
#: has no signal lineage.
#:
#: The ``status`` predicate is re-asserted (``$2``) even though the ids came
#: from a status-filtered scan: it is free on ``idx_proposed_edges_status`` and
#: it means the queue fix cannot be defeated by a stale id list.
CANDIDATE_FETCH_SQL = """
SELECT pe.id, pe.source_entity, pe.target_entity, pe.evidence_text,
       pe.confidence, pe.produced_at, pe.derived_from,
       (
         SELECT string_agg(
                  btrim(
                    coalesce(s.payload->>'title', '') || ' ' ||
                    coalesce(s.payload->>'summary', '')
                  ),
                  ' '
                )
           FROM signals s
          WHERE s.id = ANY(pe.derived_from)
       ) AS source_signal_text
  FROM proposed_edges pe
 WHERE pe.id = ANY($1::uuid[])
   AND pe.status = $2
"""

#: Stage 2's bulk guard. Takes the KEEPER-RESOLVED, lowercased endpoint pairs as
#: two parallel arrays and returns the subset already covered by an OPEN nexus
#: in EITHER direction. One round trip for the whole window; each probe rides
#: ``idx_nexuses_triple_open`` (lower(subject), …, lower(object)).
ALREADY_REIFIED_SQL = """
SELECT DISTINCT p.a AS a, p.b AS b
  FROM unnest($1::text[], $2::text[]) AS p(a, b)
 WHERE EXISTS (
     SELECT 1 FROM nexuses n
      WHERE n.valid_until IS NULL AND n.superseded_by IS NULL
        AND (
              (lower(n.subject) = p.a AND lower(n.object) = p.b)
           OR (lower(n.subject) = p.b AND lower(n.object) = p.a)
        )
 )
"""


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


@dataclass
class SelectionCounters:
    """Per-run selection receipt. Rides the reifier's summary ``FindingPayload``.

    The point of counting each stage separately is that the K-G2 defect was
    INVISIBLE: the old run summary reported ``candidates=40`` every tick and
    said nothing about those 40 being dead rows. A window that collapses now
    says WHERE it collapsed.
    """

    #: Rows the SCORING scan returned: pending, above the confidence floor, above
    #: the hard independent-source floor, best-scoring first, bounded by
    #: ``limit × EXAMINE_MULTIPLIER``.
    examined: int = 0
    #: Of those, how many cleared the qualification bar.
    #:
    #: Read this against ``examined``. Equal ⇒ the scan's LIMIT was the binding
    #: constraint, i.e. more qualifying work exists than the run could look at
    #: (the DRAIN state). Less ⇒ the qualifying pool ran out inside the window
    #: (the STEADY state, where throughput is arrival-bound). That distinction
    #: costs nothing and is the difference between "we are behind" and "we are
    #: caught up".
    qualified: int = 0
    #: Dropped before any keeper work: junk endpoint or a canon self-loop.
    skipped_endpoints: int = 0
    #: Dropped by the merge-aware guard — an open nexus already covers the
    #: KEEPER-RESOLVED pair in one direction or the other.
    already_reified: int = 0
    #: Dropped because the keeper rewrite collapsed both endpoints onto one
    #: entity (two surfaces of the same actor — never a relationship).
    keeper_self_loop: int = 0
    #: Survived every drop. May exceed ``selected`` — the run cap is the last
    #: thing applied, so this is the true depth of live work available.
    eligible: int = 0
    #: Handed to the typer (``min(eligible, limit)``).
    selected: int = 0

    def as_dict(self) -> dict[str, int]:
        return {k: int(v) for k, v in asdict(self).items()}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def resolve_pair(
    conn: Any,
    source: str,
    target: str,
    *,
    cache: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Canonicalize + keeper-resolve one candidate pair, or ``None`` to drop it.

    Mirrors the WRITE path's ordering exactly — ``canonicalize_entity`` (demonym
    → country, HTML strip, alias map, junk gate) and only THEN
    :func:`resolve_keeper` (the elected ``entity_profiles`` keeper). Comparing
    the dedup guard on anything less is the defect this module fixes.

    ``None`` on: a junk endpoint, a canon self-loop ("Iran"/"Iranian"), or a
    KEEPER self-loop — two distinct surfaces that both elect the same keeper
    ("Axis of Resistance" + "Resistance") are one actor, not a relationship.
    """
    if is_junk_entity(source) or is_junk_entity(target):
        return None
    c_source, _ = canonicalize_entity(source, "entity")
    c_target, _ = canonicalize_entity(target, "entity")
    if not c_source or not c_target or same_referent(c_source, c_target):
        return None
    k_source = (
        await resolve_keeper(conn, c_source, entity_class="entity", cache=cache)
    ).strip() or c_source
    k_target = (
        await resolve_keeper(conn, c_target, entity_class="entity", cache=cache)
    ).strip() or c_target
    if same_referent(k_source, k_target):
        return None
    return k_source, k_target


async def already_reified(
    conn: Any, pairs: Sequence[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Which KEEPER-RESOLVED ``pairs`` already carry an open nexus, either way.

    Returns lowercased pairs in the orientation they were ASKED about, so the
    caller can test membership with its own tuple. Degrade-not-break: any error
    returns the empty set — a failed guard costs a duplicate typing call, which
    is far cheaper than a failed run.
    """
    if not pairs:
        return set()
    a_side = [str(a or "").strip().lower() for a, _ in pairs]
    b_side = [str(b or "").strip().lower() for _, b in pairs]
    try:
        rows = await conn.fetch(ALREADY_REIFIED_SQL, a_side, b_side)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("reifier_selection.dedup_probe_failed err=%s", exc)
        return set()
    return {(str(r["a"]), str(r["b"])) for r in rows}


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


async def select_candidates(
    conn: Any,
    *,
    limit: int,
    status: str = PENDING_STATUS,
    min_confidence: float = MIN_EDGE_CONFIDENCE,
    bar: float = RECOMMENDED_BAR,
    min_sources: int = MIN_INDEPENDENT_SOURCES,
    keeper_cache: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], SelectionCounters]:
    """The per-run candidate window: pending, QUALIFIED, merge-aware-deduped, capped.

    Ordering is by qualification score (:mod:`.edge_qualification`), never by
    ``proposed_edges.confidence`` — see :data:`QUALIFICATION_SCAN_SQL`.

    Two gates, both from the bake-off's recommendation (§7.4): a hard floor of
    ``min_sources`` INDEPENDENT sources, then the weighted score against ``bar``.
    At the recommended 0.42 the floor does no work — a one-source pair zeroes
    both ``multi_source`` (0.45) and ``source_diversity`` (0.20) and so cannot
    exceed 0.35 — but it is applied anyway, so that LOWERING the bar later to
    widen the queue can never silently re-admit single-sourced sludge.

    Each returned row is the ``proposed_edges`` row dict PLUS:

      ``qual_score``
          The score it was ranked on, carried through for the run receipt.
      ``keeper_source`` / ``keeper_target``
          The canonicalized, keeper-elected endpoint surfaces. These are what
          the dedup guard compared, and they are what the typer should be shown.

    ``keeper_cache`` is the caller's per-run memo (endpoints repeat heavily
    across pairs and the keeper fallback probe is an unindexed scan). Pass the
    same dict the write path uses so one run resolves each surface once.
    """
    counters = SelectionCounters()
    want = max(1, int(limit))
    examine = min(MAX_EXAMINE, want * EXAMINE_MULTIPLIER)
    cache = keeper_cache if keeper_cache is not None else {}

    scan = await conn.fetch(
        QUALIFICATION_SCAN_SQL,
        str(status), float(min_confidence), int(min_sources), int(examine),
    )
    counters.examined = len(scan)

    # The scan is already ordered best-first and already carries the hard source
    # floor, so the bar is a prefix cut — everything below it is below it.
    scored = [r for r in scan if float(r["qual_score"] or 0.0) >= float(bar)]
    counters.qualified = len(scored)
    if not scored:
        logger.info("reifier_selection.window %s", counters.as_dict())
        return [], counters

    by_id = {r["id"]: float(r["qual_score"] or 0.0) for r in scored}
    fetched = await conn.fetch(CANDIDATE_FETCH_SQL, list(by_id), str(status))
    # Restore the scan's ranking — ANY(...) does not preserve array order.
    rows = sorted(
        fetched, key=lambda r: by_id.get(r["id"], 0.0), reverse=True
    )

    # Resolve every examined pair BEFORE the guard runs, so the bulk probe sees
    # the same surfaces the write path will.
    resolved: list[tuple[dict[str, Any], tuple[str, str]]] = []
    for row in rows:
        cand = dict(row)
        cand["qual_score"] = by_id.get(row["id"], 0.0)
        pair = await resolve_pair(
            conn,
            str(cand.get("source_entity") or ""),
            str(cand.get("target_entity") or ""),
            cache=cache,
        )
        if pair is None:
            # resolve_pair folds three drop reasons; separate the keeper one for
            # the receipt by re-testing the cheap canon-only condition.
            raw_s = str(cand.get("source_entity") or "")
            raw_t = str(cand.get("target_entity") or "")
            c_s, _ = canonicalize_entity(raw_s, "entity")
            c_t, _ = canonicalize_entity(raw_t, "entity")
            if (
                is_junk_entity(raw_s)
                or is_junk_entity(raw_t)
                or not c_s
                or not c_t
                or same_referent(c_s, c_t)
            ):
                counters.skipped_endpoints += 1
            else:
                counters.keeper_self_loop += 1
            continue
        resolved.append((cand, pair))

    covered = await already_reified(conn, [p for _, p in resolved])

    # Guard the WHOLE examined set before capping, so the drop counters describe
    # the same population ``examined`` does. The cap is applied last and only to
    # the survivors — a receipt that stopped counting mid-window would hide
    # exactly the collapse this module exists to make visible.
    out: list[dict[str, Any]] = []
    for cand, (k_source, k_target) in resolved:
        if (k_source.lower(), k_target.lower()) in covered:
            counters.already_reified += 1
            continue
        cand["keeper_source"] = k_source
        cand["keeper_target"] = k_target
        out.append(cand)

    counters.eligible = len(out)
    out = out[:want]
    counters.selected = len(out)
    logger.info(
        "reifier_selection.window %s", counters.as_dict(),
    )
    return out, counters


__all__ = [
    "MIN_EDGE_CONFIDENCE",
    "PENDING_STATUS",
    "EXAMINE_MULTIPLIER",
    "MAX_EXAMINE",
    "QUALIFICATION_SCAN_SQL",
    "CANDIDATE_FETCH_SQL",
    "ALREADY_REIFIED_SQL",
    "SelectionCounters",
    "resolve_pair",
    "already_reified",
    "select_candidates",
]
