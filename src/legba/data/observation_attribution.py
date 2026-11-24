# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Licence, attribution and declared subject kind of LIVE series rows — in one
query, for rows written before or after the lane N-R stamp (lane N-R).

WHY A READ-TIME JOIN. Live ``observations`` rows are append-only by trigger
(migration 0246: UPDATE/DELETE of an ``origin_class='live'`` row raises). The
observation sink now copies ``scope.license_class`` and ``scope.attribution``
onto each row's ``provenance``, but rows written before that (the first
PortWatch poll's 8,036 rows on 2026-11-13) carry neither, and must not be
rewritten. Writing a second "corrected" vintage would be worse: it would
invent a revision the provider never made and move ``record_time``. So the
stamp is resolved when READ:

* ``license_class`` / ``attribution`` — the row's own stamp when present
  (``basis = 'row'``), else the source descriptor version that was the
  newest at the row's ``loaded_at`` (what it was written under), else the
  current head (``basis = 'descriptor'``);
* ``subject_kind`` — the kind the series registry declares
  (``sink.config.series[].subject_kind`` of the current head), falling back to
  the stored column; ``stored_subject_kind`` is the column as written (rows
  before N-R say ``global`` for PortWatch, rows after say ``entity``).

:data:`LIVE_SERIES_ATTRIBUTED_SQL` is the one query; :func:`fetch_attributed`
runs it. ``scripts/backfill_observation_attribution.py`` is the dry-run check
that every existing live row resolves — it writes nothing.
"""

from __future__ import annotations

from datetime import date
from typing import Any

__all__ = ["LIVE_SERIES_ATTRIBUTED_SQL", "fetch_attributed"]

#: $1 source_id, $2 series_id (NULL = every series), $3 / $4 valid_from window
#: (NULL = open). Newest vintage per (series, subject, period) is the caller's
#: choice — this returns every vintage, ordered.
LIVE_SERIES_ATTRIBUTED_SQL = """
SELECT o.id, o.series_id, o.subject, o.valid_from, o.valid_to, o.record_time,
       o.loaded_at, o.value, o.value_text, o.unit, o.period,
       o.methodology_version, o.fetch_sha256, o.provenance,
       COALESCE(o.provenance->>'license_class',
                at_load.body->'scope'->>'license_class',
                head.body->'scope'->>'license_class') AS license_class,
       COALESCE(o.provenance->>'attribution',
                at_load.body->'scope'->>'attribution',
                head.body->'scope'->>'attribution') AS attribution,
       CASE WHEN o.provenance ? 'license_class' THEN 'row' ELSE 'descriptor' END
           AS licence_basis,
       COALESCE(spec.value->>'subject_kind', o.subject_kind) AS subject_kind,
       o.subject_kind AS stored_subject_kind,
       at_load.version AS written_under_version
  FROM observations o
  LEFT JOIN LATERAL (
        SELECT sd.version, sd.body FROM source_descriptors sd
         WHERE sd.descriptor_id = o.source_id AND sd.created_at <= o.loaded_at
         ORDER BY sd.created_at DESC LIMIT 1) at_load ON true
  LEFT JOIN LATERAL (
        SELECT sd.body FROM source_descriptors sd
         WHERE sd.descriptor_id = o.source_id AND sd.is_head
         LIMIT 1) head ON true
  LEFT JOIN LATERAL (
        SELECT s.value FROM jsonb_array_elements(
                 COALESCE(head.body->'sink'->'config'->'series', '[]'::jsonb)) s
         WHERE s.value->>'id' = o.series_id LIMIT 1) spec ON true
 WHERE o.origin_class = 'live'
   AND o.source_id = $1
   AND ($2::text IS NULL OR o.series_id = $2)
   AND ($3::date IS NULL OR o.valid_from >= $3)
   AND ($4::date IS NULL OR o.valid_from <= $4)
 ORDER BY o.series_id, o.subject, o.valid_from, o.record_time
"""


async def fetch_attributed(
    conn: Any, source_id: str, *, series_id: str | None = None,
    valid_from: date | None = None, valid_to: date | None = None,
) -> list[dict[str, Any]]:
    """Every live row of ``source_id`` (optionally one series / a window) with
    its resolved licence, attribution and declared subject kind."""
    rows = await conn.fetch(LIVE_SERIES_ATTRIBUTED_SQL, source_id, series_id, valid_from, valid_to)
    return [dict(r) for r in rows]
