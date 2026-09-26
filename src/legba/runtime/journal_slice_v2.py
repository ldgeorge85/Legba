# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2.3 — the journal's SECOND FETCH LEG: salience-aware stratified sampling.

THE DEFECT THIS EXISTS FOR (JOURNAL_CLUSTER_FIRST_SLICE_REPORT §0/§7).
:func:`legba.runtime.actor_substrate_slice._read_substrate_slice` fetches with
``ORDER BY fetched_at DESC LIMIT max(200, LEGBA_SLICE_ROW_CAP * 3)`` = 360 rows.
For a geo-scoped desk that is a generous over-fetch of a small window. For the
journal — a META analyst with no source, geo or predicate narrowing — the 24 h
window holds ~3,570 rows, so 360 newest-first rows are the last **~3 hours**.
Measured 2026-09-09: the day's #3 cluster (Israel-UK settlements, 77 rows across
22 sources) put **1** row in the fetch; the top-10 clusters delivered 18 of their
423 rows (4.3%). Cluster-first selection (T2.2) can only group what was fetched,
so this leg — not the selector — is the dominant aperture bound.

THE SECOND LEG. Same window, same clauses, same 360-row budget, different
SELECTION: four strata, deduped in priority order, then INTERLEAVED so that any
prefix of the delivered list holds each stratum in proportion to its BUDGET. The
prefix matters because the caller cuts 360 -> 120 with ``_diversify_by_source``
and then ``journal_slice._select_journal_slice`` cuts 120 -> 60; a leg that
emitted its strata in blocks would have block A alone fill the 120 and the
freshness reserve would never reach the narrator.

The interleave key is ``(slot_within_stratum - 0.5) / stratum_BUDGET`` — the
budget, deliberately, not the stratum's realised size. A stratum that UNDERFILLS
its budget then spends its reserved share of the prefix early: on a quiet day
where three rows clear the high bar, those three lead the list instead of being
spread thin across 360 positions and cut away by the caller's 120. Normalising by
realised size looks fairer and is not: it gives the day's one genuinely loud row
a 1-in-360 position.

  1. **A — the high bar.** ``magnitude >= 0.70``, at most ``per_source_cap`` (15)
     rows per source, at most ``row_cap`` (120) rows, ordered
     ``(magnitude, authority) DESC``. 0.70 selects 357/3,570 rows on the 09-09
     window and 349/3,454 on 09-08 — a stable ~10% "this is an event" bar. The
     per-source guard is load-bearing: ``source.telegram.org_channels`` alone
     holds 131 of those 357, and an unguarded A would be 37% one firehose.
  2. **B — per-source top-K (diversity).** Each ``source_id``'s top
     :data:`SOURCE_TOP_K` = 2 rows by the same key, taken in ROUND-ROBIN rank
     order (every source's #1 before any source's #2), at most ``row_cap``.
     97 sources x 1 = 97 <= 120, so every source in the window is represented
     before any source gets a second slot. This is the stratum that recovers a
     22-source cluster: for each of those 22 mastheads the settlement story is
     its own top-magnitude row of the day.
  3. **C — the freshness reserve.** The newest
     ``journal_slice._JOURNAL_RENDER_CAP`` (60) rows by ``fetched_at DESC``.
     Under leg 1 freshness was free (the whole fetch WAS the newest 3 h); under
     leg 2 it must be bought explicitly or ``_select_journal_slice``'s
     fresh-unscored floor (12 tail slots drawn from ``inputs``) has nothing to
     stand on. 60 x (120/360) = 20 >= 12, so the floor still has candidates
     after the diversity cut.
  4. **D — the remainder** by ``(magnitude, authority) DESC``, carrying the same
     ``per_source_cap`` guard as A, filling to the 360 budget (>= 60 rows: A+B+C
     cap at 120+120+60 = 300 < 360 for every ``LEGBA_SLICE_ROW_CAP``). The guard
     is not a taste: the CALLER caps at ``_global_slice_per_source_cap()`` rows
     per source when it cuts 360 -> 120, so a 16th row from any one source is
     provably discarded — budget spent on a row that cannot arrive. Unguarded, D
     put **99 of the 360** rows on ``source.telegram.org_channels`` (its 116
     high-magnitude rows that stratum A's guard had turned away) and the top-10
     clusters' delivered rows fell from 125 to 89.

  5. **The backfill** — everything else by ``(magnitude, authority) DESC``,
     UNGUARDED, appended at the tail only when strata 1-4 could not fill the
     budget. D's per-source guard bounds strata 1-4 at
     ``distinct_sources x per_source_cap`` rows, which is 97 x 15 = 1,455 on the
     live pool and never binds — but on a THIN day (a handful of sources) it
     would, and a leg that returned fewer rows than the recency cut would be a
     regression dressed as a feature. This is the same degrade-not-drop the
     caller's own ``_diversify_by_source`` makes ("back-fills if diversity is
     exhausted, so a thin single-source day is never smaller than the plain
     recency cut"). It sorts strictly LAST, so on a normal day it is empty and
     invisible.

Live 09-09 shape after dedup: A 120 · B 120 · C 50 · D 70 · backfill 0 = 360 —
the SAME row count leg 1 fetched, so every downstream cost (diversity cap,
render, prompt tokens, judge calls) is unchanged.

WHAT THE BAR ACTUALLY DOES, stated rather than implied. 357 of the 09-09 pool's
3,570 rows clear 0.70 and stratum A's cap is 120, so on a normal day A is
"the top ``row_cap`` rows by (magnitude, authority), <= 15 per source" and the
bar never binds. It binds on a QUIET day — it is the FLOOR that stops A padding
itself with magnitude-0.2 routine when there is no event to carry, handing those
slots to the diversity and freshness strata instead. Moving it to 0.6 or 0.8
changes nothing on either measured window; that is a fact about the bar, not a
reason to trust it less.

SCOPE. Two gates, both required: the ``LEGBA_JOURNAL_SLICE_V2`` env flag
(default OFF) and ``descriptor.identity.kind == 'journal_assessor'``. The kind
gate covers exactly the journal family (journal_assessor, chronicle_assessor,
journal_consolidator and the five lens_* tiers — one voice, four render tiers)
and NO other analyst: every desk, every composition, every deterministic sweep
keeps the recency leg byte for byte, flag on or off.

ONE STATEMENT. The strata are CTEs over a single window scan; there is no N+1
and no second round trip. :func:`journal_slice_v2_sql` returns the statement
text so the caller can hand it to the same ``conn.fetch(sql, *params)`` call the
recency leg uses, with the SAME ``{where}`` and the SAME ``$n`` params — which is
why the flag-off path can keep its literal untouched.
"""

from __future__ import annotations

import os
from typing import Any

#: The env flag. Absent/false ⇒ the journal reads the recency leg, unchanged.
JOURNAL_SLICE_V2_ENV = "LEGBA_JOURNAL_SLICE_V2"

#: The analyst KIND this leg is scoped to (``descriptor.identity.kind``).
JOURNAL_KIND = "journal_assessor"

#: Stratum A's magnitude bar. See the module docstring for why 0.70.
HIGH_BAR = 0.70

#: Stratum B's per-source depth.
SOURCE_TOP_K = 2

#: ``salience->>'authority'`` -> the same ranks
#: :data:`legba.data.analysts.signal_salience.AUTHORITY_RANK` uses, as a SQL
#: CASE so the ordering inside Postgres matches the Python consumption key.
_AUTHORITY_CASE = (
    "CASE salience->>'authority' "
    "WHEN 'official' THEN 4 "
    "WHEN 'reporting' THEN 3 "
    "WHEN 'analysis' THEN 2 "
    "WHEN 'state_media' THEN 1 "
    "ELSE 0 END"
)


def _fresh_reserve() -> int:
    """Stratum C's size — the narrator window, not a second literal.

    Deferred import: ``legba.data.analysts.journal_slice`` is a data-package
    module and this is runtime; importing it at module scope would put a
    runtime->data edge on every actor import for one integer.
    """
    from ..data.analysts.journal_slice import _JOURNAL_RENDER_CAP

    return _JOURNAL_RENDER_CAP


def journal_slice_v2_enabled() -> bool:
    """True when ``LEGBA_JOURNAL_SLICE_V2`` is set to a truthy value."""
    raw = os.getenv(JOURNAL_SLICE_V2_ENV)
    if not raw:
        return False
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def journal_slice_v2_applies(descriptor: Any) -> bool:
    """Both gates: the flag AND a journal-family descriptor.

    Deliberately tolerant of a descriptor that is not an ``AnalystDescriptor``
    (a test double, a partially-built object): anything without a readable
    ``identity.kind`` is NOT the journal, so it takes the recency leg.
    """
    if not journal_slice_v2_enabled():
        return False
    identity = getattr(descriptor, "identity", None)
    return getattr(identity, "kind", None) == JOURNAL_KIND


def journal_slice_v2_sql(
    *,
    columns: str,
    where: str,
    total: int,
    row_cap: int,
    per_source_cap: int,
    fresh_cap: int | None = None,
    high_bar: float = HIGH_BAR,
    source_top_k: int = SOURCE_TOP_K,
) -> str:
    """The stratified statement. Pure — no I/O, no env read.

    ``where`` and ``columns`` are the caller's own, verbatim, so this leg reads
    EXACTLY the rows the recency leg would have been choosing from (same window,
    same backfill/canonical/research clauses, same ``$n`` params) and selects
    among them differently. ``total`` is the recency leg's ``fetch_limit``.
    """
    fresh = max(1, _fresh_reserve() if fresh_cap is None else fresh_cap)
    high_cap = max(1, row_cap)
    src_cap = max(1, row_cap)
    return f"""
        WITH pool AS (
            SELECT {columns},
                   COALESCE((salience->>'magnitude')::float8, -1.0) AS _v2_mag,
                   {_AUTHORITY_CASE} AS _v2_auth
            FROM signals
            {where}
        ), ranked AS (
            SELECT *,
                   row_number() OVER (
                       ORDER BY _v2_mag DESC, _v2_auth DESC, fetched_at DESC, id
                   ) AS _v2_rn_mag,
                   row_number() OVER (
                       PARTITION BY source_id
                       ORDER BY _v2_mag DESC, _v2_auth DESC, fetched_at DESC, id
                   ) AS _v2_rn_src,
                   row_number() OVER (
                       ORDER BY fetched_at DESC, id
                   ) AS _v2_rn_fresh
            FROM pool
        ), strata AS (
            SELECT id, 1 AS _v2_stratum,
                   row_number() OVER (
                       ORDER BY _v2_mag DESC, _v2_auth DESC, fetched_at DESC, id
                   ) AS _v2_rn
            FROM ranked
            WHERE _v2_mag >= {high_bar} AND _v2_rn_src <= {per_source_cap}
          UNION ALL
            SELECT id, 2,
                   row_number() OVER (
                       ORDER BY _v2_rn_src, _v2_mag DESC, _v2_auth DESC,
                                fetched_at DESC, id
                   )
            FROM ranked
            WHERE _v2_rn_src <= {source_top_k}
          UNION ALL
            SELECT id, 3, _v2_rn_fresh FROM ranked WHERE _v2_rn_fresh <= {fresh}
          UNION ALL
            SELECT id, 4, _v2_rn_mag FROM ranked WHERE _v2_rn_src <= {per_source_cap}
          UNION ALL
            SELECT id, 5, _v2_rn_mag FROM ranked
        ), owned AS (
            SELECT DISTINCT ON (id) id, _v2_stratum, _v2_rn
            FROM strata ORDER BY id, _v2_stratum, _v2_rn
        ), slotted AS (
            SELECT id, _v2_stratum,
                   row_number() OVER (
                       PARTITION BY _v2_stratum ORDER BY _v2_rn, id
                   ) AS _v2_slot
            FROM owned
        ), capped AS (
            SELECT * FROM slotted
            WHERE (_v2_stratum = 1 AND _v2_slot <= {high_cap})
               OR (_v2_stratum = 2 AND _v2_slot <= {src_cap})
               OR (_v2_stratum = 3 AND _v2_slot <= {fresh})
               OR (_v2_stratum >= 4)
        ), budgeted AS (
            SELECT *,
                   GREATEST(
                       {total} - count(*) FILTER (WHERE _v2_stratum < 4) OVER (),
                       0
                   ) AS _v2_room4,
                   count(*) FILTER (WHERE _v2_stratum = 4) OVER () AS _v2_have4
            FROM capped
        ), kept AS (
            SELECT id, _v2_stratum, _v2_slot, _v2_room4 FROM budgeted
            WHERE _v2_stratum < 4
               OR (_v2_stratum = 4 AND _v2_slot <= _v2_room4)
               OR (_v2_stratum = 5
                   AND _v2_slot <= GREATEST(_v2_room4 - LEAST(_v2_have4, _v2_room4), 0))
        ), sized AS (
            SELECT k.id AS _v2_id, k._v2_stratum, k._v2_slot,
                   CASE k._v2_stratum
                       WHEN 1 THEN {high_cap}
                       WHEN 2 THEN {src_cap}
                       WHEN 3 THEN {fresh}
                       WHEN 4 THEN GREATEST(k._v2_room4, 1)
                       ELSE GREATEST(count(*) OVER (PARTITION BY k._v2_stratum), 1)
                   END AS _v2_n
            FROM kept k
        )
        SELECT {columns}
        FROM ranked r JOIN sized s ON s._v2_id = r.id
        ORDER BY (CASE WHEN s._v2_stratum = 5 THEN 1 ELSE 0 END) ASC,
                 (s._v2_slot - 0.5) / s._v2_n ASC,
                 s._v2_stratum ASC, s._v2_slot ASC
        LIMIT {total}
        """


def journal_v2_sql_for(
    descriptor: Any,
    *,
    columns: str,
    where: str,
    total: int,
    row_cap: int,
    per_source_cap: int,
) -> str | None:
    """``journal_slice_v2_sql`` when BOTH gates pass, else ``None``.

    The single call site in ``_read_substrate_slice`` reads this and falls
    through to its own untouched literal on ``None`` — so at flag-off (and for
    every non-journal analyst at flag-on) the statement that reaches Postgres is
    the pre-change text, character for character.
    """
    if not journal_slice_v2_applies(descriptor):
        return None
    return journal_slice_v2_sql(
        columns=columns, where=where, total=total,
        row_cap=row_cap, per_source_cap=per_source_cap,
    )


__all__ = [
    "HIGH_BAR",
    "JOURNAL_KIND",
    "JOURNAL_SLICE_V2_ENV",
    "SOURCE_TOP_K",
    "journal_slice_v2_applies",
    "journal_slice_v2_enabled",
    "journal_slice_v2_sql",
    "journal_v2_sql_for",
]
