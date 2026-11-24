# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``indicator_tracker`` persistence — the stream watermarks, the flip record
(migration 0237) and the one public reader, :func:`debounced_flips`.

Where the state lives
---------------------

* The per-stream WATERMARK rides the existing ``alert_trigger_watermarks``
  table under ``trigger_class = 'indicator_tracker'``, one row per
  ``(source analyst, target)`` stream keyed ``<analyst_id>|<target_id>`` (an
  empty target for a target-less stream). Its ``state`` is
  :meth:`StreamState.to_json`: the newest source run walked (finding id +
  ``produced_at``) and, per indicator lineage, the status, the current block
  and the last CONFIRMED status. A sweep reads only source runs newer than the
  watermark; a stream with nothing newer is not touched and its row is not
  rewritten.
* Every flip lands in ``indicator_flips`` (0237), graded ``raw`` or
  ``debounced``, once: the unique index
  ``(grade, source_analyst_id, coalesce(target_id, ''), indicator_id,
  from_status, to_status, evidence_finding_id)`` turns a re-run over the same
  evidence into ``ON CONFLICT DO NOTHING``. ``RETURNING`` is what the handler
  trusts for "new": only debounced rows this sweep actually inserted reach the
  receipt.

Flips and watermarks are written in ONE transaction, so a failed sweep leaves
neither behind and the next sweep redoes it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Iterable, Mapping
from uuid import UUID

from ._indicator_debounce import StreamState

logger = logging.getLogger(__name__)

#: ``alert_trigger_watermarks.trigger_class`` of the stream watermarks.
WATERMARK_CLASS = "indicator_tracker"

#: Most source runs read per stream in one sweep. A first sight of a stream
#: (no watermark yet) reads up to this many of its newest runs inside the
#: lookback as its history; a stream that fell further behind than this
#: continues from its watermark across the gap (logged).
MAX_RUNS_PER_STREAM = 64


def stream_key(analyst_id: str | None, target_id: str | None) -> str:
    return f"{analyst_id or ''}|{target_id or ''}"


# ---------------------------------------------------------------------------
# Reads the sweep needs
# ---------------------------------------------------------------------------

_WATERMARKS_SQL = """
SELECT watermark_key, state FROM alert_trigger_watermarks
 WHERE trigger_class = $1
"""

#: The source runs newer than each stream's watermark, newest
#: ``MAX_RUNS_PER_STREAM`` per stream, inside the lookback. The watermarks ride
#: in as three parallel arrays (no temp table). Only the indicator array is
#: lifted, re-wrapped in the persisted ``data -> 'data'`` shape.
_NEW_RUNS_SQL = """
WITH wm AS (
    SELECT * FROM unnest($2::text[], $3::timestamptz[], $4::uuid[])
        AS w(stream, at, fid)
)
SELECT id, target_id, analyst_id, produced_at,
       jsonb_build_object('data', jsonb_build_object('indicators', inds)) AS data
  FROM (
    SELECT ao.id, ao.target_id, ao.analyst_id, ao.produced_at,
           ao.data -> 'data' -> 'indicators' AS inds,
           row_number() OVER (
               PARTITION BY ao.target_id, ao.analyst_id
               ORDER BY ao.produced_at DESC, ao.id DESC
           ) AS rn
      FROM analyst_outputs ao
      LEFT JOIN wm
        ON wm.stream = coalesce(ao.analyst_id, '') || '|' || coalesce(ao.target_id, '')
     WHERE ao.kind = 'finding'
       AND ao.produced_at > NOW() - make_interval(days => $1::int)
       AND jsonb_typeof(ao.data -> 'data' -> 'indicators') = 'array'
       AND jsonb_array_length(ao.data -> 'data' -> 'indicators') > 0
       AND (wm.at IS NULL OR (ao.produced_at, ao.id) > (wm.at, wm.fid))
  ) t
 WHERE t.rn <= $5
 ORDER BY analyst_id, target_id, produced_at, id
"""

#: One cadence of each source unit — its head descriptor's
#: ``cadence.cooldown_seconds`` (the same cadence ``carried_state`` reads).
_CADENCE_SQL = """
SELECT descriptor_id,
       CASE WHEN body -> 'cadence' ->> 'cooldown_seconds' ~ '^[0-9]+$'
            THEN (body -> 'cadence' ->> 'cooldown_seconds')::bigint END AS cooldown_s
  FROM analyst_descriptors
 WHERE is_head AND descriptor_id = ANY($1::text[])
"""


async def read_watermarks(conn: Any) -> dict[str, StreamState]:
    rows = await conn.fetch(_WATERMARKS_SQL, WATERMARK_CLASS)
    out: dict[str, StreamState] = {}
    for r in rows:
        raw = r["state"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        try:
            out[str(r["watermark_key"])] = StreamState.from_json(raw)
        except (KeyError, TypeError, ValueError) as exc:
            # A state this code cannot read is treated as no watermark: the
            # stream re-reads its history and the unique index absorbs it.
            logger.warning(
                "indicator_tracker.watermark_unreadable key=%s err=%s",
                r["watermark_key"], exc,
            )
    return out


async def fetch_new_runs(
    conn: Any, watermarks: Mapping[str, StreamState], *, lookback_days: int,
) -> list[dict[str, Any]]:
    keys, ats, fids = [], [], []
    for k, st in watermarks.items():
        if st.last_produced_at is None or not st.last_finding_id:
            continue
        keys.append(k)
        ats.append(st.last_produced_at)
        fids.append(UUID(str(st.last_finding_id)))
    rows = await conn.fetch(
        _NEW_RUNS_SQL, int(lookback_days), keys, ats, fids, MAX_RUNS_PER_STREAM,
    )
    return [dict(r) for r in rows]


async def read_cadence_hours(conn: Any, analyst_ids: Iterable[str]) -> dict[str, float]:
    ids = sorted({a for a in analyst_ids if a})
    if not ids:
        return {}
    rows = await conn.fetch(_CADENCE_SQL, ids)
    return {
        str(r["descriptor_id"]): int(r["cooldown_s"]) / 3600.0
        for r in rows
        if r["cooldown_s"] is not None and int(r["cooldown_s"]) > 0
    }


# ---------------------------------------------------------------------------
# The sweep's one write
# ---------------------------------------------------------------------------

_INSERT_FLIP_SQL = """
INSERT INTO indicator_flips (
    grade, target_id, source_analyst_id, indicator_id, statement,
    from_status, to_status, activation, match, citations,
    evidence_finding_id, evidence_at, prev_finding_id,
    confirmed_finding_id, confirmed_at, confirmed_by,
    held_runs, held_hours, raw_flips_between, backfill, method_version
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb,
    $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21
)
ON CONFLICT (grade, source_analyst_id, (coalesce(target_id, '')), indicator_id,
             from_status, to_status, evidence_finding_id)
DO NOTHING
RETURNING id
"""

_WRITE_WATERMARK_SQL = """
INSERT INTO alert_trigger_watermarks (trigger_class, watermark_key, state, updated_at)
VALUES ($1, $2, $3::jsonb, now())
ON CONFLICT (trigger_class, watermark_key) DO UPDATE
   SET state = EXCLUDED.state, updated_at = now()
"""


def _uuid_or_none(raw: Any) -> UUID | None:
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


async def persist_sweep(
    pool: Any,
    flips: list[dict[str, Any]],
    states: Mapping[str, StreamState],
    *,
    method_version: str,
) -> list[dict[str, Any]]:
    """Insert every flip once and advance the touched watermarks, in one
    transaction. Returns the flips that were NEW (inserted this call)."""
    inserted: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for f in flips:
                row = await conn.fetchrow(
                    _INSERT_FLIP_SQL,
                    f["grade"], f.get("target_id"), str(f.get("source_analyst_id") or ""),
                    str(f["indicator_id"]), str(f.get("statement") or ""),
                    f["from_status"], f["to_status"], bool(f["activation"]),
                    str(f.get("match") or "id"), json.dumps(f.get("citations") or []),
                    _uuid_or_none(f["evidence_finding_id"]), f["evidence_at"],
                    _uuid_or_none(f.get("prev_finding_id")),
                    _uuid_or_none(f.get("confirmed_finding_id")),
                    f.get("confirmed_at"), f.get("confirmed_by"),
                    f.get("held_runs"), f.get("held_hours"),
                    f.get("raw_flips_between"), bool(f.get("backfill")),
                    method_version,
                )
                if row is not None:
                    inserted.append(f)
            for key, st in states.items():
                await conn.execute(
                    _WRITE_WATERMARK_SQL, WATERMARK_CLASS, key, json.dumps(st.to_json()),
                )
    return inserted


# ---------------------------------------------------------------------------
# The reader — the feed's, the Wall's and the bake-off arm E's one source
# ---------------------------------------------------------------------------

_DEBOUNCED_SQL = """
SELECT id, target_id, source_analyst_id, indicator_id, statement,
       from_status, to_status, activation, match,
       evidence_finding_id::text AS onset_finding_id, evidence_at AS onset_at,
       prev_finding_id::text AS prev_finding_id,
       confirmed_finding_id::text AS confirmed_finding_id, confirmed_at,
       confirmed_by, held_runs, held_hours, raw_flips_between, backfill,
       method_version, recorded_at
  FROM indicator_flips
 WHERE grade = 'debounced'
   AND ($1::text IS NULL OR target_id = $1)
   AND confirmed_at > $2
   AND ($3::timestamptz IS NULL OR confirmed_at <= $3)
 ORDER BY confirmed_at, id
 LIMIT $4
"""


async def debounced_flips(
    conn: Any,
    target_id: str | None,
    since: datetime,
    *,
    until: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """The DEBOUNCED indicator flips confirmed in ``(since, until]``.

    ``conn`` is anything with an asyncpg ``fetch`` (a connection or a pool).
    ``target_id`` scopes to one desk; ``None`` returns every desk. ``until``
    (default: no upper bound) is the as-of cut — a reader answering "as of T"
    passes T so no flip confirmed after T leaks in. Ordered oldest first.

    Each row: ``target_id``, ``source_analyst_id`` (the unit), ``indicator_id``
    (the slug at onset), ``statement``, ``from_status`` → ``to_status``,
    ``activation``, ``onset_finding_id`` / ``onset_at`` (the first run in the
    new status), ``prev_finding_id`` (the run before it, in the old status),
    ``confirmed_finding_id`` / ``confirmed_at`` (the run that made it hold),
    ``confirmed_by`` (``runs`` | ``hours``), ``held_runs``, ``held_hours``,
    ``raw_flips_between`` (raw toggles it took to settle: 1 = a clean change;
    more = it wobbled first), ``backfill`` (found on the stream's first sweep,
    from history, and never fed), ``method_version``, ``recorded_at``.
    Raw flips are never returned.
    """
    rows = await conn.fetch(_DEBOUNCED_SQL, target_id, since, until, int(limit))
    return [dict(r) for r in rows]


__all__ = [
    "MAX_RUNS_PER_STREAM",
    "WATERMARK_CLASS",
    "debounced_flips",
    "fetch_new_runs",
    "persist_sweep",
    "read_cadence_hours",
    "read_watermarks",
    "stream_key",
]
