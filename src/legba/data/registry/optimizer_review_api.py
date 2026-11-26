# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optimizer-candidate queue ROUTES — list, diff, review (P-11 / S4).

Extracted from ``v3_api.build_v3_router`` (X2-B2, 2026-10-08; see
``optimizer_review``'s docstring for why). NO BEHAVIOUR CHANGE: the three
route bodies moved byte-for-byte; ``build_v3_router`` calls
:func:`register_optimizer_review_routes` ON ITS OWN ROUTER at the exact position
the routes used to be declared, so every route keeps the parent's ``runtime``
tag, its path under ``/api/v1/v3``, its operation id and its place in the
app's route order — and ``build_v3_router(...).routes`` stays a FLAT list of
routes (an ``include_router`` would nest them behind an included-router entry,
which the v3 route-registration tests walk).

  * ``GET  /optimizer/candidates``                    — the queue
  * ``GET  /optimizer/candidates/{candidate_id}/diff`` — current vs candidate
  * ``POST /optimizer/candidates/{candidate_id}/review`` — promote / reject

REGISTRY-SLIM: fastapi + the ``optimizer_review`` leaf + ``_deps`` only.
"""
from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from legba.shared.registry_principal import role_for_actor

from ._deps import RegistryAPIDeps, require_bearer
from .route_roles import recorded_author
from .optimizer_review import (
    OptimizerCandidate,
    OptimizerReviewBody,
    OptimizerReviewResult,
    PromptModuleDiff,
    apply_optimizer_review,
    candidate_target_path,
)


def register_optimizer_review_routes(
    router: APIRouter, deps: RegistryAPIDeps,
) -> None:
    """Declare the three optimizer-queue routes on ``router`` (the v3 router).

    Declared on the CALLER's router rather than a router of its own, so the
    routes carry exactly the tags, prefix and position they had when
    ``build_v3_router`` declared them inline.
    """
    @router.get(
        "/optimizer/candidates", response_model=list[OptimizerCandidate],
    )
    async def list_optimizer_candidates(
        state: Literal["pending", "rejected", "all"] = Query(default="pending"),
        principal: str = Depends(require_bearer),
    ) -> list[OptimizerCandidate]:
        if state == "pending":
            gate_clause = (
                "AND (data->>'promotion_gate') "
                "IN ('human_gated', 'auto_with_threshold')"
            )
        elif state == "rejected":
            gate_clause = "AND (data->>'promotion_gate') = 'rejected'"
        else:
            gate_clause = ""

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, data, produced_at
                  FROM analyst_outputs
                 WHERE kind = 'prompt_module_candidate'
                       {gate_clause}
                 ORDER BY produced_at DESC
                 LIMIT 500
                """
            )

        out: list[OptimizerCandidate] = []
        for r in rows:
            raw = r["data"]
            if isinstance(raw, str):
                raw = json.loads(raw)
            data: dict[str, Any] = dict(raw) if raw else {}
            gate = str(data.get("promotion_gate", "human_gated"))
            row_state: Literal["pending", "rejected"] = (
                "rejected" if gate == "rejected" else "pending"
            )
            tw = data.get("temporal_workflow_id")
            # Real workflow method. The persisted row's `data` column IS the
            # PromptModuleCandidatePayload dump, whose free-form `data` bag
            # carries `method` (top-level) + `diagnostics.method` (the optimizer
            # now stamps both from workflow_result.diagnostics['method']).
            # Resolution: bag.method → bag.diagnostics.method → 'unknown'.
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            method = inner.get("method")
            if not method:
                diag = inner.get("diagnostics")
                if isinstance(diag, dict):
                    method = diag.get("method")
            method = str(method or "unknown")
            out.append(OptimizerCandidate(
                id=str(r["id"]),
                analyst_id=str(data.get("analyst_id", "")),
                analyst_version=str(data.get("analyst_version", "")),
                parent_prompt_module_path=str(
                    data.get("parent_prompt_module_path", ""),
                ),
                eval_score=float(data.get("eval_score", 0.0)),
                eval_score_delta=float(data.get("eval_score_delta", 0.0)),
                training_set_size=int(data.get("training_set_size", 0)),
                gepa_generation=int(data.get("gepa_generation", 0)),
                promotion_gate=gate,
                state=row_state,
                temporal_workflow_id=str(tw) if tw else None,
                produced_at=r["produced_at"],
                method=method,
            ))
        return out

    @router.get(
        "/optimizer/candidates/{candidate_id}/diff",
        response_model=PromptModuleDiff,
    )
    async def optimizer_candidate_diff(
        candidate_id: str,
        _principal: str = Depends(require_bearer),
    ) -> PromptModuleDiff:
        """Current-vs-candidate prompt-module diff for one queued candidate.

        Built ENTIRELY from substrate (the persisted candidate row +, when
        present, the analyst's live promoted-prompt row). This route MUST NOT
        import the prompt module or dspy — the parent text is read from the
        ``parent_prompt_module_text`` snapshot the optimizer captured at
        compile time. ``current_text`` resolution:

          1. the analyst's live promoted candidate text (newest
             ``promotion_gate='promoted'`` row) if one exists — what the
             candidate would actually replace today; else
          2. this candidate's own ``parent_prompt_module_text`` snapshot — the
             baseline its ``eval_score_delta`` was measured against; else
          3. empty string (rows written before the snapshot field existed —
             the UI still renders the candidate side + a degraded note).

        404 when the candidate id is unknown / not a candidate row.
        """
        try:
            cid = UUID(candidate_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=404, detail="unknown candidate")

        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, data
                  FROM analyst_outputs
                 WHERE id = $1 AND kind = 'prompt_module_candidate'
                """,
                cid,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="unknown candidate")

            raw = row["data"]
            if isinstance(raw, str):
                raw = json.loads(raw)
            payload: dict[str, Any] = dict(raw) if raw else {}

            analyst_id = str(payload.get("analyst_id", ""))
            parent_path = str(payload.get("parent_prompt_module_path", ""))
            candidate_text = str(payload.get("candidate_prompt_module_text", ""))
            snapshot = str(payload.get("parent_prompt_module_text", "") or "")

            # Live promoted-prompt override — pure substrate read (no dspy
            # import). When a previously-promoted candidate is the analyst's
            # live prompt, diff against THAT (what this candidate would replace
            # today); otherwise fall back to this candidate's own parent
            # snapshot (the baseline its delta was measured against).
            current_text = snapshot
            if analyst_id:
                promoted = await conn.fetchrow(
                    """
                    SELECT data->>'candidate_prompt_module_text' AS text
                      FROM analyst_outputs
                     WHERE kind = 'prompt_module_candidate'
                       AND data->>'analyst_id' = $1
                       AND data->>'promotion_gate' = 'promoted'
                     ORDER BY created_at DESC
                     LIMIT 1
                    """,
                    analyst_id,
                )
                if promoted and promoted["text"]:
                    current_text = str(promoted["text"])

        try:
            candidate_module_path = candidate_target_path(payload)
        except ValueError:
            candidate_module_path = parent_path

        return PromptModuleDiff(
            candidate_id=str(row["id"]),
            analyst_id=analyst_id,
            current_module_path=parent_path,
            candidate_module_path=candidate_module_path,
            current_text=current_text,
            candidate_text=candidate_text,
            eval_score=float(payload.get("eval_score", 0.0)),
            eval_score_delta=float(payload.get("eval_score_delta", 0.0)),
        )

    @router.post(
        "/optimizer/candidates/{candidate_id}/review",
        response_model=OptimizerReviewResult,
    )
    async def review_optimizer_candidate(
        candidate_id: str,
        body: OptimizerReviewBody,
        principal: str = Depends(require_bearer),
    ) -> OptimizerReviewResult:
        """Promote or reject one queued optimizer candidate.

        See ``_apply_optimizer_review`` for the descriptor-lifecycle
        contract.  Promote flips the parent analyst's
        ``method.prompt_module`` via the registry's ``update()`` (which
        mints a new content-hash version + writes a signed audit row +
        emits the ``descriptor.updated.analyst.<id>`` event).  Reject
        only flips the candidate's ``promotion_gate`` JSONB field and
        writes one audit row tagged ``optimizer_reject``.
        """
        reviewer = recorded_author(principal, body.reviewer)
        if reviewer != body.reviewer:  # A-1: the caller, never the body
            body = body.model_copy(update={"reviewer": reviewer})
        return await apply_optimizer_review(
            deps, candidate_id=candidate_id, body=body,
            # The audit role follows the authenticated principal
            # (``service:…`` / ``operator:…``), never a hard-coded "operator".
            actor_role=role_for_actor(principal),
        )

