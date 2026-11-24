# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The raw readers' ``known_at`` cut — the MACHINERY (lane E0).

What each raw reader does under ``known_at`` is classified in
:mod:`legba.data.raw_reader_known_at`; this module is the shared code
those readers call. The predicate is the temporal leaf's existing half-open
anchor window (:func:`legba.runtime.substrate_temporal.anchor_window_clause`)
with only the upper bound: ``<anchor> < $n`` — "entered the record before K".

Pure SQL rendering plus one read-only confirmation query; no writes.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

from ..data.raw_reader_known_at import KNOWN_AT_VERSION
from . import substrate_temporal as _temporal

__all__ = [
    "RECORD_ANCHOR",
    "confirm_recorded_before",
    "cut_result_rows",
    "cut_stamp",
    "parse_known_at",
    "recorded_before_sql",
]

#: The record-entry column on the two tables a raw reader can hand out ids from.
RECORD_ANCHOR = "created_at"

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def parse_known_at(value: Any) -> datetime:
    """``known_at`` as an aware UTC instant, or a loud refusal (never ``now()``)."""
    return _temporal.parse_instant(value, name="known_at")


def recorded_before_sql(anchor_expr: str, param: int) -> str:
    """``<anchor_expr> < $<param>`` through the temporal leaf's one rendering."""
    return _temporal.anchor_window_clause(anchor_expr, since_param=None, until_param=param)


#: Ids that were in the record before K: a ``signals`` row or an
#: ``analyst_outputs`` row (``read_document``'s finding fallback) whose
#: ``created_at`` is before K. Anything else — post-K, or no row at all — is
#: not confirmed.
_CONFIRM_SQL = (
    "SELECT id::text AS id FROM signals "
    f"WHERE id = ANY($1::uuid[]) AND {recorded_before_sql(RECORD_ANCHOR, 2)} "
    "UNION "
    "SELECT id::text AS id FROM analyst_outputs "
    f"WHERE id = ANY($1::uuid[]) AND {recorded_before_sql(RECORD_ANCHOR, 2)}"
)


async def confirm_recorded_before(
    pool: Any, refs: Iterable[Any], known_at: datetime,
) -> tuple[set[str], set[str]]:
    """``(confirmed, refused)`` over ``refs``: confirmed = in the record before K.

    A ref that is not a UUID, has no row, or entered at/after K is REFUSED —
    an unknown record time is treated as not known, never as known.
    """
    ids = [str(r) for r in dict.fromkeys(refs) if r is not None]
    uuids = [i for i in ids if _UUID_RE.match(i)]
    confirmed: set[str] = set()
    if uuids:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_CONFIRM_SQL, uuids, known_at)
        confirmed = {str(r["id"]) for r in rows}
    # Compare case-insensitively: Postgres renders uuids lower-case.
    lower = {c.lower() for c in confirmed}
    kept = {i for i in ids if i.lower() in lower}
    return kept, set(ids) - kept


def cut_stamp(known_at: datetime, *, dropped: int, how: str) -> dict[str, Any]:
    """The block a cut reader adds to its result, so a trace shows the cut ran."""
    return {
        "known_at": known_at.isoformat(),
        "version": KNOWN_AT_VERSION,
        "how": how,
        "dropped_after_known_at": int(dropped),
    }


async def cut_result_rows(
    pool: Any,
    result: dict[str, Any],
    known_at: datetime,
    *,
    id_key: str,
    how: str,
    limit: int | None = None,
) -> dict[str, Any]:
    """Drop every row (and ref) of a reader's result not in the record before K.

    ``id_key`` names the row field carrying the substrate id (``id`` for a
    corpus hit, ``signal_id`` for a vector hit). ``limit`` truncates the
    survivors (a reader that over-fetched to make up for the drop). Rows with
    no id are dropped: an unknown record time is not known. The result gains a
    ``known_at_cut`` block with the count dropped.
    """
    rows = list(result.get("rows") or [])
    ids = [r.get(id_key) for r in rows if isinstance(r, dict)]
    kept, _refused = await confirm_recorded_before(pool, [i for i in ids if i], known_at)
    survivors = [r for r in rows if isinstance(r, dict) and r.get(id_key) and str(r[id_key]) in kept]
    dropped = len(rows) - len(survivors)
    if limit is not None:
        survivors = survivors[: max(0, int(limit))]
    keep_ids = {str(r[id_key]) for r in survivors}
    out = dict(result)
    out["rows"] = survivors
    out["refs"] = [r for r in (result.get("refs") or []) if str(r) in keep_ids]
    if "count" in out:
        out["count"] = len(survivors)
    out["known_at_cut"] = cut_stamp(known_at, dropped=dropped, how=how)
    return out
