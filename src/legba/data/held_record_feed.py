# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What feeds the held family: the desk-row
shadow records, and nothing else, behind a switch that is OFF by default.

``LEGBA_HELD_RECORD_SHADOW`` — a comma list of analyst ids (unset / empty =
OFF, the shipped default; the intended first value is ``energy_security``,
the one desk whose shadow runs). With it on, the runtime's loop
(``runtime/held_record_loop.py``) reads the named desks' WRITTEN shadow
records that have no held-record receipt yet, oldest first, and writes each in
its own transaction (:func:`held_record_store.write_shadow_record`). A record
that fails is logged with its class, counted, and retried on the next pass;
nothing is raised into the runtime.

WHY HERE AND NOT IN THE SHADOW ITSELF: the shadow (``desk_rows_shadow``) is a
measurement that writes one ledger row and nothing else (its isolation test
holds that); the held family READS that ledger afterwards, so the desk run,
its finding and its shadow are untouched whether this is on or off. The
prompts of the seats that produce the rows are not yet reviewed, so this feeds the
tables ONLY, and nothing a reader sees reads them (the two
``GET /v3/record/assertions`` routes and the verify script are the readers).

This module is one of the two places allowed to name the shadow ledger
(``tests/data_pkg/test_desk_rows_shadow_isolation.py``), with the replay
script.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["FEED_ENV", "FeedReport", "PENDING_SQL", "feed_analysts", "feed_enabled", "feed_pending", "row_as_shadow"]

FEED_ENV = "LEGBA_HELD_RECORD_SHADOW"

#: The named desks' written shadow records with no held-record receipt (the
#: live feed writes no batch), oldest first. Indexed: the ledger's
#: (analyst_id, occurred_at) index and the receipts' unique index.
PENDING_SQL = """
SELECT s.id, s.run_id, s.analyst_id, s.target_id, s.occurred_at, s.contract_version, s.gate_version, s.record
  FROM desk_rows_shadow s
 WHERE s.analyst_id = ANY($1::text[]) AND s.status = 'written'
   AND NOT EXISTS (SELECT 1 FROM assertion_shadow_receipts r
                    WHERE r.shadow_id = s.id AND coalesce(r.batch, '') = '')
 ORDER BY s.occurred_at, s.id
 LIMIT $2
"""


def feed_analysts() -> frozenset[str]:
    """The ONE read site for :data:`FEED_ENV`."""
    raw = os.environ.get(FEED_ENV, "")
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def feed_enabled() -> bool:
    return bool(feed_analysts())


@dataclass
class FeedReport:
    read: int = 0
    written: int = 0
    already: int = 0
    errors: int = 0
    counts: Counter = field(default_factory=Counter)


def row_as_shadow(row: Mapping[str, Any]) -> dict[str, Any]:
    """A ledger row as the planner reads it (``record`` decoded)."""
    import json

    rec = row["record"]
    if isinstance(rec, str):
        rec = json.loads(rec)
    return {**dict(row), "record": rec}


async def feed_pending(pool: Any, *, analysts: frozenset[str] | None = None, limit: int = 20) -> FeedReport:
    """One pass: read the pending records, write each in its own transaction."""
    from .held_record_store import write_shadow_record

    analysts = feed_analysts() if analysts is None else analysts
    rep = FeedReport()
    if not analysts:
        return rep
    async with pool.acquire() as conn:
        rows = await conn.fetch(PENDING_SQL, sorted(analysts), int(limit))
    rep.read = len(rows)
    for row in rows:
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    res = await write_shadow_record(conn, row_as_shadow(row))
        except Exception as exc:  # noqa: BLE001 — counted and retried next pass, never raised
            rep.errors += 1
            logger.warning("held_record.feed_error shadow_id=%s err=%s", row["id"], type(exc).__name__)
            continue
        if res.written:
            rep.written += 1
            for k, v in res.counts.items():
                if isinstance(v, int):
                    rep.counts[k] += v
        else:
            rep.already += 1
    return rep
