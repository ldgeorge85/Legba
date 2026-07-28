# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Correctness gold-set labeling loop (P2-5) — the weekly worksheet surface.

The correctness-vs-reference gold set was n≈1 because labeling had no cheap
surface. The operator agreed to label a handful of findings per week IF one
exists; this router is that surface:

  * ``GET  /eval/goldset/worksheet`` — this ISO week's stratified sample of
    verified head findings (title + claims prose + citations + current label
    state per item). The sample is deterministic (``goldset_sampling``, seeded
    by the ISO week) and PINNED on first read (``goldset_week_samples``) so the
    same week always shows the same items even as the substrate churns.
  * ``POST /eval/goldset/label`` — upsert ONE operator verdict for a finding
    (``correct | partially_correct | incorrect | unresolvable`` + optional
    rationale). The server snapshots the finding's title/claims/citations AT
    label time into ``correctness_labels.finding_snapshot`` so a later
    supersession can never orphan the judgment. Labeling is not restricted to
    the week's sample — any finding id may be judged (the E4a worksheet
    spirit: the sampler proposes, the human disposes).

Labels flow into the eval scoreboard: ``labels_api`` ``GET /eval/scores``
overlays a per-unit operator-correctness aggregate (its own keys + badge
segment, n growing live with every labeled row) beside the deterministic
source-overlap correctness — segregated, never pooled.

Mount under ``/api/v1/v3`` (beside ``v3_api`` / ``since_api``) so the paths
resolve at ``/api/v1/v3/eval/goldset/*``.

REGISTRY-IMAGE SAFE (deploy hazard): stdlib + fastapi + pydantic + ``.api`` +
``.goldset_sampling`` (stdlib-only) — never the deterministic-handler package
or anything that transitively pulls pycountry / networkx. Wiring convention
mirrors ``labels_api.py`` (router factory over the shared deps bundle + the
same ``require_bearer`` gate + the primary pg pool via
``deps.descriptor_registry.pg.acquire()``).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer
from .goldset_sampling import (
    DEFAULT_SAMPLE_SIZE,
    DEFAULT_UNITS,
    Candidate,
    iso_week_key,
    next_week_start_utc,
    select_weekly_sample,
    week_start_utc,
)

logger = logging.getLogger(__name__)

#: How far back a verified head finding is still a labeling candidate.
DEFAULT_LOOKBACK_DAYS = 28

GoldsetLabel = Literal["correct", "partially_correct", "incorrect", "unresolvable"]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class LabelState(BaseModel):
    """One stored operator verdict (a ``correctness_labels`` row)."""

    id: str
    finding_id: str
    unit_analyst_id: str
    target_id: str | None = None
    label: GoldsetLabel
    rationale: str | None = None
    labeled_by: str | None = None
    labeled_at: datetime
    created_at: datetime


class WorksheetItem(BaseModel):
    """One sampled finding, ready to read + judge.

    ``data`` is the finding's full JSONB envelope — the UI's reading kit
    (``extractCitations`` → ``CitedProse``) resolves the ``[N]`` markers from
    its nested ``data.citations`` exactly as the Findings panels do.
    ``superseded`` is honest state, not a filter: a finding superseded after
    the week's pin stays visible (the operator judges the snapshot-in-hand or
    marks it unresolvable).
    """

    finding_id: str
    unit: str
    target_id: str | None = None
    title: str
    body: str
    data: dict[str, Any] = Field(default_factory=dict)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    faithfulness: float | None = None
    produced_at: datetime
    superseded: bool = False
    label: LabelState | None = None


class WorksheetOut(BaseModel):
    """``GET /eval/goldset/worksheet`` body — this week's sample + progress.

    ``sample_size`` counts the PINNED membership; ``items`` may be shorter only
    if a pinned finding row has vanished from the substrate entirely.
    ``all_labeled`` + ``next_sample_at`` back the UI's honest empty state
    ("all labeled — next sample Monday")."""

    week: str
    week_started_at: datetime
    next_sample_at: datetime
    sample_size: int
    labeled_count: int
    all_labeled: bool
    items: list[WorksheetItem] = Field(default_factory=list)


class LabelIn(BaseModel):
    """One verdict to upsert. ``labeled_by`` defaults to the authenticated
    principal so a label is never anonymous."""

    finding_id: str = Field(min_length=1)
    label: GoldsetLabel
    rationale: str | None = None
    labeled_by: str | None = None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Candidate pull — verified-only (INNER LATERAL: a finding with no
# 'Faithfulness verify%' critique is NOT a candidate; unverified prose is not
# product and judging it teaches nothing about the verified plane), head
# findings (superseded_by IS NULL) from the bounded units, recent window.
# The critique join shape mirrors scorecard_banding._GATHER_SQL.
_CANDIDATES_SQL = """
    SELECT f.id::text        AS finding_id,
           f.analyst_id      AS unit,
           f.target_id       AS target_id,
           v.faithfulness_score AS faithfulness
      FROM analyst_outputs f
      JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE f.kind = 'finding'
       AND f.analyst_id = ANY($1::text[])
       AND f.superseded_by IS NULL
       AND f.produced_at > NOW() - make_interval(days => $2)
"""

# Hydration of the pinned sample (LEFT critique join here: a pinned item whose
# critique vanished must still render — the pin is the membership truth).
_HYDRATE_SQL = """
    SELECT f.id::text    AS finding_id,
           f.analyst_id  AS unit,
           f.target_id   AS target_id,
           f.title       AS title,
           f.body        AS body,
           f.data        AS data,
           f.produced_at AS produced_at,
           (f.superseded_by IS NOT NULL) AS superseded,
           v.faithfulness_score AS faithfulness
      FROM analyst_outputs f
      LEFT JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE f.id = ANY($1::uuid[])
"""

_LABEL_COLS = (
    "id::text AS id, finding_id::text AS finding_id, unit_analyst_id, "
    "target_id, label, rationale, labeled_by, labeled_at, created_at"
)

_UPSERT_LABEL_SQL = f"""
    INSERT INTO correctness_labels
        (finding_id, unit_analyst_id, target_id, label, rationale,
         labeled_by, finding_snapshot)
    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
    ON CONFLICT (finding_id) DO UPDATE SET
        label            = EXCLUDED.label,
        rationale        = EXCLUDED.rationale,
        labeled_by       = EXCLUDED.labeled_by,
        labeled_at       = NOW(),
        finding_snapshot = EXCLUDED.finding_snapshot
    RETURNING {_LABEL_COLS}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_jsonb(raw: Any) -> dict[str, Any]:
    """The finding ``data`` column as a dict (asyncpg may hand back str)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _citations_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The resolved citation entries (``data.data.citations``) — the same
    nesting the UI's ``extractCitations`` and the correctness scorer read."""
    nested = data.get("data")
    cites = nested.get("citations") if isinstance(nested, dict) else None
    return [c for c in cites if isinstance(c, dict)] if isinstance(cites, list) else []


def _label_state(row: Any) -> LabelState:
    return LabelState(
        id=row["id"],
        finding_id=row["finding_id"],
        unit_analyst_id=row["unit_analyst_id"],
        target_id=row["target_id"],
        label=row["label"],
        rationale=row["rationale"],
        labeled_by=row["labeled_by"],
        labeled_at=row["labeled_at"],
        created_at=row["created_at"],
    )


def _coerce_finding_uuid(finding_id: str) -> UUID:
    try:
        return UUID(str(finding_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"finding_id must be a UUID; got {finding_id!r}",
        )


async def _ensure_week_pinned(
    conn: Any, week: str, week_started_at: datetime, sample_size: int, lookback_days: int
) -> list[Any]:
    """Return the week's pinned sample rows, computing + pinning on first read.

    The sampler is deterministic, so the concurrent-first-read race resolves to
    the identical set on both sides; ON CONFLICT DO NOTHING + reread converges.
    """
    rows = await conn.fetch(
        "SELECT finding_id::text AS finding_id, rank, unit_analyst_id "
        "FROM goldset_week_samples WHERE week = $1 ORDER BY rank",
        week,
    )
    if rows:
        return rows

    # Exclusion — findings first labeled BEFORE this week never re-enter.
    labeled = await conn.fetch(
        "SELECT finding_id::text AS finding_id FROM correctness_labels "
        "WHERE created_at < $1",
        week_started_at,
    )
    exclude = {r["finding_id"] for r in labeled}

    cand_rows = await conn.fetch(_CANDIDATES_SQL, list(DEFAULT_UNITS), lookback_days)
    candidates = [
        Candidate(
            finding_id=r["finding_id"],
            unit=r["unit"],
            target_id=r["target_id"],
            faithfulness=r["faithfulness"],
        )
        for r in cand_rows
    ]
    sample = select_weekly_sample(
        candidates, week=week, sample_size=sample_size, exclude=exclude
    )
    for s in sample:
        await conn.execute(
            "INSERT INTO goldset_week_samples (week, finding_id, rank, unit_analyst_id) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (week, finding_id) DO NOTHING",
            week,
            UUID(s.finding_id),
            s.rank,
            s.unit,
        )
    return await conn.fetch(
        "SELECT finding_id::text AS finding_id, rank, unit_analyst_id "
        "FROM goldset_week_samples WHERE week = $1 ORDER BY rank",
        week,
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_goldset_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the gold-set worksheet router bound to the registry deps.

    Mount under ``/api/v1/v3`` so the paths resolve at
    ``/api/v1/v3/eval/goldset/worksheet`` + ``/api/v1/v3/eval/goldset/label``.
    Both routes are bearer-gated on the primary Postgres pool — the labels_api
    conventions."""
    router = APIRouter(tags=["eval-goldset"])

    @router.get("/eval/goldset/worksheet", response_model=WorksheetOut)
    async def goldset_worksheet(
        lookback_days: int = Query(default=DEFAULT_LOOKBACK_DAYS, ge=1, le=365),
        principal: str = Depends(require_bearer),
    ) -> WorksheetOut:
        """This ISO week's labeling worksheet — pinned stratified sample with
        each finding's title, claims prose, citations, faithfulness, and its
        current label state. An empty week (no verified candidates at all) is a
        first-class honest state, not a 404."""
        now = datetime.now(timezone.utc)
        week = iso_week_key(now.date())
        week_started_at = week_start_utc(now.date())
        next_sample_at = next_week_start_utc(now.date())

        async with deps.descriptor_registry.pg.acquire() as conn:
            pinned = await _ensure_week_pinned(
                conn, week, week_started_at, DEFAULT_SAMPLE_SIZE, lookback_days
            )
            ids = [UUID(r["finding_id"]) for r in pinned]
            finding_rows = await conn.fetch(_HYDRATE_SQL, ids) if ids else []
            label_rows = (
                await conn.fetch(
                    f"SELECT {_LABEL_COLS} FROM correctness_labels "
                    "WHERE finding_id = ANY($1::uuid[])",
                    ids,
                )
                if ids
                else []
            )

        by_id = {r["finding_id"]: r for r in finding_rows}
        labels_by_id = {r["finding_id"]: _label_state(r) for r in label_rows}

        items: list[WorksheetItem] = []
        for r in pinned:
            f = by_id.get(r["finding_id"])
            if f is None:
                # A pinned finding hard-deleted from the substrate — the pin
                # stays (membership truth) but there is nothing to render.
                logger.warning(
                    "goldset.worksheet.pinned_finding_missing week=%s id=%s",
                    week,
                    r["finding_id"],
                )
                continue
            data = _parse_jsonb(f["data"])
            items.append(
                WorksheetItem(
                    finding_id=f["finding_id"],
                    unit=f["unit"],
                    target_id=f["target_id"],
                    title=f["title"],
                    body=f["body"],
                    data=data,
                    citations=_citations_of(data),
                    faithfulness=f["faithfulness"],
                    produced_at=f["produced_at"],
                    superseded=bool(f["superseded"]),
                    label=labels_by_id.get(f["finding_id"]),
                )
            )

        labeled_count = sum(1 for it in items if it.label is not None)
        return WorksheetOut(
            week=week,
            week_started_at=week_started_at,
            next_sample_at=next_sample_at,
            sample_size=len(pinned),
            labeled_count=labeled_count,
            all_labeled=bool(items) and labeled_count == len(items),
            items=items,
        )

    @router.post("/eval/goldset/label", response_model=LabelState)
    async def goldset_label(
        body: LabelIn,
        principal: str = Depends(require_bearer),
    ) -> LabelState:
        """Upsert ONE operator verdict for a finding (one verdict per finding,
        latest wins; ``created_at`` keeps the first-label time for weekly
        exclusion). The finding's title/claims/citations are snapshotted HERE,
        at label time, so supersession can never orphan the judgment."""
        fid = _coerce_finding_uuid(body.finding_id)
        labeled_by = body.labeled_by or principal
        async with deps.descriptor_registry.pg.acquire() as conn:
            f = await conn.fetchrow(
                "SELECT id::text AS finding_id, analyst_id, target_id, title, "
                "body, data, produced_at, superseded_by::text AS superseded_by "
                "FROM analyst_outputs WHERE id = $1 AND kind = 'finding'",
                fid,
            )
            if f is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"finding {body.finding_id} not found",
                )
            data = _parse_jsonb(f["data"])
            snapshot = {
                "title": f["title"],
                "body": f["body"],
                "citations": _citations_of(data),
                "unit_analyst_id": f["analyst_id"],
                "target_id": f["target_id"],
                "produced_at": (
                    f["produced_at"].isoformat()
                    if hasattr(f["produced_at"], "isoformat")
                    else str(f["produced_at"])
                ),
                "superseded_by": f["superseded_by"],
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
            }
            row = await conn.fetchrow(
                _UPSERT_LABEL_SQL,
                fid,
                f["analyst_id"] or "",
                f["target_id"],
                body.label,
                body.rationale,
                labeled_by,
                json.dumps(snapshot),
            )
        return _label_state(row)

    return router
