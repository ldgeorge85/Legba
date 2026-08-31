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

from .. import correctness_axis
from ..schemas import AnalystDescriptor
from . import source_freshness
from .api import RegistryAPIDeps, require_bearer
from .descriptor import Family
from .errors import (
    DescriptorNotFound,
    DescriptorValidationError,
    IllegalLifecycleTransition,
    VersionConflict,
)

# B0-5 (audit W6) — the scorecard↔composition reconciliation reducers + model
# now live in a PURE, substrate-free module so the journal's ``get_assessments``
# instrument (runtime image) reconciles with the SAME code this endpoint uses.
# Aliased to the historical private names so the call sites below stay identical
# (this extraction is a no-behavior-change refactor).
from .scorecard_reconcile import (
    ScorecardDisagreement,
    composition_usages as _composition_usages,
    scorecard_disagreements as _scorecard_disagreements,
)

# H3 — the escalation-delivery route models + pure reducer moved to their own
# leaf module (the module-size gate's extract-don't-raise rule; see
# escalation_delivery.py's docstring). Aliased to the historical private
# function names so the call sites below — and test_v3_escalations.py, which
# imports these names off THIS module — stay identical.
from .escalation_delivery import (
    EscalationDeliveriesResponse,
    EscalationDeliveryRow,
    EscalationDeliverySummary,
    EscalationNonDelivery,
    build_escalations_response as _build_escalations_response,
    escalation_delivery_row as _escalation_delivery_row,
)

logger = logging.getLogger(__name__)

# KW-3 — the `staleness_debt` gauge, MIRRORED from
# ``analysts.deterministic_handlers.claim_watch._STALENESS_DEBT_SQL``. A local
# copy on purpose (the registry-slim rule: importing the handler package would
# drag the embedder/alert-scan sub-handler modules into this route module —
# same rationale as `substrate_reads_api._FAITH_FLOOR` and
# `goldset_sampling.DEFAULT_UNITS`). Lockstep is test-enforced:
# tests/data_pkg/test_v3_staleness_debt.py::test_staleness_debt_sql_mirrors_matcher
# asserts byte equality with the matcher's own constant, so the route can never
# publish a different number than the receipt.
_STALENESS_DEBT_SQL = """
    SELECT count(*)::int AS debt
      FROM review_flags rf
     WHERE rf.closed_at IS NULL
       AND NOT EXISTS (
             SELECT 1 FROM analyst_outputs ao
              WHERE ao.id = rf.output_id
                AND ao.superseded_by IS NOT NULL
       )
"""

#: The analyst id the claim_watch matcher writes its traces under (the
#: deterministic META analyst's descriptor id) — used only to report WHEN the
#: gauge was last recomputed, so a reader can tell a genuine zero from a
#: matcher that never ran.
_CLAIM_WATCH_ANALYST_ID = "claim_watch"


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

    M-2: ``judge_pipeline_version`` names WHICH judge produced ``overall_score``.
    The UI rolls these rows into per-analyst means and trends, so without the
    stamp a trend line silently spans a judge swap (the 07-30 change moved mean
    faithfulness +7pp on its own) and reads as an analyst that improved
    overnight. ``None`` = graded before the split key existed — a real
    population, not a gap, and it must be shown as its own series rather than
    merged into the current one.
    """
    id: str
    analyst_id: str
    analyst_version: str | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    overall_score: float
    ground_truth_accuracy: float | None = None
    produced_at: str
    judge_pipeline_version: str | None = None


class BandCalibrationSection(BaseModel):
    """P2-3 — the band-calibration harness aggregate, in its OWN section.

    The freshest ``band_calibration_tracker`` finding's ``data.data.
    band_calibration`` block (scorecard band transitions logged as resolvable
    claims, auto-resolved at T0+14d/28d against LATER scorecard rows only).
    Read INLINE registry-side (the ``eval_calibration`` slim precedent).

    HONESTY: bands are ordinal risk categories, not probabilities — this
    section carries persistence/reversal RATES with their sample sizes and NO
    Brier / Brier-skill key of any kind (``no_brier`` + ``honesty_note`` state
    that explicitly). Rates are ``None`` inside ``horizons`` when the scored
    denominator is zero — an honest empty state, never a fabricated number.
    ``available`` is false before the tracker's first finding exists.
    """
    available: bool
    produced_at: str | None = None
    claims_total: int | None = None
    resolution_spec: str | None = None
    # Per-horizon overall aggregate: {"14d": {resolved, open, outcomes,
    # confirmed, reverted, scored, persistence_rate, reversal_rate, ...}, "28d": …}
    horizons: dict[str, Any] = Field(default_factory=dict)
    # Same block shape split by direction (deterioration / improvement) and by
    # scorecard dimension.
    by_direction: dict[str, Any] = Field(default_factory=dict)
    by_dimension: dict[str, Any] = Field(default_factory=dict)
    no_brier: bool = True
    honesty_note: str | None = None
    refs: list[str] = Field(default_factory=list)


class UnitCorrectnessRow(BaseModel):
    """One bounded unit's OPERATOR correctness — the judge-independent axis.

    Deliberately NOT a section of :class:`CalibrationScoreboard`. Correctness is
    a human's read of whether the finding was RIGHT; calibration and
    faithfulness are machine aggregates over the pipeline's own judge. The
    standing rule (``labels_api`` P2-5) is that they are never pooled, and
    giving this axis its own route is that rule made structural rather than
    documented.

    TINY-n: ``correctness`` is reported even when ``sufficient`` is False,
    because it is the only judge-independent signal that exists and hiding it
    is how it stayed invisible for a week. It NEVER travels alone —
    ``n_scored``, ``mix`` and ``status`` are always beside it, so a reader sees
    "one verdict, 'correct'", not "1.00".
    """
    unit: str
    correctness: float | None = None
    n_labels: int = 0
    n_scored: int = 0
    n_unresolvable: int = 0
    mix: dict[str, int] = Field(default_factory=dict)
    sufficient: bool = False
    min_labels: int = 0
    status: str
    display: str
    # The DIAGNOSTIC second axis, from the latest scorer run (source-id overlap
    # vs `unit_reference_labels`). Segregated: never averaged with the above.
    correctness_vs_reference: float | None = None
    n_reference_labels: int = 0
    reference_status: str | None = None
    # The unit's faithfulness from the same scorer run, carried for CONTRAST —
    # the gap between the two is the finding the gold-set round produced. Named
    # with its judge population so it is never read as a pooled number.
    faithfulness: float | None = None
    judge_pipeline_version: str | None = None


class UnitCorrectnessBoard(BaseModel):
    """``GET /v3/eval/correctness`` — the operator gold-set axis, surfaced.

    The 2026-08-02 engine review found this number (0.625 over n=8, against a
    same-window faithfulness of 0.92) computed, stored, and displayed nowhere a
    reader would meet it. This route is where it lives.

    ``fleet`` pools every verdict ONCE rather than averaging per-unit means:
    with n=1 on most units a mean of means weights a single verdict as heavily
    as a fully-labelled unit. ``labeling`` reports the loop's own health (how
    many weeks are pinned, how many of this week's samples are judged) so a
    stalled labeling loop is visible next to the number it starves.
    """
    available: bool
    fleet: UnitCorrectnessRow | None = None
    units: list[UnitCorrectnessRow] = Field(default_factory=list)
    scored_at: str | None = None
    labeling: dict[str, Any] = Field(default_factory=dict)
    honesty_note: str


class CalibrationScoreboard(BaseModel):
    """The platform's HONEST skill scoreboard — the freshest ``calibration_tracking``
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

    ``band_calibration`` (P2-3) is a purely ADDITIVE section (existing consumers
    parse named keys only): the band-persistence harness aggregate from the
    ``band_calibration_tracker`` finding — segregated like the acute pilot, and
    carrying NO Brier by design (bands are not probabilities).
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
    # P2-3 — the band-calibration harness section (additive; None only when the
    # section read itself failed, available=False when no finding exists yet).
    band_calibration: BandCalibrationSection | None = None


class DeskBaselineRow(BaseModel):
    """P3-7 — one persisted per-desk statistical baseline (``desk_baselines``).

    A projection of one ``desk_baselines`` sidecar row (migration 0103): the
    trailing baseline EXPECTATION + uncertainty band + current-window deviation
    for one desk × metric, computed daily by the ``desk_baseline`` deterministic
    analyst over our own substrate.

    HONESTY (the whole point of P3-7): this is a DESCRIPTIVE statistical
    baseline, NOT a forecast. ``expected`` is a trailing mean rate; there is no
    Brier, no skill score, no probability-of-event, and nothing here is a
    prediction-as-claim. ``deviation`` (within / above / below) is the useful
    anomaly signal — computed with the SAME absolute floors as the P1-3
    baseline_deviation trigger, so a perennially-quiet desk's σ≈0 blip never
    reads as a deviation. ``insufficient_history`` warns the band rests on thin
    history WITHOUT suppressing an absolute-floor exceedance.
    """
    desk_id: str
    metric: str
    geo: list[str] = Field(default_factory=list)
    baseline_days: int
    n_sigma: float
    expected: float
    center_median: float
    robust_sigma: float
    band_low: float
    band_high: float
    current: float
    deviation: str
    deviation_sigma: float | None = None
    min_current_floor: float
    sample_days: int
    active_days: int
    insufficient_history: bool
    spillover_current: float
    features: dict[str, Any] = Field(default_factory=dict)
    computed_at: str | None = None


class DeskBaselineBoard(BaseModel):
    """P3-7 — the per-desk baseline board (the divergence surfacing).

    Projects the ``desk_baselines`` sidecar so the operator/UI can see "desk X
    is running Kσ above its 28-day baseline" — the honest anomaly read. Reads
    INLINE registry-side (the ``eval_calibration`` slim precedent), no
    deterministic-handler import. An empty ``rows`` (no baseline computed yet) is
    a first-class honest state, NOT a 404. ``note`` carries the explicit
    no-forecast framing so no consumer can misread the board as a prediction.
    """
    available: bool = False
    computed_at: str | None = None
    note: str = (
        "Descriptive statistical baseline over our own substrate — NOT a "
        "forecast/prediction/skill claim. `deviation` is the current 24h window "
        "vs the trailing band (absolute floors mirror the P1-3 trigger)."
    )
    counts: dict[str, int] = Field(default_factory=dict)
    rows: list[DeskBaselineRow] = Field(default_factory=list)


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

    ``disagreements`` (B0-5, audit W6) reconciles this card against the CURRENT
    ``country_composition`` head: every finding the scorecard excluded from a
    dimension's basis that the composition nonetheless cites / derives from.
    Empty is the normal post-B0-1 state (both products share the faithfulness
    floor now); a row makes any REMAINING divergence — e.g. from the different
    windows (scorecard 336h vs composition 24h) — visible instead of silent.

    ``banding_semantics`` / ``damping_semantics`` (H3) are the card's own
    :data:`scorecard_banding.BANDING_SEMANTICS` /
    :data:`scorecard_banding.DAMPING_SEMANTICS` stamps, projected verbatim so a
    reader off this route — not just the deterministic handlers that already
    parse ``data.bands`` themselves — can tell which contract wrote a card
    without guessing a deploy date. ``None`` for a card written before a stamp
    existed (every damping_semantics-less card predates H3).
    """
    target_id: str
    id: str
    produced_at: str
    generated_at: str | None = None
    floors: dict[str, float] = Field(default_factory=dict)
    dimensions: dict[str, Any] = Field(default_factory=dict)
    composition: dict[str, Any] = Field(default_factory=dict)
    disagreements: list[ScorecardDisagreement] = Field(default_factory=list)
    banding_semantics: str | None = None
    damping_semantics: str | None = None


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


class StalenessDebtReason(BaseModel):
    """One ``review_flags.reason`` bucket within the open debt."""

    reason: str
    open_flags: int


class StalenessDebtOut(BaseModel):
    """``GET /system/staleness-debt`` — the ``claim_watch`` review-flag gauge.

    The number the KW-3 matcher already computes on every run and buries in its
    ``analyst_traces`` receipt, finally readable without opening a receipt.

    WHAT IT COUNTS: open ``review_flags`` (migration 0107) whose flagged
    consumer output is still LIVE — i.e. not itself superseded. A flag on an
    output that has since been superseded is real history but not live debt:
    the record already moved on. ``superseded_consumer_flags`` carries that
    remainder so the two never get confused.

    HONESTY (SEAMS #49, unchanged by exposing it): this is a *flags-found,
    match-unverified* count, NEVER a corrected-or-closed metric. The claim_watch
    CLOSER is not built — nothing in-tree writes correction content, writes back
    to a flagged producer, or recomposes anything; flags close by supersession
    only. ``match_verified`` is therefore hard-``False`` on the wire, and stays
    False until the DEC-K1 match-precision bar is formally met and recorded. A
    reader must not read this as "N things are wrong", only as "N live products
    rest on foundations the matcher believes have moved".
    """

    #: Open flags on still-live consumers — the receipt's own gauge, computed
    #: with the matcher's own SQL (mirrored; drift is test-enforced).
    staleness_debt: int = 0
    #: Every open flag, including those whose consumer is already superseded.
    open_flags: int = 0
    #: ``open_flags - staleness_debt`` — open flags the record already outran.
    superseded_consumer_flags: int = 0
    #: Distinct still-live consumer outputs carrying debt.
    flagged_consumers: int = 0
    #: Distinct moved foundations behind the debt.
    moved_foundations: int = 0
    #: Flags closed by supersession, ever (flags are never deleted).
    closed_flags: int = 0
    oldest_open_at: datetime | None = None
    newest_open_at: datetime | None = None
    by_reason: list[StalenessDebtReason] = Field(default_factory=list)
    #: When the matcher last ran (``analyst_traces``), so a reader can tell a
    #: genuine zero from a matcher that has not run.
    last_matcher_run_at: datetime | None = None
    #: HARD False — see the class docstring. Present on the wire so no consumer
    #: can mistake this for a verified/closed number.
    match_verified: bool = False


class SourceFiringRow(BaseModel):
    """One source's firing snapshot for the System Status panel.

    Composes ``signals`` (count + freshest ``created_at`` per ``source_id``),
    ``source_poll_outcomes`` (latest poll outcome + recent error count — one
    row per poll, ``success``/``empty``/``error``, since migration 0114; it was
    a FAILURE-ONLY ledger before that, which is why this route derives firing
    from signal production and keeps the ledger strictly secondary), and the
    head ``source_descriptors`` row (declared ``state``).

    ``status`` is derived PRIMARILY from actual signal production (recency),
    with the poll ledger used only as a secondary error signal so a
    genuinely-producing source is never mislabelled ``error``/``silent``:

      * ``paused``  — descriptor state is not ``active``
      * ``firing``  — produced a signal within the last 48h (regardless of
        any recent ``empty``/``error`` poll rows)
      * ``error``   — no recent signal AND recent hard poll errors
      * ``silent``  — active head, no recent signal, no recent errors

    ADDITIVE (A7 freshness taxonomy): ``freshness_grade`` grades the freshest
    signal's age against a budget derived from the source's OWN declared
    cadence (``body.cadence.schedule.raw`` cron × grace — see
    :mod:`legba.data.registry.source_freshness`), reported alongside as
    ``budget_minutes``:

      * ``ok`` / ``stale`` / ``warn`` — within budget / over it / badly over
        (>3× budget)
      * ``empty``    — active + cadence-declared but NEVER produced a signal
      * ``ungraded`` — no parsable cadence declaration (never a fake ``ok``),
        or a non-active head (no live polling expectation to grade against);
        ``budget_minutes`` is ``None`` exactly when no budget was derivable
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
    freshness_grade: Literal["ok", "stale", "warn", "empty", "ungraded"] = (
        "ungraded"
    )
    budget_minutes: int | None = None


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


def _reduce_band_calibration(row: Any) -> BandCalibrationSection:
    """Reduce the freshest ``band_calibration_tracker`` finding row to the
    additive P2-3 scoreboard section.

    The writer emits ``kind='finding'`` with the aggregate one JSONB level down
    at ``data.data.band_calibration`` (the calibration_tracking nesting
    contract). Fully defensive: a missing row, an unreadable payload, or a
    finding without the section reads ``available=False`` — never a fabricated
    aggregate, never an exception out of the calibration read.
    """
    if row is None:
        return BandCalibrationSection(available=False)
    try:
        raw = row["data"]
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        data = payload.get("data") if isinstance(payload, dict) else None
        bc = data.get("band_calibration") if isinstance(data, dict) else None
        if not isinstance(bc, dict):
            return BandCalibrationSection(available=False)
        produced = row["produced_at"]
        claims_total = bc.get("claims_total")
        return BandCalibrationSection(
            available=True,
            produced_at=(
                produced.isoformat()
                if hasattr(produced, "isoformat")
                else str(produced)
            ),
            claims_total=(
                claims_total
                if isinstance(claims_total, int) and not isinstance(claims_total, bool)
                else None
            ),
            resolution_spec=(
                str(bc["resolution_spec"])
                if bc.get("resolution_spec") is not None
                else None
            ),
            horizons=bc.get("horizons") if isinstance(bc.get("horizons"), dict) else {},
            by_direction=(
                bc.get("by_direction")
                if isinstance(bc.get("by_direction"), dict)
                else {}
            ),
            by_dimension=(
                bc.get("by_dimension")
                if isinstance(bc.get("by_dimension"), dict)
                else {}
            ),
            no_brier=bool(bc.get("no_brier", True)),
            honesty_note=(
                str(bc["honesty_note"]) if bc.get("honesty_note") is not None else None
            ),
            refs=[str(row["id"])],
        )
    except Exception:  # noqa: BLE001 — additive section never breaks the read
        logger.debug("v3_api.band_calibration_reduce_failed", exc_info=True)
        return BandCalibrationSection(available=False)


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
        "/system/staleness-debt",
        response_model=StalenessDebtOut,
    )
    async def system_staleness_debt(
        principal: str = Depends(require_bearer),
    ) -> StalenessDebtOut:
        """The ``claim_watch`` review-flag debt — the first read route for it.

        Until now the gauge existed ONLY inside the producing run's
        ``analyst_traces`` receipt (SEAMS #49), so reading it meant reading a
        receipt. The headline number here is computed with the matcher's OWN
        SQL (mirrored above, drift test-enforced) rather than a re-derivation,
        so the route and the receipt cannot disagree.

        What the fields mean, and what they deliberately do not: see
        :class:`StalenessDebtOut`. In short — open flags on still-live
        consumers, a *flags-found, match-unverified* count, never a
        corrected-or-closed metric. ``match_verified`` is hard-``False``.

        Fully defensive: any query failure (including migration 0107 not
        applied) degrades to an honest all-zero payload at HTTP 200, with
        ``last_matcher_run_at`` null — the same "no data" shape a genuinely
        empty flag table produces, which is the truthful reading either way.
        """
        try:
            async with deps.descriptor_registry.pg.acquire() as conn:
                debt_row = await conn.fetchrow(_STALENESS_DEBT_SQL)
                agg_row = await conn.fetchrow(
                    """
                    SELECT
                        count(*) FILTER (WHERE rf.closed_at IS NULL)::int
                            AS open_flags,
                        count(*) FILTER (WHERE rf.closed_at IS NOT NULL)::int
                            AS closed_flags,
                        count(DISTINCT rf.output_id) FILTER (
                            WHERE rf.closed_at IS NULL
                              AND NOT EXISTS (
                                    SELECT 1 FROM analyst_outputs ao
                                     WHERE ao.id = rf.output_id
                                       AND ao.superseded_by IS NOT NULL
                              )
                        )::int AS flagged_consumers,
                        count(DISTINCT rf.founded_on_id) FILTER (
                            WHERE rf.closed_at IS NULL
                              AND NOT EXISTS (
                                    SELECT 1 FROM analyst_outputs ao
                                     WHERE ao.id = rf.output_id
                                       AND ao.superseded_by IS NOT NULL
                              )
                        )::int AS moved_foundations,
                        min(rf.created_at) FILTER (WHERE rf.closed_at IS NULL)
                            AS oldest_open_at,
                        max(rf.created_at) FILTER (WHERE rf.closed_at IS NULL)
                            AS newest_open_at
                      FROM review_flags rf
                    """
                )
                reason_rows = await conn.fetch(
                    """
                    SELECT rf.reason, count(*)::int AS open_flags
                      FROM review_flags rf
                     WHERE rf.closed_at IS NULL
                     GROUP BY rf.reason
                     ORDER BY open_flags DESC, rf.reason
                     LIMIT 50
                    """
                )
                last_run = await conn.fetchval(
                    """
                    SELECT max(run_started_at)
                      FROM analyst_traces
                     WHERE analyst_id = $1
                    """,
                    _CLAIM_WATCH_ANALYST_ID,
                )
        except Exception as exc:  # noqa: BLE001 — degrade to zeros, HTTP 200
            logger.info("v3.system.staleness_debt.unavailable err=%s", exc)
            return StalenessDebtOut()

        debt = int(debt_row["debt"]) if debt_row is not None else 0
        open_flags = int(agg_row["open_flags"] or 0) if agg_row else 0
        return StalenessDebtOut(
            staleness_debt=debt,
            open_flags=open_flags,
            superseded_consumer_flags=max(0, open_flags - debt),
            flagged_consumers=int(agg_row["flagged_consumers"] or 0) if agg_row else 0,
            moved_foundations=int(agg_row["moved_foundations"] or 0) if agg_row else 0,
            closed_flags=int(agg_row["closed_flags"] or 0) if agg_row else 0,
            oldest_open_at=agg_row["oldest_open_at"] if agg_row else None,
            newest_open_at=agg_row["newest_open_at"] if agg_row else None,
            by_reason=[
                StalenessDebtReason(
                    reason=str(r["reason"]), open_flags=int(r["open_flags"]),
                )
                for r in reason_rows
            ],
            last_matcher_run_at=last_run,
        )

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
        (``source_poll_outcomes`` — one row per poll since migration 0114:
        ``success`` / ``empty`` / ``error``; before it, successes were never
        inserted, so a firing source's newest row stayed frozen at whatever it
        last failed with — which is why this route derives firing state from
        signal production and never lets the ledger flip a producing source to
        ``error``/``silent``), and the declared head descriptor ``state``
        (``source_descriptors`` WHERE is_head).

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
                        SELECT descriptor_id AS source_id, state,
                               -- A7 freshness taxonomy: the declared poll
                               -- cadence the per-source budget derives from
                               body->'cadence'->'schedule'->>'raw'
                                   AS cadence_raw
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
                        -- SECONDARY only: source_poll_outcomes informs error
                        -- context but never primary firing state (real signal
                        -- production is the truth). It records successes too
                        -- since migration 0114 — this filter counts only the
                        -- 'error' rows either way, so the read is unchanged.
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
                           h.cadence_raw,
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
            # A7 freshness taxonomy — graded against the source's OWN
            # cadence-derived budget (memoized per cron expression); an
            # undeclared/unparsable cadence grades ungraded, never a fake ok.
            budget_minutes = source_freshness.derive_budget_minutes(
                r["cadence_raw"]
            )
            grade = source_freshness.grade_freshness(
                state=state,
                age_seconds=age_int,
                budget_minutes=budget_minutes,
            )
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
                    freshness_grade=grade,
                    budget_minutes=budget_minutes,
                )
            )
        return out

    @router.get(
        "/system/escalations",
        response_model=EscalationDeliveriesResponse,
    )
    async def system_escalations(
        status: str | None = Query(default=None),
        sink_kind: str | None = Query(default=None),
        target_id: str | None = Query(default=None),
        severity: str | None = Query(default=None),
        window_hours: int = Query(default=24, ge=1, le=720),
        limit: int = Query(default=200, ge=1, le=1000),
        principal: str = Depends(require_bearer),
    ) -> EscalationDeliveriesResponse:
        """Human-visible escalation-delivery edge (audit finding C3 / decision D1).

        Serves the ``alert_sink_deliveries`` per-delivery audit (migration 0061)
        — the durable record of every escalate/incident emit the ChannelEmitter
        writes — so a human can finally SEE whether an escalation LANDED or went
        NOWHERE. Today those rows land in Postgres + NATS ``channels.escalations``
        but nothing renders them; this route closes that gap.

        Returns:

          * ``summary`` — a rollup over the last ``window_hours`` (default 24h,
            DELIBERATELY unfiltered): delivered / failed / logged_only / retrying
            counts + the ``non_delivery`` total (failed + logged_only) and its
            per-``(sink_kind, status)`` breakdown. That non-delivery signal is
            EXACTLY what the W1-T3 integrity-sweep canary alarms on — surfacing it
            here means the operator sees the same failure the canary does.
          * ``rows`` — the recent deliveries, newest-first, optionally filtered by
            ``status`` / ``sink_kind`` / ``target_id`` / ``severity``, capped at
            ``limit``.

        Read-only + honest: an empty table returns a zeroed summary + no rows (a
        first-class "nothing escalated" state, never fabricated). Fully defensive
        — any query failure degrades to that same honest-empty response at HTTP
        200 so the panel renders "no deliveries" rather than polling a 500.
        """
        try:
            async with deps.descriptor_registry.pg.acquire() as conn:
                summary_rows = await conn.fetch(
                    """
                    SELECT status, sink_kind,
                           count(*) AS n,
                           max(error_message) AS sample_err
                      FROM public.alert_sink_deliveries
                     WHERE attempted_at > now()
                           - make_interval(hours => $1)
                     GROUP BY status, sink_kind
                    """,
                    int(window_hours),
                )
                delivery_rows = await conn.fetch(
                    """
                    SELECT id, alert_row_id, channel_name, sink_kind, sink_target,
                           target_id, severity, effective_confidence, status,
                           error_message, attempt_number, attempted_at,
                           delivered_at, payload_summary
                      FROM public.alert_sink_deliveries
                     WHERE ($1::text IS NULL OR status = $1)
                       AND ($2::text IS NULL OR sink_kind = $2)
                       AND ($3::text IS NULL OR target_id = $3)
                       AND ($4::text IS NULL OR severity = $4)
                     ORDER BY attempted_at DESC, id DESC
                     LIMIT $5
                    """,
                    status, sink_kind, target_id, severity, int(limit),
                )
        except Exception as exc:  # noqa: BLE001 — degrade to honest empty, HTTP 200
            logger.info("v3.system.escalations.unavailable err=%s", exc)
            return EscalationDeliveriesResponse(
                summary=EscalationDeliverySummary(window_hours=int(window_hours)),
            )

        return _build_escalations_response(
            [dict(r) for r in delivery_rows],
            [dict(r) for r in summary_rows],
            window_hours=int(window_hours),
        )

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
                       created_at AS produced_at,
                       -- M-2: the population split key, so the UI's per-analyst
                       -- means and trends never span a judge swap unlabelled.
                       data->'data'->'verification'->>'judge_pipeline_version'
                           AS judge_pipeline_version
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
                    judge_pipeline_version=r["judge_pipeline_version"],
                )
            )
        return out

    @router.get("/eval/calibration", response_model=CalibrationScoreboard)
    async def eval_calibration(
        principal: str = Depends(require_bearer),
    ) -> CalibrationScoreboard:
        """The honest skill scoreboard — the exogenous Brier + the SEGREGATED
        acute-forecast BSS, tagged ready / accumulating / degenerate.

        Reduces the freshest ``calibration_tracking`` finding EXACTLY as
        :meth:`SubstrateQueryPort.get_calibration` (~2091), but reads it INLINE
        here so the registry image stays slim (no runtime / deterministic-handler
        import — the ``journal_api._read_calibration`` precedent, ~329). B0-3
        (read-truth): the writer produces ``kind='finding'`` +
        ``analyst_id='calibration_tracking'`` (nothing writes
        ``kind='calibration'``), and the metrics live one JSONB level down at
        ``data.data`` (the eval_country_scorecard precedent below). The panel
        keys every displayed string off the flags this returns, so a thin exogenous
        sample OR a degenerate acute pilot reads as an honest withheld state, never
        a bare positive number.

        P2-3 (additive): the response also carries a ``band_calibration``
        section — the freshest ``band_calibration_tracker`` finding's
        persistence/reversal aggregate (its OWN keys, NO Brier by design:
        bands are not probabilities). Fully defensive: a failed band read
        degrades to ``available=False`` and never breaks the main scoreboard.
        """
        band_row: Any = None
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, produced_at, data FROM public.analyst_outputs "
                "WHERE kind = 'finding' AND analyst_id = 'calibration_tracking' "
                "AND superseded_by IS NULL "
                "ORDER BY produced_at DESC, id DESC LIMIT 1"
            )
            try:
                band_row = await conn.fetchrow(
                    "SELECT id, produced_at, data FROM public.analyst_outputs "
                    "WHERE kind = 'finding' "
                    "AND analyst_id = 'band_calibration_tracker' "
                    "AND superseded_by IS NULL "
                    "ORDER BY produced_at DESC, id DESC LIMIT 1"
                )
            except Exception:  # noqa: BLE001 — additive, never breaks the read
                band_row = None
        band_calibration = _reduce_band_calibration(band_row)
        if row is None:
            # No calibration finding computed yet — a DISTINCT honest state
            # ("no pilot yet"), not a failed pilot. Both legs read unproven.
            # The band section still reports independently (its tracker may
            # have run first).
            return CalibrationScoreboard(
                available=False, band_calibration=band_calibration
            )
        raw = row["data"]
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
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
            band_calibration=band_calibration,
        )

    @router.get("/eval/correctness", response_model=UnitCorrectnessBoard)
    async def eval_correctness(
        principal: str = Depends(require_bearer),
    ) -> UnitCorrectnessBoard:
        """The OPERATOR gold-set correctness axis (M-1) — judge-independent.

        Its OWN route, not a section of ``/eval/calibration``: correctness is a
        human's read of whether a finding was right, calibration and
        faithfulness are aggregates over the pipeline's own judge, and the
        standing rule is that they never pool. Separate routes make that
        structural.

        The verdict counts are read LIVE from ``correctness_labels`` (n grows
        the moment a label lands, never gated on the scorer's daily cadence);
        the diagnostic source-overlap axis and the faithfulness contrast come
        from the latest ``unit_correctness_scorer`` finding. Both reads are
        defensive — a missing table or an absent scorer run degrades to the
        honest empty board, never a fabricated row.
        """
        op_rows: list[Any] = []
        scorer_row: Any = None
        weeks: list[Any] = []
        async with deps.descriptor_registry.pg.acquire() as conn:
            try:
                op_rows = list(await conn.fetch(correctness_axis.UNIT_LABELS_SQL))
            except Exception:  # noqa: BLE001 — honest empty, never a broken board
                op_rows = []
            try:
                scorer_row = await conn.fetchrow(
                    "SELECT data, produced_at FROM public.analyst_outputs "
                    "WHERE analyst_id = 'unit_correctness_scorer' "
                    "AND kind = 'finding' AND superseded_by IS NULL "
                    "ORDER BY produced_at DESC, id DESC LIMIT 1"
                )
            except Exception:  # noqa: BLE001
                scorer_row = None
            try:
                # The LOOP's health, beside the number it feeds: a sampler that
                # stopped pinning weeks is why n stops growing, and that has to
                # be visible or a flat 0.625 reads as a stable measurement
                # rather than a stalled one.
                weeks = list(await conn.fetch(
                    "SELECT s.week, count(*)::int AS sampled, "
                    "count(l.finding_id)::int AS labeled "
                    "FROM goldset_week_samples s "
                    "LEFT JOIN correctness_labels l "
                    "  ON l.finding_id = s.finding_id "
                    "GROUP BY s.week ORDER BY s.week DESC LIMIT 8"
                ))
            except Exception:  # noqa: BLE001
                weeks = []

        by_unit, fleet = correctness_axis.score_by_unit(op_rows)

        scorer_units: dict[str, Any] = {}
        scored_at: str | None = None
        if scorer_row is not None:
            raw = scorer_row["data"]
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            nested = payload.get("data") if isinstance(payload, dict) else None
            found = nested.get("units") if isinstance(nested, dict) else None
            if isinstance(found, dict):
                scorer_units = found
            produced = scorer_row["produced_at"]
            scored_at = (
                produced.isoformat()
                if hasattr(produced, "isoformat")
                else str(produced)
            )

        def _row(unit: str, record: dict[str, Any]) -> UnitCorrectnessRow:
            rec = scorer_units.get(unit)
            rec = rec if isinstance(rec, dict) else {}
            pop = rec.get("faithfulness_population")
            pop = pop if isinstance(pop, dict) else {}
            faith = rec.get("faithfulness")
            corr_ref = rec.get("correctness_vs_reference")
            return UnitCorrectnessRow(
                unit=unit,
                correctness=record.get("correctness"),
                n_labels=int(record.get("n_labels") or 0),
                n_scored=int(record.get("n_scored") or 0),
                n_unresolvable=int(record.get("n_unresolvable") or 0),
                mix=dict(record.get("mix") or {}),
                sufficient=bool(record.get("sufficient")),
                min_labels=int(record.get("min_labels") or 0),
                status=str(record.get("status")),
                display=correctness_axis.describe(record),
                correctness_vs_reference=(
                    float(corr_ref) if isinstance(corr_ref, (int, float))
                    and not isinstance(corr_ref, bool) else None
                ),
                n_reference_labels=int(rec.get("n_labeled") or 0),
                reference_status=rec.get("status"),
                faithfulness=(
                    float(faith) if isinstance(faith, (int, float))
                    and not isinstance(faith, bool) else None
                ),
                judge_pipeline_version=pop.get("judge_pipeline_version"),
            )

        # Every unit the gold set knows about, plus every unit the scorer
        # reported — a unit with verdicts but no scorer run must still appear,
        # and vice versa. Absence of one axis is never absence of the row.
        all_units = sorted(set(by_unit) | set(scorer_units))
        rows = [_row(u, by_unit.get(u) or correctness_axis.score(())) for u in all_units]

        return UnitCorrectnessBoard(
            available=bool(rows),
            fleet=_row("__fleet__", fleet) if op_rows else None,
            units=rows,
            scored_at=scored_at,
            labeling={
                "weeks": [
                    {
                        "week": w["week"],
                        "sampled": int(w["sampled"] or 0),
                        "labeled": int(w["labeled"] or 0),
                    }
                    for w in weeks
                ],
                "weeks_pinned": len(weeks),
            },
            honesty_note=(
                "Operator gold-set correctness is JUDGE-INDEPENDENT and is "
                "never pooled with faithfulness, the Brier plane, or the "
                "deterministic source-overlap axis. The gold set is "
                "hand-labelled and does not scale by construction: below "
                f"{correctness_axis.MIN_UNIT_LABELS} scored verdicts per unit "
                f"({correctness_axis.MIN_FLEET_LABELS} fleet-wide) the mean is "
                "reported as INDICATIVE and never as a measured rate. "
                "'unresolvable' verdicts are excluded from both numerator and "
                "denominator and reported in the mix."
            ),
        )

    @router.get("/eval/desk_baselines", response_model=DeskBaselineBoard)
    async def eval_desk_baselines(
        desk: str | None = Query(default=None),
        deviating_only: bool = Query(default=False),
        principal: str = Depends(require_bearer),
    ) -> DeskBaselineBoard:
        """P3-7 — the per-desk statistical baseline board (divergence surfacing).

        PROJECTS the ``desk_baselines`` sidecar (migration 0103; the
        ``desk_baseline`` deterministic analyst recomputes it daily) so the
        operator/UI can see, per desk × metric, the trailing baseline
        expectation + uncertainty band + whether the CURRENT 24h window
        deviates — "desk X is running Kσ above its 28-day baseline", the honest
        anomaly read. Read INLINE registry-side (the ``eval_calibration`` slim
        precedent), no deterministic-handler import.

        HONESTY: this is a DESCRIPTIVE statistical baseline, NEVER a forecast —
        no Brier, no skill, no prediction. The response ``note`` states that
        plainly and the deviation carries the SAME absolute floors as the P1-3
        trigger, so a perennially-quiet desk cannot false-fire. Rows are ordered
        most-deviating first (above/below before within, then by |σ|). ``?desk=``
        filters to one desk; ``?deviating_only=true`` drops the within-band
        rows. Empty rows (nothing computed yet, or the table is absent) is a
        first-class honest state — ``available=False``, NOT a 404.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if desk:
            params.append(desk)
            clauses.append(f"desk_id = ${len(params)}")
        if deviating_only:
            clauses.append("deviation <> 'within'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # Most-deviating first: non-within rows ahead of within, then by the
        # magnitude of the running sigma (NULLs last), then a stable desk/metric.
        sql = (
            "SELECT desk_id, metric, geo, baseline_days, n_sigma, expected, "
            "       center_median, robust_sigma, band_low, band_high, current, "
            "       deviation, deviation_sigma, min_current_floor, sample_days, "
            "       active_days, insufficient_history, spillover_current, "
            "       features, computed_at "
            "  FROM public.desk_baselines "
            f"  {where} "
            "ORDER BY (deviation <> 'within') DESC, "
            "         abs(deviation_sigma) DESC NULLS LAST, desk_id, metric"
        )
        try:
            async with deps.descriptor_registry.pg.acquire() as conn:
                rows = await conn.fetch(sql, *params)
        except Exception:  # noqa: BLE001 — a missing/thin table is an honest empty
            logger.debug("v3_api.desk_baselines_read_failed", exc_info=True)
            return DeskBaselineBoard(available=False)

        if not rows:
            return DeskBaselineBoard(available=False)

        def _jsonish(raw: Any) -> Any:
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except (ValueError, TypeError):
                    return None
            return raw

        out: list[DeskBaselineRow] = []
        counts = {"total": 0, "above": 0, "below": 0, "insufficient_history": 0}
        latest: Any = None
        for r in rows:
            geo = _jsonish(r["geo"])
            feats = _jsonish(r["features"])
            dev = str(r["deviation"])
            counts["total"] += 1
            if dev in ("above", "below"):
                counts[dev] += 1
            if r["insufficient_history"]:
                counts["insufficient_history"] += 1
            ca = r["computed_at"]
            if ca is not None and (latest is None or ca > latest):
                latest = ca
            out.append(
                DeskBaselineRow(
                    desk_id=str(r["desk_id"]),
                    metric=str(r["metric"]),
                    geo=[str(g) for g in geo] if isinstance(geo, list) else [],
                    baseline_days=int(r["baseline_days"]),
                    n_sigma=float(r["n_sigma"]),
                    expected=float(r["expected"]),
                    center_median=float(r["center_median"]),
                    robust_sigma=float(r["robust_sigma"]),
                    band_low=float(r["band_low"]),
                    band_high=float(r["band_high"]),
                    current=float(r["current"]),
                    deviation=dev,
                    deviation_sigma=(
                        float(r["deviation_sigma"])
                        if r["deviation_sigma"] is not None
                        else None
                    ),
                    min_current_floor=float(r["min_current_floor"]),
                    sample_days=int(r["sample_days"]),
                    active_days=int(r["active_days"]),
                    insufficient_history=bool(r["insufficient_history"]),
                    spillover_current=float(r["spillover_current"]),
                    features=feats if isinstance(feats, dict) else {},
                    computed_at=(
                        ca.isoformat() if hasattr(ca, "isoformat") else (
                            str(ca) if ca is not None else None
                        )
                    ),
                )
            )
        return DeskBaselineBoard(
            available=True,
            computed_at=(
                latest.isoformat() if hasattr(latest, "isoformat") else (
                    str(latest) if latest is not None else None
                )
            ),
            counts=counts,
            rows=out,
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

        B0-5 (audit W6) — each card also carries ``disagreements``: the
        scorecard reconciled against the CURRENT ``country_composition`` head
        (its ``data.data.citations[*].ref_id``/``source`` + its
        ``derived_from`` lineage column). A row = a finding this scorecard
        EXCLUDED from a dimension's basis that the composition nonetheless
        uses; empty is the normal reconciled state. Fully defensive (the
        ``system_escalations`` precedent): any failure in the reconciliation
        queries degrades to ``disagreements: []`` at HTTP 200 — it never
        breaks the scorecard read itself.
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
                    # H3 — projected verbatim; None for a card written before
                    # the stamp existed (no re-deriving, no guessed value).
                    banding_semantics=bands.get("banding_semantics"),
                    damping_semantics=bands.get("damping_semantics"),
                )
            )

        # B0-5 (audit W6) — reconcile each card against the CURRENT
        # country_composition head. Fully defensive: any failure below degrades
        # to disagreements=[] at HTTP 200 (the system_escalations precedent) —
        # the scorecard read above is already complete and must not break.
        try:
            targets = [card.target_id for card in out]
            if targets:
                async with deps.descriptor_registry.pg.acquire() as conn:
                    comp_rows = await conn.fetch(
                        """
                        SELECT DISTINCT ON (target_id)
                               target_id,
                               derived_from::text[] AS derived_from,
                               data
                          FROM public.analyst_outputs
                         WHERE kind = 'finding'
                           AND analyst_id = 'country_composition'
                           AND superseded_by IS NULL
                           AND target_id = ANY($1::text[])
                         ORDER BY target_id, produced_at DESC, id DESC
                        """,
                        targets,
                    )
                    # Parse each head's LIVE-VERIFIED shapes: citations at
                    # data['data']['citations'] (ref_id + source = the producing
                    # analyst) and the derived_from uuid[] COLUMN. Lineage-only
                    # ids (no covering citation) need an id→analyst_id lookup.
                    parsed: dict[str, tuple[Any, list[str]]] = {}
                    unresolved: set[str] = set()
                    for cr in comp_rows:
                        raw_comp = cr["data"]
                        payload = (
                            json.loads(raw_comp)
                            if isinstance(raw_comp, str)
                            else (raw_comp or {})
                        )
                        payload = payload if isinstance(payload, dict) else {}
                        citations = (payload.get("data") or {}).get("citations")
                        derived = [str(x) for x in (cr["derived_from"] or [])]
                        parsed[cr["target_id"]] = (citations, derived)
                        cited = _composition_usages(citations, [], {})
                        unresolved.update(f for f in derived if f not in cited)
                    derived_analysts: dict[str, str] = {}
                    if unresolved:
                        lookup_rows = await conn.fetch(
                            "SELECT id::text AS id, analyst_id "
                            "FROM public.analyst_outputs "
                            "WHERE id = ANY($1::uuid[])",
                            sorted(unresolved),
                        )
                        derived_analysts = {
                            lr["id"]: lr["analyst_id"]
                            for lr in lookup_rows
                            if lr["analyst_id"]
                        }
                for card in out:
                    comp = parsed.get(card.target_id)
                    if comp is None:
                        continue
                    citations, derived = comp
                    card.disagreements = _scorecard_disagreements(
                        card.dimensions,
                        _composition_usages(citations, derived, derived_analysts),
                    )
        except Exception as exc:  # noqa: BLE001 — degrade to honest empty, HTTP 200
            logger.info(
                "v3.eval.country_scorecard.disagreements_unavailable err=%s", exc
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
