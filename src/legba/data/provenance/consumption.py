# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Forward-consumption index writer (KW-1, migration 0106).

``output_consumption`` materializes FORWARD lineage edges at consumption
time: "consumer output C read row F in role <context>". The backward
question (what did C read?) already lives on ``derived_from[]``; this
sidecar is the inverted index for the forward question (WHO consumed F?)
— the substrate for the review-flag plane (migration 0107) and a later
behavior-identical fast path under the F-1 freshness-advisory BFS.

Producer contract (data-driven, no kind switch in the runtime):

  * A kind that wants its consumption materialized stamps
    ``AnalystMethodResult.consumed_edges`` — a list of
    ``(consumed_id, context)`` pairs — at ITS OWN consumption points
    (the composition orient/periphery split in
    ``meta_findings_synthesizer._run``; the journal's rendered slice
    selection in ``journal_assessor.run_method``).
  * The actor host calls :func:`record_output_consumption` on the SAME
    connection/flow as ``write_analyst_output``, right after the output
    row lands, with the new row's id as ``consumer_id``.

DEGRADE-NOT-BREAK: :func:`record_output_consumption` never raises — a
consumption-write failure logs and returns 0; the compose/journal write it
sidecars is load-bearing and must never fail on this index.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Sequence
from uuid import UUID

logger = logging.getLogger(__name__)

# The role a consumed row played for its consumer. Open vocabulary (TEXT
# column, no CHECK) — these are the three roles the current producers stamp.
CONSUMPTION_CONTEXT_BASIS = "composition_basis"
"""A load-bearing (verified, above-floor) input head of a composition."""

CONSUMPTION_CONTEXT_PERIPHERY = "composition_periphery"
"""A below-floor/unverified periphery row of a C-TIER tiered composition."""

CONSUMPTION_CONTEXT_JOURNAL = "journal_slice"
"""A row of the journal's rendered priming slice (post-selection)."""

_INSERT_SQL = """
INSERT INTO output_consumption
    (consumer_id, consumed_id, consumed_at, consumer_kind, context)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (consumer_id, consumed_id, context) DO NOTHING
"""


async def record_output_consumption(
    conn,  # type: ignore[no-untyped-def] — asyncpg.Connection (pool-acquired)
    *,
    consumer_id: UUID,
    consumer_kind: str,
    edges: Sequence[tuple[UUID, str]] | Iterable[tuple[UUID, str]],
    consumed_at: datetime | None = None,
) -> int:
    """Materialize forward-consumption edges for one consumer output.

    ``edges`` is the ``(consumed_id, context)`` list the producing kind
    stamped on its result (``AnalystMethodResult.consumed_edges``).
    Duplicate pairs within the batch fold via ON CONFLICT DO NOTHING (the
    (consumer, consumed, context) PK), so a row that is both cited twice
    or re-recorded lands once.

    Returns the number of edges ATTEMPTED (0 on failure or empty input).
    NEVER raises: the consumption index is a sidecar — the output write it
    accompanies is load-bearing and a failure here must degrade, not break
    (log + continue), mirroring the composition-fold discipline.
    """
    ts = consumed_at or datetime.now(timezone.utc)
    rows = [
        (consumer_id, consumed_id, ts, consumer_kind, context)
        for consumed_id, context in edges
    ]
    if not rows:
        return 0
    try:
        await conn.executemany(_INSERT_SQL, rows)
        return len(rows)
    except Exception:  # noqa: BLE001 — intentional degrade-not-break sidecar
        logger.warning(
            "output_consumption.write_failed consumer_id=%s kind=%s edges=%d "
            "(non-fatal: the output row is durable; the forward index misses "
            "this run)",
            consumer_id,
            consumer_kind,
            len(rows),
            exc_info=True,
        )
        return 0


__all__ = [
    "CONSUMPTION_CONTEXT_BASIS",
    "CONSUMPTION_CONTEXT_JOURNAL",
    "CONSUMPTION_CONTEXT_PERIPHERY",
    "record_output_consumption",
]
