# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Labeled reference-set (gold) write + read surface (P2-T4).

Backs the per-bounded-unit CORRECTNESS scorer: Phase 2 measures each small
reasoning unit individually, and scoring "is the unit's read RIGHT?" needs a
labeled reference answer to compare against (distinct from faithfulness, which
asks "is the prose faithful to its cites?"). This router curates that gold set —
``unit_reference_labels`` (migration 0057) — one row per (unit, target) gold
answer, GROUNDED to the provenance it was drawn from (``canonical_source_ids``)
so a label is anchored to real substrate rows rather than free opinion.

Two endpoints, both bearer-gated, mounted under ``/api/v1`` so they resolve at:

  * ``POST /api/v1/eval/labels``  — insert one label → 201 with the stored row.
  * ``GET  /api/v1/eval/labels``  — read labels back, optionally filtered by
    ``unit_analyst_id`` and/or ``target_id`` (the scorer's per-unit lookup;
    supports >=10 labels for one unit).

REGISTRY-IMAGE SAFE (deploy hazard): the registry image is SLIM. This module
imports ONLY stdlib + fastapi + pydantic + ``.api`` (RegistryAPIDeps /
require_bearer) — never the deterministic-handler package or anything that
transitively pulls pycountry / networkx. Wiring convention mirrors
``journal_api.py`` (a small router via ``build_labels_router(deps)`` over the
shared deps bundle + the same ``require_bearer`` gate + the primary pg pool via
``deps.descriptor_registry.pg.acquire()``).
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class LabelIn(BaseModel):
    """A reference label to record for one bounded unit.

    ``canonical_source_ids`` GROUNDS the label to the substrate rows it was drawn
    from (provenance, not free opinion); each must be a UUID string. ``target_id``
    is optional (NULL for a meta / non-target unit). ``labeled_by`` defaults to
    the authenticated principal when omitted so a label is never anonymous."""

    unit_analyst_id: str = Field(min_length=1)
    reference_answer: str = Field(min_length=1)
    target_id: str | None = None
    canonical_source_ids: list[str] = Field(default_factory=list)
    labeled_by: str | None = None


class LabelOut(BaseModel):
    """One ``unit_reference_labels`` row as stored."""

    id: str
    unit_analyst_id: str
    target_id: str | None
    reference_answer: str
    canonical_source_ids: list[str] = Field(default_factory=list)
    labeled_by: str | None
    created_at: datetime


class LabelsOut(BaseModel):
    """``GET /eval/labels`` body — the matching labels, newest first."""

    labels: list[LabelOut]


class UnitEvalScore(BaseModel):
    """One bounded unit's eval scoreboard row (P2-T6) — read off the latest
    ``unit_correctness_scorer`` run (P2-T5). ``correctness_vs_reference`` is None
    (NOT a fabricated number) until the unit has scorable gold labels; the
    ``badge`` string is composed HERE, server-side, so the "no invented number"
    honesty contract is enforced in one place."""

    unit: str
    faithfulness: float | None = None
    correctness_vs_reference: float | None = None
    n_labeled: int = 0
    n_findings: int = 0
    status: str | None = None
    badge: str


class EvalScoresOut(BaseModel):
    """``GET /eval/scores`` body — per-unit eval scoreboard + when it was scored.

    ``scored_at`` is the latest scorer run's ``produced_at`` (None if the scorer
    has never run); ``units`` is empty in that case (honest — no invented rows)."""

    scored_at: datetime | None = None
    units: list[UnitEvalScore] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_source_ids(raw: list[str]) -> list[UUID]:
    """Coerce the grounding ids to ``UUID`` (the column is ``uuid[]``); reject a
    malformed id loudly so a label is never silently dropped or mis-grounded."""
    out: list[UUID] = []
    for s in raw:
        try:
            out.append(UUID(str(s)))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"canonical_source_ids must be UUIDs; got {s!r}",
            )
    return out


def _validate_limit(limit: int) -> int:
    if limit < 1 or limit > MAX_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"limit must be in [1, {MAX_LIMIT}]",
        )
    return limit


def _hydrate(row: object) -> LabelOut:
    return LabelOut(
        id=str(row["id"]),
        unit_analyst_id=row["unit_analyst_id"],
        target_id=row["target_id"],
        reference_answer=row["reference_answer"],
        canonical_source_ids=[str(x) for x in (row["canonical_source_ids"] or [])],
        labeled_by=row["labeled_by"],
        created_at=row["created_at"],
    )


_COLS = (
    "id, unit_analyst_id, target_id, reference_answer, "
    "canonical_source_ids, labeled_by, created_at"
)


def _as_float(x: object) -> float | None:
    """Coerce a JSON number to float, or None for a JSON null / non-number — so a
    missing score reads as honestly unmeasured, never 0.0."""
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x))
    except (ValueError, TypeError):
        return None


def _compose_badge(
    faithfulness: float | None,
    correctness: float | None,
    n_labeled: int,
) -> str:
    """The honest eval badge (P2-T6) — composed server-side so "no invented
    number" is enforced in ONE place. Examples:
      * labeled + scored : ``verified | faithfulness 0.90 | correctness 0.78 (n=12)``
      * unlabeled        : ``verified | faithfulness 0.45 | unmeasured (0 labels)``
      * never verified   : ``verified | unmeasured (0 labels)``
    ``verified`` denotes the unit runs the mandatory faithfulness-verify pass; the
    faithfulness figure is its measured score. Correctness is only shown when there
    are scorable gold labels — otherwise the badge SAYS it is unmeasured (with the
    label count) rather than inventing a number."""
    parts = ["verified"]
    if faithfulness is not None:
        parts.append(f"faithfulness {faithfulness:.2f}")
    if correctness is not None:
        parts.append(f"correctness {correctness:.2f} (n={n_labeled})")
    else:
        parts.append(f"unmeasured ({n_labeled} labels)")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_labels_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the gold-label write+read router bound to the registry deps.

    Mount under ``/api/v1`` so the paths resolve at ``/api/v1/eval/labels``. Both
    routes are bearer-gated and read/write the primary Postgres pool via
    ``deps.descriptor_registry.pg.acquire()`` — the same path the journal /
    substrate-reads routers use."""
    router = APIRouter(tags=["eval-labels"])

    @router.post(
        "/eval/labels",
        response_model=LabelOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_label(
        body: LabelIn,
        principal: str = Depends(require_bearer),
    ) -> LabelOut:
        source_ids = _validate_source_ids(body.canonical_source_ids)
        labeled_by = body.labeled_by or principal
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO unit_reference_labels
                    (unit_analyst_id, target_id, reference_answer,
                     canonical_source_ids, labeled_by)
                VALUES ($1, $2, $3, $4::uuid[], $5)
                RETURNING {_COLS}
                """,
                body.unit_analyst_id,
                body.target_id,
                body.reference_answer,
                source_ids,
                labeled_by,
            )
        return _hydrate(row)

    @router.get("/eval/labels", response_model=LabelsOut)
    async def list_labels(
        unit_analyst_id: str | None = Query(default=None),
        target_id: str | None = Query(default=None),
        limit: int = Query(default=DEFAULT_LIMIT),
        principal: str = Depends(require_bearer),
    ) -> LabelsOut:
        limit = _validate_limit(limit)
        args: list[object] = []
        where: list[str] = []
        if unit_analyst_id is not None:
            args.append(unit_analyst_id)
            where.append(f"unit_analyst_id = ${len(args)}")
        if target_id is not None:
            args.append(target_id)
            where.append(f"target_id = ${len(args)}")
        args.append(limit)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            f"SELECT {_COLS} FROM unit_reference_labels{clause} "
            f"ORDER BY created_at DESC, id DESC LIMIT ${len(args)}"
        )
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return LabelsOut(labels=[_hydrate(r) for r in rows])

    @router.get("/eval/scores", response_model=EvalScoresOut)
    async def eval_scores(
        unit_analyst_id: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> EvalScoresOut:
        """Per-unit eval scoreboard (P2-T6) read off the LATEST
        ``unit_correctness_scorer`` run (P2-T5) — faithfulness (measured) +
        correctness-vs-reference (None until scorable gold labels exist) + an
        honest ``badge`` per unit. No scorer run yet → empty units (no invented
        rows). Optionally filter to one ``unit_analyst_id``."""
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT data, produced_at
                FROM analyst_outputs
                WHERE analyst_id = 'unit_correctness_scorer'
                  AND kind = 'finding'
                  AND superseded_by IS NULL
                ORDER BY produced_at DESC, id DESC
                LIMIT 1
                """
            )
        if row is None:
            return EvalScoresOut(scored_at=None, units=[])
        data = row["data"]
        if isinstance(data, str):
            import json as _json

            try:
                data = _json.loads(data)
            except ValueError:
                data = {}
        # The scorer's payload nests under data['data'] (the FindingPayload
        # envelope); 'units' maps unit_id -> the per-unit record.
        nested = (data or {}).get("data") if isinstance(data, dict) else None
        units_map = (nested or {}).get("units") if isinstance(nested, dict) else None
        out: list[UnitEvalScore] = []
        for unit_id, rec in sorted((units_map or {}).items()):
            if unit_analyst_id is not None and unit_id != unit_analyst_id:
                continue
            if not isinstance(rec, dict):
                continue
            faith = _as_float(rec.get("faithfulness"))
            corr = _as_float(rec.get("correctness_vs_reference"))
            n_labeled = int(rec.get("n_labeled") or 0)
            out.append(
                UnitEvalScore(
                    unit=str(rec.get("unit", unit_id)),
                    faithfulness=faith,
                    correctness_vs_reference=corr,
                    n_labeled=n_labeled,
                    n_findings=int(rec.get("n_findings") or 0),
                    status=rec.get("status"),
                    badge=_compose_badge(faith, corr, n_labeled),
                )
            )
        return EvalScoresOut(scored_at=row["produced_at"], units=out)

    return router
