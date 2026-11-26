# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The map's two missing reads (U-4 globe): series at their place, and
incidents at sea.

``GET /v3/map/series?collection_id=live:<source>&series_id=<id>``
    The latest held value of ONE live series for every subject of one live
    collection, with its 30-day baseline, its deviation from that baseline,
    whether the latest day was revised, its as-of, and the last 30 values. It
    is what the map's "% of 30-day baseline" glyph draws (layout design §4
    item 4). The read picks ONE row per (subject, day) — the latest vintage by
    ``record_time`` — in one set-based statement on the leading columns of
    ``observations_subject_period_idx``, with the partition-pruning
    ``valid_from`` bound the observations reads always carry
    (``runtime/_observations_read.py``). Only ``live:`` collections are served:
    a curated HISTORY holding is read through its own approved readers and is
    never put on a map by this route.

    It carries NO coordinates: an observation's subject is the provider's own
    key (for PortWatch, ``chokepoint4``). The UI places a subject only where a
    gazetteer it ships names that key; an unplaced subject is listed, not
    guessed at.

``GET /v3/map/at_sea``
    Canonical signals the geocoder filed AT SEA (the ``at_sea`` tag and
    ``payload.geo.at_sea``): the point, the place name, the coastal states it
    lies off (``payload.geo.inferred``, a relation and never a filing), the
    publisher's instant (valid time) and the fetch instant (record time).
    Source-general: any source whose rows the geocoder files at sea appears.
    Measured live 2026-11-14: 10 ms (GIN ``signals_tags_gin`` bitmap scan,
    31 rows).

``GET /v3/map/subject_reads?analyst_id=<producer>&limit=``
    The subject reads (a producer's findings, newest first) at VERDICT weight:
    the subject, the instant, the stored and the effective confidence (the
    critic fold — ``min(confidence, critic score)``, and the structural score
    too when the structural gate is on, exactly as ``GET /findings`` computes
    it), the verification block reduced to its scalar leaves, and the citation
    count. It is what the globe's blanks, the Wall's band grid and the rail's
    confidence chips need, and none of the assembly's quoted blocks: a
    200-row page that weighs ~23 MB at the default ``/findings`` weight
    (2026-11-14 live) is a few tens of KB here. The same two critique folds
    as the full read (``substrate_reads_folds``), so the numbers cannot
    disagree.

Read-only; the default ``read`` role. Fully defensive: a query failure is an
empty answer at HTTP 200 with ``available: false``, so "nothing there" and
"could not be read" stay two different answers.

Registry-slim: stdlib + fastapi + pydantic + ``.api``. Nothing from
``legba.data.analysts`` or ``legba.runtime``.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..provenance.verify import structural_verify_gate_enabled
from ..signal_slice_scope import CANONICAL_ONLY_SQL
from .api import RegistryAPIDeps, require_bearer
from .substrate_reads_folds import CRITIC_SCORE_CTE, STRUCTURAL_BADGE_CTE

logger = logging.getLogger(__name__)

MAP_LAYERS_ROUTE_VERSION = "2026-11/u4"

#: The most subjects one series read returns (the map's hard cap per layer).
MAX_SUBJECTS = 250
#: The most values per subject (the sparkline and the baseline window).
MAX_DAYS = 90
#: The baseline window, days before the latest held day.
BASELINE_DAYS = 30
#: The most incidents one at-sea read returns.
MAX_AT_SEA = 500

_CANONICAL_ONLY_SQL = CANONICAL_ONLY_SQL  # the one spelling (data/signal_slice_scope.py)

# One row per (subject, day): the latest vintage, plus how many vintages the
# day has (a count > 1 is a revision). The window is anchored on the newest
# held day of the series, so a provider four days behind still answers.
_SERIES_SQL = """
    WITH newest AS (
        SELECT max(valid_from) AS d
          FROM observations
         WHERE collection_id = $1 AND series_id = $2
    ),
    held AS (
        SELECT o.subject, o.valid_from, o.value, o.unit, o.record_time,
               o.provenance,
               count(*) OVER (PARTITION BY o.subject, o.valid_from) AS vintages,
               row_number() OVER (PARTITION BY o.subject, o.valid_from
                                  ORDER BY o.record_time DESC) AS rn
          FROM observations o, newest
         WHERE o.collection_id = $1
           AND o.series_id = $2
           AND newest.d IS NOT NULL
           AND o.valid_from >= newest.d - $3::int
           AND o.valid_from <= newest.d
    )
    SELECT subject, valid_from, value, unit, record_time, provenance, vintages
      FROM held
     WHERE rn = 1
     ORDER BY subject, valid_from
"""

_AT_SEA_SQL = f"""
    SELECT id, source_id, fetched_at, payload->'geo' AS geo,
           payload->>'title' AS title, payload->>'published_at' AS published_at
      FROM signals
     WHERE tags @> ARRAY['at_sea']::text[]
       AND {_CANONICAL_ONLY_SQL}
       AND fetched_at >= $1
     ORDER BY fetched_at DESC, id DESC
     LIMIT $2
"""


#: The verification leaves a verdict reads (``lib/verdictModel.ts``); the
#: per-claim arrays (``claim_verdicts``, ``unsupported_spans``, …) are dropped.
VERIFICATION_LEAVES: tuple[str, ...] = (
    "faithfulness_score", "judge_status", "confidence_ceiling", "overall_score",
    "score_state", "unverified_reason", "judge_chain",
)
MAX_SUBJECT_READS = 500

_SUBJECT_READS_SQL = f"""
    WITH f AS MATERIALIZED (
        SELECT f.id, f.title, f.target_id, f.analyst_id, f.produced_at,
               f.confidence,
               jsonb_array_length(CASE
                   WHEN jsonb_typeof(f.data->'data'->'citations') = 'array' THEN f.data->'data'->'citations'
                   WHEN jsonb_typeof(f.data->'citations') = 'array' THEN f.data->'citations'
                   ELSE '[]'::jsonb END) AS citation_count
          FROM analyst_outputs f
         WHERE f.kind = 'finding' AND f.analyst_id = $1
         ORDER BY f.produced_at DESC, f.id DESC
         LIMIT $2
    ), {CRITIC_SCORE_CTE}, {STRUCTURAL_BADGE_CTE}
    SELECT f.id, f.title, f.target_id, f.analyst_id, f.produced_at,
           f.confidence, f.citation_count,
           c.critic_score, c.verification,
           s.structural_score, s.structural_verification
      FROM f
      LEFT JOIN c ON c.fid = f.id::text
      LEFT JOIN s ON s.fid = f.id::text
     ORDER BY f.produced_at DESC, f.id DESC
"""


class SubjectRead(BaseModel):
    id: str
    title: str = ""
    target_id: Optional[str] = None
    analyst_id: Optional[str] = None
    produced_at: str
    confidence: float
    effective_confidence: float
    critic_score: Optional[float] = None
    verification: Optional[dict[str, Any]] = None
    citation_count: int = 0


class SubjectReadsOut(BaseModel):
    version: str = MAP_LAYERS_ROUTE_VERSION
    available: bool = True
    analyst_id: str
    data: list[SubjectRead] = Field(default_factory=list)


def fold_subject_read(row: Mapping[str, Any], *, structural_gate: bool) -> SubjectRead:
    """One row → a verdict-weight read, with ``GET /findings``' own fold:
    effective = min(confidence, critic score), and the structural score too
    when the structural gate is on; the faithfulness block, else the
    structural one, reduced to its scalar leaves."""
    confidence = _num(row.get("confidence")) or 0.0
    critic = _num(row.get("critic_score"))
    effective = min(confidence, critic) if critic is not None else confidence
    if structural_gate:
        sv = _num(row.get("structural_score"))
        if sv is not None:
            effective = min(effective, sv)
    block = _json(row.get("verification"))
    if not isinstance(block, dict):
        block = _json(row.get("structural_verification"))
    verification = {k: block[k] for k in VERIFICATION_LEAVES if k in block} if isinstance(block, dict) else None
    return SubjectRead(
        id=str(row.get("id")),
        title=str(row.get("title") or ""),
        target_id=row.get("target_id"),
        analyst_id=row.get("analyst_id"),
        produced_at=_iso(row.get("produced_at")) or "",
        confidence=confidence,
        effective_confidence=effective,
        critic_score=critic,
        verification=verification,
        citation_count=int(row.get("citation_count") or 0),
    )


class SeriesValue(BaseModel):
    d: str
    v: float


class SeriesAtPlace(BaseModel):
    subject: str
    #: The provider's own name for the subject, where its rows carry one.
    name: Optional[str] = None
    latest_day: str
    latest: float
    #: Mean of the held days in the 30 before ``latest_day`` (None when none).
    baseline_30d: Optional[float] = None
    baseline_days: int = 0
    #: ``latest`` as a percentage of the baseline (None without a baseline, or
    #: with one too thin to divide by: see ``_baseline_holds``).
    pct_of_baseline: Optional[float] = None
    #: The latest day has more than one vintage.
    revised: bool = False
    #: When the latest value was recorded by the provider.
    recorded_at: Optional[str] = None
    values: list[SeriesValue] = Field(default_factory=list)


class SeriesOut(BaseModel):
    version: str = MAP_LAYERS_ROUTE_VERSION
    available: bool = True
    collection_id: str
    series_id: str
    unit: Optional[str] = None
    #: The newest ``record_time`` among the rows read.
    as_of: Optional[str] = None
    total_subjects: int = 0
    subjects: list[SeriesAtPlace] = Field(default_factory=list)


class InferredCoast(BaseModel):
    iso2: str
    distance_km: Optional[float] = None


class AtSeaIncident(BaseModel):
    id: str
    source_id: Optional[str] = None
    title: str = ""
    lat: float
    lon: float
    location_name: Optional[str] = None
    inferred: list[InferredCoast] = Field(default_factory=list)
    #: The publisher's instant (valid time); None when the row carries none.
    published_at: Optional[str] = None
    #: When we fetched it (record time).
    fetched_at: str


class AtSeaOut(BaseModel):
    version: str = MAP_LAYERS_ROUTE_VERSION
    available: bool = True
    since: str
    total: int = 0
    truncated: bool = False
    incidents: list[AtSeaIncident] = Field(default_factory=list)


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        f = float(v)
        return f if f == f else None  # NaN is not a number here
    return None


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v


def _baseline_holds(values: list[float]) -> bool:
    """Whether a baseline is one a percentage can stand on: a positive mean
    with at least half its days above zero. A window of mostly empty days (a
    seasonal lane, a provider that reports nothing on quiet days) has a mean
    near zero, so the next ordinary day reads as a thousand per cent and an
    empty one as zero: noise drawn as data. Such a subject gets no percentage
    and the map says it has no baseline."""
    if not values or sum(values) <= 0:
        return False
    nonzero = sum(1 for v in values if v > 0)
    return nonzero * 2 >= len(values)


def fold_series(rows: Iterable[Mapping[str, Any]], *, keep_days: int = BASELINE_DAYS) -> list[SeriesAtPlace]:
    """Rows (one per subject and day, oldest first) → one entry per subject.

    The baseline is the mean of the held days STRICTLY BEFORE the latest day
    and within :data:`BASELINE_DAYS` of it; a subject with no earlier day has
    no baseline and no percentage (never a 100 % it did not earn), and neither
    has one whose earlier days are mostly empty (:func:`_baseline_holds`)."""
    by_subject: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        s = r.get("subject")
        if isinstance(s, str) and s and _num(r.get("value")) is not None and isinstance(r.get("valid_from"), date):
            by_subject.setdefault(s, []).append(r)
    out: list[SeriesAtPlace] = []
    for subject, rs in sorted(by_subject.items()):
        rs = sorted(rs, key=lambda r: r["valid_from"])
        last = rs[-1]
        last_day: date = last["valid_from"]
        lo = last_day - timedelta(days=BASELINE_DAYS)
        base = [_num(r["value"]) for r in rs[:-1] if r["valid_from"] >= lo]
        base_vals = [b for b in base if b is not None]
        baseline = sum(base_vals) / len(base_vals) if base_vals else None
        latest = _num(last["value"]) or 0.0
        pct = (latest / baseline * 100.0) if baseline and _baseline_holds(base_vals) else None
        prov = _json(last.get("provenance")) or {}
        name = prov.get("portname") or prov.get("name") if isinstance(prov, dict) else None
        out.append(SeriesAtPlace(
            subject=subject,
            name=name if isinstance(name, str) else None,
            latest_day=last_day.isoformat(),
            latest=latest,
            baseline_30d=round(baseline, 3) if baseline is not None else None,
            baseline_days=len(base_vals),
            pct_of_baseline=round(pct, 1) if pct is not None else None,
            revised=int(last.get("vintages") or 1) > 1,
            recorded_at=_iso(last.get("record_time")),
            values=[SeriesValue(d=r["valid_from"].isoformat(), v=_num(r["value"]) or 0.0) for r in rs[-keep_days:]],
        ))
    return out


def fold_at_sea(rows: Iterable[Mapping[str, Any]]) -> list[AtSeaIncident]:
    """Rows → incidents; a row without a usable point is dropped (it cannot be
    drawn, and the map never invents one)."""
    out: list[AtSeaIncident] = []
    for r in rows:
        geo = _json(r.get("geo"))
        if not isinstance(geo, dict) or geo.get("at_sea") is not True:
            continue
        lat, lon = _num(geo.get("lat")), _num(geo.get("lon"))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        inferred = []
        for e in geo.get("inferred") or []:
            if isinstance(e, dict) and isinstance(e.get("iso2"), str) and e["iso2"]:
                inferred.append(InferredCoast(iso2=e["iso2"], distance_km=_num(e.get("distance_km"))))
        loc = geo.get("location_name")
        out.append(AtSeaIncident(
            id=str(r.get("id")),
            source_id=r.get("source_id"),
            title=str(r.get("title") or ""),
            lat=lat, lon=lon,
            location_name=loc if isinstance(loc, str) else None,
            inferred=inferred,
            published_at=r.get("published_at") or None,
            fetched_at=_iso(r.get("fetched_at")) or "",
        ))
    return out


def build_map_layers_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["map"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get("/map/series", response_model=SeriesOut)
    async def map_series(
        request: Request,
        collection_id: str = Query(..., min_length=6, max_length=200, pattern=r"^live:[A-Za-z0-9_.:-]+$"),
        series_id: str = Query(..., min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$"),
        days: int = Query(default=BASELINE_DAYS, ge=1, le=MAX_DAYS),
        principal: str = Depends(require_bearer),
    ) -> SeriesOut:
        d = _get_deps(request)
        window = max(days, BASELINE_DAYS)
        try:
            async with d.descriptor_registry.pg.acquire() as conn:
                rows = await conn.fetch(_SERIES_SQL, collection_id, series_id, window)
        except Exception as exc:  # noqa: BLE001 — degrade, HTTP 200
            logger.info("v3.map.series.unavailable err_class=%s", type(exc).__name__)
            return SeriesOut(available=False, collection_id=collection_id, series_id=series_id)
        rows = [dict(r) for r in rows]
        subjects = fold_series(rows, keep_days=days)
        unit = next((r.get("unit") for r in rows if r.get("unit")), None)
        rec = [r.get("record_time") for r in rows if r.get("record_time") is not None]
        return SeriesOut(
            collection_id=collection_id, series_id=series_id, unit=unit,
            as_of=_iso(max(rec)) if rec else None,
            total_subjects=len(subjects),
            subjects=subjects[:MAX_SUBJECTS],
        )

    @router.get("/map/subject_reads", response_model=SubjectReadsOut)
    async def map_subject_reads(
        request: Request,
        analyst_id: str = Query(..., min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$"),
        limit: int = Query(default=200, ge=1, le=MAX_SUBJECT_READS),
        principal: str = Depends(require_bearer),
    ) -> SubjectReadsOut:
        d = _get_deps(request)
        try:
            async with d.descriptor_registry.pg.acquire() as conn:
                rows = await conn.fetch(_SUBJECT_READS_SQL, analyst_id, limit)
        except Exception as exc:  # noqa: BLE001 — degrade, HTTP 200
            logger.info("v3.map.subject_reads.unavailable err_class=%s", type(exc).__name__)
            return SubjectReadsOut(available=False, analyst_id=analyst_id)
        gate = structural_verify_gate_enabled()
        return SubjectReadsOut(analyst_id=analyst_id, data=[fold_subject_read(dict(r), structural_gate=gate) for r in rows])

    @router.get("/map/at_sea", response_model=AtSeaOut)
    async def map_at_sea(
        request: Request,
        days: int = Query(default=30, ge=1, le=90),
        limit: int = Query(default=200, ge=1, le=MAX_AT_SEA),
        principal: str = Depends(require_bearer),
    ) -> AtSeaOut:
        d = _get_deps(request)
        since = datetime.now(timezone.utc) - timedelta(days=days)
        try:
            async with d.descriptor_registry.pg.acquire() as conn:
                rows = await conn.fetch(_AT_SEA_SQL, since, limit + 1)
        except Exception as exc:  # noqa: BLE001 — degrade, HTTP 200
            logger.info("v3.map.at_sea.unavailable err_class=%s", type(exc).__name__)
            return AtSeaOut(available=False, since=since.isoformat())
        rows = [dict(r) for r in rows]
        incidents = fold_at_sea(rows[:limit])
        return AtSeaOut(since=since.isoformat(), total=len(incidents), truncated=len(rows) > limit, incidents=incidents)

    return router
