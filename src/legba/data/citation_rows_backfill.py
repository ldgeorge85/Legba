# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citations as rows: derive the rows for findings written before the dual write.

The rows come from each finding's STORED fields only (``body`` and
``data->'data'->'citations'``), by the same planner the live writer uses
(``citation_rows.plan_citation_rows``) and the same writer
(``citation_rows_store.write_citation_rows``) with ``origin = 'backfill'``
and a ``backfill_batch`` id on every row, the writer named as
``rule:citation_rows_backfill``. What the run was HANDED is not recovered (``handed_recorded = false``): the render index was never stored,
and the trace that approximates it is pruned after 45 days.

* :func:`dry_run` — read-only: how many findings lack rows, and a plan over a
  bounded sample of them (sentences, support links by kind, unresolved by
  reason, foreign markers), extrapolated to the whole set.
* :func:`apply_batches` — bounded batches (``batch_size`` findings, keyset on
  ``(produced_at, id)``), each batch one transaction, resumable from the
  printed cursor, idempotent (a finding with a receipt row is skipped).
* :func:`rollback_batch` — deletes every row of one batch (the insert-only
  trigger admits a DELETE only of rows carrying the batch named in
  ``legba.citation_rows_rollback``).
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .citation_rows import plan_citation_rows, target_specs
from .citation_rows_store import write_citation_rows
from .principals import rule_handle

__all__ = ["BACKFILL_WRITER", "BackfillCursor", "apply_batches", "dry_run", "rollback_batch"]

#: Who writes a backfill row this rule, never the finding's seat
#: (the seat wrote the finding; the backfill derived the rows from it later).
BACKFILL_WRITER = rule_handle("citation_rows_backfill")

_WINDOW_WHERE = """
  o.kind = 'finding'
  AND ($1::timestamptz IS NULL OR o.produced_at >= $1)
  AND ($2::timestamptz IS NULL OR o.produced_at < $2)
"""
_NOT_WRITTEN = "\n  AND NOT EXISTS (SELECT 1 FROM finding_citation_receipts r WHERE r.finding_id = o.id)"


def _count_sql(has_rows: bool) -> str:
    return f"SELECT count(*) FROM analyst_outputs o WHERE {_WINDOW_WHERE}{_NOT_WRITTEN if has_rows else ''}"


def _page_sql(has_rows: bool) -> str:
    return f"""
SELECT o.id, o.analyst_id, o.target_id, o.run_id, o.produced_at, o.body,
       o.data->'data'->'citations' AS citations
  FROM analyst_outputs o
 WHERE {_WINDOW_WHERE}{_NOT_WRITTEN if has_rows else ''}
   AND ($3::timestamptz IS NULL OR (o.produced_at, o.id) > ($3, $4::uuid))
 ORDER BY o.produced_at, o.id
 LIMIT $5
"""


_HAS_ROWS_SQL = "SELECT to_regclass('public.finding_citation_receipts') IS NOT NULL"

#: Delete order: children first (citation_support / citation_unresolved name
#: a sentence), the receipt last (its absence is what re-admits a finding).
_ROLLBACK_TABLES = ("citation_support", "citation_unresolved", "run_handed",
                    "finding_sentences", "finding_citation_receipts")


@dataclass
class BackfillCursor:
    produced_at: datetime | None = None
    finding_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"produced_at": self.produced_at.isoformat() if self.produced_at else None,
                "finding_id": self.finding_id}


@dataclass
class _Tally:
    findings: int = 0
    sentences: int = 0
    cited_sentences: int = 0
    markers: int = 0
    foreign_markers: int = 0
    supports: Counter = field(default_factory=Counter)
    roles: Counter = field(default_factory=Counter)
    unresolved: Counter = field(default_factory=Counter)
    sentence_chars: int = 0

    def add(self, plan: Any) -> None:
        self.findings += 1
        self.sentences += len(plan.sentences)
        self.cited_sentences += plan.cited_sentences
        self.markers += plan.markers
        self.foreign_markers += plan.foreign_markers
        self.sentence_chars += sum(len(s.text) for s in plan.sentences)
        for s in plan.supports:
            self.supports[s.target.kind] += 1
            self.roles[s.role] += 1
        for u in plan.unresolved:
            self.unresolved[u.reason] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "findings": self.findings, "sentences": self.sentences,
            "cited_sentences": self.cited_sentences, "markers": self.markers,
            "foreign_markers": self.foreign_markers,
            "supports": sum(self.supports.values()), "supports_by_kind": dict(self.supports),
            "supports_by_role": dict(self.roles), "unresolved_by_reason": dict(self.unresolved),
            "sentence_text_bytes": self.sentence_chars,
        }


def _citations(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [e for e in raw if isinstance(e, Mapping)] if isinstance(raw, list) else []


async def _page(conn: Any, since: datetime | None, until: datetime | None,
                cursor: BackfillCursor, limit: int, has_rows: bool = True) -> list[Mapping[str, Any]]:
    return list(await conn.fetch(_page_sql(has_rows), since, until, cursor.produced_at,
                                 cursor.finding_id, limit))


async def dry_run(conn: Any, *, since: datetime | None = None, until: datetime | None = None,
                  max_findings: int = 2000, page: int = 200) -> dict[str, Any]:
    """Read-only: the pending count and a plan over the first ``max_findings``
    pending findings (oldest first), extrapolated to the pending count. Runs
    before the migration too (then every finding is pending)."""
    has_rows = bool(await conn.fetchval(_HAS_ROWS_SQL))
    pending = int(await conn.fetchval(_count_sql(has_rows), since, until))
    specs = target_specs()
    tally = _Tally()
    cursor = BackfillCursor()
    while tally.findings < max_findings:
        rows = await _page(conn, since, until, cursor, min(page, max_findings - tally.findings), has_rows)
        if not rows:
            break
        for r in rows:
            tally.add(plan_citation_rows(r["body"], _citations(r["citations"]), specs=specs))
        cursor = BackfillCursor(rows[-1]["produced_at"], str(rows[-1]["id"]))
    sample = tally.as_dict()
    factor = pending / tally.findings if tally.findings else 0.0
    estimate = {k: round(v * factor) for k, v in sample.items() if isinstance(v, int)}
    return {"tables_present": has_rows, "pending_findings": pending, "sampled": tally.findings, "sample": sample,
            "estimate_all_pending": estimate, "next_cursor": cursor.as_dict()}


async def apply_batches(conn: Any, *, batch_id: str, since: datetime | None = None,
                        until: datetime | None = None, batch_size: int = 200,
                        max_batches: int = 1, cursor: BackfillCursor | None = None) -> dict[str, Any]:
    """Write up to ``max_batches`` batches of ``batch_size`` findings, each
    batch in its own transaction. Returns the tally and the resume cursor."""
    if not batch_id or len(batch_id) > 64:
        raise ValueError("batch_id is required (at most 64 characters)")
    specs = target_specs()
    cursor = cursor or BackfillCursor()
    written = skipped = batches = 0
    for _ in range(max(0, int(max_batches))):
        rows = await _page(conn, since, until, cursor, batch_size)
        if not rows:
            break
        async with conn.transaction():
            for r in rows:
                out = await write_citation_rows(
                    conn, finding_id=r["id"], body=r["body"], citations=_citations(r["citations"]),
                    produced_at=r["produced_at"], run_id=r["run_id"], analyst_id=r["analyst_id"],
                    target_id=r["target_id"], handed=None, origin="backfill", batch=batch_id,
                    written_by=BACKFILL_WRITER, specs=specs)
                if out is None:
                    skipped += 1
                else:
                    written += 1
        batches += 1
        cursor = BackfillCursor(rows[-1]["produced_at"], str(rows[-1]["id"]))
    return {"batch_id": batch_id, "batches": batches, "findings_written": written,
            "findings_skipped": skipped, "next_cursor": cursor.as_dict()}


async def rollback_batch(conn: Any, batch_id: str) -> dict[str, int]:
    """Delete every row of ``batch_id`` (backfill rows only), in one transaction."""
    if not batch_id:
        raise ValueError("batch_id is required")
    out: dict[str, int] = {}
    async with conn.transaction():
        await conn.execute("SELECT set_config('legba.citation_rows_rollback', $1, true)", batch_id)
        for table in _ROLLBACK_TABLES:
            status = await conn.execute(
                f"DELETE FROM {table} WHERE origin = 'backfill' AND backfill_batch = $1", batch_id)
            out[table] = int(str(status).rsplit(" ", 1)[-1])
    return out
