# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``bearing_edges`` writer (migration 0107) — dated, typed "NEW evidence
bears on OLD claim" pointers.

The ``claim_watch`` deterministic analyst (signal ↔ open-question matcher)
INSERTs its own edges inline (a batched, per-run write over many pairs). This
module is the SAME table's writer for the R-1 corpus_researcher backlog
answer-link: exactly ONE edge per run, ``finding -> question``
(``src_kind='finding'``, ``dst_kind='hypothesis'``), stamped when a run
resolves the model's ``addressed_question`` tag against the GROUND-phase
standing-question sink (see ``legba.data.analysts.inline_target.run_method``).

Producer contract (mirrors ``provenance.consumption.record_output_consumption``
— data-driven, no kind switch in the runtime):

  * A kind that resolved a backlog answer-link stamps
    ``FindingPayload.data['addressed_question'] = {"hypothesis_id": ...,
    "produced_at": ..., "harvest_class": ...}`` (inline_target's REFLECT
    phase).
  * The actor host calls :func:`record_bearing_edge` on the SAME
    connection/flow as ``write_analyst_output``, right after the output row
    lands, with the new row's id + produced_at as the ``src`` side.

APPEND-ONLY, NEVER-CLOSE: unlike ``review_flags`` (closed by supersession),
a ``bearing_edges`` row has no close/delete path at all — it is a permanent,
dated pointer. This writer NEVER mutates the destination question's row
(status, content) in any way; it only ever INSERTs a new edge.

DEGRADE-NOT-BREAK: :func:`record_bearing_edge` never raises — a write
failure logs and returns ``False``; the finding/output row it sidecars is
durable regardless (the same discipline ``record_output_consumption`` uses).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Sequence
from uuid import UUID

logger = logging.getLogger(__name__)

#: Edge kind this writer stamps by default (the standing arbiter vocabulary —
#: see migration 0107's ``bearing_edges`` docstring).
DEFAULT_EDGE_KIND = "bears_on"

#: The provenance class every producer writer stamps — a LIVE production run,
#: never a curated/gold example (``exemplar`` is reserved for the K-4 gold
#: loop and is never written by a runtime matcher).
DEFAULT_PROVENANCE_CLASS = "live"

_INSERT_SQL = """
INSERT INTO bearing_edges
    (edge_kind, src_kind, src_id, src_as_of, dst_kind, dst_id, dst_as_of,
     weight, planes, provenance_class, matcher_version)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::text[], $10, $11)
ON CONFLICT (src_id, dst_id, edge_kind) DO NOTHING
"""


async def record_bearing_edge(
    conn,  # type: ignore[no-untyped-def] — asyncpg.Connection (pool-acquired)
    *,
    src_kind: str,
    src_id: UUID,
    src_as_of: datetime,
    dst_kind: str,
    dst_id: UUID,
    dst_as_of: datetime,
    weight: float,
    planes: Sequence[str],
    matcher_version: str,
    edge_kind: str = DEFAULT_EDGE_KIND,
    provenance_class: str = DEFAULT_PROVENANCE_CLASS,
) -> bool:
    """INSERT one ``bearing_edges`` row; idempotent via ON CONFLICT DO NOTHING.

    Every column the 0107 schema requires NOT NULL is a required keyword
    here too (no silent None-slips-through-as-NULL path) EXCEPT ``planes``,
    which this function itself refuses to write empty (mirroring the
    schema's own ``bearing_edges_planes_nonempty`` CHECK) — an edge with no
    plane is not an edge, so an empty/missing ``planes`` short-circuits to a
    logged no-write rather than letting the DB reject it.

    Returns ``True`` iff a NEW row was inserted, ``False`` on a dedup'd
    re-match, an empty ``planes``, OR any write failure — the caller does
    not need to distinguish those cases, only whether to trust a fresh edge
    now exists. NEVER raises: the finding/output row this sidecars is
    load-bearing and must never fail on this pointer write (degrade-not-
    break, mirroring ``consumption.record_output_consumption``).
    """
    if not planes:
        logger.warning(
            "bearing_edges.write_skipped_no_planes src_kind=%s src_id=%s "
            "dst_kind=%s dst_id=%s — an edge with no contributing plane is "
            "not an edge (mirrors the schema's own CHECK)",
            src_kind, src_id, dst_kind, dst_id,
        )
        return False
    try:
        tag = await conn.execute(
            _INSERT_SQL,
            edge_kind,
            src_kind,
            src_id,
            src_as_of,
            dst_kind,
            dst_id,
            dst_as_of,
            float(weight),
            list(planes),
            provenance_class,
            matcher_version,
        )
        # asyncpg's execute() returns a command tag string, e.g. "INSERT 0 1"
        # (1 row) or "INSERT 0 0" (ON CONFLICT DO NOTHING deduped it).
        return isinstance(tag, str) and tag.strip().endswith(" 1")
    except Exception:  # noqa: BLE001 — intentional degrade-not-break sidecar
        logger.warning(
            "bearing_edges.write_failed src_kind=%s src_id=%s dst_kind=%s "
            "dst_id=%s (non-fatal: the output row is durable; the bearing "
            "pointer misses this run)",
            src_kind, src_id, dst_kind, dst_id,
            exc_info=True,
        )
        return False


__all__ = [
    "DEFAULT_EDGE_KIND",
    "DEFAULT_PROVENANCE_CLASS",
    "record_bearing_edge",
]
