# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 6 L2 — the DIVERGENCE MAP read surface (7b-v).

``GET /api/v1/v3/layers/divergence`` — the newest ``layer_divergence`` receipt,
desk by desk, plus the newest FIRED finding each desk carries.

WHY A ROUTE OVER THE RECEIPT AND NOT OVER A TABLE
-------------------------------------------------
The unit writes no table (docs/LAYERS.md, "The divergence measure (L2)"): the
series and the receipt ride ``analyst_outputs.data``, and a run that fired
nothing is suppressed to trace-only so an idempotent re-run never repeats "no
gap change" in the feed. Which means the QUIET days — the overwhelming majority,
and the ones that prove the instrument is running rather than merely not
complaining — exist only in ``analyst_traces.output_payload``. A reader surface
that read ``analyst_outputs`` alone would show an empty page on exactly the days
the measure worked correctly. So this route reads the TRACE for the receipt and
the OUTPUT for the fires, and reports both.

WHAT IT REFUSES TO DO
---------------------
* **It computes no statistic.** Every pair row is passed through VERBATIM from
  the receipt — the series, the baselines, the ``no_fire_reason``, the excluded
  layers and the operator's own reason text. The route's whole arithmetic is
  turning the receipt's ``daily`` MAP into a day-ordered LIST so a caller need
  not re-sort dates to draw them. A number this route emitted that the handler
  did not compute would be a second instrument with no method version.
* **It never fills an absence with a zero.** A desk with no receipt is absent
  from ``desks`` and ``receipt_run_id`` is null — "no run yet", which is a
  different fact from "a run that measured nothing".
* **It never infers an aperture.** ``declared`` is whatever the handler read out
  of ``desk_apertures``: ``present`` / ``absent`` / ``unmeasured`` /
  ``undeclared``, each carrying the reason the operator wrote (blank where the
  vocabulary does not require one). Undeclared is NOT unmeasured.
* **It does not hide the audit gap.** ``classification_audit`` is the handler's
  own SEAMS #60 stamp, served on every response, because an un-audited layer map
  is a DIRECTIONAL error in every number below it and a reader must not be able
  to mistake one for an audited map.

REGISTRY-SLIM
-------------
This module ships in the REGISTRY image, which does not carry the analyst
runtime. It therefore may not import ``legba.data.analysts`` at all — not even
inside a function body, since deferring an import moves WHEN the graph is walked
and never how far. The three pairs of interest and the method's identity are
MIRRORED here and drift-guarded against the handler in
``tests/data_pkg/test_layers_divergence_api.py``. ``legba.data.layers._vocab``
is imported for real: it is a declared stdlib-only leaf, importable from the
slim image by construction (see its own module banner).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..layers._vocab import LAYER_VOCAB
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

_ROUTE = "/layers/divergence"

#: The analyst whose receipts this route reads. One id, one instrument.
ANALYST_ID: str = "layer_divergence"

#: The READER's own version stamp — bumped when the WIRE SHAPE below moves, so a
#: panel can tell "the keys changed" from "the statistic changed" (the handler's
#: ``METHOD_VERSION`` is passed through separately and answers the second).
LAYER_DIVERGENCE_READER_VERSION: str = "2026-09/p6-l2"

#: Mirrors ``layer_divergence.PAIRS_OF_INTEREST`` — id, the two layers, and what
#: the gap is ABOUT. NOT imported: the registry image stays slim. Drift-guarded.
PAIRS_OF_INTEREST: tuple[tuple[str, str, str, str], ...] = (
    (
        "regime_public_gap", "official", "social_digest",
        "the regime-public gap — what the state publishes against what the "
        "monitored channels carry",
    ),
    (
        "narrative_control", "domestic_press", "foreign_press",
        "narrative control — how much the domestic press says about this "
        "country against how much the outside press does",
    ),
    (
        "credibility_gap", "official", "public_data",
        "the credibility gap — state publication against structured "
        "third-party data on the same country",
    ),
)

#: The one sentence the panel header renders, served by the server rather than
#: copied into the bundle so the reader surface cannot drift from docs/LAYERS.md.
UNIT_SENTENCE: str = (
    "Per country, the same stack of source layers is counted per day and the "
    "finding is the CHANGE in that country's own layer-to-layer divergence "
    "against its own rolling baseline — never the raw gap, which every country "
    "has. A layer declared structurally absent contributes no count and says "
    "why, so a missing layer never reads as agreement."
)

#: Hard bound on the summary findings scanned for the per-desk fires. The unit
#: emits at most one summary a day and only when something fired, so 60 is two
#: months of fires; the cap exists so the query cannot grow with the table.
_MAX_FINDING_ROWS: int = 60

#: Default / maximum lookback for the fired-finding read, in days.
_DEFAULT_FIRED_DAYS: int = 30
_MAX_FIRED_DAYS: int = 365


# ---------------------------------------------------------------------------
# SQL — two bounded reads, both index-driven (live EXPLAIN in the lane report)
# ---------------------------------------------------------------------------

#: The newest receipt. ONE row carries every desk the run resolved, so this is
#: the whole map. Rides ``analyst_traces_analyst_idx (analyst_id,
#: run_started_at DESC)``.
_NEWEST_RECEIPT_SQL = """
SELECT run_id, run_started_at, output_payload
  FROM analyst_traces
 WHERE analyst_id = $1
   AND output_payload IS NOT NULL
 ORDER BY run_started_at DESC
 LIMIT 1
"""

#: The newest FIRED divergence per desk. The summary finding is one row for the
#: whole run (a META analyst writes no ``target_id``), so the desk lives inside
#: ``data.divergences[]`` and the DISTINCT ON walks the unnested array rather
#: than the column. The CASE guards a row whose ``divergences`` is not an array —
#: ``jsonb_array_elements`` would raise, and one malformed row must not take the
#: panel down.
_FIRED_PER_DESK_SQL = """
WITH recent AS (
    SELECT o.id, o.produced_at, o.data
      FROM analyst_outputs o
     WHERE o.analyst_id = $1
       AND o.kind = 'finding'
       AND o.superseded_by IS NULL
       AND o.produced_at >= now() - make_interval(days => $2)
     ORDER BY o.produced_at DESC
     LIMIT $3
)
SELECT DISTINCT ON (d->>'target_id')
       d->>'target_id'  AS target_id,
       r.id             AS finding_id,
       r.produced_at    AS produced_at,
       d->>'pair_id'    AS pair_id,
       d->>'direction'  AS direction,
       d->>'severity'   AS severity,
       d->>'day'        AS day,
       d->>'z'          AS z,
       d->>'thin'       AS thin
  FROM recent r
  CROSS JOIN LATERAL jsonb_array_elements(
      CASE WHEN jsonb_typeof(r.data->'divergences') = 'array'
           THEN r.data->'divergences' ELSE '[]'::jsonb END) AS d
 ORDER BY d->>'target_id', r.produced_at DESC
"""


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


class LayerDayOut(BaseModel):
    """One UTC day of one layer, as the fold counted it."""

    day: str
    #: Rows the scan saw before the wire fold.
    raw: int = 0
    #: What entered the ratio — ``raw`` minus the folded wire copies.
    kept: int = 0
    #: Copies folded away inside this (layer, day) bucket.
    folded: int = 0


class LayerOut(BaseModel):
    """One layer's declaration and its day-ordered counts for this desk."""

    #: ``present`` | ``absent`` | ``unmeasured`` | ``undeclared``.
    declared: str
    #: The operator's own words. Required by the table for ``absent``; blank for
    #: ``present`` and for ``undeclared`` (nobody wrote a row at all).
    reason: str = ""
    #: Sources the curated layer table files under this layer for this country.
    sources_mapped: int = 0
    #: Days in the window carrying fewer than ``thin_min_per_day`` folded items.
    #: Only counted for a ``present`` layer; ``null`` otherwise.
    thin_days: Optional[int] = None
    daily: list[LayerDayOut] = Field(default_factory=list)


class FiredOut(BaseModel):
    """The newest divergence this desk actually fired, from the finding feed."""

    finding_id: str
    produced_at: Optional[datetime] = None
    pair_id: str
    #: ``widening`` | ``narrowing`` — the handler's own word, never re-derived.
    direction: Optional[str] = None
    severity: Optional[str] = None
    #: The UTC day the two-day rule closed on.
    day: Optional[str] = None
    z: Optional[float] = None
    #: True when a side of the fired pair was under the thin floor that day.
    thin: Optional[bool] = None


class DeskOut(BaseModel):
    """One desk's slice of the receipt, plus its fire if it has one."""

    target_id: str
    country: str = ""
    map_version: str = ""
    sources_mapped: int = 0
    rows_scanned: int = 0
    rows_truncated: bool = False
    #: The aperture split as the handler wrote it: ``present`` (a list of
    #: layers) and ``absent`` / ``unmeasured`` / ``undeclared`` (lists of
    #: ``{layer, reason}``).
    aperture: dict[str, Any] = Field(default_factory=dict)
    #: Layers the map carries sources for that the aperture calls ``absent`` —
    #: the count that was suppressed. The map and the declaration disagreeing is
    #: itself worth reading, so it is reported rather than silently applied.
    counts_suppressed_by_aperture: dict[str, int] = Field(default_factory=dict)
    layers: dict[str, LayerOut] = Field(default_factory=dict)
    #: The receipt's pair rows, VERBATIM — ``pair_id``, ``layer_a``,
    #: ``layer_b``, ``evaluable``, ``no_fire_reason``, and then either
    #: ``excluded_layers`` (not evaluable) or ``series`` (the per-day counts,
    #: log-ratio, baseline and z). Nothing here is computed by this route.
    pairs: list[dict[str, Any]] = Field(default_factory=list)
    fired: Optional[FiredOut] = None


class PairDeclaredOut(BaseModel):
    """One of the three pairs of interest and what its gap is about."""

    pair_id: str
    layer_a: str
    layer_b: str
    meaning: str


class LayerDivergenceOut(BaseModel):
    """The divergence map: one receipt, every desk it resolved."""

    #: False when the read itself failed. An empty ``desks`` with
    #: ``measured=true`` and a null ``receipt_run_id`` means "no run yet".
    measured: bool = True
    generated_at: datetime
    reader_version: str = LAYER_DIVERGENCE_READER_VERSION
    unit_sentence: str = UNIT_SENTENCE

    #: The ONE day the run measured at (the receipt's own stamp), ISO date.
    as_of: Optional[str] = None
    receipt_run_id: Optional[str] = None
    run_started_at: Optional[datetime] = None

    #: The handler's instrument revision — bumped when the STATISTIC moves.
    method_version: Optional[str] = None
    #: The handler's payload-shape version — bumped when the receipt KEYS move.
    payload_schema: Optional[str] = None
    #: SEAMS #60, verbatim from the receipt. Always rendered.
    classification_audit: Optional[str] = None

    window_days: Optional[int] = None
    baseline_days: Optional[int] = None
    z_threshold: Optional[float] = None
    mad_floor: Optional[float] = None
    consecutive_days: Optional[int] = None
    thin_min_per_day: Optional[int] = None

    layer_vocab: list[str] = Field(default_factory=list)
    pairs_declared: list[PairDeclaredOut] = Field(default_factory=list)
    desks: list[DeskOut] = Field(default_factory=list)
    #: Desks the run was asked for and could not resolve, verbatim.
    desks_unresolved: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure projection (no DB — unit-tested directly)
# ---------------------------------------------------------------------------


def _jsonish(raw: Any) -> Any:
    """JSONB arrives as dict/list (asyncpg codec) or str — normalise."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _declared_pairs() -> list[PairDeclaredOut]:
    return [
        PairDeclaredOut(pair_id=p, layer_a=a, layer_b=b, meaning=m)
        for (p, a, b, m) in PAIRS_OF_INTEREST
    ]


def daily_series(daily: Any) -> list[LayerDayOut]:
    """The receipt's ``{day: {raw, kept, folded}}`` MAP as a day-ordered LIST.

    Ordering by the key is correct because the keys are ISO dates, which sort
    lexicographically as they sort chronologically. A day the receipt does not
    carry is NOT invented — it is simply absent from the list, so a caller
    drawing the window sees the window the run actually measured.
    """
    if not isinstance(daily, Mapping):
        return []
    out: list[LayerDayOut] = []
    for day in sorted(str(k) for k in daily.keys()):
        cell = daily.get(day)
        if not isinstance(cell, Mapping):
            continue
        out.append(
            LayerDayOut(
                day=str(day),
                raw=_int(cell.get("raw")),
                kept=_int(cell.get("kept")),
                folded=_int(cell.get("folded")),
            )
        )
    return out


def desk_from_receipt(
    target: Mapping[str, Any], fired: Mapping[str, FiredOut],
) -> DeskOut:
    """Project one receipt target row onto the wire shape.

    Everything except the ``daily`` map→list turn is a pass-through. The layer
    rows are emitted in ``LAYER_VOCAB`` order (the canonical six) followed by
    anything the receipt carried that the vocabulary does not know, so a
    vocabulary change lands visibly rather than silently dropping a layer.
    """
    raw_layers = target.get("layers")
    layers_in: Mapping[str, Any] = (
        raw_layers if isinstance(raw_layers, Mapping) else {}
    )
    ordered = [lay for lay in LAYER_VOCAB if lay in layers_in]
    ordered += [lay for lay in layers_in if lay not in LAYER_VOCAB]

    layers: dict[str, LayerOut] = {}
    for layer in ordered:
        row = layers_in.get(layer)
        if not isinstance(row, Mapping):
            continue
        declared = str(row.get("declared") or "undeclared")
        layers[layer] = LayerOut(
            declared=declared,
            reason=str(row.get("reason") or ""),
            sources_mapped=_int(row.get("sources_mapped")),
            # Only a `present` layer is counted at all, so a thin-day tally on
            # an excluded layer would be a statement about days nobody read.
            thin_days=(
                _int(row.get("thin_days")) if declared == "present" else None
            ),
            daily=daily_series(row.get("daily")),
        )

    pairs_raw = target.get("pairs")
    pairs = (
        [dict(p) for p in pairs_raw if isinstance(p, Mapping)]
        if isinstance(pairs_raw, list)
        else []
    )

    suppressed_raw = target.get("counts_suppressed_by_aperture")
    suppressed = (
        {str(k): _int(v) for k, v in suppressed_raw.items()}
        if isinstance(suppressed_raw, Mapping)
        else {}
    )

    target_id = str(target.get("target_id") or "")
    aperture = target.get("aperture")
    return DeskOut(
        target_id=target_id,
        country=str(target.get("country") or ""),
        map_version=str(target.get("map_version") or ""),
        sources_mapped=_int(target.get("sources_mapped")),
        rows_scanned=_int(target.get("rows_scanned")),
        rows_truncated=bool(target.get("rows_truncated")),
        aperture=dict(aperture) if isinstance(aperture, Mapping) else {},
        counts_suppressed_by_aperture=suppressed,
        layers=layers,
        pairs=pairs,
        fired=fired.get(target_id),
    )


def project_receipt(
    payload: Any,
    *,
    run_id: Any,
    run_started_at: Any,
    fired: Mapping[str, FiredOut],
    now: datetime | None = None,
) -> LayerDivergenceOut:
    """Turn one ``analyst_traces.output_payload`` into the whole response.

    A payload whose ``data`` is not a mapping yields an honest empty map with
    ``measured=true`` and a null ``as_of`` rather than a 500: the run existed,
    it just did not write a receipt this reader understands.
    """
    stamp = now or datetime.now(timezone.utc)
    envelope = _jsonish(payload)
    data = envelope.get("data") if isinstance(envelope, Mapping) else None
    if not isinstance(data, Mapping):
        data = {}

    targets = data.get("targets")
    desks = [
        desk_from_receipt(t, fired)
        for t in (targets if isinstance(targets, list) else [])
        if isinstance(t, Mapping)
    ]
    desks.sort(key=lambda d: (d.country, d.target_id))

    unresolved = data.get("targets_unresolved")
    warnings = data.get("warnings")
    return LayerDivergenceOut(
        measured=True,
        generated_at=stamp,
        as_of=(str(data["as_of"]) if data.get("as_of") else None),
        receipt_run_id=(str(run_id) if run_id is not None else None),
        run_started_at=run_started_at,
        method_version=(
            str(data["method_version"]) if data.get("method_version") else None
        ),
        payload_schema=(
            str(data["payload_schema"]) if data.get("payload_schema") else None
        ),
        classification_audit=(
            str(data["classification_audit"])
            if data.get("classification_audit")
            else None
        ),
        window_days=_int(data["window_days"]) if "window_days" in data else None,
        baseline_days=(
            _int(data["baseline_days"]) if "baseline_days" in data else None
        ),
        z_threshold=_opt_float(data.get("z_threshold")),
        mad_floor=_opt_float(data.get("mad_floor")),
        consecutive_days=(
            _int(data["consecutive_days"]) if "consecutive_days" in data else None
        ),
        thin_min_per_day=(
            _int(data["thin_min_per_day"]) if "thin_min_per_day" in data else None
        ),
        layer_vocab=list(LAYER_VOCAB),
        pairs_declared=_declared_pairs(),
        desks=desks,
        desks_unresolved=[
            dict(u)
            for u in (unresolved if isinstance(unresolved, list) else [])
            if isinstance(u, Mapping)
        ],
        warnings=[str(w) for w in (warnings if isinstance(warnings, list) else [])],
    )


def fired_by_desk(rows: list[Mapping[str, Any]]) -> dict[str, FiredOut]:
    """Index the DISTINCT-ON rows by ``target_id``.

    ``z`` and ``thin`` arrive as the JSON text they were stored as; an
    unparseable one becomes ``null`` rather than a fabricated number.
    """
    out: dict[str, FiredOut] = {}
    for row in rows:
        target_id = str(row.get("target_id") or "")
        if not target_id:
            continue
        thin_raw = row.get("thin")
        out[target_id] = FiredOut(
            finding_id=str(row.get("finding_id")),
            produced_at=row.get("produced_at"),
            pair_id=str(row.get("pair_id") or ""),
            direction=(str(row["direction"]) if row.get("direction") else None),
            severity=(str(row["severity"]) if row.get("severity") else None),
            day=(str(row["day"]) if row.get("day") else None),
            z=_opt_float(row.get("z")),
            thin=(None if thin_raw is None else str(thin_raw).lower() == "true"),
        )
    return out


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_layers_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["layers"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=LayerDivergenceOut)
    async def layer_divergence_map(
        fired_days: int = Query(
            default=_DEFAULT_FIRED_DAYS, ge=1, le=_MAX_FIRED_DAYS,
            description=(
                "How far back to look for each desk's most recent FIRED "
                "divergence. Does not affect the receipt, which is always the "
                "newest one."
            ),
        ),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> LayerDivergenceOut:
        """The divergence map: the newest receipt, desk by desk, with fires."""
        stamp = datetime.now(timezone.utc)
        try:
            async with deps_.descriptor_registry.pg.acquire() as conn:
                receipt = await conn.fetchrow(_NEWEST_RECEIPT_SQL, ANALYST_ID)
                fired_rows = await conn.fetch(
                    _FIRED_PER_DESK_SQL,
                    ANALYST_ID, fired_days, _MAX_FINDING_ROWS,
                )
        except Exception as exc:  # noqa: BLE001 — a polling panel never gets a 500
            logger.info("v3.layers.divergence.unavailable err=%s", exc)
            return LayerDivergenceOut(
                measured=False,
                generated_at=stamp,
                layer_vocab=list(LAYER_VOCAB),
                pairs_declared=_declared_pairs(),
            )

        if receipt is None:
            # No run yet. Distinct from a run that measured nothing: `desks` is
            # empty AND `receipt_run_id` is null, and the panel says so.
            return LayerDivergenceOut(
                measured=True,
                generated_at=stamp,
                layer_vocab=list(LAYER_VOCAB),
                pairs_declared=_declared_pairs(),
            )

        return project_receipt(
            receipt["output_payload"],
            run_id=receipt["run_id"],
            run_started_at=receipt["run_started_at"],
            fired=fired_by_desk([dict(r) for r in fired_rows]),
            now=stamp,
        )

    return router


__all__ = [
    "ANALYST_ID",
    "DeskOut",
    "FiredOut",
    "LAYER_DIVERGENCE_READER_VERSION",
    "LayerDayOut",
    "LayerDivergenceOut",
    "LayerOut",
    "PAIRS_OF_INTEREST",
    "PairDeclaredOut",
    "UNIT_SENTENCE",
    "build_layers_router",
    "daily_series",
    "desk_from_receipt",
    "fired_by_desk",
    "project_receipt",
]
