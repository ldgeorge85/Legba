# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Journal corrections — append-only withdrawal of published journal rows.

A ``correction`` is a ``journal_entries`` row of its own (``entry_kind =
'correction'``) that cites the ids it corrects, with a reason per id in
``data.corrects``. Each corrected row is then CLOSED the way a superseded
consolidation is (:func:`legba.data.provenance.writes.supersede_prior_consolidation`):
``valid_until = now()`` + ``superseded_by = <correction id>``, plus
``data.corrected_by`` so a reader can show the link. Nothing is deleted and no
body is touched: the stream (``valid_until IS NULL``) and every memory reader
(:data:`legba.data.journal_evidence.MEMORY_ROW_FILTER_SQL`) stop serving the
row, while ``GET /journal/{id}`` still returns it, with its "corrected by" link.

Idempotent by construction: a correction is keyed by ``data.correction_key``
(re-running with the same key reuses the existing correction row), and the
close only touches a row that is still open.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from ..journal_evidence import CORRECTION_ENTRY_KIND

#: analyst_id stamped on a correction row (an operator act, not an analyst run).
CORRECTION_AUTHOR = "journal_correction"

_FIND_SQL = (
    "SELECT id FROM journal_entries WHERE entry_kind = $1 "
    "AND data->>'correction_key' = $2 ORDER BY produced_at LIMIT 1"
)

_INSERT_SQL = """
INSERT INTO journal_entries (
    id, entry_kind, title, body, claims, cited_substrate_refs,
    period_start, period_end, honesty_flags, superseded_by,
    analyst_id, analyst_version, produced_at, derived_from, run_id, data
) VALUES ($1, $2, $3, $4, '[]'::jsonb, $5, $6, $7, '{}', NULL,
          $8, $9, $10, '{}', NULL, $11::jsonb)
"""

_CLOSE_SQL = """
UPDATE journal_entries
   SET valid_until   = now(),
       superseded_by = $1::uuid,
       updated_at    = now(),
       data = COALESCE(data, '{}'::jsonb)
              || jsonb_build_object('corrected_by', ($1::uuid)::text, 'closed_by', 'correction')
 WHERE id = ANY($2::uuid[])
   AND valid_until IS NULL
   AND superseded_by IS NULL
RETURNING id
"""


def correction_body(corrects: Sequence[Mapping[str, Any]], reason: str) -> str:
    """The correction row's prose: the reason, then one line per corrected id."""
    lines = [
        f"Correction: {len(corrects)} journal row(s) withdrawn. {reason}",
        "",
        "Each row below stays readable by its id, marked as corrected by this "
        "entry; it is no longer served in the stream or remembered.",
        "",
    ]
    for c in corrects:
        detail = c.get("detail") or c.get("reason") or ""
        lines.append(f"- {c['id']} ({c.get('entry_kind', '?')}): {detail}")
    return "\n".join(lines)


async def write_correction_entry(
    conn: Any,
    *,
    correction_key: str,
    corrects: Sequence[Mapping[str, Any]],
    reason: str,
    analyst_version: str = "1",
    now: datetime | None = None,
) -> tuple[UUID, bool]:
    """Insert (or find, by ``correction_key``) the correction row citing
    ``corrects`` — each ``{id, entry_kind, period_start, period_end, reason,
    detail?}``. Returns ``(correction_id, created)``."""
    existing = await conn.fetchval(_FIND_SQL, CORRECTION_ENTRY_KIND, correction_key)
    if existing is not None:
        return UUID(str(existing)), False
    now = now or datetime.now(timezone.utc)
    ids = [UUID(str(c["id"])) for c in corrects]
    starts = [c["period_start"] for c in corrects if c.get("period_start")]
    ends = [c["period_end"] for c in corrects if c.get("period_end")]
    cid = uuid4()
    data = {
        "correction_key": correction_key,
        "reason": reason,
        "corrects": [
            {k: (str(v) if k == "id" else v) for k, v in c.items()
             if k in ("id", "entry_kind", "reason", "detail", "refs_total", "refs_resolved")}
            for c in corrects
        ],
    }
    await conn.execute(
        _INSERT_SQL,
        cid,
        CORRECTION_ENTRY_KIND,
        f"Correction: {len(ids)} journal row(s) withdrawn",
        correction_body(corrects, reason),
        ids,
        min(starts) if starts else now,
        max(ends) if ends else now,
        CORRECTION_AUTHOR,
        analyst_version,
        now,
        json.dumps(data, default=str),
    )
    return cid, True


async def close_rows_by_correction(
    conn: Any, *, correction_id: UUID, ids: Sequence[Any],
) -> list[str]:
    """Close every still-open row in ``ids`` with ``superseded_by`` +
    ``valid_until`` pointing at ``correction_id``. Returns the ids closed now
    (a row already closed is left exactly as it is)."""
    uuids = [UUID(str(i)) for i in ids]
    if not uuids:
        return []
    rows = await conn.fetch(_CLOSE_SQL, correction_id, uuids)
    return [str(r["id"]) for r in rows]


__all__ = [
    "CORRECTION_AUTHOR",
    "close_rows_by_correction",
    "correction_body",
    "write_correction_entry",
]
