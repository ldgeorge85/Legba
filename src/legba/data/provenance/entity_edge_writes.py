# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G1 — the `entity_edges` DUAL-WRITE (migration 0143).

Every producer that writes a `nexuses` row also writes the id-keyed
`entity_edges` row, in the SAME transaction, from the same call. There is
exactly one ``INSERT INTO nexuses`` in the codebase (``_insert_nexus`` in
``writes.py``), so there is exactly one place to mirror — which is why this is a
choke point and not a six-way patch. `relationship_reifier`,
`proposed_edge_governance`, all four seed adapters, `manual_batch`,
`graph_mining`'s proxy chains and the `seed_import` CLI all funnel through it.

**Nexus writes are UNCHANGED.** Readers are still on `nexuses` for the whole of
this tranche; this module only adds a parallel row. Nothing here may alter what
the legacy write does or whether it succeeds — except by failing the whole
transaction, which is the point of being in it (see ATOMICITY below).

ATOMICITY. Both writes are in the same Postgres, so a single transaction is
free. The house precedent "facts-first, graph-second; a graph hiccup must never
fail the facts write" is correct ACROSS stores and is the anti-pattern WITHIN
one: a swallowed exception here is exactly how a store diverges silently. So a
genuine database error on the edge write rolls the nexus write back and raises.

UNRESOLVABLE ENDPOINTS ARE NOT AN ERROR. They are a measurement. ~2.75 % of open
nexus rows name something that resolves to no entity, or to more than one; that
is a property of the data, not a fault in the write. Such a write PARKS the pair
in `entity_edges_unresolved` with a reason, counts it, and lets the nexus write
proceed untouched. Nothing is guessed: `resolve_entity_name()` returns NULL for
an ambiguous name rather than picking the profile with more mentions, because
picking one manufactures an edge nobody asserted.

The `edge_family` map is derived from the producer, and it is exhaustive over
every producer combination present in the live substrate.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Sequence
from uuid import UUID

logger = logging.getLogger(__name__)

#: The canonical co-occurrence predicate AFTER ``_canonical_rel_type`` folds the
#: producer's surface (``CoOccursWith``, ``co-occurs-with``, ...) onto one form.
CO_OCCURRENCE_REL_TYPE = "co occurs with"

#: Producers whose rows are the imported reference lattice rather than anything
#: Legba derived. Matched on the ``seed.`` analyst_id prefix as well, because
#: the ACLED adapter stamps ``source_type='backfill'`` while still being a seed.
_REFERENCE_SOURCE_TYPES = frozenset({"seed", "manual"})

#: Outcome tags. Exactly one is returned per attempted edge write.
WRITTEN = "written"
SRC_UNRESOLVED = "src_unresolved"
DST_UNRESOLVED = "dst_unresolved"
AMBIGUOUS = "ambiguous"
SELF_EDGE = "self_edge"

_PARK_REASONS = (SRC_UNRESOLVED, DST_UNRESOLVED, AMBIGUOUS)

_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def edge_family_for(source_type: str | None, analyst_id: str | None,
                    rel_type: str) -> str:
    """Map a nexus producer onto its `edge_family` tier (migration 0143).

    The split exists because 86 % of the open SIGNED edge set is imported
    Wikidata country->IGO membership at polarity +1: filing the seed lattice as
    ``relation`` is what has structural balance reporting a ratio about UN
    co-membership rather than about world-state alignment. Verified exhaustive
    against every (analyst_id, source_type, rel_type) combination in the live
    substrate — seven of them, all covered.

    ``structural`` IS NOT REACHABLE FROM HERE, AND THAT IS WHY THE TIER IS
    EMPTY. W3-A checked, because a family with zero rows usually means a filter
    swallowed something: it does not. This function returns only ``reference``,
    ``cooccurrence`` or ``relation``, and the three backfills (0144/0145/0180)
    never emit ``structural`` either — so the tier has NO PRODUCER anywhere in
    the codebase. It exists in 0143's CHECK constraint and in
    ``vocabulary_entries`` because it was RESERVED, in 0143's own words, "for a
    later fold of the bearing/echo edge class". That fold has not been written.

    ``bearing_edges`` holds 18,184 rows waiting for it (``review_flags``, the
    other candidate, holds 0). Projecting them is not a one-line change: it
    needs a migration that decides what a bearing edge's ENDPOINTS are (the
    table is not entity-keyed), what its polarity means, and whether echo
    strength maps onto ``confidence`` or ``observed_count``. Left for the train
    that owns the bearing plane. Every reader on the W3-A cutover already
    INCLUDES ``structural`` in its default family list, so those edges light up
    the moment a producer writes them.
    """
    st = (source_type or "").strip().lower()
    aid = (analyst_id or "").strip().lower()
    if st in _REFERENCE_SOURCE_TYPES or aid.startswith("seed."):
        return "reference"
    if (rel_type or "").strip().lower() == CO_OCCURRENCE_REL_TYPE:
        return "cooccurrence"
    return "relation"


@dataclass
class EdgeWriteCounters:
    """Process-local tally of dual-write outcomes, for logs and tests.

    The DURABLE record is `entity_edges_unresolved` (parked rows survive the
    process and are countable in SQL); this is the convenience surface for a
    receipt or a log line, not the source of truth.

    V3/P5: ``written`` is now split — ``inserted`` minted a new row while
    ``folded`` re-observed an existing open one (the ON CONFLICT DO UPDATE
    branch), which is the ~15-20 % fold the spec's §5.2.5 measured but the
    receipt threw away. ``inserted + folded == written`` always.
    """

    written: int = 0
    inserted: int = 0
    folded: int = 0
    parked: int = 0
    self_edges: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def record(self, outcome: str, *, inserted: bool | None = None) -> None:
        """Tally one outcome; ``inserted`` splits a ``written`` write."""
        if outcome == WRITTEN:
            self.written += 1
            if inserted is True:
                self.inserted += 1
            elif inserted is False:
                self.folded += 1
        elif outcome == SELF_EDGE:
            self.self_edges += 1
        else:
            self.parked += 1
            self.by_reason[outcome] = self.by_reason.get(outcome, 0) + 1

    def to_data(self) -> dict[str, Any]:
        return {
            "entity_edges_written": self.written,
            "entity_edges_inserted": self.inserted,
            "entity_edges_folded": self.folded,
            "endpoint_unresolved": self.parked,
            "entity_edges_self_skipped": self.self_edges,
            "endpoint_unresolved_by_reason": dict(self.by_reason),
        }

    def reset(self) -> None:
        self.written = 0
        self.inserted = 0
        self.folded = 0
        self.parked = 0
        self.self_edges = 0
        self.by_reason.clear()


#: V3/P5 — a task-local outcome sink. ``COUNTERS`` is process-wide, so a run
#: receipt cannot subtract a clean delta out of it without inheriting every
#: OTHER nexus producer interleaved in this process (the reifier, governance,
#: seed adapters and the importer all share the runtime). A ContextVar is the
#: same mechanism ``run_accounting`` binds its per-run account with: it is
#: visible to this task and any task it spawns, and invisible to everything
#: else — exactly "this run's writes".
_EDGE_WRITE_SINK: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "legba_entity_edge_write_sink", default=None
)


@contextmanager
def bind_edge_write_sink() -> Iterator[list[dict[str, Any]]]:
    """Collect every edge-write outcome attempted under this context.

    Yields a list each entry is ``{"outcome": <tag>, "inserted": bool|None}``
    appended to — ``inserted`` is set only on ``written`` outcomes (True = new
    row, False = folded into an open one). Callers that do not bind see no
    sink and no overhead.
    """
    sink: list[dict[str, Any]] = []
    token = _EDGE_WRITE_SINK.set(sink)
    try:
        yield sink
    finally:
        _EDGE_WRITE_SINK.reset(token)


def _emit_outcome(outcome: str, *, inserted: bool | None = None) -> None:
    """Record one outcome to the process counters and any bound run sink."""
    COUNTERS.record(outcome, inserted=inserted)
    sink = _EDGE_WRITE_SINK.get()
    if sink is not None:
        sink.append({"outcome": outcome, "inserted": inserted})


#: Module-level tally. Reset by tests; read by anything that wants a cheap
#: "is the dual-write actually writing" gauge without a SQL round trip.
COUNTERS = EdgeWriteCounters()


_LEDGER_ENV = "LEGBA_EDGE_TRANSITION_LEDGER"


def edge_transition_ledger_enabled() -> bool:
    """Honor ``LEGBA_EDGE_TRANSITION_LEDGER`` (default OFF) — the V3/P3
    append-only transition ledger gate (migration 0206, spec §3.5).

    The ONE read site (the ``LEGBA_EVENTS`` / ``events_enabled`` pattern):
    every ``entity_edge_events`` write routes through
    :func:`_write_edge_transitions`, which is only reached under this flag,
    so the flag's off state is structural — an unflagged write cannot land a
    ledger row, and the edge write itself is byte-identical with the flag
    off. Only "1"/"true"/"yes"/"on" enable it.
    """
    raw = os.environ.get(_LEDGER_ENV, "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


_RESOLVE_SQL = """
WITH slots(slot, txt) AS (
    VALUES ('src', $1::text), ('dst', $2::text), ('via', $3::text)
)
SELECT s.slot,
       count(DISTINCT public.resolve_entity(ep.id))              AS n,
       (array_agg(DISTINCT public.resolve_entity(ep.id)))[1]     AS rid
  FROM slots s
  LEFT JOIN public.entity_profiles ep
         ON lower(ep.canonical_name) = lower(btrim(s.txt))
 WHERE s.txt IS NOT NULL AND btrim(s.txt) <> ''
 GROUP BY s.slot
"""


async def _resolve_endpoints(
    conn: Any, subject: str, object_: str, intermediary: str | None
) -> dict[str, tuple[int, UUID | None]]:
    """Resolve all three endpoint names in ONE round trip.

    Returns ``{slot: (match_count, terminal_id)}``. ``match_count`` is the
    number of DISTINCT terminal ids the lowered name reaches: 0 = unknown,
    1 = resolved, >1 = ambiguous. Tombstones are matched deliberately — an edge
    naming a merged loser must land on its keeper, not be lost — and then chased
    to the terminal survivor by ``resolve_entity()`` (0086, cycle-safe).
    """
    rows = await conn.fetch(_RESOLVE_SQL, subject, object_, intermediary)
    return {r["slot"]: (int(r["n"] or 0), r["rid"]) for r in rows}


async def _park(
    conn: Any, *, subject: str, object_: str, edge_type: str,
    edge_family: str, reason: str, origin_table: str,
    origin_id: UUID | None, payload: dict[str, Any],
) -> None:
    """Record an endpoint we could not resolve. Never a drop, never a guess.

    Dual-write parks carry ``origin_id=None`` and dedupe on the name triple:
    the nexus upsert may pass a row id that never lands (its ON CONFLICT branch
    keeps the existing row), so keying the park on that id would inflate the
    residue by one row per retry of the same unresolvable pair.
    """
    await conn.execute(
        """
        INSERT INTO entity_edges_unresolved
            (src_text, dst_text, edge_type, edge_family, reason,
             origin_table, origin_id, payload)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        ON CONFLICT (origin_table, lower(src_text), lower(dst_text),
                     lower(edge_type))
              WHERE origin_id IS NULL
        DO UPDATE SET reason     = EXCLUDED.reason,
                      payload    = EXCLUDED.payload,
                      created_at = now()
        """,
        subject[:2048], object_[:2048], edge_type, edge_family, reason,
        origin_table, origin_id, json.dumps(payload, default=str),
    )


async def close_prior_entity_edges(
    conn: Any, *, src_id: UUID, dst_id: UUID, intermediary_id: UUID | None,
    edge_type: str, polarity: int,
) -> list[UUID]:
    """Close open edges for this triple whose POLARITY differs from the incoming
    one, mirroring :func:`writes.supersede_prior_nexuses`. Returns the closed ids
    so the caller can point them at the new row once it exists.

    The two tables share a "what holds now" contract keyed on the same triple, so
    they must agree on when a re-assert is a supersession. A same-polarity
    re-assert closes nothing — that path belongs to the upsert, which lifts
    confidence, unions evidence and increments ``observed_count``.

    THREE STATEMENTS, NOT ONE, and the split is forced by the schema. The nexus
    version stamps ``superseded_by`` in the same UPDATE because `nexuses` has no
    foreign key on that column; `entity_edges` does, so the pointer cannot be set
    before its target row exists. But the target row cannot be inserted while the
    prior row is still open, because they share the partial unique index. So:
    close (freeing the index), insert, then link.
    """
    rows = await conn.fetch(
        """
        UPDATE entity_edges
           SET valid_until = now(),
               updated_at  = now()
         WHERE src_id    = $1
           AND dst_id    = $2
           AND COALESCE(intermediary_id, $6::uuid) = COALESCE($3, $6::uuid)
           AND lower(edge_type) = lower($4)
           AND polarity  <> $5
           AND valid_until IS NULL
           AND superseded_by IS NULL
        RETURNING id
        """,
        src_id, dst_id, intermediary_id, edge_type, int(polarity), _NIL_UUID,
    )
    return [r["id"] for r in rows]


_INSERT_SQL = """
INSERT INTO entity_edges (
    id, src_id, dst_id, intermediary_id, edge_type, edge_family, polarity,
    intent, channel, confidence, valid_from, valid_until,
    first_seen_at, last_seen_at,
    source_signal_ids, derived_from, evidence_set,
    source_type, seed_batch_id, analyst_id, analyst_version, run_id,
    target_id, target_version, produced_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7,
    $8, $9, $10, $11, $12,
    $13, $13,
    $14, $15, $16::jsonb,
    $17, $18, $19, $20, $21,
    $22, $23, $24
)
ON CONFLICT (src_id, dst_id, edge_type,
             COALESCE(intermediary_id, '00000000-0000-0000-0000-000000000000'::uuid))
      WHERE valid_until IS NULL AND superseded_by IS NULL
DO UPDATE SET
    confidence     = GREATEST(entity_edges.confidence, EXCLUDED.confidence),
    -- Decay becomes EVIDENTIAL: an edge ages because nobody reported it again,
    -- not because its row is old. Every re-observation is a sighting.
    observed_count = entity_edges.observed_count + 1,
    last_seen_at   = GREATEST(entity_edges.last_seen_at, EXCLUDED.last_seen_at),
    -- array_agg over zero rows is NULL, which would violate the NOT NULL when
    -- both sides are empty (same trap the nexus upsert documents).
    source_signal_ids = COALESCE((SELECT array_agg(DISTINCT e)
                         FROM unnest(entity_edges.source_signal_ids
                                     || EXCLUDED.source_signal_ids) e),
                        '{}'::uuid[]),
    derived_from      = COALESCE((SELECT array_agg(DISTINCT e)
                         FROM unnest(entity_edges.derived_from
                                     || EXCLUDED.derived_from) e),
                        '{}'::uuid[]),
    evidence_set   = COALESCE(EXCLUDED.evidence_set, entity_edges.evidence_set),
    -- Carry a NEWLY-supplied creation-time TTL, never clobber an existing one
    -- to NULL (the conflict target is open rows only, so a supersession close
    -- is invisible here).
    valid_until    = COALESCE(EXCLUDED.valid_until, entity_edges.valid_until),
    updated_at     = now()
-- (xmax = 0) is true exactly on the INSERT branch of this upsert — the
-- fold-vs-insert split the V3/P5 receipt counts (the situation_clustering
-- idiom, same engine guarantees).
RETURNING id, (xmax = 0) AS inserted
"""


async def write_entity_edge_for_nexus(
    conn: Any,
    *,
    edge_id: UUID,
    subject: str,
    object_: str,
    intermediary: str | None,
    rel_type: str,
    polarity: int,
    intent: str,
    channel: str,
    confidence: float,
    valid_from: datetime | None,
    valid_until: datetime | None,
    produced_at: datetime,
    source_signal_ids: Sequence[UUID],
    derived_from: Sequence[UUID],
    data: dict[str, Any] | None,
    source_type: str,
    seed_batch_id: UUID | None,
    analyst_id: str | None,
    analyst_version: str | None,
    run_id: UUID | None,
    target_id: str | None,
    target_version: str | None,
    nexus_id: UUID | None = None,
) -> str:
    """Mirror one nexus write into `entity_edges`. Returns an outcome tag.

    Called from ``_insert_nexus`` with the values it already computed, so the
    two rows cannot disagree about the triple, the canonical rel_type or the
    lineage. Raises on a genuine database error — the caller's transaction rolls
    the nexus write back with it.
    """
    edge_family = edge_family_for(source_type, analyst_id, rel_type)
    slots = await _resolve_endpoints(conn, subject, object_, intermediary)
    src_n, src_id = slots.get("src", (0, None))
    dst_n, dst_id = slots.get("dst", (0, None))
    via_n, via_id = slots.get("via", (0, None))

    outcome: str | None = None
    if src_n > 1 or dst_n > 1:
        outcome = AMBIGUOUS
    elif src_n == 0 or src_id is None:
        outcome = SRC_UNRESOLVED
    elif dst_n == 0 or dst_id is None:
        outcome = DST_UNRESOLVED

    if outcome in _PARK_REASONS:
        await _park(
            conn, subject=subject, object_=object_, edge_type=rel_type,
            edge_family=edge_family, reason=outcome, origin_table="nexuses",
            origin_id=None,
            payload={
                "nexus_id": str(nexus_id) if nexus_id else None,
                "analyst_id": analyst_id,
                "source_type": source_type,
                "src_matches": src_n,
                "dst_matches": dst_n,
            },
        )
        _emit_outcome(outcome)
        logger.info(
            "entity_edges.endpoint_unresolved reason=%s family=%s subject=%r "
            "object=%r analyst=%s", outcome, edge_family, subject[:120],
            object_[:120], analyst_id)
        return outcome

    if src_id == dst_id:
        # Both names resolved to the SAME entity — a merge has since made this
        # triple a self-reference. Not an error and not a park: the nexus row is
        # the historical assertion, and an entity is not related to itself.
        _emit_outcome(SELF_EDGE)
        return SELF_EDGE

    # An intermediary that does not resolve degrades to NULL rather than
    # sinking the edge: "A relates to B" survives losing "via C".
    resolved_via = via_id if via_n == 1 else None

    closed = await close_prior_entity_edges(
        conn, src_id=src_id, dst_id=dst_id, intermediary_id=resolved_via,
        edge_type=rel_type, polarity=polarity)

    evidence = _evidence_set(data)
    row = await conn.fetchrow(
        _INSERT_SQL,
        edge_id, src_id, dst_id, resolved_via, rel_type, edge_family,
        int(polarity), intent or "", channel or "direct", float(confidence),
        valid_from, valid_until, produced_at,
        list(source_signal_ids), list(derived_from),
        json.dumps(evidence, default=str) if evidence is not None else None,
        source_type, seed_batch_id, analyst_id, analyst_version, run_id,
        target_id, target_version, produced_at,
    )
    landed = row["id"] if row is not None else None
    edge_inserted = bool(row["inserted"]) if row is not None else None

    if closed and landed is not None:
        # Link the rows we just closed to the row that replaced them. Read the
        # landed id back rather than assuming `edge_id`: on the ON CONFLICT
        # branch the row that survives is the pre-existing one, and pointing at
        # an id that never landed would violate the foreign key.
        await conn.execute(
            "UPDATE entity_edges SET superseded_by = $1, updated_at = now() "
            " WHERE id = ANY($2::uuid[]) AND id <> $1",
            landed, closed)

    if edge_transition_ledger_enabled():
        # V3/P3 — the transition ledger (0206), in THIS transaction: a ledger
        # row and the transition it records commit or roll back together.
        # With the flag off this block is unreachable and the write path is
        # byte-identical to before the ledger existed.
        await _write_edge_transitions(
            conn,
            closed_ids=closed,
            landed_id=landed,
            inserted=edge_inserted,
            occurred_at=produced_at,
            polarity=int(polarity),
            edge_type=rel_type,
            derived_from=list(derived_from),
            source_signal_ids=list(source_signal_ids),
            analyst_id=analyst_id,
            analyst_version=analyst_version,
            run_id=run_id,
        )

    _emit_outcome(WRITTEN, inserted=edge_inserted)
    return WRITTEN


async def _write_edge_transitions(
    conn: Any,
    *,
    closed_ids: list[UUID],
    landed_id: UUID | None,
    inserted: bool | None,
    occurred_at: datetime,
    polarity: int,
    edge_type: str,
    derived_from: list[UUID],
    source_signal_ids: list[UUID],
    analyst_id: str | None,
    analyst_version: str | None,
    run_id: UUID | None,
) -> None:
    """Append `entity_edge_events` rows for what this write TRANSITIONED.

    Transitions only, never observations (spec §3.5): a same-polarity
    re-assert that folds into the open row writes NOTHING here — the fold is
    already summarised by ``observed_count``/``last_seen_at``, and a row per
    re-observation is the ~2,900/week of noise the spec priced. What lands:

    * ``observed`` on the landed row when this write MINTED an edge (the
      ``xmax = 0`` branch) — its first observation. ``derived_from`` may be
      empty; the CHECK allows that for 'observed' alone.
    * ``polarity_flip`` on each edge ``close_prior_entity_edges`` just
      closed — the incoming re-assert contradicted their sign. The replacing
      edge id leads ``derived_from`` so the row always carries its evidence.
    """
    evidence = list(dict.fromkeys(
        [str(u) for u in derived_from] + [str(u) for u in source_signal_ids]))

    if inserted and landed_id is not None:
        await conn.execute(
            """
            INSERT INTO entity_edge_events
                (edge_id, occurred_at, transition,
                 polarity_to, edge_type_to, why, derived_from,
                 analyst_id, analyst_version, run_id)
            VALUES ($1, $2, 'observed', $3, $4, $5, $6::uuid[],
                    $7, $8, $9)
            """,
            landed_id, occurred_at, polarity, edge_type,
            f"edge asserted by {analyst_id or 'unknown'} ({edge_type})",
            evidence, analyst_id, analyst_version, run_id,
        )

    if closed_ids:
        # The closed rows carry their own pre-flip polarity/edge_type —
        # read them back rather than assuming they matched the incoming
        # triple's shape (a retyped edge is closed by a different caller,
        # but polarity mismatches close here under the incoming type too).
        old = await conn.fetch(
            "SELECT id, polarity, edge_type FROM entity_edges "
            " WHERE id = ANY($1::uuid[])",
            closed_ids)
        for r in old:
            transition_evidence = list(dict.fromkeys(
                ([str(landed_id)] if landed_id is not None else [])
                + evidence))
            await conn.execute(
                """
                INSERT INTO entity_edge_events
                    (edge_id, occurred_at, transition,
                     polarity_from, polarity_to,
                     edge_type_from, edge_type_to, why, derived_from,
                     analyst_id, analyst_version, run_id)
                VALUES ($1, $2, 'polarity_flip', $3, $4, $5, $6, $7, $8::uuid[],
                        $9, $10, $11)
                """,
                r["id"], occurred_at,
                r["polarity"], polarity,
                r["edge_type"], edge_type,
                f"re-asserted with polarity {polarity} "
                f"(was {r['polarity']}) by {analyst_id or 'unknown'}",
                transition_evidence, analyst_id, analyst_version, run_id,
            )


def _evidence_set(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Lift the citable parts of a nexus `data` blob onto the edge.

    Only the fields that make an edge auditable travel: the free-text evidence a
    promoted candidate carried, and the id of the proposal it came from. The
    rest of `data` stays on the nexus row — this is a projection, not a copy.
    """
    if not data:
        return None
    keep = {k: data[k] for k in ("evidence_text", "promoted_from_proposed_edge")
            if data.get(k)}
    return keep or None
