# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optimizer-candidate REVIEW family — models + the promote/reject action.

Extracted from ``v3_api`` (X2-B2, 2026-10-08 — the module-size gate's
extract-don't-raise rule: ``v3_api.py`` sat at 2,299 lines against a 2,300
ceiling, and the route-role lane had to land beside it). A NO-BEHAVIOUR-CHANGE
move: the four response/request models, the promotion-target resolver and the
promote/reject body moved here byte-for-byte; only the two helpers dropped
their leading underscore (they now cross a module boundary). ``v3_api`` imports
this module ONE WAY and re-exports every name under its historical spelling
(``_candidate_target_path`` / ``_apply_optimizer_review``), so every caller
and test that reaches for them off ``v3_api`` resolves unchanged.

The three routes that use these names live in the sibling
``optimizer_review_api``, which ``build_v3_router`` declares on its own router
in place, so the app's route ORDER is unchanged).

P-11 panel: optimizer candidate review
--------------------------------------

The ``POST /optimizer/candidates/{id}/review`` mutation lets an operator
promote or reject a queued candidate.  Promotion goes *through* the
registry's descriptor lifecycle (``DescriptorRegistry.update``) so the
audit log, content-hash, dead-letter, and NATS event paths stay
authoritative — see :func:`apply_optimizer_review` for the body of the
action.

REGISTRY-SLIM: imports fastapi, pydantic, the descriptor schema and the
registry leaf modules only — never ``legba.runtime`` / ``legba.data.analysts``
/ dspy (the diff route's snapshot rule).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ..schemas import AnalystDescriptor
from ._deps import RegistryAPIDeps
from .descriptor import Family
from .errors import (
    DescriptorNotFound,
    DescriptorValidationError,
    IllegalLifecycleTransition,
    VersionConflict,
)

__all__ = [
    "OptimizerCandidate",
    "OptimizerReviewBody",
    "OptimizerReviewResult",
    "PromptModuleDiff",
    "apply_optimizer_review",
    "candidate_target_path",
]


class OptimizerCandidate(BaseModel):
    """One optimizer-emitted prompt-module candidate awaiting decision.

    Surfaces `analyst_outputs` rows with `kind='prompt_module_candidate'`.
    Field layout follows `PromptModuleCandidatePayload`
    (`src/legba/data/provenance/models.py`) — `_insert_analyst_output`
    persists the full payload into the `data` JSONB column, so all
    fields below extract from there.

    `state` is derived from `promotion_gate` per L-176:

      * `human_gated`, `auto_with_threshold` → `pending`
      * `rejected`                            → `rejected`
      * `promoted` requires a downstream promotion log that doesn't
        exist yet; the value is reserved for when that table lands.
    """
    id: str
    analyst_id: str
    analyst_version: str
    parent_prompt_module_path: str
    eval_score: float
    eval_score_delta: float
    training_set_size: int
    gepa_generation: int
    promotion_gate: str
    state: Literal["pending", "rejected"]
    temporal_workflow_id: str | None
    produced_at: datetime
    # The REAL method the GEPA workflow took for this candidate
    # (dspy_gepa / naive_best_of_n / noop_empty_training / skipped_validation /
    # unknown). Lets the UI flag a non-dspy fallback (a worker-less deploy
    # silently runs naive search). Read from data['method']; falls back to
    # data['diagnostics']['method'] for rows written before the top-level field.
    method: str


class PromptModuleDiff(BaseModel):
    """Current-vs-candidate prompt-module diff for one optimizer candidate.

    Drives the ``system.optimizer.diff`` panel (``OptimizerDiff.tsx``). Built
    entirely from the persisted candidate row — ``current_text`` is the parent
    snapshot the candidate was scored against (``parent_prompt_module_text``,
    captured at compile time), with a live promoted-prompt override when one
    exists. CRITICAL: this route NEVER imports the prompt module / dspy — the
    text comes from substrate columns only (the snapshot lives on the row so
    the registry process stays dspy-free; test asserts no dspy import).
    """
    candidate_id: str
    analyst_id: str
    current_module_path: str
    candidate_module_path: str
    current_text: str
    candidate_text: str
    eval_score: float
    eval_score_delta: float


class OptimizerReviewBody(BaseModel):
    """Body of POST ``/optimizer/candidates/{id}/review``.

    ``action`` is the operator's decision; ``reviewer`` is the principal
    identifier stamped on the resulting audit-log row; ``note`` is a free-form
    rationale persisted on the audit row's ``change_summary``.
    """
    action: Literal["promote", "reject"]
    reviewer: str = Field(min_length=1, max_length=256)
    note: str | None = Field(default=None, max_length=4096)


class OptimizerReviewResult(BaseModel):
    """Response from POST ``/optimizer/candidates/{id}/review``.

    On promote: ``new_descriptor_version`` is the content-hash of the new
    head row of the parent analyst's descriptor (the registry mints it from
    the updated body; see ``DescriptorRegistry.update``).

    On reject: ``new_descriptor_version`` is ``null`` (no descriptor
    mutation) — the only side effect is an audit-log row and an update
    to the candidate's ``data->promotion_gate`` JSONB field flipping it
    to ``rejected``.
    """
    candidate_id: str
    action: Literal["promote", "reject"]
    analyst_id: str
    new_descriptor_version: str | None
    promotion_gate: Literal["promoted", "rejected"]


def candidate_target_path(payload: dict[str, Any]) -> str:
    """Compute the prompt_module path that the parent descriptor's
    ``method.prompt_module`` should be flipped to.

    Resolution order:

      1. If the candidate's stored ``data`` dict carries an explicit
         ``candidate_prompt_module_path``, use it as-is.  (The optimizer
         doesn't currently set this field; reserved for future generators
         that mint distinct paths per candidate.)
      2. Otherwise derive a versioned sibling of the parent path by
         appending ``.gepa_gen_{N}`` (where N is ``gepa_generation``).
         This matches the L-176 §"Promotion gates" §6 brief: each promoted
         candidate version of an analyst's prompt module is stored at a
         distinct importable path.
    """
    explicit = (payload.get("data") or {}).get("candidate_prompt_module_path")
    if isinstance(explicit, str) and explicit:
        return explicit
    parent = str(payload.get("parent_prompt_module_path") or "")
    gen = int(payload.get("gepa_generation") or 0)
    if not parent:
        raise ValueError(
            "candidate row missing parent_prompt_module_path; cannot derive "
            "promotion target",
        )
    return f"{parent}.gepa_gen_{gen}"


async def apply_optimizer_review(
    deps_: RegistryAPIDeps,
    *,
    candidate_id: str,
    body: OptimizerReviewBody,
    actor_role: str = "operator",
) -> OptimizerReviewResult:
    """Promotion strategy (Lewis's call per
    ``plans/legba_done_plan_2026_05_28.md`` §6 Q6 — option (b)):

      * Flip the parent analyst descriptor's ``method.prompt_module``
        field directly to the candidate's new path; the registry's
        ``update()`` mints a new content-hash version + writes a signed
        audit row + publishes the ``descriptor.updated.analyst.<id>``
        event.
      * The candidate row in ``analyst_outputs`` stays in place as the
        historical record (its ``data->promotion_gate`` is flipped from
        ``human_gated`` / ``auto_with_threshold`` to ``promoted`` so the
        P-11 queue surfaces it as decided).
      * No separate ``prompt_module_promotions`` table — historical
        promotions are reconstructible by joining the analyst's
        descriptor history against the candidate rows.

    On reject:

      * No descriptor mutation.
      * Candidate row's ``data->promotion_gate`` flipped to ``rejected``.
      * One audit-log row written against the parent analyst with
        ``action='optimizer_reject'`` so the rationale is preserved in
        the audit chain (the rest of the registry only writes audit rows
        as a side effect of descriptor mutations; the reject path is
        the one operator action that emits an audit row without
        otherwise touching descriptor state).
    """
    try:
        cand_uid = UUID(candidate_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"candidate id must be a UUID: {exc}",
        ) from exc

    # Load the candidate row.
    async with deps_.descriptor_registry.pg.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, kind, data
              FROM analyst_outputs
             WHERE id = $1
            """,
            cand_uid,
        )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"optimizer candidate {candidate_id!r} not found",
        )
    if row["kind"] != "prompt_module_candidate":
        raise HTTPException(
            status_code=400,
            detail=(
                f"analyst_outputs row {candidate_id!r} is kind={row['kind']!r}, "
                f"not 'prompt_module_candidate'"
            ),
        )
    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)
    payload: dict[str, Any] = dict(data or {})

    current_gate = str(payload.get("promotion_gate") or "human_gated")
    if current_gate in ("promoted", "rejected"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"candidate {candidate_id!r} already decided "
                f"(promotion_gate={current_gate!r})"
            ),
        )

    analyst_id = str(payload.get("analyst_id") or "")
    if not analyst_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"candidate {candidate_id!r} missing analyst_id in its data "
                f"payload; cannot resolve parent descriptor"
            ),
        )

    if body.action == "reject":
        # Flip the candidate's stored promotion_gate + emit an audit row.
        new_payload = dict(payload)
        new_payload["promotion_gate"] = "rejected"
        new_payload["reviewed_by"] = body.reviewer
        new_payload["reviewed_at"] = datetime.now(tz=timezone.utc).isoformat()
        if body.note is not None:
            new_payload["review_note"] = body.note
        async with deps_.descriptor_registry.pg.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE analyst_outputs SET data = $1::jsonb WHERE id = $2",
                    json.dumps(new_payload),
                    cand_uid,
                )
                await deps_.audit_logger.record(
                    conn,
                    actor_id=body.reviewer,
                    namespace=Family.ANALYST.value,
                    descriptor_id=analyst_id,
                    action="optimizer_reject",
                    actor_role=actor_role,  # W11-F62: the principal's
                    from_version=str(payload.get("analyst_version") or "") or None,
                    to_version=None,
                    change_summary={
                        "candidate_id": candidate_id,
                        "reason": body.note,
                        "prior_gate": current_gate,
                    },
                )
        return OptimizerReviewResult(
            candidate_id=candidate_id,
            action="reject",
            analyst_id=analyst_id,
            new_descriptor_version=None,
            promotion_gate="rejected",
        )

    # ----- promote -----
    try:
        candidate_path = candidate_target_path(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Pull the parent analyst's HEAD descriptor as a typed instance so we
    # can mutate it and feed it back through the registry. Falls back to
    # raw `get()` + manual pydantic parse if `get_typed` blows up (e.g.
    # the auto_upgrade conversion path isn't wired in the test substrate).
    try:
        typed = await deps_.descriptor_registry.get_typed(
            analyst_id, family=Family.ANALYST, auto_upgrade=False,
        )
    except DescriptorNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"parent analyst descriptor {analyst_id!r} not found; "
                f"cannot promote candidate {candidate_id!r}"
            ),
        ) from exc
    if not isinstance(typed, AnalystDescriptor):  # pragma: no cover — defensive
        raise HTTPException(
            status_code=500,
            detail=(
                f"parent descriptor {analyst_id!r} is not an analyst; "
                f"got {type(typed).__name__}"
            ),
        )

    # Build the new descriptor body with the flipped prompt_module.
    new_body = typed.model_dump(mode="json", by_alias=True)
    method_block = dict(new_body.get("method") or {})
    method_block["prompt_module"] = candidate_path
    new_body["method"] = method_block

    try:
        new_descriptor = AnalystDescriptor.model_validate(new_body, strict=False)
        new_row = await deps_.descriptor_registry.update(
            analyst_id, new_descriptor, actor=body.reviewer,
            actor_role=actor_role,
        )
    except DescriptorNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DescriptorValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation",
                "message": str(exc),
                "dead_letter_id": exc.dead_letter_id,
            },
        ) from exc
    except (VersionConflict, IllegalLifecycleTransition) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Mutate the candidate row's promotion_gate now that the descriptor
    # flip succeeded.  Outside the descriptor's transaction is fine: this
    # row is informational; even if the JSONB update fails the descriptor
    # promotion is the canonical record.
    new_payload = dict(payload)
    new_payload["promotion_gate"] = "promoted"
    new_payload["reviewed_by"] = body.reviewer
    new_payload["reviewed_at"] = datetime.now(tz=timezone.utc).isoformat()
    new_payload["promoted_to_descriptor_version"] = new_row.version
    new_payload["promoted_prompt_module_path"] = candidate_path
    if body.note is not None:
        new_payload["review_note"] = body.note
    async with deps_.descriptor_registry.pg.acquire() as conn:
        await conn.execute(
            "UPDATE analyst_outputs SET data = $1::jsonb WHERE id = $2",
            json.dumps(new_payload),
            cand_uid,
        )

    return OptimizerReviewResult(
        candidate_id=candidate_id,
        action="promote",
        analyst_id=analyst_id,
        new_descriptor_version=new_row.version,
        promotion_gate="promoted",
    )
