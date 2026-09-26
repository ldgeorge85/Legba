# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ORIGIN-CLASS axis — where a substrate row came from, in one closed
vocabulary, and the reader firewall that keeps history out of "now"
(DATA MODEL V3 / P7, migration 0209).

``retrieval_origin`` (migration 0112, :mod:`legba.data.retrieval_origin`)
already says *how a signal reached us*. ``origin_class`` is the coarser
question a reader actually asks: **is this row part of the live present, or
is it imported history?** The open-row gate —
``superseded_by IS NULL AND valid_until IS NULL`` — cannot answer it: a
backfilled 2016 fact with an open ``valid_until`` is indistinguishable from
a fact minted this hour, and it would silently feed freshness, source
health, calibration, salience and the alert plane. The firewall is the
class, not the window, and it had to exist before the first historical row.

THE VOCABULARY — closed, a CHECK constraint, not an enum type:

``live``
    Written by the live ingestion/analysis lanes. The default.
``web_retrieval``
    Arrived off the open web (``retrieval_origin`` ``web_search:*`` /
    ``web_evidence``). Live by definition — it was retrieved *now* — but
    labelled so a reader can discount it.
``seed``
    Stamped by the curated seed-batch machinery (``seed_batches``).
``backfill_native`` · ``backfill_reconstructed`` · ``archive``
    The three HISTORY classes. Program 7g-1 finished the READER sweep
    (SEAMS #57 resolved): every open-row read and supersession site on
    ``facts``/``events``, and every reader on the eight surfaces a
    collection is fenced from, renders its predicate from this module,
    and the reactive trigger plane gates the delivered row. They are
    still refused at the table, by the renamed
    ``*_origin_class_history_writer_not_built`` CHECKs (migration 0220,
    SEAMS #62) — not because a reader would misread one, but because
    nothing may WRITE one to these three tables yet. Collection SERIES
    land in ``observations``, which carries its own history-only CHECK.

``LIVE_CLASSES = {live, web_retrieval, seed}`` is what every reader sees
today. It is rendered in canonical :data:`ORIGIN_CLASSES` order so the SQL
is byte-stable.

Every reader takes its gate string from this module and nowhere else.
``tests/data_pkg/test_origin_gate_inventory.py`` pins each
``superseded_by IS NULL`` site in the tree into one of three buckets —
swept, an ``ON CONFLICT`` index predicate that cannot carry the leg
without ceasing to match its partial index, or a table with no
``origin_class`` column — so a new site fails loud in whichever way it
is wrong.

A LEAF: stdlib plus ``legba.data.retrieval_origin`` only — no runtime, no
I/O, loadable in the slim image.
"""

from __future__ import annotations

from typing import Iterable

from ..retrieval_origin import WEB_EVIDENCE_RESOLUTION, is_web_retrieved

#: Deploy marker — the rolling deploy greps for this exact string.
ORIGIN_CLASS_VERSION = "2026-09/p7"

#: The closed six-class vocabulary, in canonical order (the CHECK
#: constraint's IN-list and the SQL renderers below both follow it).
ORIGIN_CLASSES: tuple[str, ...] = (
    "live",
    "web_retrieval",
    "seed",
    "backfill_native",
    "backfill_reconstructed",
    "archive",
)

#: The classes every reader sees today — the live present plus the two
#: non-live arrivals that are still of-the-moment (a web retrieval happened
#: now; a seed row is operator-vetted present-tense knowledge).
LIVE_CLASSES: frozenset[str] = frozenset({"live", "web_retrieval", "seed"})

#: The live classes as one SQL literal, in canonical order — the third leg
#: of every live-gate rendering.
_LIVE_SQL_LIST: str = ",".join(
    f"'{c}'" for c in ORIGIN_CLASSES if c in LIVE_CLASSES
)

__all__ = [
    "LIVE_CLASSES",
    "ORIGIN_CLASSES",
    "ORIGIN_CLASS_VERSION",
    "OriginClassError",
    "as_of_gate_sql",
    "live_gate_sql",
    "origin_class_clause",
    "origin_class_for",
]


class OriginClassError(ValueError):
    """An ``origin_class`` value outside the closed vocabulary was asked for.

    Raised rather than silently returning zero rows: a mistyped class in an
    ``include_origin`` parameter must not read as "the substrate is empty".
    """


def origin_class_for(retrieval_origin: str | None) -> str:
    """Derive the ``origin_class`` a signal write should stamp.

    ``web_search:<provider>`` and ``web_evidence`` (bare or provider-suffixed)
    map to ``web_retrieval``; everything else — ``None``, ``curated_source``,
    any unrecognised label — is ``live``, mirroring the curated-default rule
    in :mod:`legba.data.retrieval_origin`. The history classes are never
    derived here: nothing that runs live writes them, which is the point of
    the firewall.
    """
    if is_web_retrieved(retrieval_origin):
        return "web_retrieval"
    if isinstance(retrieval_origin, str):
        label = retrieval_origin.strip()
        if (
            label == WEB_EVIDENCE_RESOLUTION
            or label.startswith(f"{WEB_EVIDENCE_RESOLUTION}:")
        ):
            return "web_retrieval"
    return "live"


def live_gate_sql(alias: str) -> str:
    """The open-row predicate for an origin-classed table — ONE rendering.

    ``<a>.superseded_by IS NULL AND <a>.valid_until IS NULL AND
    <a>.origin_class IN ('live','web_retrieval','seed')`` — the migration-0032
    open-row pair plus the origin-class leg (P7). Call it only on tables that
    carry the columns (``facts`` and ``events``; ``signals`` has no
    ``superseded_by``/``valid_until`` and takes the class leg alone if a read
    ever needs it).
    """
    col = f"{alias}." if alias else ""
    return (
        f"{col}superseded_by IS NULL AND {col}valid_until IS NULL"
        f" AND {col}origin_class IN ({_LIVE_SQL_LIST})"
    )


def as_of_gate_sql(alias: str, param_index: int) -> str:
    """The canonical validity-time as-of predicate bound to ``$<param_index>``.

    ``COALESCE(<a>.valid_from, '-infinity'::timestamptz) <= $n AND
    (<a>.valid_until IS NULL OR <a>.valid_until > $n)`` — the ONE rendering;
    P3 wrote it out inline six more times and every site now reads from here.
    ``superseded_by`` is deliberately absent: ``valid_until`` alone carries
    the close (spec §3.2's second rule). The caller appends the parsed
    instant at exactly ``param_index``.
    """
    col = f"{alias}." if alias else ""
    return (
        f"COALESCE({col}valid_from, '-infinity'::timestamptz) <= ${param_index}"
        f" AND ({col}valid_until IS NULL OR {col}valid_until > ${param_index})"
    )


def origin_class_clause(
    alias: str, classes: Iterable[str] | None = None
) -> str:
    """A ``<a>.origin_class IN (...)`` WHERE fragment over validated classes.

    ``classes=None`` renders :data:`LIVE_CLASSES`; a supplied iterable is
    validated against :data:`ORIGIN_CLASSES` — ANY unknown member raises
    :class:`OriginClassError` rather than silently dropping it (a typo'd
    class must not read as "the substrate has no such rows"), and an empty
    selection raises too. Classes render in canonical order so the SQL is
    byte-stable.
    """
    if classes is None:
        selected = [c for c in ORIGIN_CLASSES if c in LIVE_CLASSES]
    else:
        asked = frozenset(classes)
        unknown = sorted(asked - frozenset(ORIGIN_CLASSES))
        if unknown:
            raise OriginClassError(
                f"include_origin: unknown origin_class {unknown} — the "
                f"closed vocabulary is {list(ORIGIN_CLASSES)}"
            )
        selected = [c for c in ORIGIN_CLASSES if c in asked]
        if not selected:
            raise OriginClassError(
                "include_origin: no origin_class selected — the closed "
                f"vocabulary is {list(ORIGIN_CLASSES)}"
            )
    col = f"{alias}." if alias else ""
    listed = ",".join(f"'{c}'" for c in selected)
    return f"{col}origin_class IN ({listed})"
