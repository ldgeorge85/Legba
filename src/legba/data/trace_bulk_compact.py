# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compaction of old trace bulk into bundles (see :mod:`legba.data.trace_bulk`).

Run by the daily ``analyst_traces_retention`` maintenance analyst after its
own pass, and only when ``LEGBA_TRACE_COMPRESS`` is on. Each batch is one
short transaction over at most ``bundle_size`` rows of ONE analyst:

1. lock the analyst's oldest not-yet-compacted rows older than the cut-off
   (``FOR UPDATE SKIP LOCKED``: a row another session holds is skipped, never
   waited on; nothing else is locked, so analysts keep inserting);
2. build the bundle and insert it;
3. read the stored bundle back, decode it, and require every member's text to
   be byte-identical to the row's, and every jsonb member to parse to the same
   structure;
4. point the rows at the bundle and set their three bulk columns to NULL.

Any failure rolls the whole batch back, so a row is either untouched or fully
compacted, and the next run starts again from the rows still pending (the NULL
``bulk_bundle_id`` is the only resume state).

A run stops at its time budget. An analyst's last few rows wait for a full
bundle until the oldest of them is a week past the cut-off, so slow analysts
are compacted too without every run writing a small bundle per analyst.

The space the old values held is reused by the table for new rows after
autovacuum; it goes back to the operating system only when the table is
rewritten (``VACUUM FULL`` or ``pg_repack``), which this job never does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence
from uuid import uuid4

from .trace_bulk import BULK_COLUMNS, CODEC, JSON_COLUMNS, decode_bundle, encode_bundle

logger = logging.getLogger(__name__)

__all__ = [
    "CompactSettings",
    "CompactStats",
    "MIN_AGE_DAYS",
    "compact",
    "settings_from_env",
]

ENV_SWITCH = "LEGBA_TRACE_COMPRESS"
ENV_AGE_DAYS = "LEGBA_TRACE_COMPRESS_AFTER_DAYS"
ENV_BUNDLE_SIZE = "LEGBA_TRACE_COMPRESS_BUNDLE_SIZE"
ENV_MAX_SECONDS = "LEGBA_TRACE_COMPRESS_MAX_SECONDS"

DEFAULT_AGE_DAYS = 30
#: The youngest a run may be when compacted. Readers that filter bulk columns
#: in SQL over a recent window (the frame guard reads 48 hours of
#: alert_trigger_scan output) rely on recent rows never being compacted.
MIN_AGE_DAYS = 14
DEFAULT_BUNDLE_SIZE = 32
MAX_BUNDLE_SIZE = 256
DEFAULT_MAX_SECONDS = 300
#: How long past the cut-off a partial bundle waits for more rows.
FLUSH_AFTER_DAYS = 7


@dataclass(frozen=True)
class CompactSettings:
    enabled: bool = False
    age_days: int = DEFAULT_AGE_DAYS
    bundle_size: int = DEFAULT_BUNDLE_SIZE
    max_seconds: float = DEFAULT_MAX_SECONDS


@dataclass
class CompactStats:
    bundles: int = 0
    traces: int = 0
    raw_bytes: int = 0
    stored_bytes: int = 0
    skipped_analysts: int = 0
    failures: list[str] = field(default_factory=list)
    stopped_on_budget: bool = False

    def as_data(self) -> dict[str, Any]:
        return {
            "compress_bundles": self.bundles,
            "compress_traces": self.traces,
            "compress_raw_bytes": self.raw_bytes,
            "compress_stored_bytes": self.stored_bytes,
            "compress_failures": len(self.failures),
            "compress_stopped_on_budget": self.stopped_on_budget,
        }


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, lo: int, hi: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("trace_bulk_compact.bad_env %s=%r; using %s", name, raw, default)
        return default
    if value < lo:
        logger.warning("trace_bulk_compact.env_below_floor %s=%s; using %s", name, value, lo)
        value = lo
    if hi is not None and value > hi:
        value = hi
    return value


def settings_from_env() -> CompactSettings:
    """Read the switch and its knobs (docs/TUNABLES.md)."""
    return CompactSettings(
        enabled=_truthy(os.getenv(ENV_SWITCH, "")),
        age_days=_int_env(ENV_AGE_DAYS, DEFAULT_AGE_DAYS, MIN_AGE_DAYS),
        bundle_size=_int_env(ENV_BUNDLE_SIZE, DEFAULT_BUNDLE_SIZE, 1, MAX_BUNDLE_SIZE),
        max_seconds=_int_env(ENV_MAX_SECONDS, DEFAULT_MAX_SECONDS, 1),
    )


_PENDING_ANALYSTS_SQL = """
SELECT analyst_id, min(run_started_at) AS oldest
  FROM analyst_traces
 WHERE bulk_bundle_id IS NULL
   AND run_started_at < $1
 GROUP BY analyst_id
 ORDER BY oldest
"""

_LOCK_BATCH_SQL = """
SELECT run_id, run_started_at,
       prompt_rendered,
       input_payload::text  AS input_payload,
       output_payload::text AS output_payload
  FROM analyst_traces
 WHERE analyst_id = $1
   AND bulk_bundle_id IS NULL
   AND run_started_at < $2
 ORDER BY run_started_at
 LIMIT $3
   FOR UPDATE SKIP LOCKED
"""

_INSERT_BUNDLE_SQL = """
INSERT INTO analyst_trace_bundles
    (bundle_id, analyst_id, codec, member_count, raw_bytes, stored_bytes, body)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

_READ_BACK_SQL = "SELECT codec, body FROM analyst_trace_bundles WHERE bundle_id = $1"

_POINT_ROWS_SQL = """
UPDATE analyst_traces t
   SET bulk_bundle_id = $1,
       bulk_columns   = string_to_array(m.cols, ','),
       prompt_rendered = NULL,
       input_payload   = NULL,
       output_payload  = NULL
  FROM unnest($2::uuid[], $3::text[]) AS m(run_id, cols)
 WHERE t.run_id = m.run_id
   AND t.bulk_bundle_id IS NULL
"""


class _VerifyFailed(RuntimeError):
    pass


def _verify(members: list[tuple[str, dict[str, str]]], decoded: dict[str, dict[str, str]]) -> None:
    """Every member must come back byte-identical, and its jsonb must parse equal."""
    if set(decoded) != {rid for rid, _ in members}:
        raise _VerifyFailed("bundle members differ from the batch")
    for rid, cols in members:
        got = decoded[rid]
        if set(got) != set(cols):
            raise _VerifyFailed(f"run {rid}: columns {sorted(got)} != {sorted(cols)}")
        for column, text in cols.items():
            if got[column].encode("utf-8") != text.encode("utf-8"):
                raise _VerifyFailed(f"run {rid}: {column} bytes differ")
            if column in JSON_COLUMNS and json.loads(got[column]) != json.loads(text):
                raise _VerifyFailed(f"run {rid}: {column} structure differs")


async def _compact_batch(
    pool: Any,
    analyst_id: str,
    *,
    cutoff: datetime,
    flush_before: datetime,
    bundle_size: int,
    stats: CompactStats,
) -> bool:
    """Compact one batch of ``analyst_id``. False when nothing (more) is due."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            await conn.execute("SET LOCAL statement_timeout = '120s'")
            rows = await conn.fetch(_LOCK_BATCH_SQL, analyst_id, cutoff, bundle_size)
            if not rows:
                return False
            if len(rows) < bundle_size and rows[0]["run_started_at"] >= flush_before:
                return False
            members = [
                (str(r["run_id"]), {c: r[c] for c in BULK_COLUMNS if r[c] is not None})
                for r in rows
            ]
            # lzma releases the GIL; a thread keeps the event loop (and every
            # other actor in this process) responsive while a bundle packs.
            body, raw_len = await asyncio.to_thread(encode_bundle, members)
            bundle_id = uuid4()
            await conn.execute(
                _INSERT_BUNDLE_SQL, bundle_id, analyst_id, CODEC,
                len(members), raw_len, len(body), body,
            )
            stored = await conn.fetchrow(_READ_BACK_SQL, bundle_id)
            _verify(members, await asyncio.to_thread(decode_bundle, stored["body"]))
            status = await conn.execute(
                _POINT_ROWS_SQL, bundle_id,
                [r["run_id"] for r in rows],
                [",".join(c for c in BULK_COLUMNS if c in cols) for _, cols in members],
            )
            if status != f"UPDATE {len(rows)}":
                raise _VerifyFailed(f"pointed {status!r} of {len(rows)} locked rows")
    stats.bundles += 1
    stats.traces += len(rows)
    stats.raw_bytes += raw_len
    stats.stored_bytes += len(body)
    return True


async def compact(
    pool: Any,
    settings: CompactSettings,
    *,
    now: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
    max_bundles: int | None = None,
    analyst_ids: Sequence[str] | None = None,
) -> CompactStats:
    """Compact pending old traces until done or out of time budget.

    ``max_bundles`` and ``analyst_ids`` narrow a run (a forced or test run);
    the scheduled run passes neither.
    """
    stats = CompactStats()
    if not settings.enabled:
        return stats
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(settings.age_days, MIN_AGE_DAYS))
    flush_before = cutoff - timedelta(days=FLUSH_AFTER_DAYS)
    deadline = clock() + settings.max_seconds

    async with pool.acquire() as conn:
        analysts = [r["analyst_id"] for r in await conn.fetch(_PENDING_ANALYSTS_SQL, cutoff)]
    if analyst_ids is not None:
        analysts = [a for a in analysts if a in set(analyst_ids)]

    for analyst_id in analysts:
        while True:
            if clock() >= deadline or (max_bundles is not None and stats.bundles >= max_bundles):
                stats.stopped_on_budget = True
                return stats
            try:
                more = await _compact_batch(
                    pool, analyst_id, cutoff=cutoff, flush_before=flush_before,
                    bundle_size=settings.bundle_size, stats=stats,
                )
            except Exception as exc:  # the batch rolled back; leave this analyst for the next run
                logger.warning("trace_bulk_compact.batch_failed analyst=%s err=%s", analyst_id, exc)
                stats.failures.append(f"{analyst_id}: {exc}")
                stats.skipped_analysts += 1
                break
            if not more:
                break
    return stats
