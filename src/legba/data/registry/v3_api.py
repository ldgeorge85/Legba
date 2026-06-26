# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""v3 telemetry API — runtime actor health + optimizer candidate queue.

Designed per L-092 §3.5 S4 (Optimizer Candidates Queue) and §3.5 S6
(Runtime Actor Health). Reads live state directly from the substrate;
no fakes, no derived metrics that aren't tracked yet — fields the UI
panel design called out but that aren't observable from substrate
today (NATS queue depth, dapr eviction state) are intentionally not
exposed rather than synthesised.

Mount alongside the v1 registry router under `/api/v1/v3`. Uses the
same `RegistryAPIDeps` bundle + `require_bearer` gate so auth, pg
pool, and audit context all match the rest of the surface.

P-11 panel: optimizer candidate review
--------------------------------------

The ``POST /optimizer/candidates/{id}/review`` mutation lets an operator
promote or reject a queued candidate.  Promotion goes *through* the
registry's descriptor lifecycle (``DescriptorRegistry.update``) so the
audit log, content-hash, dead-letter, and NATS event paths stay
authoritative — see :func:`build_v3_router` for the route and the
``_apply_optimizer_review`` helper below for the body of the action.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..schemas import AnalystDescriptor
from .api import RegistryAPIDeps, require_bearer
from .descriptor import Family
from .errors import (
    DescriptorNotFound,
    DescriptorValidationError,
    IllegalLifecycleTransition,
    VersionConflict,
)

logger = logging.getLogger(__name__)


class ActorRow(BaseModel):
    """One row of the runtime actor health roster.

    Mirrors `public.actor_state` columns column-for-column. The runtime
    writes these rows on every reconcile / activation / lifecycle
    transition (`src/legba/runtime/state.py`).
    """
    actor_id: str
    actor_kind: str
    descriptor_id: str
    descriptor_version: str
    lifecycle: str
    last_run_at: datetime | None
    last_outcome: str | None
    cooldown_until: datetime | None
    error_count: int
    last_error: str | None
    updated_at: datetime


class ScorecardRow(BaseModel):
    """One critic-judgement row for the eval scorecard (UI buildScorecards()).

    ``analyst_id`` is the ANALYZED analyst (whose quality is graded), not the
    judge — recovered from the dual-sink critique payload.
    """
    id: str
    analyst_id: str
    analyst_version: str | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    overall_score: float
    ground_truth_accuracy: float | None = None
    produced_at: str


class ConsumerLagRow(BaseModel):
    """One JetStream durable-consumer lag snapshot (StreamLag panel)."""
    stream: str
    durable: str
    scope_kind: str = "consumer"
    scope_id: str = ""
    num_pending: int = 0
    num_ack_pending: int = 0
    num_redelivered: int = 0
    num_waiting: int = 0
    delivered_stream_seq: int | None = None
    ack_floor_stream_seq: int | None = None


class AnalystCadenceRow(BaseModel):
    """One analyst's true cadence snapshot for the System Status panel.

    Sourced from ``analyst_traces`` (GROUP BY analyst_id) — the AUTHORITATIVE
    cadence truth, not ``actor_state`` whose ``last_run_at`` is NULL for the
    LLM analyst path. ``status`` is derived purely from recency:

      * ``never``   — zero traces for this analyst id
      * ``stale``   — ``age_seconds`` > 21600 (6h since the last run started)
      * ``healthy`` — ran within the last 6h
    """
    analyst_id: str
    last_run_at: datetime | None = None
    age_seconds: int | None = None
    runs_1h: int = 0
    runs_24h: int = 0
    last_outcome: str | None = None
    status: Literal["never", "stale", "healthy"] = "never"


class SourceFiringRow(BaseModel):
    """One source's firing snapshot for the System Status panel.

    Composes ``signals`` (count + freshest ``fetched_at`` per ``source_id``),
    ``source_poll_outcomes`` (latest poll outcome + recent error count — note
    this table only logs ``empty``/``error`` polls, never successes), and the
    head ``source_descriptors`` row (declared ``state``). ``status``:

      * ``paused``  — descriptor state is paused/retired/draft/configured
      * ``error``   — recent poll errors recorded
      * ``silent``  — active head but zero signals in the last 24h
      * ``firing``  — active and producing signals
    """
    source_id: str
    state: str | None = None
    signals_24h: int = 0
    signals_7d: int = 0
    last_seen_at: datetime | None = None
    age_seconds: int | None = None
    last_poll_outcome: str | None = None
    recent_error_count: int = 0
    status: Literal["firing", "silent", "error", "paused"] = "silent"


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


def _candidate_target_path(payload: dict[str, Any]) -> str:
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


async def _apply_optimizer_review(
    deps_: RegistryAPIDeps,
    *,
    candidate_id: str,
    body: OptimizerReviewBody,
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
                    actor_role="operator",
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
        candidate_path = _candidate_target_path(payload)
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


def build_v3_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the v3 telemetry router bound to the registry deps."""
    router = APIRouter(tags=["runtime"])

    @router.get("/runtime/actors", response_model=list[ActorRow])
    async def list_actors(
        principal: str = Depends(require_bearer),
    ) -> list[ActorRow]:
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT actor_id, actor_kind, descriptor_id, descriptor_version,
                       lifecycle, last_run_at, last_outcome, cooldown_until,
                       error_count, last_error, updated_at
                  FROM public.actor_state
                 ORDER BY updated_at DESC
                 LIMIT 500
                """
            )
        return [ActorRow(**dict(r)) for r in rows]

    @router.get(
        "/system/analyst-cadence",
        response_model=list[AnalystCadenceRow],
    )
    async def system_analyst_cadence(
        principal: str = Depends(require_bearer),
    ) -> list[AnalystCadenceRow]:
        """True per-analyst cadence from ``analyst_traces`` (System Status).

        The felt gap this closes: the Actor Health roster reads
        ``actor_state`` whose ``last_run_at`` is NULL for the LLM analyst
        path, so it can't tell a healthy analyst from a dead one. The trace
        log IS the cadence truth — GROUP BY analyst_id, max(run_started_at).

        Fully defensive: any query failure returns an empty list (HTTP 200)
        so the panel renders "no data" rather than polling a 500 every few
        seconds.
        """
        try:
            async with deps.descriptor_registry.pg.acquire() as conn:
                rows = await conn.fetch(
                    """
                    WITH agg AS (
                        SELECT analyst_id,
                               max(run_started_at) AS last_run_at,
                               count(*) FILTER (
                                   WHERE run_started_at > now()
                                         - interval '1 hour'
                               ) AS runs_1h,
                               count(*) FILTER (
                                   WHERE run_started_at > now()
                                         - interval '24 hours'
                               ) AS runs_24h
                          FROM public.analyst_traces
                         GROUP BY analyst_id
                    ),
                    latest AS (
                        SELECT DISTINCT ON (analyst_id)
                               analyst_id, status AS last_outcome
                          FROM public.analyst_traces
                         ORDER BY analyst_id, run_started_at DESC
                    )
                    SELECT a.analyst_id,
                           a.last_run_at,
                           EXTRACT(
                               EPOCH FROM (now() - a.last_run_at)
                           )::bigint AS age_seconds,
                           a.runs_1h,
                           a.runs_24h,
                           l.last_outcome
                      FROM agg a
                      LEFT JOIN latest l USING (analyst_id)
                     ORDER BY a.last_run_at DESC NULLS LAST
                     LIMIT 500
                    """
                )
        except Exception as exc:  # noqa: BLE001 — degrade to empty, HTTP 200
            logger.info("v3.system.analyst_cadence.unavailable err=%s", exc)
            return []

        out: list[AnalystCadenceRow] = []
        for r in rows:
            age = r["age_seconds"]
            age_int = int(age) if age is not None else None
            if age_int is None:
                status: str = "never"
            elif age_int > 21600:
                status = "stale"
            else:
                status = "healthy"
            out.append(
                AnalystCadenceRow(
                    analyst_id=str(r["analyst_id"]),
                    last_run_at=r["last_run_at"],
                    age_seconds=age_int,
                    runs_1h=int(r["runs_1h"] or 0),
                    runs_24h=int(r["runs_24h"] or 0),
                    last_outcome=r["last_outcome"],
                    status=status,  # type: ignore[arg-type]
                )
            )
        return out

    @router.get(
        "/system/source-firing",
        response_model=list[SourceFiringRow],
    )
    async def system_source_firing(
        principal: str = Depends(require_bearer),
    ) -> list[SourceFiringRow]:
        """Per-source firing health (System Status panel).

        Composes signal flow (``signals`` count + freshest ``fetched_at`` per
        source), the latest poll outcome + recent error count
        (``source_poll_outcomes`` — note that table only records ``empty`` /
        ``error`` polls, so absence of rows is normal for a firing source),
        and the declared head descriptor ``state``
        (``source_descriptors`` WHERE is_head).

        ``status`` rules (first match wins):

          * ``paused``  — descriptor state is not ``active``
          * ``error``   — one or more poll errors in the last 24h
          * ``silent``  — active head, zero signals in the last 24h
          * ``firing``  — active and producing signals

        Defensive: empty list (HTTP 200) on any query failure.
        """
        try:
            async with deps.descriptor_registry.pg.acquire() as conn:
                rows = await conn.fetch(
                    """
                    WITH heads AS (
                        SELECT descriptor_id AS source_id, state
                          FROM public.source_descriptors
                         WHERE is_head
                    ),
                    sig AS (
                        SELECT source_id,
                               count(*) FILTER (
                                   WHERE fetched_at > now()
                                         - interval '24 hours'
                               ) AS signals_24h,
                               count(*) FILTER (
                                   WHERE fetched_at > now()
                                         - interval '7 days'
                               ) AS signals_7d,
                               max(fetched_at) AS last_seen_at
                          FROM public.signals
                         GROUP BY source_id
                    ),
                    poll AS (
                        SELECT source_id,
                               count(*) FILTER (
                                   WHERE outcome = 'error'
                                     AND occurred_at > now()
                                         - interval '24 hours'
                               ) AS recent_error_count
                          FROM public.source_poll_outcomes
                         GROUP BY source_id
                    ),
                    latest_poll AS (
                        SELECT DISTINCT ON (source_id)
                               source_id, outcome AS last_poll_outcome
                          FROM public.source_poll_outcomes
                         ORDER BY source_id, occurred_at DESC
                    ),
                    ids AS (
                        SELECT source_id FROM heads
                        UNION
                        SELECT source_id FROM sig
                        UNION
                        SELECT source_id FROM poll
                    )
                    SELECT i.source_id,
                           h.state,
                           COALESCE(s.signals_24h, 0) AS signals_24h,
                           COALESCE(s.signals_7d, 0) AS signals_7d,
                           s.last_seen_at,
                           EXTRACT(
                               EPOCH FROM (now() - s.last_seen_at)
                           )::bigint AS age_seconds,
                           lp.last_poll_outcome,
                           COALESCE(p.recent_error_count, 0)
                               AS recent_error_count
                      FROM ids i
                      LEFT JOIN heads h USING (source_id)
                      LEFT JOIN sig s USING (source_id)
                      LEFT JOIN poll p USING (source_id)
                      LEFT JOIN latest_poll lp USING (source_id)
                     ORDER BY signals_24h DESC, i.source_id
                     LIMIT 1000
                    """
                )
        except Exception as exc:  # noqa: BLE001 — degrade to empty, HTTP 200
            logger.info("v3.system.source_firing.unavailable err=%s", exc)
            return []

        out: list[SourceFiringRow] = []
        for r in rows:
            state = r["state"]
            signals_24h = int(r["signals_24h"] or 0)
            recent_errors = int(r["recent_error_count"] or 0)
            age = r["age_seconds"]
            age_int = int(age) if age is not None else None
            if state is not None and state != "active":
                status: str = "paused"
            elif recent_errors > 0:
                status = "error"
            elif signals_24h > 0:
                status = "firing"
            else:
                status = "silent"
            out.append(
                SourceFiringRow(
                    source_id=str(r["source_id"]),
                    state=state,
                    signals_24h=signals_24h,
                    signals_7d=int(r["signals_7d"] or 0),
                    last_seen_at=r["last_seen_at"],
                    age_seconds=age_int,
                    last_poll_outcome=r["last_poll_outcome"],
                    recent_error_count=recent_errors,
                    status=status,  # type: ignore[arg-type]
                )
            )
        return out

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
            candidate_module_path = _candidate_target_path(payload)
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
        _principal: str = Depends(require_bearer),
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
        return await _apply_optimizer_review(
            deps, candidate_id=candidate_id, body=body,
        )

    @router.get("/streams/consumer_lag", response_model=list[ConsumerLagRow])
    async def streams_consumer_lag(
        principal: str = Depends(require_bearer),
    ) -> list[ConsumerLagRow]:
        """JetStream durable-consumer lag for the signal stream (StreamLag).

        Projects ``num_pending`` (headline lag) + the ack/redelivery counters
        per per-target/per-source consumer on ``legba_signals`` via the JSM.
        Fully defensive: if NATS is unwired or the query fails it returns an
        empty list (HTTP 200) — the panel renders "no lag" instead of the UI
        polling a 404 every 5s.
        """
        from ..nats import SIGNAL_STREAM_NAME

        nats = getattr(deps, "nats_store", None)
        if nats is None:
            return []
        try:
            jsm = nats.nc.jsm()
        except Exception:  # noqa: BLE001 — nats unwired / not connected
            return []
        out: list[ConsumerLagRow] = []
        try:
            consumers = await jsm.consumers_info(SIGNAL_STREAM_NAME)
        except Exception as exc:  # noqa: BLE001 — stream absent / transient
            logger.info("v3.consumer_lag.unavailable err=%s", exc)
            return []
        # Orphan filter (phantom-lag guard). A durable whose ack_floor sits BELOW
        # the stream's first retained sequence is a superseded/abandoned consumer —
        # e.g. the per-target durables replaced by the shared `legba-trigger-engine`,
        # or dead autowire generations. Its `num_pending` is a retention artifact
        # (the WHOLE retained window counted against a frozen ack floor), NOT real
        # backlog, so unfiltered it buries the panel under tens of thousands of
        # phantom lag. Drop those rows; keep every consumer at/above the retained
        # window (its lag is genuine). Degrade to NO filter if stream_info is
        # unavailable — never hide a real consumer on a transient error.
        stream_first_seq = 0
        try:
            sinfo = await jsm.stream_info(SIGNAL_STREAM_NAME)
            stream_first_seq = int(
                getattr(getattr(sinfo, "state", None), "first_seq", 0) or 0
            )
        except Exception as exc:  # noqa: BLE001 — degrade to unfiltered
            logger.info("v3.consumer_lag.stream_info_unavailable err=%s", exc)
        dropped = 0
        for ci in consumers or []:
            durable = str(getattr(ci, "name", "") or "")
            ack_floor = getattr(ci, "ack_floor", None)
            af_seq = getattr(ack_floor, "stream_seq", None) if ack_floor else None
            if stream_first_seq and af_seq is not None and af_seq < stream_first_seq:
                dropped += 1
                continue
            scope_kind, scope_id = "consumer", durable
            low = durable.lower()
            if "target" in low or low.startswith("tgt"):
                scope_kind = "target"
            elif "source" in low or low.startswith("src"):
                scope_kind = "source"
            delivered = getattr(ci, "delivered", None)
            out.append(
                ConsumerLagRow(
                    stream=SIGNAL_STREAM_NAME,
                    durable=durable,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    num_pending=int(getattr(ci, "num_pending", 0) or 0),
                    num_ack_pending=int(getattr(ci, "num_ack_pending", 0) or 0),
                    num_redelivered=int(getattr(ci, "num_redelivered", 0) or 0),
                    num_waiting=int(getattr(ci, "num_waiting", 0) or 0),
                    delivered_stream_seq=getattr(delivered, "stream_seq", None) if delivered else None,
                    ack_floor_stream_seq=af_seq,
                )
            )
        if dropped:
            logger.info(
                "v3.consumer_lag.orphans_filtered dropped=%d kept=%d first_seq=%s",
                dropped, len(out), stream_first_seq,
            )
        return out

    @router.get("/eval/scorecard", response_model=list[ScorecardRow])
    async def eval_scorecard(
        analyst_id: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=2000),
        principal: str = Depends(require_bearer),
    ) -> list[ScorecardRow]:
        """Cross-analyst eval scorecard — one row per critic judgement.

        Sourced from the dual-sink ``analyst_outputs`` critique rows (kind=
        'critique'), whose payload carries the ANALYZED analyst id + the
        per-rubric-axis ``scores`` + ``overall_score`` (the ``analyst_critiques``
        table keys ``trace_id`` to the JUDGE's run, so it can't recover the
        analyzed analyst on its own). The UI (``buildScorecards``) rolls these
        up per analyst into latest/mean/trend/axis-means.
        """
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text AS id,
                       data->>'analyzed_analyst_id'      AS analyst_id,
                       data->>'analyzed_analyst_version' AS analyst_version,
                       COALESCE(data->'scores', '{}'::jsonb) AS scores,
                       COALESCE((data->>'overall_score')::float8, 0.0) AS overall_score,
                       created_at AS produced_at
                  FROM public.analyst_outputs
                 WHERE kind = 'critique'
                   AND data->>'analyzed_analyst_id' IS NOT NULL
                   AND ($1::text IS NULL OR data->>'analyzed_analyst_id' = $1)
                 ORDER BY created_at DESC
                 LIMIT $2
                """,
                analyst_id, limit,
            )
        out: list[ScorecardRow] = []
        for r in rows:
            raw = r["scores"]
            parsed = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            scores = {
                str(k): float(v)
                for k, v in parsed.items()
                if isinstance(v, (int, float))
            }
            produced = r["produced_at"]
            out.append(
                ScorecardRow(
                    id=r["id"],
                    analyst_id=r["analyst_id"],
                    analyst_version=r["analyst_version"],
                    scores=scores,
                    overall_score=float(r["overall_score"] or 0.0),
                    produced_at=(
                        produced.isoformat()
                        if hasattr(produced, "isoformat")
                        else str(produced)
                    ),
                )
            )
        return out

    return router
