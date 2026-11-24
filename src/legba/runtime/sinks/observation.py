# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``observation`` sink — numeric series into the bitemporal ``observations``.

A poll's :class:`~legba.data.sources.records.ObservationRecord` s are buffered
and written once, at the end of the poll:

1. **Validate against the declared series registry** (``sink.config.series``,
   validated at descriptor save): an undeclared series, or a unit / period /
   subject kind / methodology that disagrees with the declaration, is
   REJECTED with a reason — never written under a guessed unit.
2. **Read the latest vintage** of every (series, subject, period) the poll
   touched, in ONE statement over the live partial index (0246).
3. **Write only a change.** Equal value, unit and methodology = ``unchanged``,
   no row. A key never seen = ``written``. A different value for a held key =
   a NEW row (``revised``): the table is append-only for live rows (trigger),
   the old vintage stays, and an as-of read before this poll still returns it.

``record_time`` is the provider's revision stamp when the record carries one
(``record_time_basis='provider'``), else the time of the poll that first saw
this value (``first_fetch``). ``loaded_at`` (the column default) is our write.

Every row carries ``fetch_sha256`` (and ``sha256``, the 0220 file digest
column) = the response it was parsed from, so a citation of the row resolves
to stored bytes. A record with no fetch receipt is refused (``no_fetch_receipt``)
— a live series value must rest on what the poll received.

Every row also carries the source's licence and attribution in ``provenance``
(``license_class`` from ``scope.license_class``, ``attribution`` from
``scope.attribution``; a value the record already carries wins) — the same
stamp the document sink puts on signal payloads (lane N-R). Rows written
before the stamp existed are attributed at READ time
(:mod:`legba.data.observation_attribution`), never rewritten: live rows are
append-only by trigger.

WRITES ARE BATCHED (lane N-R): the rows of one flush go in as
``INSERT ... SELECT FROM unnest(...)``, at most :data:`INSERT_BATCH_ROWS` rows
per statement (the first PortWatch poll's 8,036 row-by-row inserts held the
actor for 2 min 12 s). ``ON CONFLICT DO NOTHING`` and the change test are
exactly as before; a conflicting row is absent from ``RETURNING`` and is
reported as ``identity_conflict``, as a per-row insert reported it.

Nothing here touches ``signals``, NATS, the corpus or the vector index.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from ...data.sources.records import ObservationRecord
from . import SinkResult, register_sink

logger = logging.getLogger(__name__)

__all__ = ["INSERT_BATCH_ROWS", "ObservationSink"]

#: Rows per INSERT statement. Bounded so one statement's parameter arrays and
#: lock footprint stay small; 8,036 rows are nine statements.
INSERT_BATCH_ROWS = 1000

_LATEST_VINTAGE_SQL = """
SELECT DISTINCT ON (series_id, subject, valid_from, valid_to)
       series_id, subject, valid_from, valid_to, value, value_text, unit,
       methodology_version, record_time
  FROM observations
 WHERE origin_class = 'live'
   AND source_id = $1
   AND series_id = ANY($2::text[])
   AND subject = ANY($3::text[])
   AND valid_from >= $4 AND valid_from <= $5
 ORDER BY series_id, subject, valid_from, valid_to, record_time DESC
"""

_INSERT_BATCH_SQL = """
INSERT INTO observations (
    collection_id, series_id, subject_kind, subject, valid_from, valid_to,
    record_time, value, unit, value_text, source_url, sha256, origin_class,
    provenance, source_id, methodology_version, fetch_sha256, period,
    record_time_basis
)
SELECT $1, r.series_id, r.subject_kind, r.subject, r.valid_from, r.valid_to,
       r.record_time, r.value, r.unit, r.value_text, r.source_url, r.sha256,
       'live', r.provenance::jsonb, $2, r.methodology_version, r.sha256,
       r.period, r.record_time_basis
  FROM unnest($3::text[], $4::text[], $5::text[], $6::date[], $7::date[],
              $8::timestamptz[], $9::numeric[], $10::text[], $11::text[],
              $12::text[], $13::text[], $14::text[], $15::text[], $16::text[],
              $17::text[])
       AS r(series_id, subject_kind, subject, valid_from, valid_to,
            record_time, value, unit, value_text, source_url, sha256,
            provenance, methodology_version, period, record_time_basis)
ON CONFLICT DO NOTHING
RETURNING id, series_id, subject, valid_from, valid_to
"""


def live_collection_id(source_id: str) -> str:
    """``live:<source_id>`` — never a valid CollectionId, so no history read
    can select a live row (migration 0246)."""
    return f"live:{source_id}"


class ObservationSink:
    family = "observation"
    record_types: tuple[type, ...] = (ObservationRecord,)

    def __init__(self, core: Any) -> None:
        self._core = core
        cfg = core.descriptor.sink.observation_config()
        self._series = {s.id: s for s in cfg.series}
        self._buf: dict[tuple, ObservationRecord] = {}
        self._result = SinkResult()

    @property
    def source_id(self) -> str:
        return self._core.descriptor.identity.id

    def accept(self, record: ObservationRecord) -> None:
        spec = self._series.get(record.series_id)
        reason = None
        if spec is None:
            reason = f"undeclared_series:{record.series_id}"
        elif record.unit != spec.unit:
            reason = f"unit_mismatch:{record.series_id}:{record.unit}!={spec.unit}"
        elif record.period != spec.period:
            reason = f"period_mismatch:{record.series_id}:{record.period}!={spec.period}"
        elif record.subject_kind != spec.subject_kind:
            reason = f"subject_kind_mismatch:{record.series_id}"
        elif record.methodology_version != spec.methodology_version:
            reason = f"methodology_mismatch:{record.series_id}:{record.methodology_version}"
        elif not record.fetch_sha256:
            reason = f"no_fetch_receipt:{record.series_id}"
        if reason is not None:
            self._result.rejected.append(reason)
            return
        # A key seen twice in one poll (overlapping pages): the later wins.
        self._buf[record.key()] = record

    async def flush(self, conn: Any, *, polled_at: datetime) -> SinkResult:
        result, self._result = self._result, SinkResult()
        records = list(self._buf.values())
        self._buf = {}
        if not records:
            return result
        latest = {}
        rows = await conn.fetch(
            _LATEST_VINTAGE_SQL, self.source_id,
            sorted({r.series_id for r in records}),
            sorted({r.subject for r in records}),
            min(r.valid_from for r in records), max(r.valid_from for r in records),
        )
        for row in rows:
            latest[(row["series_id"], row["subject"], row["valid_from"], row["valid_to"])] = row
        pending: list[tuple[ObservationRecord, datetime, bool]] = []
        for rec in records:
            prior = latest.get(rec.key())
            if prior is not None and _same(prior, rec):
                result.unchanged += 1
                continue
            pending.append((rec, rec.record_time or polled_at, prior is not None))
        stamp = self._licence_stamp()
        for start in range(0, len(pending), INSERT_BATCH_ROWS):
            await self._insert_batch(conn, pending[start:start + INSERT_BATCH_ROWS], stamp, result)
        if result.written or result.rejected:
            logger.info(
                "observation_sink.flush source=%s written=%d revised=%d unchanged=%d rejected=%d",
                self.source_id, result.written, result.revised, result.unchanged,
                len(result.rejected),
            )
        return result

    def _licence_stamp(self) -> dict[str, str]:
        """``license_class`` + ``attribution`` from the source scope (set keys only)."""
        scope = self._core.descriptor.scope
        stamp = {"license_class": getattr(scope, "license_class", None),
                 "attribution": getattr(scope, "attribution", None)}
        return {k: v for k, v in stamp.items() if v}

    async def _insert_batch(
        self, conn: Any, batch: list[tuple[ObservationRecord, datetime, bool]],
        stamp: dict[str, str], result: SinkResult,
    ) -> None:
        """ONE statement for ``batch``. ``RETURNING`` maps ids back by identity
        (a flush buffers one record per key, so the key is unique here)."""
        cols: list[list[Any]] = [[] for _ in range(15)]
        for rec, record_time, _revised in batch:
            row = (
                rec.series_id, rec.subject_kind, rec.subject, rec.valid_from, rec.valid_to,
                record_time, rec.value, rec.unit, rec.value_text, rec.source_url,
                rec.fetch_sha256, json.dumps({**stamp, **rec.provenance}, default=str),
                rec.methodology_version, rec.period,
                "provider" if rec.record_time is not None else "first_fetch",
            )
            for col, value in zip(cols, row):
                col.append(value)
        rows = await conn.fetch(
            _INSERT_BATCH_SQL, live_collection_id(self.source_id), self.source_id, *cols,
        )
        ids = {(r["series_id"], r["subject"], r["valid_from"], r["valid_to"]): r["id"] for r in rows}
        for rec, _record_time, revised in batch:
            new_id = ids.get(rec.key())
            if new_id is None:
                result.rejected.append(
                    f"identity_conflict:{rec.series_id}:{rec.subject}:{rec.valid_from}")
                continue
            result.written += 1
            result.written_ids.append((new_id, rec))
            if revised:
                result.revised += 1


def _same(prior: Any, rec: ObservationRecord) -> bool:
    if prior["unit"] != rec.unit or prior["methodology_version"] != rec.methodology_version:
        return False
    if rec.value is not None:
        return prior["value"] is not None and prior["value"] == rec.value
    return prior["value_text"] == rec.value_text


register_sink("observation", ObservationSink)
