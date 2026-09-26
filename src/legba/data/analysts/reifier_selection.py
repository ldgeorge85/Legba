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

ADDENDUM (2026-09-23) — the already-reified HALF of stage 2 moved into stage 1.
Measured on the live pool (the 2026-09-23 12:45Z receipt): 1,330 of every 1,800
examined rows were dead on arrival at stage 2 — a pending row whose
keeper-resolved pair already carried an open nexus, discovered only AFTER
paying for the keeper resolution on all 1,800. :data:`QUALIFICATION_SCAN_SQL`
now carries the SAME bidirectional open-nexus test as a negated predicate
(:data:`_ALREADY_REIFIED_GUARD_SQL`, over a SET-BASED keeper map —
:func:`_guarded_pool_sql`), so the scan itself returns only
rows that have NOT been reified — stage 1's head is now ``examine`` USEFUL
candidates, not ``examine`` raw rows most of which stage 2 would have thrown
away. Stage 2 still runs :func:`resolve_pair` per survivor (junk/self-loop
detection, and the ``keeper_source``/``keeper_target`` the caller needs); the
receipt's ``already_reified`` counter is now read from a companion count query
(:data:`ALREADY_REIFIED_COUNT_SQL`) instead of a Python loop, because a row the
SQL excludes is invisible to the query that excluded it.

Nothing here writes. It reads, it resolves, it counts, and it hands the caller a
window plus a :class:`SelectionCounters` receipt.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .._entity_canon import canonicalize_entity, is_junk_entity, same_referent
from .._entity_resolve import _CLASS_PRIORITY_SQL, resolve_keeper
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
#: candidates stage 2 still drops (junk endpoints, self-loops) now that the
#: already-reified guard runs INSIDE the SQL scan itself (2026-09-23 — see
#: :data:`_ALREADY_REIFIED_GUARD_SQL`) rather than after the fact. Before that
#: fix this multiplier ALSO had to cover the already-reified drop, and it did
#: not: the 2026-09-23 12:45Z receipt measured 1,330 of 1,800 examined rows
#: (74%) as already-reified, not "well under a third" as this comment used to
#: claim.
EXAMINE_MULTIPLIER: int = 3

#: Absolute ceiling on the SQL stage regardless of ``limit × EXAMINE_MULTIPLIER``.
#: Bounds one run's read + keeper-resolution work; the cap is the throughput
#: dial, this is the blast radius.
MAX_EXAMINE: int = 8000
#: H9 (2026-09-23) — a candidate the typer REJECTED is stamped ``reviewed_at`` and
#: waits this many days before the scan may hand it to the model again. Measured
#: before this: of the scan's top 1,800 pending, 907 were not reified and 847 of
#: those were older than 14 days — the same ~470 high-scoring rejects typed twice a
#: day for up to the 30-day age-out, while fresh candidates never reached the head.
#: A rejection is stochastic at temperature 1.0, so the row stays ``pending`` (the
#: governance pass keeps owning ``status``); only the re-type spacing changes.
REJECT_COOLDOWN_DAYS: int = 7


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

#: H9's cooldown predicate, templated so :data:`QUALIFICATION_SCAN_SQL` and
#: :data:`ALREADY_REIFIED_COUNT_SQL` render it identically and can never
#: silently drift apart on which rows even ENTER the qualifying pool. The two
#: queries number their bind parameters differently (the scan carries a row
#: cap the count query has no use for), so the cooldown's own placeholder
#: number is the one thing each caller supplies.
def _status_filter(cooldown_param: str) -> str:
    return (
        "pe.status = $1 AND pe.confidence >= $2 "
        "AND (pe.reviewed_at IS NULL OR pe.reviewed_at < now() - "
        f"make_interval(days => {cooldown_param}))"
    )


#: ``resolve_keeper``'s active-row filter, restated for the SET-BASED keeper
#: map below (same predicate ``_entity_resolve._EXACT_SQL`` carries).
_ACTIVE_PROFILE_SQL = "COALESCE(ep.data->>'gc_status', '') NOT IN ('merged', 'junk')"


#: The scored pool with BOTH endpoints keeper-resolved — SET-BASED, never a
#: correlated probe per row. Measured on the live pool (2026-09-24 03:37Z, the
#: review of this lane): a per-row correlated ``entity_profiles`` probe (an
#: ``OR`` over an exact-name match and an alias-containment ``EXISTS``, which
#: no index can serve) could not finish over the ~36,000-row qualifying pool
#: inside a 180 s statement timeout — 36,000 × 2 × 148,000 profile rows. This
#: shape finishes in ~8 s, of which ~6 s is the pre-existing pool scan itself:
#:
#: * ``keeper_aliases`` expands every active profile's ``merged_aliases`` ONCE
#:   (~23,000 rows on the live substrate, one 80 ms scan);
#: * ``pool`` is :func:`~.edge_qualification.scored_pool_sql` with the hard
#:   independent-source floor pushed inside, so ``surfaces`` sees only rows the
#:   caller could ever type;
#: * ``surfaces`` is the DISTINCT set of endpoint surfaces in that pool
#:   (~13,000 live);
#: * ``keeper_map`` resolves each surface the way ``resolve_keeper``'s two
#:   FASTEST probes do — exact canonical name (hash-joined against
#:   ``lower(canonical_name)``) UNION exact alias containment (hash-joined
#:   against ``keeper_aliases``) — with the SAME active-row filter and the SAME
#:   class-priority tie-break (:data:`_CLASS_PRIORITY_SQL`, IMPORTED from
#:   ``_entity_resolve`` rather than restated; this module already imports
#:   :func:`resolve_keeper` from that sibling, so the layering direction is
#:   established);
#: * ``resolved`` is the pool with ``ks``/``kt`` — the keeper surface, or the
#:   raw surface itself when no keeper matches (``resolve_keeper``'s own
#:   degrade-not-break contract).
#:
#: Deliberately DOES NOT run the normalized/article-stripped fallback probe
#: (``_ALIAS_SQL``) or the E1.1 override — duplicating ``resolve_keeper``'s
#: full branching a second time in SQL would be the wrong trade for a
#: pre-filter. The Python-side :func:`resolve_pair` (still run per survivor,
#: for ``keeper_source``/``keeper_target``) stays the single source of truth
#: for a candidate that actually reaches the typer; this map only needs to
#: catch the common case cheaply, in the planner, before stage 2 pays for a
#: full keeper resolution.
#:
#: ``status_filter`` is the H9-templated pool predicate; ``floor_param`` the
#: bind placeholder carrying the independent-source floor (the two callers
#: number their parameters differently).
def _guarded_pool_sql(status_filter: str, floor_param: str) -> str:
    prio = _CLASS_PRIORITY_SQL.replace("CASE entity_class", "CASE ep.entity_class", 1)
    return f"""WITH keeper_aliases AS (
    SELECT lower(btrim(al)) AS alias, ep.canonical_name, {prio} AS prio, ep.created_at
      FROM entity_profiles ep
      CROSS JOIN LATERAL jsonb_array_elements_text(
          CASE WHEN jsonb_typeof(ep.data->'merged_aliases') = 'array'
               THEN ep.data->'merged_aliases' ELSE '[]'::jsonb END) AS al
     WHERE {_ACTIVE_PROFILE_SQL}
), pool AS (
    SELECT * FROM (
{scored_pool_sql(status_filter=status_filter)}
    ) p WHERE p.independent_sources >= {floor_param}
), surfaces AS (
    SELECT DISTINCT lower(btrim(source_entity)) AS s FROM pool
    UNION
    SELECT DISTINCT lower(btrim(target_entity)) FROM pool
), keeper_map AS (
    SELECT DISTINCT ON (m.s) m.s, lower(m.canonical_name) AS keeper
      FROM (
        SELECT s.s, ep.canonical_name, {prio} AS prio, ep.created_at
          FROM surfaces s
          JOIN entity_profiles ep ON lower(ep.canonical_name) = s.s
         WHERE {_ACTIVE_PROFILE_SQL}
        UNION ALL
        SELECT s.s, ka.canonical_name, ka.prio, ka.created_at
          FROM surfaces s
          JOIN keeper_aliases ka ON ka.alias = s.s
      ) m
     ORDER BY m.s, m.prio, m.created_at ASC
), resolved AS (
    SELECT e.*, coalesce(ks.keeper, lower(btrim(e.source_entity))) AS ks,
                coalesce(kt.keeper, lower(btrim(e.target_entity))) AS kt
      FROM pool e
      LEFT JOIN keeper_map ks ON ks.s = lower(btrim(e.source_entity))
      LEFT JOIN keeper_map kt ON kt.s = lower(btrim(e.target_entity))
)
"""


#: The already-reified test itself, as a boolean SQL expression over the
#: ``resolved`` CTE's ``e.ks`` / ``e.kt`` (:func:`_guarded_pool_sql`) — the
#: SAME bidirectional open-nexus question :data:`ALREADY_REIFIED_SQL` asks for
#: an already-resolved pair, inlined so the planner answers it server-side
#: (two ``idx_nexuses_triple_open`` probes per row) instead of round-tripping
#: through Python once per examined row (the cost the 2026-09-23 12:45Z
#: receipt measured: 1,330 of 1,800 examined rows paid for a keeper resolution
#: whose only purpose was to find out they were dead). Referenced by BOTH
#: :data:`QUALIFICATION_SCAN_SQL` (negated, to EXCLUDE) and
#: :data:`ALREADY_REIFIED_COUNT_SQL` (bare, to COUNT), so the two can never
#: disagree about what "already reified" means.
_ALREADY_REIFIED_GUARD_SQL = (
    "EXISTS (\n"
    "    SELECT 1 FROM nexuses n\n"
    "     WHERE n.valid_until IS NULL AND n.superseded_by IS NULL\n"
    "       AND (\n"
    "             (lower(n.subject) = e.ks AND lower(n.object) = e.kt)\n"
    "          OR (lower(n.subject) = e.kt AND lower(n.object) = e.ks)\n"
    "       )\n"
    ")"
)

#: Stage 1a — SCORE the live queue. ``$1`` status, ``$2`` confidence floor,
#: ``$3`` minimum independent sources, ``$4`` row cap, ``$5`` the reject cooldown in days
#: (H9: a row stamped ``reviewed_at`` inside the cooldown is not offered again).
#:
#: Ordered by the SAME qualification score the bar is measured against, so the
#: window is a prefix of the best-qualified pending rows — never the
#: highest-confidence rows that happen to fail the bar. Two thresholds:
#: ``pe.confidence >= $2`` cuts the raw queue on the extractor's own number;
#: ``e.independent_sources >= $3`` is the HARD floor — anything below it can
#: never clear the bar, so it never enters the window. On the live substrate
#: the vast majority of the pending pool rests on a single independent source,
#: so the floor takes the scan from ~176,000 rows to ~36,000 in one predicate.
#:
#: The already-reified guard (2026-09-23, :data:`_ALREADY_REIFIED_GUARD_SQL`,
#: set-based since 2026-09-24 via :func:`_guarded_pool_sql`) is ALSO pushed
#: into SQL, negated, so the LIMIT below applies to USEFUL candidates rather
#: than to a raw window most of which stage 2 would throw away — see the module
#: docstring's 2026-09-23 addendum.
#:
#: Deliberately narrow: ids and numbers only. The row payload (evidence text,
#: lineage) is fetched for the WINNERS by :data:`CANDIDATE_FETCH_SQL`, so a
#: 36,000-row scan never drags 36,000 evidence excerpts through the CTE chain.
QUALIFICATION_SCAN_SQL = (
    _guarded_pool_sql(status_filter=_status_filter("$5"), floor_param="$3")
    + f"""SELECT e.*
  FROM resolved e
 WHERE NOT ({_ALREADY_REIFIED_GUARD_SQL})  -- reifier_selection.py:already_reified
 ORDER BY qual_score DESC, e.produced_at DESC
 LIMIT $4
"""
)

#: The companion to :data:`QUALIFICATION_SCAN_SQL` that makes the receipt's
#: ``already_reified`` counter possible. A row the scan EXCLUDES is invisible
#: to the scan itself, so the count can't come from ``len(scan)`` arithmetic —
#: this asks the identical already-reified question, un-negated, over the SAME
#: qualifying pool (status / confidence floor / H9 cooldown / independent-
#: source floor — everything the scan filters on EXCEPT the guard and the row
#: cap) and returns how many of it the guard would exclude. ``$1`` status,
#: ``$2`` confidence floor, ``$3`` minimum independent sources, ``$4`` the H9
#: reject cooldown in days. No row cap: a ``count(*)`` over the qualifying pool
#: is one aggregate, not a materialized, LIMIT-bound window.
ALREADY_REIFIED_COUNT_SQL = (
    _guarded_pool_sql(status_filter=_status_filter("$4"), floor_param="$3")
    + f"""SELECT count(*) AS n
  FROM resolved e
 WHERE ({_ALREADY_REIFIED_GUARD_SQL})
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

    #: Rows the SCORING scan returned: pending, above the confidence floor,
    #: above the hard independent-source floor, NOT already reified (2026-09-23
    #: — the already-reified guard now runs INSIDE the scan, see
    #: :data:`_ALREADY_REIFIED_GUARD_SQL`), best-scoring first, bounded by
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
    #: V3/P5 — the DRAIN/STEADY distinction above, as a named flag rather than
    #: an inference the reader has to make. ``True`` exactly when the scoring
    #: scan returned its LIMIT's worth of rows (``examined == examine``) AND
    #: every examined row cleared the bar — i.e. the qualifying supply provably
    #: extends below the cut. ``False`` covers both "the pool ran out inside
    #: the window" and the degenerate empty scan.
    scan_limit_binding: bool = False
    #: Dropped before any keeper work: junk endpoint or a canon self-loop.
    skipped_endpoints: int = 0
    #: Excluded by the merge-aware guard — an open nexus already covers the
    #: KEEPER-RESOLVED pair in one direction or the other. The guard itself now
    #: runs INSIDE :data:`QUALIFICATION_SCAN_SQL` (2026-09-23), so these rows
    #: never reach ``examined``; the count comes from the companion
    #: :data:`ALREADY_REIFIED_COUNT_SQL` query, not a Python loop over survivors.
    already_reified: int = 0
    #: Dropped because the keeper rewrite collapsed both endpoints onto one
    #: entity (two surfaces of the same actor — never a relationship).
    keeper_self_loop: int = 0
    #: Survived every drop. May exceed ``selected`` — the run cap is the last
    #: thing applied, so this is the true depth of live work available.
    eligible: int = 0
    #: Handed to the typer (``min(eligible, limit)``).
    selected: int = 0

    def as_dict(self) -> dict[str, Any]:
        # Preserve field types — scan_limit_binding is a bool and must
        # serialize as jsonb `true`, not the integer 1 an int() map makes.
        return dict(asdict(self))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


#: H9 — the rejection write-back. ``status`` is untouched (the governance pass owns
#: it); ``reviewed_at`` is the stamp :data:`QUALIFICATION_SCAN_SQL` reads against the
#: cooldown. Only still-pending rows are stamped, so a row governance promoted or
#: aged out between the scan and this write is left as governance left it.
MARK_REJECTED_SQL = """
UPDATE proposed_edges
   SET reviewed_at = now()
 WHERE id = ANY($1::uuid[])
   AND status = 'pending'
RETURNING id
"""


async def mark_rejected_candidates(conn: Any, ids: Sequence[Any]) -> int:
    """Stamp ``reviewed_at`` on the candidates the typer rejected; returns how many rows took the stamp."""
    if not ids:
        return 0
    rows = await conn.fetch(MARK_REJECTED_SQL, [str(i) for i in ids])
    return len(rows)


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
    reject_cooldown_days: int = REJECT_COOLDOWN_DAYS,
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
        int(reject_cooldown_days),
    )
    counters.examined = len(scan)

    # 2026-09-23 — the already-reified guard now runs INSIDE the scan above
    # (QUALIFICATION_SCAN_SQL's NOT EXISTS), so an excluded row never reaches
    # `scan` and can't be counted by len()/loop arithmetic here. This companion
    # query asks the SAME question, un-negated, over the same qualifying pool.
    already_reified_n = await conn.fetchval(
        ALREADY_REIFIED_COUNT_SQL,
        str(status), float(min_confidence), int(min_sources),
        int(reject_cooldown_days),
    )
    counters.already_reified = int(already_reified_n or 0)

    # The scan is already ordered best-first and already carries the hard source
    # floor, so the bar is a prefix cut — everything below it is below it.
    scored = [r for r in scan if float(r["qual_score"] or 0.0) >= float(bar)]
    counters.qualified = len(scored)
    # V3/P5 — name the DRAIN state on the receipt. The LIMIT binds only when
    # the scan actually RETURNED its limit's worth (examined == examine);
    # qualified == examined alone is ambiguous when the pool ran out early.
    counters.scan_limit_binding = (
        counters.examined == examine
        and counters.qualified == counters.examined
    )
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

    # The already-reified guard already ran (QUALIFICATION_SCAN_SQL, above) —
    # every survivor here cleared it. Attach the keeper-resolved endpoints the
    # write path needs and let the cap apply last, so ``eligible`` is the true
    # depth of live work available (a receipt that stopped counting mid-window
    # would hide exactly the collapse this module exists to make visible).
    out: list[dict[str, Any]] = []
    for cand, (k_source, k_target) in resolved:
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
    "ALREADY_REIFIED_COUNT_SQL",
    "CANDIDATE_FETCH_SQL",
    "ALREADY_REIFIED_SQL",
    "SelectionCounters",
    "resolve_pair",
    "already_reified",
    "select_candidates",
]
