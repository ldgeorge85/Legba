# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The provenance census's one read — what a consult answer's CITED ids are.

Split out of ``substrate_query_port.py`` (W4b F-9) because that module sits at
its size ceiling and this read needed two more behaviours: the ``events`` fold
and the bound. The port keeps a thin ``classify_cited_refs`` that delegates
here with the same signature; nothing here reaches back into the port.

What the read answers: consult cites bare substrate uuids, and "how much of this
answer rests on live reporting, how much on a curated holding, how much on a
web retrieval" is a question the ids alone cannot answer. It is answered from
the rows' OWN ``origin_class`` column — never from which tool returned them,
which is a guess dressed as provenance.

* ``by_origin_class`` — the class-bearing tables: ``signals``,
  ``observations`` and (W4b F-9, decided 2026-10-04) ``events``. An event row
  carries an ``origin_class`` exactly as a signal does and is cited exactly as
  often as one is, so it counts the same way. Before F-9 a cited event landed
  in ``by_kind`` and ``unresolved`` and no class bucket.
* ``by_kind`` — what every resolved id IS (signal, observation, event, finding,
  situation, hypothesis, nexus), so a kind is named rather than counted as a
  miss.
* ``fabricated`` — a well-formed uuid that matches **no** table. The single
  most important number on this object.
* ``malformed`` — a citation token that is not a uuid at all (``sit-001``),
  bounded and de-duplicated.
* ``unresolved`` — an id that matched a real row whose ``origin_class`` this
  census does not read (a finding, prediction, situation, hypothesis, nexus,
  event lifecycle row, event edge, entity, situation ledger row).
* ``unclassified_over_cap`` — refs past :data:`CITED_REFS_CLASSIFY_CAP`. The
  read is bounded so a run citing hundreds of refs cannot issue an unbounded
  ``IN (...)``; the refs past the cap are COUNTED here and never silently
  dropped, and they are never filed as ``fabricated`` (they were not looked up).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

__all__ = ["CITED_REFS_CLASSIFY_CAP", "classify_cited_refs"]

#: The most distinct, well-formed cited ids one classification reads. Refs are
#: taken in citation order; the rest are reported as ``unclassified_over_cap``.
CITED_REFS_CLASSIFY_CAP = 400

#: Bounds on the MALFORMED citation tokens reported back (2026-09-28 §B.3).
#: Model-authored text riding a response the panel renders is bounded at the
#: seam that produces it. Mirrored in ``data.analysts.consult_reply_parsing``
#: rather than imported, because this module is on the runtime side.
_MALFORMED_REF_MAX_LEN = 40
_MALFORMED_REF_MAX_ITEMS = 20

#: Tables whose rows carry an ``origin_class`` this census reads.
_CLASS_TABLES: tuple[tuple[str, str], ...] = (
    ("signals", "signal"),
    ("observations", "observation"),
    ("events", "event"),
)

#: The tables a consult citation can legitimately name beyond the class-bearing
#: ones. Every one is a substrate row a consult tool RETURNS, so a citation
#: landing on one is the planner doing what it was asked. Each entry is
#: ``(table, kind label)``; ``analyst_outputs`` has no label because the row
#: names its own (``finding`` / ``composition`` / ...).
CITED_REF_TABLES: tuple[tuple[str, str | None], ...] = (
    ("analyst_outputs", None),
    ("situations", "situation"),
    ("hypotheses", "hypothesis"),
    ("nexuses", "nexus"),
    # The event drill tool (``substrate_frame_reads`` get_event) hands out the
    # ids of an event's lifecycle ledger rows, its edges and its actors as
    # citable refs. Without these a consult that cites one was counted
    # "fabricated" (W11-F155). ``situation_events`` is the situation ledger.
    ("event_lifecycle_events", "event_lifecycle"),
    ("event_edges", "event_edge"),
    ("entity_profiles", "entity"),
    ("situation_events", "situation_event"),
)


async def classify_cited_refs(pool: Any, *, refs: list[str]) -> dict[str, Any]:
    """What a consult answer's CITED ids actually are — by class and by kind."""
    wanted: list[UUID] = []
    seen: set[str] = set()
    malformed: list[str] = []
    malformed_seen: set[str] = set()
    over_cap = 0
    for raw in refs or ():
        text = str(raw).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        try:
            ref = UUID(text)
        except (AttributeError, TypeError, ValueError):
            # NEVER a silent drop. A non-uuid in citation position is a
            # fabrication with a shape of its own and the census reports it.
            clipped = text[:_MALFORMED_REF_MAX_LEN]
            if (
                clipped not in malformed_seen
                and len(malformed) < _MALFORMED_REF_MAX_ITEMS
            ):
                malformed_seen.add(clipped)
                malformed.append(clipped)
            continue
        if len(wanted) < CITED_REFS_CLASSIFY_CAP:
            wanted.append(ref)
        else:
            over_cap += 1
    by_class: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    matched: set[UUID] = set()
    resolved = 0
    unresolved = 0
    if wanted:
        async with pool.acquire() as conn:
            for table, kind in _CLASS_TABLES:
                rows = await conn.fetch(
                    f"SELECT id, origin_class FROM {table} "
                    "WHERE id = ANY($1::uuid[])",
                    wanted,
                )
                for r in rows:
                    klass = str(r["origin_class"] or "unknown")
                    by_class[klass] = by_class.get(klass, 0) + 1
                    by_kind[kind] = by_kind.get(kind, 0) + 1
                    matched.add(r["id"])
                    resolved += 1
            for table, kind in CITED_REF_TABLES:
                column = "kind" if kind is None else "NULL AS kind"
                rows = await conn.fetch(
                    f"SELECT id, {column} FROM {table} "
                    "WHERE id = ANY($1::uuid[])",
                    wanted,
                )
                for r in rows:
                    label = kind or str(r["kind"] or table)
                    by_kind[label] = by_kind.get(label, 0) + 1
                    if r["id"] not in matched:
                        matched.add(r["id"])
                        # Matched a real row, but not one this census reads an
                        # origin class off. That is what "unresolved" means.
                        unresolved += 1
    return {
        "by_origin_class": by_class,
        "by_kind": by_kind,
        "resolved": resolved,
        "unresolved": unresolved,
        # Every cited id that landed on a REAL row of any table — the count the
        # evidence floor and the provenance hold key on (2026-09-28, roll-28
        # proof). ``resolved`` keeps its narrower, class-bearing meaning.
        "matched": len(matched),
        "fabricated": max(0, len(wanted) - len(matched)),
        "malformed": malformed,
        # ids actually looked up; ``unclassified_over_cap`` are the ones not.
        "asked": len(wanted),
        "unclassified_over_cap": over_cap,
    }
