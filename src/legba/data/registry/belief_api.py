# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /api/v1/v3/belief`` — "what did Legba believe on date D".

DATA MODEL V3 / P3 (spec §3.4). A DECISION-time read on ``analyst_outputs``:
the findings Legba had published and not yet superseded on date D —
``produced_at <= D AND (superseded_at IS NULL OR superseded_at > D)``.
``analyst_outputs`` has no ``valid_*`` columns (§3.1's first correction), so
this is a different clock from the ``as_of`` the substrate readers take, and
a different parameter name on purpose.

The faithfulness VERDICT folds two ways, and the response stamps which:

* ``fold_verdicts="as_of"`` (default) — the verdict Legba HELD on D (the
  latest faithfulness critique produced by then). A finding whose verdict
  had not yet landed on D returns ``effective_confidence=null`` and counts
  toward ``verdict_pending_at_as_of`` — the honest "not yet graded" answer,
  never a silently-empty score.
* ``fold_verdicts="latest"`` — today's verdict for each finding, for "what
  we believe NOW about what we said THEN".

There is NO pooled score by design: each row carries its own
``effective_confidence`` and the envelope carries none. A malformed
``as_of`` 422s at the FastAPI boundary — the date is parsed to a real
``timestamptz``, never defaulted to ``now()``.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + the leaf SQL builder
``legba.data.critic_fold`` (no imports of its own) only. Nothing from
``legba.data.analysts`` or ``legba.runtime`` — this module ships in the slim
registry image.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from .. import critic_fold
from .api import RegistryAPIDeps, require_bearer

BELIEF_READER_VERSION = "2026-09/p3"

_ROUTE = "/belief"

#: Same clamp the substrate-reads pages use — a page, not a firehose.
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 20


class BeliefRow(BaseModel):
    """One finding as it stood on date D, with the named fold's verdict."""

    id: str
    title: str | None = None
    body: str | None = None
    confidence: float | None = None
    severity: str | None = None
    target_id: str | None = None
    analyst_id: str | None = None
    produced_at: datetime | None = None
    verdict_score: float | None = None
    verdict_at: datetime | None = None
    #: min(confidence, verdict) under the stamped fold — NULL when that fold
    #: has no verdict (pending), never pooled across rows.
    effective_confidence: float | None = None
    #: Whether a faithfulness verdict existed BY date D — always about D,
    #: regardless of which fold the scores came from.
    verdict_pending_at_as_of: bool


class BeliefOut(BaseModel):
    """The belief register: per-row verdicts, a fold stamp, a pending count."""

    data: list[BeliefRow]
    count: int
    as_of: datetime
    target_id: str | None = None
    fold_verdicts: str
    verdict_pending_at_as_of: int
    note: str = (
        "rows carry their own effective_confidence; no pooled score is "
        "computed — a belief-as-of read is a register, not a number"
    )


def build_belief_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the /v3/belief router bound to the registry deps.

    The route reads directly from the primary Postgres pool via
    ``deps.descriptor_registry.pg.acquire()`` — the same path the
    substrate-reads router and the v3 telemetry routers use.
    """
    router = APIRouter(tags=["substrate-reads"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=BeliefOut)
    async def v3_belief(
        as_of: datetime = Query(
            ..., description="the decision-time instant (ISO-8601)"
        ),
        target_id: str | None = Query(default=None),
        fold_verdicts: Literal["as_of", "latest"] = Query(default="as_of"),
        limit: int = Query(default=_DEFAULT_LIMIT),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> BeliefOut:
        """Findings published and not yet superseded on ``as_of``.

        ``as_of`` is a required ISO-8601 instant: FastAPI parses it to a
        ``timestamptz`` and 422s on a malformed value — the refusal is loud,
        never a silent "now". ``fold_verdicts`` is a closed literal, so a
        bad fold 422s too.
        """
        clamped_limit = max(1, min(int(limit), _MAX_LIMIT))

        clauses: list[str] = [
            "f.kind = 'finding'",
            "f.produced_at <= $1",
            "(f.superseded_at IS NULL OR f.superseded_at > $1)",
        ]
        args: list[Any] = [as_of]
        if target_id is not None:
            args.append(target_id)
            clauses.append(f"f.target_id = ${len(args)}")
        args.append(clamped_limit)

        # H17 — SET-BASED, and the same definition the consult port's
        # `belief_as_of` runs: this route and that tool were carrying identical
        # copies of the two verdict laterals.
        sql = critic_fold.dual_verdict_findings_sql(
            " AND ".join(clauses), len(args),
        )

        async with deps_.descriptor_registry.pg.acquire() as conn:
            records = await conn.fetch(sql, *args)

        rows: list[BeliefRow] = []
        pending = 0
        for r in records:
            score_key = (
                "verdict_score_as_of" if fold_verdicts == "as_of"
                else "verdict_score_latest"
            )
            at_key = (
                "verdict_at_as_of" if fold_verdicts == "as_of"
                else "verdict_at_latest"
            )
            verdict_score = (
                float(r[score_key]) if r[score_key] is not None else None
            )
            confidence = (
                float(r["confidence"]) if r["confidence"] is not None else None
            )
            row_pending = r["verdict_score_as_of"] is None
            if row_pending:
                pending += 1
            rows.append(
                BeliefRow(
                    id=str(r["id"]),
                    title=r["title"],
                    body=(r["body"] or "")[:2000],
                    confidence=confidence,
                    severity=r["severity"],
                    target_id=r["target_id"],
                    analyst_id=r["analyst_id"],
                    produced_at=r["produced_at"],
                    verdict_score=verdict_score,
                    verdict_at=r[at_key],
                    effective_confidence=(
                        min(confidence, verdict_score)
                        if confidence is not None and verdict_score is not None
                        else None
                    ),
                    verdict_pending_at_as_of=row_pending,
                )
            )

        return BeliefOut(
            data=rows,
            count=len(rows),
            as_of=as_of,
            target_id=target_id,
            fold_verdicts=fold_verdicts,
            verdict_pending_at_as_of=pending,
        )

    return router


__all__ = ["BELIEF_READER_VERSION", "BeliefOut", "BeliefRow",
           "build_belief_router"]
