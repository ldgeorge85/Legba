# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator review surface for the journal's PROPOSE-AND-GATE queue
(planning/JOURNAL_ASSESSOR_PLAN.md §7.4 / §12 Wave 4).

The journal SUGGESTS into ``journal_proposals``; a human DISPOSES here. Three
routes (all bearer-gated, mounted under ``/api/v1``):

  * ``GET  /journal_proposals``            — list, filter by ``status``.
  * ``POST /journal_proposals/{id}/accept`` — accept + APPLY via the existing
                                              write/lifecycle path (idempotent on
                                              replay — never double-applies).
                                              Takes an OPTIONAL decision_reason.
  * ``POST /journal_proposals/{id}/reject`` — reject; REQUIRES a decision_reason.

THE DECISION REASON, on BOTH sides (GLASS-3). ``decision_reason`` has existed on
the row since migration 0048, but only ``reject`` ever wrote it: the accept path
set ``status``/``decided_by``/``decided_at`` and hardcoded ``decision_reason=None``
on the way out, with no body model to carry one. The audit trail was therefore
asymmetric by construction — every refusal explained itself and every APPLIED
change, the half that actually mutates the substrate, could not. An operator
reviewing the queue afterwards could read why the journal was told no and never
why it was told yes.

Accept now takes the same reason, with one deliberate difference: it is OPTIONAL
where reject's is REQUIRED. The asymmetry is kept because it is real — a refusal
is only legible through its reason, whereas an accept is already fully described
by the applied diff the ``applied`` audit returns. Making it mandatory would buy
a compliance field full of "ok" and break every existing caller (the panel posts
``{}``); making it available means the reasons that DO matter get recorded and
read back. An omitted, empty, or whitespace-only reason stores NULL — the honest
"none given", never an empty string masquerading as one.

§7.5 SAFEGUARDS for self_revision:
  (a) the list + the detail surfaced to the operator carries OBJECTIVE EVIDENCE —
      the recent critic/calibration of the journal's own entries — alongside the
      rationale, so a beautifully-argued self-revision is reviewed against the
      journal's actual track record, not its prose alone.
  (b) a self_revision diff touching the prompt's grounding / honesty / anti-self-
      confirmation PROTECTED SECTION is AUTO-REJECTED at accept time (the
      apply worker raises ProtectedSectionViolation → the row archives with the
      reason, NOTHING is applied).

IDEMPOTENCY (§7.4). Accept flips ``status`` ``pending → accepted`` ATOMICALLY
(``UPDATE … WHERE status='pending' RETURNING``) BEFORE applying; only the run
that won the transition applies. A replayed accept on an already-decided row
returns the recorded decision WITHOUT re-applying.

Wiring mirrors ``journal_api.py``: ``build_journal_proposals_router(deps)``, the
shared ``RegistryAPIDeps`` bundle, the same ``require_bearer`` gate.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer
from .journal_proposals_apply import (
    ProposalApplyError,
    ProtectedSectionViolation,
    apply_accepted_proposal,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_VALID_STATUSES = {"pending", "accepted", "rejected", "archived"}


# ---------------------------------------------------------------------------
# Response / request models
# ---------------------------------------------------------------------------


class CalibrationEvidence(BaseModel):
    """§7.5(a) — the OBJECTIVE evidence the operator sees ALONGSIDE a self_revision
    rationale: the journal's own recent critic/calibration posture. The honest
    backstop against an over-persuasive self-revision — review the diff against
    the journal's actual track record, not its prose."""

    available: bool = False
    forecast_unproven: bool = True
    calibration_thin: bool = True
    brier_skill_score: float | None = None
    journal_critic_mean: float | None = None
    journal_critic_n: int = 0


class ProposalOut(BaseModel):
    """One ``journal_proposals`` row for the operator review surface."""

    id: str
    proposal_kind: str
    proposed_by_analyst_id: str
    run_id: str | None
    rationale: str
    diff: dict[str, Any]
    cited_substrate_refs: list[str] = Field(default_factory=list)
    status: str
    decided_by: str | None
    decision_reason: str | None
    decided_at: datetime | None
    produced_at: datetime
    # §7.5(a): present ONLY for self_revision (the highest-scrutiny class) — the
    # objective evidence the operator must weigh against the rationale.
    self_revision_evidence: CalibrationEvidence | None = None


class ProposalsListOut(BaseModel):
    proposals: list[ProposalOut]


class DecisionOut(BaseModel):
    """The result of an accept/reject. ``applied`` is the apply-worker audit on a
    fresh accept; ``replayed`` is True when the decision was already recorded (the
    idempotent no-op path)."""

    id: str
    status: str
    decided_by: str | None
    decision_reason: str | None
    applied: dict[str, Any] | None = None
    replayed: bool = False


class RejectBody(BaseModel):
    decision_reason: str = Field(..., min_length=1)


class AcceptBody(BaseModel):
    """The OPTIONAL reason recorded alongside an accept.

    Mirrors ``RejectBody`` except for the requirement, and the whole body is
    optional besides — ``POST`` with no body at all is the pre-existing contract
    and stays valid, so no existing caller breaks."""

    decision_reason: str | None = Field(default=None, max_length=2048)


def _clean_reason(raw: str | None) -> str | None:
    """Normalize a supplied reason to text-or-NULL.

    An omitted, empty, or whitespace-only reason must store SQL NULL, never
    ``''``: a column that can hold an empty string acquires a third state that
    reads as "a reason was given" to every ``IS NOT NULL`` check downstream."""
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned[:2048] if cleaned else None


def _with_operator_note(machine_reason: str, operator_note: str | None) -> str:
    """Compose the archive reason for an accept that FAILED after being claimed.

    The machine's reason leads — it is why the row is archived rather than
    accepted — and the operator's note follows it, so a decision trail never
    silently drops the human half. Truncated as a whole to the column's 2048."""
    if not operator_note:
        return machine_reason[:2048]
    return f"{machine_reason} [operator's accept note: {operator_note}]"[:2048]


# ---------------------------------------------------------------------------
# Hydration helpers
# ---------------------------------------------------------------------------


def _load_jsonb(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


async def _journal_calibration_evidence(conn: Any) -> CalibrationEvidence:
    """Read the §7.5(a) objective evidence for a self_revision review: the live
    calibration verdict + the journal's own recent critic mean. Best-effort —
    absent data reads as 'unproven' (absence of proof is not proof of skill)."""
    # B0-3 (read-truth): the calibration writer produces ``kind='finding'`` +
    # ``analyst_id='calibration_tracking'`` (nothing writes ``kind='calibration'``)
    # and the metrics live one JSONB level down at ``data.data`` (the row's
    # ``data`` column is the WHOLE FindingPayload dump).
    cal = await conn.fetchrow(
        "SELECT data FROM analyst_outputs "
        "WHERE kind = 'finding' AND analyst_id = 'calibration_tracking' "
        "AND superseded_by IS NULL "
        "ORDER BY produced_at DESC, id DESC LIMIT 1"
    )
    forecast_unproven = True
    calibration_thin = True
    bss: float | None = None
    available = False
    if cal is not None:
        available = True
        payload = _load_jsonb(cal["data"]) or {}
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        raw_bss = data.get("brier_skill_score")
        bss = float(raw_bss) if isinstance(raw_bss, (int, float)) else None
        ready = bool(data.get("forecast_acute_ready"))
        degenerate = bool(data.get("forecast_acute_degenerate"))
        forecast_unproven = not (
            ready and not degenerate and bss is not None and bss > 0.0
        )
        exo_n = data.get("exogenous_sample_size")
        calibration_thin = not isinstance(exo_n, int) or exo_n < 5

    # The journal's OWN recent critic scores (its track record, §7.5(a)).
    crit = await conn.fetchrow(
        """
        SELECT avg((data->>'overall_score')::float) AS mean, count(*) AS n
        FROM analyst_critiques
        WHERE analyzed_analyst_id IN ('journal_assessor', 'journal_consolidator')
          AND produced_at > now() - interval '30 days'
          AND (data->>'overall_score') IS NOT NULL
        """
    )
    critic_mean = (
        float(crit["mean"]) if crit and crit["mean"] is not None else None
    )
    critic_n = int(crit["n"]) if crit and crit["n"] is not None else 0
    return CalibrationEvidence(
        available=available,
        forecast_unproven=forecast_unproven,
        calibration_thin=calibration_thin,
        brier_skill_score=bss,
        journal_critic_mean=critic_mean,
        journal_critic_n=critic_n,
    )


def _hydrate(row: Any, evidence: CalibrationEvidence | None) -> ProposalOut:
    return ProposalOut(
        id=str(row["id"]),
        proposal_kind=row["proposal_kind"],
        proposed_by_analyst_id=row["proposed_by_analyst_id"],
        run_id=str(row["run_id"]) if row["run_id"] is not None else None,
        rationale=row["rationale"],
        diff=_load_jsonb(row["diff"]) or {},
        cited_substrate_refs=[str(r) for r in (row["cited_substrate_refs"] or [])],
        status=row["status"],
        decided_by=row["decided_by"],
        decision_reason=row["decision_reason"],
        decided_at=row["decided_at"],
        produced_at=row["produced_at"],
        self_revision_evidence=(
            evidence if row["proposal_kind"] == "self_revision" else None
        ),
    )


_COLS = (
    "id, proposal_kind, proposed_by_analyst_id, run_id, rationale, diff, "
    "cited_substrate_refs, status, decided_by, decision_reason, decided_at, "
    "produced_at"
)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_journal_proposals_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the operator review router. Mount under ``/api/v1`` so the paths
    resolve at ``/api/v1/journal_proposals``."""
    router = APIRouter(tags=["journal_proposals"])

    def _pg():
        return deps.descriptor_registry.pg

    @router.get("/journal_proposals", response_model=ProposalsListOut)
    async def list_proposals(
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=DEFAULT_LIMIT),
        principal: str = Depends(require_bearer),
    ) -> ProposalsListOut:
        if limit < 1 or limit > MAX_LIMIT:
            raise HTTPException(status_code=400, detail=f"limit must be in [1, {MAX_LIMIT}]")
        args: list[Any] = []
        where = ""
        if status_filter is not None:
            if status_filter not in _VALID_STATUSES:
                raise HTTPException(
                    status_code=400,
                    detail=f"status must be one of {sorted(_VALID_STATUSES)}",
                )
            args.append(status_filter)
            where = f"WHERE status = ${len(args)}"
        args.append(limit)
        sql = (
            f"SELECT {_COLS} FROM journal_proposals {where} "
            f"ORDER BY produced_at DESC, id DESC LIMIT ${len(args)}"
        )
        async with _pg().acquire() as conn:
            rows = await conn.fetch(sql, *args)
            # §7.5(a): the objective evidence is read once and bound to any
            # self_revision rows in the page (cheap: one row each from
            # calibration + the journal's critic mean).
            has_self_rev = any(r["proposal_kind"] == "self_revision" for r in rows)
            evidence = (
                await _journal_calibration_evidence(conn) if has_self_rev else None
            )
        return ProposalsListOut(proposals=[_hydrate(r, evidence) for r in rows])

    @router.post("/journal_proposals/{proposal_id}/accept", response_model=DecisionOut)
    async def accept_proposal(
        proposal_id: UUID,
        body: AcceptBody | None = Body(default=None),
        actor: str = Depends(require_bearer),
    ) -> DecisionOut:
        """Accept + APPLY. The journal suggested; the human (here) causes.

        Idempotent (§7.4): the pending→accepted transition is atomic; only the
        winner applies. A self_revision touching the protected section auto-rejects
        (the diff is never applied; the row archives with the reason).

        The optional ``decision_reason`` is recorded on the SAME atomic claim that
        flips the status, so a row can never exist as accepted-without-the-reason-
        the-operator-gave: either the whole decision lands or none of it does."""
        reason = _clean_reason(body.decision_reason if body else None)
        async with _pg().acquire() as conn:
            # ATOMIC claim: flip pending→accepted, returning the row ONLY if we
            # won the transition. A replayed accept finds status != 'pending'.
            claimed = await conn.fetchrow(
                f"""
                UPDATE journal_proposals
                   SET status = 'accepted', decided_by = $2,
                       decision_reason = $3, decided_at = now(),
                       updated_at = now()
                 WHERE id = $1 AND status = 'pending'
             RETURNING {_COLS}
                """,
                proposal_id, actor, reason,
            )
            if claimed is None:
                # Either the id doesn't exist, or it was already decided (replay).
                existing = await conn.fetchrow(
                    f"SELECT {_COLS} FROM journal_proposals WHERE id = $1",
                    proposal_id,
                )
                if existing is None:
                    raise HTTPException(status_code=404, detail="proposal not found")
                # Already decided → idempotent no-op (NEVER re-apply).
                return DecisionOut(
                    id=str(existing["id"]),
                    status=existing["status"],
                    decided_by=existing["decided_by"],
                    decision_reason=existing["decision_reason"],
                    applied=None,
                    replayed=True,
                )

            proposal_kind = claimed["proposal_kind"]
            diff = _load_jsonb(claimed["diff"]) or {}
            # APPLY via the existing write/lifecycle path. The status is already
            # 'accepted' (the claim won), so the apply runs exactly once.
            try:
                applied = await apply_accepted_proposal(
                    conn, deps,
                    proposal_id=proposal_id,
                    proposal_kind=proposal_kind,
                    diff=diff,
                    actor=actor,
                )
            except ProtectedSectionViolation as exc:
                # §7.5(b) — auto-reject: archive WITH the reason, apply NOTHING.
                # The machine's reason leads (it is why the row archived), but the
                # operator's own note is carried after it rather than overwritten —
                # discarding what the human typed is the exact gap this closes.
                await conn.execute(
                    "UPDATE journal_proposals SET status = 'archived', "
                    "decision_reason = $2, updated_at = now() WHERE id = $1",
                    proposal_id,
                    _with_operator_note(
                        f"auto-rejected (protected section): {exc}", reason
                    ),
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"self_revision auto-rejected (protected section): {exc}",
                ) from exc
            except ProposalApplyError as exc:
                # A bad diff / registry failure: archive WITH the reason so the
                # row is never left dangling in 'accepted' with nothing applied.
                await conn.execute(
                    "UPDATE journal_proposals SET status = 'archived', "
                    "decision_reason = $2, updated_at = now() WHERE id = $1",
                    proposal_id,
                    _with_operator_note(f"apply failed: {exc}", reason),
                )
                raise HTTPException(
                    status_code=422, detail=f"apply failed: {exc}"
                ) from exc

            logger.info(
                "journal_proposals.accepted id=%s kind=%s by=%s applied=%s reason=%r",
                proposal_id, proposal_kind, actor, applied, reason,
            )
            return DecisionOut(
                id=str(claimed["id"]),
                status="accepted",
                decided_by=actor,
                decision_reason=reason,
                applied=applied,
                replayed=False,
            )

    @router.post("/journal_proposals/{proposal_id}/reject", response_model=DecisionOut)
    async def reject_proposal(
        proposal_id: UUID,
        body: RejectBody = Body(...),
        actor: str = Depends(require_bearer),
    ) -> DecisionOut:
        """Reject → archived WITH the required decision_reason. Idempotent on a
        replayed reject of an already-decided row (returns the recorded decision)."""
        reason = body.decision_reason.strip()
        if not reason:
            raise HTTPException(
                status_code=422, detail="decision_reason is required to reject"
            )
        async with _pg().acquire() as conn:
            claimed = await conn.fetchrow(
                f"""
                UPDATE journal_proposals
                   SET status = 'rejected', decided_by = $2, decision_reason = $3,
                       decided_at = now(), updated_at = now()
                 WHERE id = $1 AND status = 'pending'
             RETURNING {_COLS}
                """,
                proposal_id, actor, reason,
            )
            if claimed is None:
                existing = await conn.fetchrow(
                    f"SELECT {_COLS} FROM journal_proposals WHERE id = $1",
                    proposal_id,
                )
                if existing is None:
                    raise HTTPException(status_code=404, detail="proposal not found")
                return DecisionOut(
                    id=str(existing["id"]),
                    status=existing["status"],
                    decided_by=existing["decided_by"],
                    decision_reason=existing["decision_reason"],
                    replayed=True,
                )
        logger.info(
            "journal_proposals.rejected id=%s by=%s reason=%r",
            proposal_id, actor, reason,
        )
        return DecisionOut(
            id=str(claimed["id"]),
            status="rejected",
            decided_by=actor,
            decision_reason=reason,
            replayed=False,
        )

    return router


__all__ = ["build_journal_proposals_router"]
