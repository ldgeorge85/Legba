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


class CalibrationScoreboard(BaseModel):
    """The platform's HONEST skill scoreboard — the freshest ``kind='calibration'``
    finding, reduced EXACTLY as :meth:`SubstrateQueryPort.get_calibration` (~2091).

    Read INLINE registry-side (the ``journal_api._read_calibration`` slim precedent,
    ~329) so the eval panel never pulls a runtime handler into the registry image.

    HONESTY (the whole point of P4): ``brier`` / ``brier_exogenous`` is the
    EXOGENOUS-only headline — the only number that measures calibration against
    reality. The acute-forecast pilot lives in its OWN keys and is NEVER pooled into
    the headline. ``forecast_unproven`` / ``calibration_thin`` are the deterministic
    honesty verdict the UI gates on: a thin exogenous sample or a degenerate pilot
    reads as a first-class honest state (``INSUFFICIENT`` / ``withheld``), never a
    bare positive number. ``available`` is false before any calibration finding
    exists — a distinct "no pilot yet" state, NOT a failed pilot.
    """
    available: bool
    produced_at: str | None = None
    # Headline calibration (exogenous-only).
    brier: float | None = None
    brier_exogenous: float | None = None
    exogenous_sample_size: int | None = None
    sample_size: int | None = None
    insufficient_exogenous: bool | None = None
    self_consistency_only: bool | None = None
    # Segregated acute-forecast pilot (n<30, reported honestly — its own keys).
    brier_forecast_acute: float | None = None
    brier_skill_score: float | None = None
    forecast_acute_sample_size: int | None = None
    forecast_acute_ready: bool = False
    forecast_acute_degenerate: bool = False
    forecast_acute_status: str | None = None
    # The deterministic honesty verdict (absence of proof is NOT proof of skill).
    forecast_unproven: bool = True
    calibration_thin: bool = True
    refs: list[str] = Field(default_factory=list)


class CountryScorecard(BaseModel):
    """P4-T3 — the latest banded per-country scorecard (kind='scorecard').

    DISTINCT from :class:`ScorecardRow` (the cross-analyst CRITIC rollup on
    ``/eval/scorecard``): this is the P4-T2 producer's per-country banded verdict,
    served on ``/eval/country_scorecard``. Read INLINE registry-side — it PROJECTS
    the persisted ``data.bands`` (no re-banding, no scorecard_banding /
    deterministic import), so the registry image stays slim (the
    ``journal_api._read_calibration`` precedent).

    HONESTY: one card per active G20 country; a dimension band NEVER exists
    without a real basis id, and an insufficient-evidence dimension carries an
    empty-but-explicit basis (the UI renders the honest not-enough-verified-claims
    state, never a fabricated band). An empty list (no scorecard computed yet) is
    a first-class honest state, NOT a 404.
    """
    target_id: str
    id: str
    produced_at: str
    generated_at: str | None = None
    floors: dict[str, float] = Field(default_factory=dict)
    dimensions: dict[str, Any] = Field(default_factory=dict)
    composition: dict[str, Any] = Field(default_factory=dict)


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

    Composes ``signals`` (count + freshest ``created_at`` per ``source_id``),
    ``source_poll_outcomes`` (latest poll outcome + recent error count — note
    this table is a FAILURE-ONLY ledger: it only logs ``empty``/``error``
    polls, never successes, so its rows must NOT be the primary firing
    signal), and the head ``source_descriptors`` row (declared ``state``).

    ``status`` is derived PRIMARILY from actual signal production (recency),
    with the poll ledger used only as a secondary error signal so a
    genuinely-producing source is never mislabelled ``error``/``silent``:

      * ``paused``  — descriptor state is not ``active``
      * ``firing``  — produced a signal within the last 48h (regardless of
        any recent ``empty``/``error`` poll rows)
      * ``error``   — no recent signal AND recent hard poll errors
      * ``silent``  — active head, no recent signal, no recent errors
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

        Composes signal flow (``signals`` count + freshest ``created_at`` per
        source), the latest poll outcome + recent error count
        (``source_poll_outcomes`` — a FAILURE-ONLY ledger that only records
        ``empty`` / ``error`` polls; successes are never inserted, so absence
        of rows is normal for a firing source and its rows must not flip a
        producing source to ``error``/``silent``), and the declared head
        descriptor ``state`` (``source_descriptors`` WHERE is_head).

        ``status`` is derived PRIMARILY from real signal production and only
        SECONDARILY from the poll ledger (first match wins):

          * ``paused``  — descriptor state is not ``active``
          * ``firing``  — produced a signal within the last 48h, regardless
            of any recent ``empty``/``error`` poll rows
          * ``error``   — active head, no signal in 48h, AND ≥1 hard poll
            error in the last 24h
          * ``silent``  — active head, no signal in 48h, no recent errors

        Obvious template / autowire junk descriptors
        (``src_autowire_p13_%`` / ``src_locked_p13_%`` / ``src_template_p13_%``
        / ``src_tmpl_aw_%`` / ``src_tmpl_ds_%`` / ``src_disc_%``) are excluded
        from the matrix — they are retired separately and would otherwise read
        as normal paused sources.

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
                           -- exclude retired template / autowire junk so it
                           -- does not read as a normal paused source
                           AND descriptor_id NOT LIKE 'src_autowire_p13_%'
                           AND descriptor_id NOT LIKE 'src_locked_p13_%'
                           AND descriptor_id NOT LIKE 'src_template_p13_%'
                           AND descriptor_id NOT LIKE 'src_tmpl_aw_%'
                           AND descriptor_id NOT LIKE 'src_tmpl_ds_%'
                           AND descriptor_id NOT LIKE 'src_disc_%'
                    ),
                    sig AS (
                        -- firing truth is ACTUAL signal production, keyed on
                        -- created_at (when the row landed in the substrate)
                        SELECT source_id,
                               count(*) FILTER (
                                   WHERE created_at > now()
                                         - interval '24 hours'
                               ) AS signals_24h,
                               count(*) FILTER (
                                   WHERE created_at > now()
                                         - interval '7 days'
                               ) AS signals_7d,
                               max(created_at) AS last_seen_at
                          FROM public.signals
                         GROUP BY source_id
                    ),
                    poll AS (
                        -- SECONDARY only: source_poll_outcomes is a
                        -- failure-only ledger (no success rows), so it informs
                        -- error context but never primary firing state
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
                           -- primary firing flag: produced a signal recently
                           -- (48h safe floor — covers a couple of cycles even
                           -- for the slowest cron cadences)
                           (s.last_seen_at IS NOT NULL
                            AND s.last_seen_at > now()
                                - interval '48 hours') AS signals_recent,
                           lp.last_poll_outcome,
                           COALESCE(p.recent_error_count, 0)
                               AS recent_error_count
                      FROM ids i
                      LEFT JOIN heads h USING (source_id)
                      LEFT JOIN sig s USING (source_id)
                      LEFT JOIN poll p USING (source_id)
                      LEFT JOIN latest_poll lp USING (source_id)
                     -- only emit rows that resolve to a real (non-junk) head
                     WHERE h.source_id IS NOT NULL
                     ORDER BY signals_recent DESC, signals_24h DESC,
                              i.source_id
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
            signals_recent = bool(r["signals_recent"])
            age = r["age_seconds"]
            age_int = int(age) if age is not None else None
            # Firing state is AUTHORITATIVE from real signal production; the
            # failure-only poll ledger is secondary and must not flip a
            # genuinely-producing source to error/silent.
            if state is not None and state != "active":
                status: str = "paused"
            elif signals_recent:
                # produced a signal within the 48h floor → firing, even if
                # recent polls logged empty/error (NASA EONET case)
                status = "firing"
            elif recent_errors > 0:
                # no recent signal AND hard poll errors → genuinely erroring
                status = "error"
            else:
                # active head, no recent signal, no recent errors → silent
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

    @router.get("/eval/calibration", response_model=CalibrationScoreboard)
    async def eval_calibration(
        principal: str = Depends(require_bearer),
    ) -> CalibrationScoreboard:
        """The honest skill scoreboard — the exogenous Brier + the SEGREGATED
        acute-forecast BSS, tagged ready / accumulating / degenerate.

        Reduces the freshest ``kind='calibration'`` finding EXACTLY as
        :meth:`SubstrateQueryPort.get_calibration` (~2091), but reads it INLINE
        here so the registry image stays slim (no runtime / deterministic-handler
        import — the ``journal_api._read_calibration`` precedent, ~329). The panel
        keys every displayed string off the flags this returns, so a thin exogenous
        sample OR a degenerate acute pilot reads as an honest withheld state, never
        a bare positive number.
        """
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, produced_at, data FROM public.analyst_outputs "
                "WHERE kind = 'calibration' "
                "ORDER BY produced_at DESC, id DESC LIMIT 1"
            )
        if row is None:
            # No calibration finding computed yet — a DISTINCT honest state
            # ("no pilot yet"), not a failed pilot. Both legs read unproven.
            return CalibrationScoreboard(available=False)
        raw = row["data"]
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        bss = data.get("brier_skill_score")
        ready = bool(data.get("forecast_acute_ready"))
        degenerate = bool(data.get("forecast_acute_degenerate"))
        # The forecast leg counts as PROVEN only if it is ready, non-degenerate,
        # and has earned positive skill (mirrors get_calibration ~2128).
        forecast_proven = (
            ready and not degenerate and isinstance(bss, (int, float)) and bss > 0.0
        )
        exo_n = data.get("exogenous_sample_size")
        calibration_thin = not isinstance(exo_n, int) or exo_n < 5
        produced = row["produced_at"]

        def _int_or_none(v: Any) -> int | None:
            return v if isinstance(v, int) and not isinstance(v, bool) else None

        return CalibrationScoreboard(
            available=True,
            produced_at=(
                produced.isoformat()
                if hasattr(produced, "isoformat")
                else str(produced)
            ),
            brier=data.get("brier"),
            brier_exogenous=data.get("brier_exogenous"),
            exogenous_sample_size=_int_or_none(exo_n),
            sample_size=_int_or_none(data.get("sample_size")),
            insufficient_exogenous=data.get("insufficient_exogenous"),
            self_consistency_only=data.get("self_consistency_only"),
            brier_forecast_acute=data.get("brier_forecast_acute"),
            brier_skill_score=bss if isinstance(bss, (int, float)) else None,
            forecast_acute_sample_size=_int_or_none(
                data.get("forecast_acute_sample_size")
            ),
            forecast_acute_ready=ready,
            forecast_acute_degenerate=degenerate,
            forecast_acute_status=data.get("forecast_acute_status"),
            forecast_unproven=not forecast_proven,
            calibration_thin=calibration_thin,
            refs=[str(row["id"])],
        )

    @router.get("/eval/country_scorecard", response_model=list[CountryScorecard])
    async def eval_country_scorecard(
        target_id: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> list[CountryScorecard]:
        """The latest P4-T2 banded scorecard per active G20 country (or one, when
        ``target_id`` is given).

        Reads the freshest live-head ``kind='scorecard'`` row per country and
        PROJECTS its persisted ``data.bands`` — no re-banding, no
        scorecard_banding / deterministic import (the ``eval_calibration`` /
        ``journal_api._read_calibration`` slim precedent), so the registry image
        stays slim. The path is DELIBERATELY ``/eval/country_scorecard`` (NOT
        ``/eval/scorecard``, which is the cross-analyst critic rollup).

        Returns an empty list when no scorecard has been computed yet — a
        first-class honest state, NOT a 404. Each row's per-dimension bands carry
        the basis ids (empty-but-explicit for an insufficient dimension) the UI
        drills into the P1 evidence + signed lineage.
        """
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (target_id)
                       target_id, id::text AS id, produced_at, data
                  FROM public.analyst_outputs
                 WHERE kind = 'scorecard'
                   AND superseded_by IS NULL
                   AND ($1::text IS NULL OR target_id = $1)
                 ORDER BY target_id, produced_at DESC, id DESC
                """,
                target_id,
            )
        out: list[CountryScorecard] = []
        for r in rows:
            raw = r["data"]
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            # The row's `data` column holds the WHOLE ScorecardPayload dump
            # (title/body/.../data/kind_marker); the product bands live one level
            # deeper under the payload's free-form `data` dict → data.data.bands.
            bands = ((data.get("data") or {}).get("bands")) or {}
            produced = r["produced_at"]
            floors_raw = bands.get("floors") or {}
            floors = {
                str(k): float(v)
                for k, v in floors_raw.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
            out.append(
                CountryScorecard(
                    target_id=r["target_id"],
                    id=r["id"],
                    produced_at=(
                        produced.isoformat()
                        if hasattr(produced, "isoformat")
                        else str(produced)
                    ),
                    generated_at=bands.get("generated_at"),
                    floors=floors,
                    dimensions=bands.get("dimensions") or {},
                    composition=(
                        bands.get("composition") or {"present": False, "basis": []}
                    ),
                )
            )
        return out

    @router.get("/eval/analyst_runtime")
    async def eval_analyst_runtime(
        window_hours: int = Query(default=24, ge=1, le=720),
        principal: str = Depends(require_bearer),
    ) -> list[dict[str, Any]]:
        """Per-analyst run-timing from ``analyst_traces`` over a window.

        Surfaces the run-time observability that is written per run (run_started_at
        / run_ended_at / status) but not otherwise exposed on an API: per analyst,
        the run count, avg/max wall-clock seconds, last run, and non-success count.
        Read-only, inline-SQL, registry-slim (no runtime/deterministic import)."""
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT analyst_id,
                       count(*) AS runs,
                       round(avg(EXTRACT(EPOCH FROM (run_ended_at - run_started_at)))::numeric, 1) AS avg_seconds,
                       round(max(EXTRACT(EPOCH FROM (run_ended_at - run_started_at)))::numeric, 1) AS max_seconds,
                       max(run_started_at) AS last_run_at,
                       count(*) FILTER (
                           WHERE status NOT IN ('success', 'ok', 'completed')
                       ) AS non_success
                  FROM analyst_traces
                 WHERE run_started_at > NOW() - make_interval(hours => $1)
                   AND run_ended_at IS NOT NULL
                 GROUP BY analyst_id
                 ORDER BY runs DESC, analyst_id
                """,
                int(window_hours),
            )
        out: list[dict[str, Any]] = []
        for r in rows:
            lr = r["last_run_at"]
            out.append({
                "analyst_id": r["analyst_id"],
                "runs": int(r["runs"]),
                "avg_seconds": float(r["avg_seconds"]) if r["avg_seconds"] is not None else None,
                "max_seconds": float(r["max_seconds"]) if r["max_seconds"] is not None else None,
                "last_run_at": lr.isoformat() if hasattr(lr, "isoformat") else str(lr),
                "non_success": int(r["non_success"]),
                "window_hours": int(window_hours),
            })
        return out

    return router
