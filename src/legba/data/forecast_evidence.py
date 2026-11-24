# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The forecast resolvers' SEARCH — read-only SQL over the record (ST1-A).

For each :class:`~legba.data.forecast_resolvers.SearchPlan` it reads the
canonical signals fetched inside the plan's bounds whose title + summary +
archived text match every pre-filter pattern (case-insensitive AREs), with each
row's source class and geo from the source descriptor heads; for a
``series_threshold`` question it reads the named series from ``observations``.
It also records the SCOPE of the search — the bounds, the patterns, how many
rows matched, how many signals the window holds at all, and which official
sources the corpus carries for each actor — so a proposal that rests on an
absence says what was looked at.

Deterministic: no model, no network; the same rows give the same evidence.
The pre-filter only narrows the rows; :mod:`legba.data.forecast_resolvers`
decides, sentence by sentence, against the sealed criterion.

LEAF: stdlib only.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Mapping

from .forecast_resolvers import ACTOR_SQL_STEMS, ResolverContext, SearchPlan, search_plans

__all__ = [
    "MAX_ROWS_PER_PART",
    "OBSERVATIONS_SQL",
    "SIGNALS_SQL",
    "SOURCE_CLASSES_SQL",
    "WINDOW_COUNT_SQL",
    "collect_evidence",
]

#: Rows read per clause set; a hit on the cap is reported (``truncated``).
MAX_ROWS_PER_PART = 4000
#: When a plan has no lower bound, look back this far from its upper bound.
DEFAULT_LOOKBACK_DAYS = 60

SOURCE_CLASSES_SQL = """
SELECT descriptor_id, body->'scope'->>'source_class' AS source_class,
       COALESCE(body->'scope'->'geo', '[]'::jsonb)::text AS geo, state
  FROM source_descriptors
 WHERE is_head AND descriptor_id LIKE 'source.%'
"""

SIGNALS_SQL = """
SELECT s.id::text AS signal_id, s.source_id, s.fetched_at,
       COALESCE(s.payload->>'published_at', s.payload->>'_published_at_dt') AS published_at,
       COALESCE(s.canonical_url, s.payload->>'link', s.payload->>'source_url') AS url,
       s.payload->>'title' AS title,
       left(s.payload->>'summary', 4000) AS summary,
       left(s.payload->>'archived_text', 20000) AS text
  FROM signals s
 WHERE s.fetched_at >= $1 AND s.fetched_at <= $2
   AND s.canonical_signal_id IS NULL
   AND (COALESCE(s.payload->>'title', '') || ' ' || COALESCE(s.payload->>'summary', '')
        || ' ' || COALESCE(s.payload->>'archived_text', '')) ~* ALL ($3::text[])
 ORDER BY s.fetched_at
 LIMIT $4
"""

WINDOW_COUNT_SQL = """
SELECT count(*) FROM signals WHERE fetched_at >= $1 AND fetched_at <= $2
"""

#: The sealed series as RECORDED at the as-of (st1.2, lane S-1): LIVE rows
#: only (each rests on a fetch receipt), the series named by the sealed
#: ``source`` + ``series`` (id ``<source>.<...>.<series>``, case-folded — EIA
#: RBRTE is ``eia.pet_spot.rbrte``), the newest vintage per day with
#: ``record_time <= $3`` (a later revision never leaks into an earlier
#: reading), from $4 on, with the receipt version the row is linked to.
#: $1 source, $2 series, $3 as-of, $4 valid_from floor.
OBSERVATIONS_SQL = """
SELECT * FROM (
    SELECT DISTINCT ON (o.series_id, o.subject, o.valid_from)
           o.id::text AS id, o.series_id, o.subject, o.valid_from, o.value::float8 AS value,
           o.unit, o.source_id, o.source_url, o.sha256, o.fetch_sha256, o.record_time,
           o.loaded_at, rec.version_id AS fetch_version_id
      FROM observations o
      LEFT JOIN LATERAL (
            SELECT r.version_id FROM fetch_version_records r
             WHERE r.record_kind = 'observation' AND r.record_id = o.id
             ORDER BY r.role LIMIT 1) rec ON true
     WHERE o.origin_class = 'live'
       AND lower(o.series_id) LIKE lower($1) || '.%'
       AND lower(o.series_id) LIKE '%.' || lower($2)
       AND o.record_time <= $3
       AND o.valid_from >= $4
     ORDER BY o.series_id, o.subject, o.valid_from, o.record_time DESC
) v
 ORDER BY valid_from DESC
 LIMIT 400
"""
#: How far before the sealed date the series is read (the holiday rule looks
#: back 7 days; the rest shows the series' recent history in the scope).
SERIES_LOOKBACK_DAYS = 30


def _day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def _geo(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(g) for g in raw]
    try:
        val = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(g) for g in val] if isinstance(val, list) else []


async def _source_classes(conn: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in await conn.fetch(SOURCE_CLASSES_SQL):
        out[str(r["descriptor_id"])] = {
            "source_class": r["source_class"],
            "geo": _geo(r["geo"]),
            "state": r["state"],
        }
    return out


def _official_roster(classes: Mapping[str, Mapping[str, Any]], actors: list[str]) -> dict[str, list[dict[str, str]]]:
    roster: dict[str, list[dict[str, str]]] = {}
    for code in actors:
        roster[code] = [
            {"source_id": sid, "state": str(meta.get("state"))}
            for sid, meta in sorted(classes.items())
            if meta.get("source_class") == "official" and code.upper() in {g.upper() for g in meta.get("geo", [])}
        ]
    return roster


async def _read_plan(conn: Any, plan: SearchPlan, classes: Mapping[str, Mapping[str, Any]],
                     as_of: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    until = min(plan.until, as_of) if plan.until else as_of
    since = plan.since or (until - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    patterns = [p for p in plan.sql_all if p]
    rows: list[dict[str, Any]] = []
    if patterns and since <= until:
        for r in await conn.fetch(SIGNALS_SQL, since, until, patterns, MAX_ROWS_PER_PART):
            d = dict(r)
            meta = classes.get(str(d.get("source_id")), {})
            d["source_class"] = meta.get("source_class")
            d["source_geo"] = meta.get("geo", [])
            rows.append(d)
    in_scope = await conn.fetchval(WINDOW_COUNT_SQL, since, until) if since <= until else 0
    searched = {
        "corpus": "signals (canonical rows; title + summary + archived text)",
        "fetched_from": since.isoformat(),
        "fetched_to": until.isoformat(),
        "prefilter_all": patterns,
        "family": plan.family,
        "rows_matched": len(rows),
        "truncated": len(rows) >= MAX_ROWS_PER_PART,
        "signals_in_scope": int(in_scope or 0),
        "publishers_matched": len({str(r.get("source_id")) for r in rows}),
        "official_sources_by_actor": _official_roster(
            classes, [a for a in plan.actors if a in ACTOR_SQL_STEMS]),
    }
    return rows, searched


async def collect_evidence(conn: Any, q: Mapping[str, Any], *, as_of: datetime,
                           ctx: ResolverContext | None = None) -> dict[str, Any]:
    """``{rows_by_part, searched_by_part, observations}`` for one question."""
    kind = str(q.get("resolver_kind"))
    spec = q.get("resolver_spec") or {}
    out: dict[str, Any] = {"rows_by_part": {}, "searched_by_part": {}, "observations": []}
    if kind == "series_threshold":
        source, series = str(spec.get("source") or ""), str(spec.get("series") or "")
        on = _day(spec.get("on")) or as_of.date()
        floor = on - timedelta(days=SERIES_LOOKBACK_DAYS)
        out["observations"] = [dict(r) for r in await conn.fetch(OBSERVATIONS_SQL, source, series, as_of, floor)]
        out["searched_by_part"]["main"] = {
            "corpus": "observations (live rows, newest vintage recorded at or before the as-of)",
            "series_like": f"{source.lower()}.%.{series.lower()}", "valid_from_from": floor.isoformat(),
            "rows_matched": len(out["observations"]),
        }
        return out
    classes = await _source_classes(conn)
    for plan in search_plans(q, ctx):
        rows, searched = await _read_plan(conn, plan, classes, as_of)
        out["rows_by_part"][plan.part] = rows
        out["searched_by_part"][plan.part] = searched
    return out
