# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GEPA optimizer core — workflow I/O dataclasses + the shared loop (P-CUT).

This module is the single home of the optimizer's GEPA algorithm and its
substrate-agnostic I/O shapes.  It was moved here (mechanically) from the
retired ``legba.runtime.temporal`` package when the Temporal substrate was
deleted — ``temporalio`` left the dependency set with L-205/P-16, which
made ``TemporalClient`` / ``OptimizerWorkflow`` / the temporal worker
permanently unreachable; the live pieces (these dataclasses + the loop)
now live in the package that actually uses them.

Two layers:

  * **Workflow I/O dataclasses** (:class:`OptimizerWorkflowInput` /
    :class:`OptimizerWorkflowResult`) — plain JSON-serializable
    dataclasses; they round-trip through Dapr Workflow's JSON
    serialization via ``dataclasses.asdict``.

  * **The GEPA loop** (:func:`_run_gepa_loop` /
    :func:`run_optimizer_in_process`) — the algorithm itself, executed
    either inside a Dapr Workflow activity (see
    :mod:`legba.runtime.dapr_workflow.workflow`) or synchronously in the
    caller's process via :class:`InProcessWorkflowClient` (the no-sidecar
    fallback + test path).  Both paths share :func:`_run_gepa_loop` so
    the algorithm lives in exactly one place.

GEPA implementation
-------------------
Uses ``dspy.GEPA`` (DSPy 3.0+ teleprompter) when available — that's the
production path per the brief ("DSPy already installed; use dspy.GEPA
if available").  When dspy or the GEPA teleprompter isn't available the
loop degrades to a naive "best-of-N candidates from the parent's
prompt" search so tests can exercise the wiring without a live LLM.

The DSPy-GEPA path is described in the arxiv paper 2507.19457
("Reflective Prompt Evolution Can Outperform Reinforcement Learning"),
specifically §3.2 (reflective-mutation Pareto frontier) — the
teleprompter compiles a student module by repeatedly reflecting on
high-scoring trajectories and proposing instruction edits.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from ...data import correctness_axis

logger = logging.getLogger(__name__)


class PromptModuleImportError(RuntimeError):
    """The descriptor's ``prompt_module`` path does not import.

    A dead reference, not a shape surprise: the optimizer must not build a
    generation on top of a placeholder, because a promoted candidate becomes
    a live analyst's system prompt.
    """


# ---------------------------------------------------------------------------
# Workflow I/O dataclasses (shared with workflow.py + client.py)
# ---------------------------------------------------------------------------


@dataclass
class TrainingSetRef:
    """A small re-fetchable handle to the GEPA training set.

    PAYLOAD-SIZE FIX (pass-by-reference)
    ------------------------------------
    The training set is up to ``MAX_TRAINING_ROWS`` (500) joined
    trace+critique rows, each carrying ~8 KiB of ``input``/``gold`` text —
    inlining the whole list into :class:`OptimizerWorkflowInput` and
    serializing it across the Dapr Workflow internal gRPC channel blows the
    default 4 MB message cap (``RESOURCE_EXHAUSTED: message larger than max
    4234332 vs 4194304``), so the orchestrator never resumes and the stuck
    workflow leaks orphan actor reminders. Instead of the rows, the input
    carries THIS small reference; the workflow worker re-materializes the
    identical rows inside the compile activity (mirroring deep_consult's
    ``resolve_*_stage_deps`` — the worker has substrate access) via
    :func:`materialize_training_set`.

    The fields are exactly the parameters
    :func:`legba.data.analysts.optimizer.read_traces_and_critiques` keys on
    PLUS ``until_ts`` (the original read's wall-clock instant) to pin the
    recent end of the read window — so the re-fetch returns the byte-for-byte
    same row set even though traces may have landed in between (see that
    function's window-anchoring note). All JSON-serializable.
    """

    analyzed_analyst_id: str
    analyzed_analyst_version: str | None = None
    read_window_days: int = 30
    limit: int = 500
    until_ts: str | None = None  # ISO-8601; the original read's wall clock


@dataclass
class OptimizerWorkflowInput:
    """Input dataclass for the optimizer workflow.

    All fields are JSON-serializable so the workflow engine's payload
    serialization can round-trip them.  UUIDs are stored as strings for
    the same reason.
    """

    # What's being optimized.
    analyst_id: str
    analyst_version: str
    parent_prompt_module_path: str

    # Training set — the GEPA loop consumes ``training_set`` (list of joined
    # trace+critique row dicts).  TWO ways it gets populated:
    #
    #   * PASS-BY-REFERENCE (production Dapr-Workflow path): the runtime
    #     leaves ``training_set`` EMPTY and supplies ``training_set_ref`` — a
    #     small re-fetch handle. The workflow worker calls
    #     :func:`materialize_training_set` inside the compile activity to
    #     re-fetch the identical rows. This keeps the serialized input well
    #     under Dapr's 4 MB gRPC message cap (the bug this fixes — inlining
    #     ~500 × ~8 KiB rows overflowed it and wedged the orchestrator).
    #
    #   * INLINE (in-process fallback + tests): ``training_set`` carries the
    #     rows directly (no gRPC hop, no size limit). ``training_set_ref`` is
    #     None and the loop uses the inlined rows as-is.
    #
    # The loop reads ``training_set`` either way; the activity is responsible
    # for hydrating it from the ref BEFORE the loop runs. Lists of dicts
    # because workflow inputs must be JSON-serializable.
    training_set: list[dict[str, Any]] = field(default_factory=list)
    training_set_ref: TrainingSetRef | None = None

    # GEPA hyperparameters per L-105 §4.  Defaults are the per-2026-05-16
    # ratified values; descriptors override via ``eval.optimizer`` dict.
    max_generations: int = 5
    reflection_minibatch_size: int = 3
    auto: str = "light"  # retained for back-compat; superseded by max_metric_calls
    # COST BOUND — the hard cap on GEPA's LLM rollouts per compile (reflection
    # + student calls). Drives dspy.GEPA's ``max_metric_calls`` instead of the
    # open-ended ``auto`` modes. Default 30 ≈ a few minutes / a few k tokens;
    # operators dial up per analyst via eval.optimizer.max_metric_calls.
    max_metric_calls: int = 30

    # Promotion gate to stamp on the resulting candidate.  The workflow
    # doesn't decide promotion — the LIVE gate is :func:`_delta_gates_ok`
    # (stamps ``data.eval.promotable`` at candidate write time) +
    # :func:`legba.data.analysts.optimizer.resolve_promoted_system_prompt`
    # (admits an operator-promoted prompt into inference ONLY when promotable).
    # The workflow just surfaces the policy declared on the descriptor so the
    # eventual row can carry it forward.
    promotion_policy: str = "human_gated"

    # Per-2026-05-16 decision: 50 GT rows default per analyst.
    min_traces_required: int = 50
    min_critiques_required: int = 0

    # ── P4-T6 — the SCOPED, MEASURED GEPA return (faithfulness fitness) ──────────
    # ALL new fields carry defaults so the dataclass round-trips unchanged: an
    # in-flight ``country_optimizer`` input (or any pre-P4-T6 serialized dict fed
    # to ``OptimizerWorkflowInput(**wf_input)`` by workflow.py) deserializes with
    # these defaults, and ``dataclasses.asdict`` re-emits them — the byte-shape of
    # the frozen monolith's result is unaffected (its fitness_metric stays the
    # default ``critique_proxy``, so the MEASURE stage below never engages for it).
    #
    #   * ``fitness_metric``          — ``critique_proxy`` (default; the cheap
    #     deterministic SEARCH heuristic that is the REPORTED fitness today) or
    #     ``faithfulness`` (P4-T6: the paired before/after faithfulness MEASURE
    #     stage). Only ``unit_optimizer`` opts into ``faithfulness``.
    #   * ``min_paired`` / ``min_promote_delta`` / ``faithfulness_valset_max`` —
    #     the paired-eval + promotion gates (mirrored onto data.eval so the LIVE
    #     ``_delta_gates_ok`` write-time stamp can re-check them). Below
    #     ``min_paired`` the delta is HONEST-NULL (degenerate, never promotable).
    #   * ``parent_system_prompt_source`` — ``""`` (import the Python prompt
    #     module, the country/india path) or ``"descriptor"`` (load the ANALYZED
    #     analyst's ``method.system_prompt`` — the inline_target UNIT path, whose
    #     live prompt IS the descriptor system_prompt, not a Python module).
    #   * ``optimizer_analyst_id`` — the OPTIMIZER analyst's OWN id (e.g.
    #     ``unit_optimizer``). The worker resolves its ``eval.optimizer`` config
    #     (fitness_metric + the gates + parent_system_prompt_source) from THIS id
    #     when the caller left them at defaults, so the config lives on the
    #     descriptor (single source of truth) rather than being re-plumbed through
    #     the actor's options.
    fitness_metric: str = "critique_proxy"
    min_paired: int = 8
    min_promote_delta: float = 0.03
    faithfulness_valset_max: int = 12
    parent_system_prompt_source: str = ""
    optimizer_analyst_id: str = ""

    def __post_init__(self) -> None:
        # ``OptimizerWorkflowInput(**wf_input)`` (workflow.py rehydrates the
        # activity payload this way) and ``dataclasses.asdict`` (the client
        # serializes this way) flatten the nested ``TrainingSetRef`` to a
        # plain dict on the round-trip. Coerce it back so worker-side code
        # can read ``ref.analyzed_analyst_id`` rather than ``ref["..."]``.
        if isinstance(self.training_set_ref, dict):
            self.training_set_ref = TrainingSetRef(**self.training_set_ref)


@dataclass
class OptimizerWorkflowResult:
    """Output dataclass returned by the optimizer workflow.

    Mirrors the load-bearing fields of
    :class:`legba.data.provenance.models.PromptModuleCandidatePayload` so
    the actor's run_method can copy them straight into a payload.
    ``temporal_workflow_id`` + ``temporal_run_id`` (the payload's durable-
    workflow handle slots) get stamped by the client at dispatch time
    (the workflow body doesn't know them).
    """

    candidate_prompt_module_text: str
    training_set_size: int
    eval_score: float
    eval_score_delta: float
    gepa_generation: int
    # Snapshot of the parent (baseline) prompt text the candidate was scored
    # against.  Copied into the payload's ``parent_prompt_module_text`` so the
    # operator diff route can render current-vs-candidate without re-importing
    # the prompt module (dspy stays out of the registry process).  Empty on the
    # validation-skip path (the parent text is never loaded there).
    parent_prompt_module_text: str = ""
    # Diagnostic blob — per-generation scores, baseline score, judge id,
    # etc.  Lands in the payload's ``data`` field.
    diagnostics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Test-double / handle protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkflowHandleLike(Protocol):
    """Minimum surface the optimizer kind expects from a workflow handle.

    :class:`legba.runtime.dapr_workflow.client.DaprWorkflowHandle`
    satisfies this naturally; the in-process path returns a
    :class:`StubWorkflowHandle` carrying an already-computed result.
    """

    id: str
    result_run_id: str

    async def result(self) -> OptimizerWorkflowResult: ...


@dataclass
class StubWorkflowHandle:
    """Handle-shaped wrapper around an already-computed in-process result.

    Used by :class:`InProcessWorkflowClient` and the optimizer kind's
    unit tests.  Mirrors the load-bearing fields of a real workflow
    handle so the kind code path is identical between the in-process
    fallback and the Dapr Workflow production path.  The wrapped result
    is REAL (``run_optimizer_in_process`` does the actual GEPA work) —
    only the workflow/run ids are synthetic (``in_process::<id>``).
    """

    id: str
    result_run_id: str
    _result: OptimizerWorkflowResult

    async def result(self) -> OptimizerWorkflowResult:
        return self._result


# ---------------------------------------------------------------------------
# In-process client — the no-sidecar fallback path
# ---------------------------------------------------------------------------


class InProcessWorkflowClient:
    """In-process fallback client (formerly ``InProcessTemporalClient``).

    Runs the GEPA loop synchronously in the caller's process — no
    workflow engine required.  The kind module routes to this when
    ``LEGBA_OPTIMIZER_IN_PROCESS=1`` is set or no Dapr Workflow client
    can be built.

    The result is exactly what the durable workflow would have returned;
    only the workflow_id + run_id are synthetic.
    """

    async def start_optimizer_workflow(
        self,
        workflow_input: OptimizerWorkflowInput,
        *,
        workflow_id: str,
    ) -> WorkflowHandleLike:
        result = await run_optimizer_in_process(workflow_input)
        return StubWorkflowHandle(
            id=workflow_id,
            result_run_id=f"in_process::{workflow_id}",
            _result=result,
        )


def build_default_client(*, force_in_process: bool = False) -> Any:
    """Construct the fallback optimizer client.

    With the Temporal substrate deleted (P-CUT) there is exactly one
    fallback shape left: the in-process GEPA loop.  The production
    durable path is resolved FIRST by
    :func:`legba.data.analysts.optimizer._resolve_workflow_client` via
    :func:`legba.runtime.dapr_workflow.client.build_dapr_workflow_client`;
    this function is the everything-else answer (tests, dev shells, no
    sidecar).  The ``force_in_process`` parameter is kept for caller
    compatibility — every resolution now lands on
    :class:`InProcessWorkflowClient`.
    """
    return InProcessWorkflowClient()


# ---------------------------------------------------------------------------
# Training-set materialization — the worker-side re-fetch (pass-by-reference)
# ---------------------------------------------------------------------------


async def materialize_training_set(
    workflow_input: OptimizerWorkflowInput,
) -> OptimizerWorkflowInput:
    """Hydrate ``workflow_input.training_set`` from its small reference.

    PASS-BY-REFERENCE re-fetch.  When the runtime dispatched the workflow it
    left ``training_set`` empty and carried only a :class:`TrainingSetRef`
    (so the serialized input stays well under Dapr's 4 MB gRPC cap — the bug
    this fixes).  The workflow worker calls THIS inside the compile +
    validate activities to pull the IDENTICAL rows back out of the substrate
    — exactly mirroring how deep_consult's activities call
    ``resolve_deep_consult_stage_deps`` to do their I/O inside the activity.

    Single source of truth for the row shape: it re-runs the kind's own
    :func:`legba.data.analysts.optimizer.read_traces_and_critiques` (window
    pinned via ``until_ts``) and projects with the kind's own
    ``_shape_training_set`` — the SAME two functions the runtime used to build
    the rows it would otherwise have inlined — so the worker-side training set
    is byte-for-byte equivalent to the inline path (same rows, same order,
    same per-field truncation).

    Returns the SAME ``workflow_input`` with ``training_set`` filled and
    ``training_set_ref`` cleared (so a downstream re-entry can't double-fetch).
    No-ops (returns the input untouched) when:

      * there's no ref (the inline / in-process path — ``training_set`` is
        already populated), or
      * ``training_set`` is already non-empty (idempotent — validate may have
        materialized it before compile runs), or
      * the ref can't be resolved to a substrate pool (degrades to the empty-
        training path, which the loop already handles as a noop candidate).
    """
    ref = workflow_input.training_set_ref
    if ref is None or workflow_input.training_set:
        return workflow_input

    pg_store = None
    try:
        from ...data.postgres import PostgresStore
        from ...data.analysts.optimizer import (
            _shape_training_set,
            read_traces_and_critiques,
        )

        until_ts: datetime | None = None
        if ref.until_ts:
            try:
                until_ts = datetime.fromisoformat(ref.until_ts)
            except ValueError:
                logger.warning(
                    "optimizer.materialize.bad_until_ts value=%r — "
                    "falling back to NOW() window",
                    ref.until_ts,
                )

        pg_store = PostgresStore.from_env()
        await pg_store.connect()
        async with pg_store.pool.acquire() as conn:
            rows = await read_traces_and_critiques(
                conn,
                analyzed_analyst_id=str(ref.analyzed_analyst_id),
                analyzed_analyst_version=(
                    str(ref.analyzed_analyst_version)
                    if ref.analyzed_analyst_version
                    else None
                ),
                read_window_days=int(ref.read_window_days),
                limit=int(ref.limit),
                until_ts=until_ts,
            )
        workflow_input.training_set = _shape_training_set(rows)
        workflow_input.training_set_ref = None
        logger.info(
            "optimizer.materialize.ok analyst=%s rows=%d until_ts=%s",
            ref.analyzed_analyst_id, len(workflow_input.training_set),
            ref.until_ts,
        )
    except Exception as exc:  # noqa: BLE001 — never wedge the activity
        # A re-fetch failure must NOT crash the workflow (that would re-create
        # the silent-death class). Leave training_set empty → the loop returns
        # the noop_empty_training candidate, which the kind records as a
        # visible audit row.
        logger.warning(
            "optimizer.materialize.failed analyst=%s err=%r — "
            "proceeding with empty training set (noop candidate)",
            getattr(ref, "analyzed_analyst_id", "?"), exc,
        )
        workflow_input.training_set = []
        workflow_input.training_set_ref = None
    finally:
        if pg_store is not None:
            try:
                await pg_store.close()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
    return workflow_input


# ---------------------------------------------------------------------------
# Validation step — shared by the in-process path + the Dapr activity
# ---------------------------------------------------------------------------


async def validate_training_set_activity(
    workflow_input: OptimizerWorkflowInput,
) -> dict[str, Any]:
    """Validate the training set meets the descriptor's min thresholds.

    Returns a small status dict: ``{"ok": bool, "training_size": int,
    "reason": str}``.  The workflow consults this BEFORE invoking the
    expensive GEPA loop so an under-trained analyst short-circuits
    cleanly rather than wasting LLM budget.

    P4-T6: resolve the OPTIMIZER descriptor's eval.optimizer thresholds FIRST
    (each Dapr activity gets its own serialized input copy, so this must run here
    too, not only in the compile) — else a scoped unit optimizer is gated on the
    monolith's default min_traces_required and short-circuits before the MEASURE
    stage. Best-effort; a fetch failure leaves the passed-in defaults.
    """
    await _apply_optimizer_eval_config(workflow_input)
    n = len(workflow_input.training_set)
    if n < workflow_input.min_traces_required:
        return {
            "ok": False,
            "training_size": n,
            "reason": (
                f"insufficient_traces: have {n} < required "
                f"{workflow_input.min_traces_required}"
            ),
        }
    n_critiqued = sum(
        1 for row in workflow_input.training_set if row.get("critique_score") is not None
    )
    if n_critiqued < workflow_input.min_critiques_required:
        return {
            "ok": False,
            "training_size": n,
            "reason": (
                f"insufficient_critiques: have {n_critiqued} < required "
                f"{workflow_input.min_critiques_required}"
            ),
        }
    return {"ok": True, "training_size": n, "reason": ""}


# ---------------------------------------------------------------------------
# P4-T6 — the MEASURED fitness (paired before/after FAITHFULNESS) + gates
# ---------------------------------------------------------------------------
#
# The "honest top": GEPA's SEARCH stage keeps its cheap
# deterministic proxy (``_score_prompt_on_dataset`` — honestly a heuristic,
# NEVER the reported fitness). When ``fitness_metric == 'faithfulness'`` a
# SEPARATE MEASURE stage computes a REAL paired before/after faithfulness delta
# over a held-out valset of the analyzed unit's most-recent findings, using the
# SAME ``llm.verify.slm_8b`` judge that gates the live findings:
#
#   * PARENT arm (BEFORE) = the EXISTING measured faithfulness — the latest
#     ``Faithfulness verify%`` critique's ``overall_score`` per finding
#     (analyst_outputs kind='critique', the scorecard_banding _GATHER pattern).
#     ZERO extra LLM cost — it is the live descriptor prompt's real production
#     faithfulness.
#   * CANDIDATE arm (AFTER) = generate-under-candidate over that finding's
#     reconstructed signal slice, then ``verify_finding_faithfulness`` under the
#     same judge.
#
# A degenerate / insufficient-sample / judge-unavailable / empty-arm delta is
# HONEST-NULL (means + delta = None, degenerate=True, promotable=False) — NEVER
# 0.0-faked. No candidate can promote without a positive, non-degenerate,
# sufficiently-sampled MEASURED delta.
#
# THE CORRECTNESS SIDE-CHANNEL (M-1, 2026-08-03). This eval carries a correctness
# block beside the faithfulness delta. Until now it counted `unit_reference_labels`
# — a table holding ONE row, for a retired analyst, with zero source ids — so the
# block read `insufficient_sample, n_labels=0` for every candidate ever evaluated
# and would have gone on doing so forever. That is not a strict gate, it is a
# DEAD one: it can never change state, so it can never inform a promotion either
# way, and calling it "insufficient sample" implied a sample that was accruing.
#
# It now reads `correctness_labels` — the weekly gold-set loop's operator
# verdicts, the table that actually gets fed. The block is still honest-null
# below its floor (today: n=1 for most units), but it is now a gate that CAN
# open, tracking a population that grows every time the operator labels a week.
# The deterministic source-overlap count rides along in its own sub-block so the
# older signal is not lost if reference labels are ever written.
#
# Correctness NEVER enters the promotion arithmetic. `promotable` is decided by
# `_delta_gates_ok` on the paired faithfulness delta alone; the correctness block
# is reported beside it, never averaged in (labels_api P2-5, the standing
# never-pool rule).

_FAITHFULNESS_FITNESS = "faithfulness"
_MIN_REFERENCE_LABELS = 20  # the SECONDARY source-overlap floor (unit_reference_labels)


def _delta_gates_ok(
    candidate_score: float | None,
    parent_score: float | None,
    *,
    eval_degenerate: bool,
    judge_available: bool,
    n_paired: int,
    min_paired: int,
    min_delta: float,
) -> tuple[bool, str]:
    """The MEASUREMENT gate — the candidate ``promotable`` stamp AND the LIVE
    write-time re-stamp in :func:`legba.data.analysts.optimizer.run_method`.

    Returns ``(ok, reason)``. The degeneracy / finiteness / judge / sample /
    margin checks run in THIS order and BEFORE any promotion-policy branch, so a
    degenerate or absent delta can never slip through on policy alone. ``ok`` is
    exactly the ``data.eval.promotable`` truth value: a positive (>=
    ``min_delta``), non-degenerate, judge-scored, sufficiently-paired measured
    improvement.

    This is the LIVE promotion gate: :func:`_pair_faithfulness` stamps its
    result into ``data.eval.promotable`` and
    :func:`legba.data.analysts.optimizer.run_method` RE-STAMPS it at candidate
    write time, so ``resolve_promoted_system_prompt`` admits an operator-promoted
    prompt into inference ONLY when this returns ``True``.
    """
    if candidate_score is None or parent_score is None or eval_degenerate:
        return False, "degenerate_or_absent_delta"
    # Defense-in-depth: a non-finite (NaN/inf) score is NOT a measured delta —
    # NaN comparisons are all False, so a NaN would silently pass the margin
    # check below. Not producer-reachable (faithfulness is [0,1]) but the LIVE
    # promotion gate must never rest on a comparison that lies. (The isfinite
    # guard lives HERE — the wired gate — not only on a helper.)
    if not (math.isfinite(candidate_score) and math.isfinite(parent_score)):
        return False, "non_finite_score"
    if not judge_available:
        return False, "faithfulness_judge_unavailable"
    if n_paired < min_paired:
        return False, f"insufficient_paired_sample:{n_paired}<{min_paired}"
    if (candidate_score - parent_score) < min_delta:
        return False, "delta_below_margin"
    return True, "delta_ok"


def _mean(values: Any) -> float | None:
    """Arithmetic mean, or ``None`` for an empty sequence (honest-null, not 0.0)."""
    vals = [float(v) for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _correctness_vs_reference(n_labels: int) -> dict[str, Any]:
    """The SECONDARY (deterministic source-overlap) correctness sub-block.

    ``unit_reference_labels`` is n≈1 and has never held a scorable row for a
    live unit, so this stays insufficient-sample — reported in its OWN key
    (NEVER 0.0-faked, NEVER pooled into the headline faithfulness). Kept because
    it costs nothing and becomes real the moment a reference label is written.

    ``brier`` is retained as an always-``None`` key for back-compatibility with
    records written before M-1; it was always a misnomer (this axis is a
    set-overlap recall, not a probabilistic score) and nothing reads it.
    """
    return {
        "status": "insufficient_sample",
        "n_labels": int(n_labels),
        "brier": None,
        "min_labels_required": _MIN_REFERENCE_LABELS,
    }


def _correctness_operator(operator: Mapping[str, Any] | None) -> dict[str, Any]:
    """The PRIMARY correctness sub-block — the OPERATOR gold set (M-1).

    ``operator`` is a :func:`legba.data.correctness_axis.score` record for the
    analyzed unit, or ``None`` when the pull failed / the table is absent (which
    degrades to the honest no-labels state, never a fabricated score).

    Unlike its predecessor this gate is LIVE: the table it counts is the one the
    weekly labeling loop writes to, so ``status`` moves from ``no_labels`` to
    ``insufficient_sample`` to ``scored`` as verdicts accrue. Below the floor the
    measured value is still reported — it is the only judge-independent signal
    there is — but ``sufficient`` is False and the mix travels with it, so a
    single verdict can never be read as a rate.
    """
    if not operator or int(operator.get("n_labels") or 0) == 0:
        return {
            "status": "no_labels",
            "correctness": None,
            "n_labels": 0,
            "n_scored": 0,
            "mix": {},
            "sufficient": False,
            "min_labels_required": correctness_axis.MIN_UNIT_LABELS,
            "source_table": "correctness_labels",
        }
    n_scored = int(operator.get("n_scored") or 0)
    sufficient = bool(operator.get("sufficient"))
    return {
        "status": (
            "scored" if sufficient
            else ("insufficient_sample" if n_scored else "all_unresolvable")
        ),
        "correctness": operator.get("correctness"),
        "n_labels": int(operator.get("n_labels") or 0),
        "n_scored": n_scored,
        "mix": dict(operator.get("mix") or {}),
        "sufficient": sufficient,
        "min_labels_required": correctness_axis.MIN_UNIT_LABELS,
        "source_table": "correctness_labels",
    }


def _honest_null_eval(
    *,
    min_paired: int,
    min_delta: float,
    judge_model: str,
    judge_available: bool,
    degenerate_reason: str,
    n_paired: int = 0,
    n_labels: int = 0,
    operator: Mapping[str, Any] | None = None,
    parent_mean: float | None = None,
    parent_judge_regime: str | None = None,
    candidate_judge_regime: str | None = None,
    n_mixed_regime_excluded: int = 0,
) -> dict[str, Any]:
    """Build the HONEST-NULL eval record: means + delta = None, degenerate=True,
    promotable=False. ``parent_mean`` may still be reported (it is real even when
    the candidate arm couldn't be scored) but the DELTA is null — you cannot
    subtract an absent candidate mean.

    The ``*_judge_regime`` stamps default ``None`` (no valid measured pair): the
    honest-null record has no before/after on a shared yardstick to name."""
    return {
        "fitness_metric": _FAITHFULNESS_FITNESS,
        "parent_faithfulness_mean": parent_mean,
        "candidate_faithfulness_mean": None,
        "faithfulness_delta": None,
        "n_paired": int(n_paired),
        "min_paired": int(min_paired),
        "min_promote_delta": float(min_delta),
        "judge_model": judge_model,
        "judge_available": bool(judge_available),
        # BOTH arms' judge regime — 'llm' (judge ran) vs 'deterministic' (floor).
        # A pair is measured on a SHARED yardstick or not at all; mixed-regime
        # rows are excluded (see _pair_faithfulness), never silently subtracted.
        "parent_judge_regime": parent_judge_regime,
        "candidate_judge_regime": candidate_judge_regime,
        "n_mixed_regime_excluded": int(n_mixed_regime_excluded),
        "degenerate": True,
        "degenerate_reason": degenerate_reason,
        "promotable": False,
        # PRIMARY correctness axis (operator gold set) — reported, never pooled
        # into the faithfulness delta or the promotion decision.
        "correctness_operator": _correctness_operator(operator),
        # SECONDARY (deterministic source-overlap) axis.
        "correctness_vs_reference": _correctness_vs_reference(n_labels),
    }


def _pair_faithfulness(
    parent_scores: Mapping[str, float],
    candidate_scores: Mapping[str, float],
    *,
    min_paired: int,
    min_delta: float,
    judge_model: str,
    n_labels: int,
    operator: Mapping[str, Any] | None = None,
    parent_regimes: Mapping[str, str] | None = None,
    candidate_regime: str = "llm",
) -> dict[str, Any]:
    """PURE pairing math: pair over the IDENTICAL finding ids scored in BOTH arms,
    then compute means + delta + promotability. HONEST-NULL when < ``min_paired``.

    Split out (no I/O) so the before/after arithmetic + the honest-null / promote
    boundary are unit-testable without a DB or an LLM.

    SAME-REGIME pairing (review H2 + the mixed-judge-regime medium): a pair is
    admitted ONLY when BOTH arms carry the SAME judge regime. The candidate arm
    already drops any floor-fallback row upstream (a judge error →
    ``_candidate_faithfulness_for_finding`` returns ``None`` → the fid is absent
    from ``candidate_scores``), so a judge-error candidate can never inflate the
    delta. Here we ALSO exclude a parent row whose LIVE verify FLOORED
    (``parent_regimes[fid] != candidate_regime``, e.g. the parent's judge errored
    and fell back to the 1.0 citation floor) — a judge-scored candidate must never
    pair against a floor-scored parent, which the 2026-07-01 recalibration would
    read as a spurious delta. ``parent_regimes=None`` (the pure-math test path)
    treats every parent row as the candidate regime.
    """
    def _parent_regime(fid: str) -> str:
        if parent_regimes is None:
            return candidate_regime
        return parent_regimes.get(fid, "deterministic")

    both = [fid for fid in parent_scores if fid in candidate_scores]
    common = [fid for fid in both if _parent_regime(fid) == candidate_regime]
    n_mixed_regime_excluded = len(both) - len(common)
    n_paired = len(common)
    parent_mean_all = _mean(parent_scores.values())
    if n_paired < min_paired:
        return _honest_null_eval(
            min_paired=min_paired, min_delta=min_delta, judge_model=judge_model,
            judge_available=True, n_paired=n_paired, n_labels=n_labels,
            operator=operator,
            parent_mean=parent_mean_all,
            candidate_judge_regime=candidate_regime,
            n_mixed_regime_excluded=n_mixed_regime_excluded,
            degenerate_reason=f"insufficient_paired_sample:{n_paired}<{min_paired}",
        )
    parent_mean = _mean(parent_scores[fid] for fid in common)
    candidate_mean = _mean(candidate_scores[fid] for fid in common)
    delta = float(candidate_mean) - float(parent_mean)
    promotable, _reason = _delta_gates_ok(
        candidate_mean, parent_mean,
        eval_degenerate=False, judge_available=True,
        n_paired=n_paired, min_paired=min_paired, min_delta=min_delta,
    )
    return {
        "fitness_metric": _FAITHFULNESS_FITNESS,
        "parent_faithfulness_mean": round(float(parent_mean), 4),
        "candidate_faithfulness_mean": round(float(candidate_mean), 4),
        "faithfulness_delta": round(delta, 4),
        "n_paired": n_paired,
        "min_paired": int(min_paired),
        "min_promote_delta": float(min_delta),
        "judge_model": judge_model,
        "judge_available": True,
        # Every ADMITTED pair shares this regime on BOTH arms (mixed excluded).
        "parent_judge_regime": candidate_regime,
        "candidate_judge_regime": candidate_regime,
        "n_mixed_regime_excluded": n_mixed_regime_excluded,
        "degenerate": False,
        "degenerate_reason": None,
        "promotable": bool(promotable),
        # Reported beside the delta, never inside it: `promotable` above was
        # decided by `_delta_gates_ok` on faithfulness alone.
        "correctness_operator": _correctness_operator(operator),
        "correctness_vs_reference": _correctness_vs_reference(n_labels),
    }


# --- worker-side descriptor config resolution (best-effort, honest-null on fail) ---


async def _get_descriptor_typed(analyst_id: str) -> dict[str, Any] | None:
    """Best-effort typed-descriptor fetch (mirrors dspy_lm's registry hop).

    Returns the JSON-dumped descriptor dict or ``None`` on ANY failure — the
    caller then degrades (config defaults / honest-null), never crashes.
    """
    registry = None
    try:
        from ..registry_client import RegistryHTTPClient

        registry = RegistryHTTPClient()
        typed = await registry.get_descriptor_typed(analyst_id, family="analyst")
        return typed if isinstance(typed, dict) else None
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.info(
            "optimizer.eval.descriptor_fetch_failed analyst=%s err=%r", analyst_id, exc,
        )
        return None
    finally:
        if registry is not None and hasattr(registry, "aclose"):
            try:
                await registry.aclose()
            except Exception:  # pragma: no cover - best-effort teardown
                pass


async def _apply_optimizer_eval_config(workflow_input: OptimizerWorkflowInput) -> None:
    """Fill ``fitness_metric`` + the paired gates + ``parent_system_prompt_source``
    from the OPTIMIZER descriptor's ``eval.optimizer`` when the caller left them
    at defaults.

    The config's single source of truth is the descriptor (not the actor's
    per-run options), so the worker resolves it here — keyed by
    ``optimizer_analyst_id``. No-ops when the caller already set a non-default
    ``fitness_metric`` (test injection / on-demand override) or when there is no
    ``optimizer_analyst_id`` (older inputs). Best-effort: a fetch failure leaves
    the defaults (``critique_proxy`` → the MEASURE stage never engages).
    """
    if workflow_input.fitness_metric != "critique_proxy":
        return  # already explicitly configured by the caller
    if not workflow_input.optimizer_analyst_id:
        return
    typed = await _get_descriptor_typed(workflow_input.optimizer_analyst_id)
    if not typed:
        return
    cfg = ((typed.get("eval") or {}).get("optimizer")) or {}
    if not isinstance(cfg, dict):
        return
    fm = cfg.get("fitness_metric")
    if isinstance(fm, str) and fm:
        workflow_input.fitness_metric = fm
    src = cfg.get("parent_system_prompt_source")
    if isinstance(src, str) and src:
        workflow_input.parent_system_prompt_source = src
    # min_traces_required / min_critiques_required gate the pre-compile
    # validation (validate_training_set_activity). They MUST resolve from the
    # descriptor too — else a scoped unit optimizer (min_traces_required=8) is
    # measured against the monolith default (50) and short-circuits as
    # skipped_validation before the MEASURE stage ever runs (P4-T6 live bug).
    for key in (
        "min_paired",
        "faithfulness_valset_max",
        "min_traces_required",
        "min_critiques_required",
    ):
        val = cfg.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            setattr(workflow_input, key, int(val))
    mpd = cfg.get("min_promote_delta")
    if isinstance(mpd, (int, float)) and not isinstance(mpd, bool):
        workflow_input.min_promote_delta = float(mpd)


async def _load_parent_text_from_descriptor(analyzed_analyst_id: str) -> str | None:
    """Load an inline_target UNIT's live baseline prompt = its descriptor
    ``method.system_prompt`` (NOT a Python prompt module).

    inline_target units carry their prompt VERBATIM in the descriptor, so the
    ``parent_system_prompt_source: descriptor`` fork loads the baseline snapshot
    from here. ``None`` on any failure → the caller falls back to the Python
    module path (which for a unit yields the honest ``<<missing prompt module>>``
    marker rather than a wrong baseline).
    """
    typed = await _get_descriptor_typed(analyzed_analyst_id)
    if not typed:
        return None
    sysp = ((typed.get("method") or {}).get("system_prompt"))
    if isinstance(sysp, str) and sysp.strip():
        return sysp
    return None


def _component_id(ref: Any) -> str | None:
    """The stack-component id a descriptor ``method.llm.*`` ref points at.

    A ref is either a ``{factory_kind, raw, ...}`` dict (the descriptor's typed
    stack_ref shape) or a bare string; ``None`` for anything else.
    """
    if isinstance(ref, dict):
        return ref.get("raw")
    if isinstance(ref, str):
        return ref
    return None


async def _resolve_verify_component_id(analyzed_analyst_id: str) -> str | None:
    """The ACTUAL judge component id the analyzed unit verifies with —
    ``method.llm.verify`` off its live descriptor.

    Since 1ed2187 the active units + compositions verify on the SAME core
    reasoning model (``llm.primary.openai_compat``), NOT the retired cross-family
    ``llm.verify.slm_8b``; stamping this resolved id (not a hardcode) keeps every
    eval record naming the judge that actually scored the candidate arm. ``None``
    on any failure → the caller records an honest ``unresolved`` marker.
    """
    typed = await _get_descriptor_typed(analyzed_analyst_id)
    if not typed:
        return None
    llm_block = ((typed.get("method") or {}).get("llm")) or {}
    return _component_id(llm_block.get("verify"))


# --- the paired MEASURE stage (parent arm real; candidate arm best-effort) ---

# Parent arm: the analyzed unit's freshest active findings + their LATEST folded
# ``Faithfulness verify%`` critique score — the EXACT scorecard_banding _GATHER
# LATERAL pattern, minus the target filter (all targets). f.body + f.data feed
# the candidate-arm regeneration; f.derived_from gives the signal lineage roots.
_PARENT_FAITHFULNESS_SQL = """
    SELECT f.id::text        AS finding_id,
           f.body            AS body,
           f.data            AS data,
           f.derived_from    AS derived_from,
           v.faithfulness_score AS faithfulness_score,
           v.judge_status    AS judge_status
      FROM analyst_outputs f
      LEFT JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score,
                 -- The regime the LIVE verify pass scored this parent row under:
                 -- 'llm' (judge ran) vs 'deterministic' (floor / judge errored).
                 -- Lets the pair exclude a floor-scored parent from a judge-scored
                 -- candidate (mixed-regime → spurious delta). NULL on legacy rows
                 -- written before the verification block carried judge_status.
                 cr.data->'data'->'verification'->>'judge_status' AS judge_status
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE f.kind = 'finding'
       AND f.analyst_id = $1
       AND f.superseded_by IS NULL
     ORDER BY f.produced_at DESC, f.id DESC
     LIMIT $2
"""

# SECONDARY axis — the deterministic source-overlap gold table. Kept for the
# sub-block; it has never held a scorable row for a live unit.
_REFERENCE_LABEL_COUNT_SQL = """
    SELECT COUNT(*)::int AS n FROM unit_reference_labels WHERE unit_analyst_id = $1
"""

# PRIMARY axis — the OPERATOR gold set, the table the weekly labeling loop
# actually writes to (M-1). Raw verdicts; the weighting and the tiny-n rules are
# applied by `legba.data.correctness_axis`, the one definition the scorer, the
# eval scoreboard and the v3 route share.
_OPERATOR_LABEL_SQL = correctness_axis.ONE_UNIT_LABELS_SQL


async def _paired_faithfulness_eval(
    workflow_input: OptimizerWorkflowInput,
    *,
    candidate_text: str,
    parent_text: str,
) -> dict[str, Any]:
    """MEASURE stage: the REAL paired before/after faithfulness delta.

    Returns the honest eval record (see the section header). ALWAYS returns a
    dict — every failure mode degrades to HONEST-NULL (means/delta None,
    degenerate=True, promotable=False) rather than raising into the workflow.
    The PARENT arm (existing measured faithfulness) is real + zero-LLM; the
    CANDIDATE arm (generate-under-candidate + verify) is best-effort and marks
    any un-regeneratable / un-judgeable row UNPAIRED (excluded from n_paired),
    NEVER scored 0 — so a lossy reconstruction under-counts the sample rather
    than under-measuring the candidate.
    """
    min_paired = int(workflow_input.min_paired)
    min_delta = float(workflow_input.min_promote_delta)
    valset_max = int(workflow_input.faithfulness_valset_max)
    analyzed = str(workflow_input.analyst_id)
    # Stamp the ACTUAL judge — the analyzed unit's method.llm.verify component —
    # never a hardcode (wrong since 1ed2187 repointed verify off llm.verify.slm_8b
    # onto the core reasoning model). Resolved up front so EVERY eval record below
    # (incl. the early honest-null returns) names the true judge.
    judge_model = (await _resolve_verify_component_id(analyzed)) or "unresolved_verify_component"

    pg_store = None
    try:
        from ...data.postgres import PostgresStore

        pg_store = PostgresStore.from_env()
        await pg_store.connect()
        async with pg_store.pool.acquire() as conn:
            rows = await conn.fetch(_PARENT_FAITHFULNESS_SQL, analyzed, valset_max)
            try:
                n_labels = int(
                    (await conn.fetchrow(_REFERENCE_LABEL_COUNT_SQL, analyzed))["n"]
                )
            except Exception:  # noqa: BLE001 — labels table optional / empty
                n_labels = 0
            try:
                operator = correctness_axis.score_unit_rows(
                    await conn.fetch(_OPERATOR_LABEL_SQL, analyzed)
                )
            except Exception:  # noqa: BLE001 — gold table optional / empty
                operator = None

        parent_scores: dict[str, float] = {}
        parent_regimes: dict[str, str] = {}
        finding_rows: dict[str, Mapping[str, Any]] = {}
        for r in rows:
            fid = str(r["finding_id"])
            fs = r["faithfulness_score"]
            if fs is None:
                continue  # no folded verify score → not a usable parent-arm row
            parent_scores[fid] = float(fs)
            # The regime the parent row was scored under. Legacy rows (NULL
            # judge_status) are treated 'deterministic' → excluded from a
            # judge-scored candidate pair (conservative, never inflates).
            parent_regimes[fid] = str(r["judge_status"] or "deterministic")
            finding_rows[fid] = r

        if not parent_scores:
            return _honest_null_eval(
                min_paired=min_paired, min_delta=min_delta, judge_model=judge_model,
                judge_available=False, n_labels=n_labels, operator=operator,
                degenerate_reason="no_parent_faithfulness",
            )

        # Resolve BOTH handlers (candidate-arm synthesis + the verify judge). The
        # judge is the SAME cross-family 8B that gates the live findings; if it
        # can't be resolved OR the LLM-judge flag is off, the candidate arm is
        # not on the same yardstick as the (judge-scored) parent → honest-null.
        arm = await _resolve_candidate_arm(analyzed)
        if arm is None:
            return _honest_null_eval(
                min_paired=min_paired, min_delta=min_delta, judge_model=judge_model,
                judge_available=False, n_labels=n_labels, operator=operator,
                parent_mean=_mean(parent_scores.values()),
                degenerate_reason="faithfulness_judge_unavailable",
            )
        synth, judge, arm_cleanup = arm
        try:
            candidate_scores: dict[str, float] = {}
            for fid, row in finding_rows.items():
                try:
                    cscore = await _candidate_faithfulness_for_finding(
                        row=row, candidate_text=candidate_text,
                        synth=synth, judge=judge, pool=pg_store.pool,
                    )
                except Exception as exc:  # noqa: BLE001 — one bad row is unpaired
                    logger.info(
                        "optimizer.faithfulness.candidate_row_failed finding=%s err=%r",
                        fid, exc,
                    )
                    cscore = None
                if cscore is not None:
                    candidate_scores[fid] = float(cscore)
        finally:
            await arm_cleanup()

        if not candidate_scores:
            return _honest_null_eval(
                min_paired=min_paired, min_delta=min_delta, judge_model=judge_model,
                judge_available=True, n_labels=n_labels, operator=operator,
                parent_mean=_mean(parent_scores.values()),
                degenerate_reason="candidate_arm_empty",
            )

        return _pair_faithfulness(
            parent_scores, candidate_scores,
            min_paired=min_paired, min_delta=min_delta,
            judge_model=judge_model, n_labels=n_labels, operator=operator,
            # Candidate rows here are all judge-scored ('llm') — a floor-fallback
            # row was already dropped in _candidate_faithfulness_for_finding — so
            # pair ONLY against 'llm'-regime parents (mixed excluded).
            parent_regimes=parent_regimes, candidate_regime="llm",
        )
    except Exception as exc:  # noqa: BLE001 — the MEASURE stage must never wedge
        logger.warning(
            "optimizer.faithfulness.eval_failed analyst=%s err=%r — honest-null",
            analyzed, exc,
        )
        return _honest_null_eval(
            min_paired=min_paired, min_delta=min_delta, judge_model=judge_model,
            judge_available=False, n_labels=0,
            degenerate_reason=f"eval_error:{type(exc).__name__}",
        )
    finally:
        if pg_store is not None:
            try:
                await pg_store.close()
            except Exception:  # pragma: no cover - best-effort teardown
                pass


async def _resolve_candidate_arm(
    analyzed_analyst_id: str,
) -> tuple[Any, Any, Any] | None:
    """Resolve ``(synthesizer_handler, verify_judge_handler, cleanup)`` for the
    candidate arm from the ANALYZED unit's ``method.llm.primary`` +
    ``method.llm.verify`` components.

    Returns ``None`` (→ honest-null) when the LLM-judge flag is off, the
    descriptor / components can't be resolved, or the build fails — so the
    candidate arm is scored on the SAME judge as the live findings or not at all.
    """
    # Single source of truth for "is the LLM judge on" — verify._llm_judge_enabled
    # accepts ONLY {1,true,yes,on} (case-insensitive), so an explicit
    # LEGBA_VERIFY_LLM_JUDGE=0/false correctly reads OFF here too (a bare
    # os.environ.get truthiness would have treated "0"/"false" as ON, mis-scoring
    # the candidate against a floored/absent parent yardstick).
    from ...data.provenance.verify import _llm_judge_enabled

    if not _llm_judge_enabled():
        # Parent-arm stored scores were judged (or floored) by the live pass; we
        # only measure the candidate on the LLM judge when it is actually on, so
        # before/after share a yardstick. Flag off → honest-null.
        return None

    typed = await _get_descriptor_typed(analyzed_analyst_id)
    if not typed:
        return None
    llm_block = ((typed.get("method") or {}).get("llm")) or {}

    primary_id = _component_id(llm_block.get("primary"))
    # P2-4: the candidate arm's judge resolves through the SAME judge-route
    # ladder as the live verify pass (env LEGBA_JUDGE_STACK_REF override →
    # method.llm.judge → method.llm.verify → method.llm.primary), so the
    # measure yardstick repoints together with every other judge call when the
    # second model lands. Today (env unset, no judge key) this resolves the
    # verify ref byte-identically to the old direct read.
    from ..analyst_deps_builder import resolve_judge_route_from_llm_block

    judge_route = resolve_judge_route_from_llm_block(llm_block)
    verify_id = judge_route.component_id if judge_route is not None else None
    if not primary_id or not verify_id:
        return None

    pg = None
    registry = None
    try:
        from ...data.config import PostgresConfig
        from ...data.postgres import PostgresStore
        from ...data.registry.credentials import CredentialVault
        from ..analyst_deps_builder import build_llm_handler_from_stack_component
        from ..registry_client import RegistryHTTPClient

        pg = PostgresStore(PostgresConfig.from_env())
        await pg.connect()
        vault = CredentialVault(pg)

        async def _secrets_resolve(secret_id: str) -> bytes:
            return await vault.resolve(secret_id)

        registry = RegistryHTTPClient()
        synth = await build_llm_handler_from_stack_component(
            primary_id, registry_client=registry, secrets_resolve=_secrets_resolve,
        )
        judge = await build_llm_handler_from_stack_component(
            verify_id, registry_client=registry, secrets_resolve=_secrets_resolve,
        )
    except Exception as exc:  # noqa: BLE001 — unresolved → honest-null
        logger.info(
            "optimizer.faithfulness.arm_resolve_failed analyst=%s err=%r",
            analyzed_analyst_id, exc,
        )
        for _h in (locals().get("synth"), locals().get("judge")):
            if _h is not None and hasattr(_h, "on_deactivate"):
                try:
                    await _h.on_deactivate(None)
                except Exception:  # pragma: no cover
                    pass
        if registry is not None and hasattr(registry, "aclose"):
            try:
                await registry.aclose()
            except Exception:  # pragma: no cover
                pass
        if pg is not None:
            try:
                await pg.close()
            except Exception:  # pragma: no cover
                pass
        return None

    async def _cleanup() -> None:
        """Await-able teardown — closes the two handlers' httpx clients + the
        registry + the pg pool. Called from within the same running loop that
        built them (the caller ``await``s it in its finally)."""
        for h in (synth, judge):
            if h is not None and hasattr(h, "on_deactivate"):
                try:
                    await h.on_deactivate(None)
                except Exception:  # pragma: no cover
                    pass
        if registry is not None and hasattr(registry, "aclose"):
            try:
                await registry.aclose()
            except Exception:  # pragma: no cover
                pass
        if pg is not None:
            try:
                await pg.close()
            except Exception:  # pragma: no cover
                pass

    return synth, judge, _cleanup


async def _candidate_faithfulness_for_finding(
    *,
    row: Mapping[str, Any],
    candidate_text: str,
    synth: Any,
    judge: Any,
    pool: Any,
) -> float | None:
    """Score ONE valset finding's candidate-arm faithfulness, or ``None`` (unpaired).

    Reconstructs the finding's cited signal slice (from ``data['citations']`` →
    the signal rows), re-renders it, generates under ``candidate_text``, resolves
    the candidate's OWN ``[N]`` citations against that slice, and verifies with
    the SAME judge. Returns ``None`` — marking the row UNPAIRED, NOT scored 0 —
    whenever the slice can't be reconstructed or generation/parse fails, so a
    lossy reconstruction shrinks n_paired rather than under-measuring the
    candidate.
    """
    from ...data.analysts.inline_target import (
        _build_citation_index,
        _coerce_finding,
        _extract_citations,
        _normalize_citation_markers,
        _render_signal,
    )
    from ...data.provenance.verify import verify_finding_faithfulness

    data = row.get("data")
    if isinstance(data, str):
        import json as _json
        try:
            data = _json.loads(data)
        except Exception:  # noqa: BLE001
            data = None
    citations = data.get("citations") if isinstance(data, Mapping) else None

    # PREFERRED: the structured [N]->signal bridge (P0-T1) in cited order.
    ordered: list[tuple[int, str]] = []
    if citations:
        for c in citations:
            if not isinstance(c, Mapping):
                continue
            marker = str(c.get("marker") or "")
            sid = c.get("signal_id")
            digits = "".join(ch for ch in marker if ch.isdigit())
            if digits and sid:
                ordered.append((int(digits), str(sid)))
        ordered.sort(key=lambda t: t[0])

    # FALLBACK: a unit that does NOT yet persist structured data['citations']
    # still NAMES its source signals in derived_from — reconstruct the slice from
    # those (stored order, numbered [1..N]) so the candidate arm can regenerate
    # instead of degrading the whole eval to candidate_arm_empty. Non-signal
    # derived_from ids (if any) are dropped by _fetch_signal_render_rows, which
    # fetches only real signals. This is what makes the paired MEASURE work on
    # today's units (n_citations=0) rather than only on future cited findings.
    if not ordered:
        derived = row.get("derived_from") or []
        ordered = [(i + 1, str(sid)) for i, sid in enumerate(derived) if sid]

    if not ordered:
        return None
    signal_ids = [sid for _, sid in ordered]

    signal_rows = await _fetch_signal_render_rows(pool, signal_ids)
    if not signal_rows:
        return None
    # Preserve the cited [N] order (fetch returns id->row); drop unresolved ids.
    slice_rows = [signal_rows[sid] for _, sid in ordered if sid in signal_rows]
    if not slice_rows:
        return None

    user_prompt = "\n".join(
        _render_signal(i, r) for i, r in enumerate(slice_rows, start=1)
    )
    resp = await synth.chat_complete(
        [{"role": "user", "content": user_prompt}],
        system=candidate_text,
        max_tokens=1536,
        temperature=0.0,
    )
    raw = getattr(resp, "content", "") or ""
    if not raw.strip():
        return None
    finding = _coerce_finding(raw, fallback_title="candidate")
    body = _normalize_citation_markers(str(getattr(finding, "body", "") or ""))
    index = _build_citation_index(slice_rows)
    cand_citations, _mc, _rc = _extract_citations(body, index)
    report = await verify_finding_faithfulness(
        body=body, citations=cand_citations, judge_llm=judge,
    )
    # H2: DROP a floor-fallback row. The candidate arm only runs when the LLM
    # judge is on (_resolve_candidate_arm), so a report whose ``judge_status`` is
    # NOT 'llm' means the judge errored/emptied on THIS row and verify soft-failed
    # to the deterministic citation floor (1.0 for a fully-cited body). Pairing
    # that inflated floor against a judge-scored parent would bias the delta UP.
    # Return None → the row is UNPAIRED (excluded from n_paired), never scored —
    # so a judge error can never inflate a computed delta.
    if getattr(report, "judge_status", None) != "llm":
        logger.info(
            "optimizer.faithfulness.candidate_row_floor_fallback finding=%s "
            "judge_status=%r reason=%r — dropped from pair",
            row.get("finding_id"),
            getattr(report, "judge_status", None),
            getattr(report, "judge_unavailable_reason", None),
        )
        return None
    return float(report.faithfulness_score)


async def _fetch_signal_render_rows(
    pool: Any, signal_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch signal rows by id → the render-shape dict ``_render_signal`` /
    ``_build_citation_index`` expect (``id`` / ``title`` / ``source_url`` /
    ``produced_at`` / ``data``). Maps the raw ``signals`` columns
    (``payload`` / ``canonical_url`` / ``fetched_at``) onto that historical
    shape, mirroring cross_target_raw's back-compat projection. Best-effort:
    returns ``{}`` on any failure (→ the row is unpaired)."""
    from uuid import UUID

    ids: list[UUID] = []
    for sid in signal_ids:
        try:
            ids.append(UUID(str(sid)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not ids:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, canonical_url, payload, fetched_at
              FROM signals
             WHERE id = ANY($1::uuid[])
            """,
            ids,
        )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            import json as _json
            try:
                payload = _json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        out[str(r["id"])] = {
            "id": r["id"],
            "title": payload.get("title") or payload.get("headline"),
            "source_url": r["canonical_url"],
            "produced_at": r["fetched_at"],
            "data": payload,
        }
    return out


# ---------------------------------------------------------------------------
# Shared GEPA loop — used by BOTH the Dapr activity + the in-process path
# ---------------------------------------------------------------------------


async def _run_gepa_loop(
    workflow_input: OptimizerWorkflowInput,
) -> OptimizerWorkflowResult:
    """Run the GEPA evolutionary loop.

    Implementation strategy:

      1. Try ``dspy.GEPA`` — DSPy 3.0+ teleprompter (arxiv 2507.19457).
      2. If GEPA isn't available, fall back to a naive best-of-N
         instruction search keeping the parent's prompt text as the
         baseline.  This path is correct (returns the parent unchanged
         when there's no LLM to call) but obviously not as effective —
         it exists so unit tests can exercise the algorithm wiring
         without a real LLM provider.

    P4-T6 — when the OPTIMIZER descriptor's ``eval.optimizer.fitness_metric`` is
    ``faithfulness`` (resolved into ``workflow_input`` here), a SEPARATE MEASURE
    stage overrides the REPORTED ``eval_score`` / ``eval_score_delta`` with a
    REAL paired before/after faithfulness delta (:func:`_paired_faithfulness_eval`);
    the SEARCH stage's cheap proxy stays an internal heuristic. The whole record
    (means, n_paired, judge status, correctness_vs_reference) lands in
    ``diagnostics['eval']`` — honest-null when degenerate.
    """
    # Resolve fitness_metric + the paired gates + parent_system_prompt_source from
    # the OPTIMIZER descriptor's eval.optimizer (best-effort; no-op when already
    # set or when there is no optimizer_analyst_id — e.g. the frozen monolith).
    await _apply_optimizer_eval_config(workflow_input)

    # Parent (baseline) text. inline_target UNITS carry their live prompt as the
    # DESCRIPTOR method.system_prompt, not a Python module — so the
    # ``parent_system_prompt_source: descriptor`` fork loads it from there for the
    # operator diff snapshot; everything else imports the prompt module.
    parent_text = ""
    if workflow_input.parent_system_prompt_source == "descriptor":
        parent_text = await _load_parent_text_from_descriptor(
            workflow_input.analyst_id
        ) or ""
    if not parent_text:
        parent_text = await _load_parent_prompt_text(
            workflow_input.parent_prompt_module_path,
        )

    # Empty / undersized training-set path: return the parent unchanged
    # with a zero-delta score so the activity can't loop forever on an
    # under-trained analyst even if the validator was skipped.
    if not workflow_input.training_set:
        return OptimizerWorkflowResult(
            candidate_prompt_module_text=parent_text,
            training_set_size=0,
            eval_score=0.0,
            eval_score_delta=0.0,
            gepa_generation=0,
            parent_prompt_module_text=parent_text,
            diagnostics={
                "method": "noop_empty_training",
                "reason": "no training rows supplied",
                # G5 — no model calls on this path; honest usage is zero.
                "usage": _zero_usage(),
            },
        )

    # Compute the baseline (parent) score on the training set.  This is
    # what GEPA's delta is measured against.
    baseline_score = _score_prompt_on_dataset(
        parent_text, workflow_input.training_set,
    )

    # Try dspy.GEPA path first — under an LM resolved from the analyzed
    # analyst's OWN provider (a custom dspy.BaseLM that never touches litellm).
    result = _run_dspy_gepa_with_lm(
        workflow_input,
        parent_text=parent_text,
        baseline_score=baseline_score,
    )
    if result is None:
        # Fallback — naive instruction search.  Generates a small set of
        # candidate variants by light-touch mutation, scores each, returns
        # the best one (or the parent if none beats the baseline).
        result = _naive_candidate_search(
            workflow_input,
            parent_text=parent_text,
            baseline_score=baseline_score,
        )

    # P4-T6 MEASURE stage — replace the SEARCH proxy's reported fitness with the
    # REAL paired faithfulness delta (or honest-null). Opt-in via fitness_metric.
    return await _apply_faithfulness_measure(
        workflow_input, result, parent_text=parent_text,
    )


async def _apply_faithfulness_measure(
    workflow_input: OptimizerWorkflowInput,
    result: OptimizerWorkflowResult,
    *,
    parent_text: str,
) -> OptimizerWorkflowResult:
    """Fold the paired-faithfulness MEASURE record into ``result`` (opt-in).

    No-op for ``critique_proxy`` (the country_optimizer default → result byte-
    unchanged). For ``faithfulness`` it runs :func:`_paired_faithfulness_eval`,
    stamps ``diagnostics['eval']`` (the honest record), and OVERRIDES the top-
    level ``eval_score`` / ``eval_score_delta`` with the measured candidate mean +
    delta when NON-degenerate; on honest-null it sets both 0.0 (the payload's
    float fields can't be null) while the authoritative null lives in
    ``diagnostics['eval']`` (means/delta=None, degenerate=True, promotable=False).
    """
    if workflow_input.fitness_metric != _FAITHFULNESS_FITNESS:
        return result
    eval_record = await _paired_faithfulness_eval(
        workflow_input,
        candidate_text=result.candidate_prompt_module_text,
        parent_text=parent_text,
    )
    result.diagnostics = dict(result.diagnostics or {})
    result.diagnostics["eval"] = eval_record
    result.diagnostics["fitness_metric"] = _FAITHFULNESS_FITNESS
    delta = eval_record.get("faithfulness_delta")
    cand_mean = eval_record.get("candidate_faithfulness_mean")
    if not eval_record.get("degenerate") and delta is not None and cand_mean is not None:
        result.eval_score = max(0.0, min(1.0, float(cand_mean)))
        result.eval_score_delta = max(-1.0, min(1.0, float(delta)))
    else:
        # HONEST-NULL — the measured fitness is absent; do NOT let the SEARCH
        # proxy masquerade as the reported faithfulness. Zero the float fields;
        # diagnostics['eval'] carries the authoritative null + promotable=False.
        result.eval_score = 0.0
        result.eval_score_delta = 0.0
    return result


# ---------------------------------------------------------------------------
# DSPy GEPA path (arxiv 2507.19457 §3.2)
# ---------------------------------------------------------------------------


def _zero_usage() -> dict[str, int]:
    """A token-usage dict with every dimension at zero.

    The non-LLM paths (empty training set, naive best-of-N fallback) make no
    model calls, so their HONEST usage is zero — but they still emit the dict
    so downstream (G5) can read ``usage`` uniformly without a ``None`` guard.
    """
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _dspy_lm_usage_snapshot(lm: Any) -> int:
    """Length of ``lm.history`` — used to delta token usage across a GEPA run.

    DSPy LMs append one dict per call to ``lm.history``; each carries a
    ``usage`` sub-dict. We snapshot the length before the compile and sum the
    usage of the entries appended during it (see :func:`_dspy_usage_delta`),
    so the reported tokens are OBSERVED, not synthetic.
    """
    history = getattr(lm, "history", None)
    return len(history) if isinstance(history, list) else 0


def _dspy_usage_delta(lm: Any, since: int) -> dict[str, int]:
    """Sum the token usage of ``lm.history`` entries appended since ``since``.

    Returns a usage dict shaped for the budget ledger (G5). Defensive against
    DSPy history shapes that omit ``usage`` or carry it under alternate keys —
    a missing field contributes zero rather than raising.
    """
    history = getattr(lm, "history", None)
    if not isinstance(history, list):
        return _zero_usage()
    prompt = completion = total = 0
    for entry in history[since:]:
        usage = entry.get("usage") if isinstance(entry, dict) else None
        if not isinstance(usage, dict):
            continue
        p = int(usage.get("prompt_tokens", 0) or 0)
        c = int(usage.get("completion_tokens", 0) or 0)
        # Per-entry total: explicit when present, else this entry's
        # prompt+completion. A global "total==0 → prompt+completion" fallback
        # under-counts when only SOME entries carry total_tokens.
        total += int(usage.get("total_tokens", 0) or 0) or (p + c)
        prompt += p
        completion += c
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _gepa_budget(workflow_input: OptimizerWorkflowInput) -> int:
    """Hard cap on GEPA metric calls (LLM rollouts) for one compile.

    dspy.GEPA requires ``max_metric_calls`` >= a small floor to do any
    reflection; clamp to a sane window so a misconfigured descriptor can
    neither no-op (too low) nor run away (too high → minutes of API spend).
    """
    raw = getattr(workflow_input, "max_metric_calls", 30) or 30
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 30
    return max(6, min(n, 200))


def _gepa_valset_max() -> int:
    """Cap on the GEPA valset size (the Pareto-tracking eval set).

    dspy.GEPA evaluates the base program AND every candidate over the full
    valset to track Pareto scores. With NO valset passed to ``compile``, dspy
    falls back to using the entire trainset (up to 500 rows here) as the
    valset — so the base eval alone is ~500 LLM calls (~3.5 min) and every
    rollout re-evaluates 500, which is why a daily compile could not finish a
    single rollout inside its window (it sat at ``0/30 rollouts`` until the
    next cron preempted it). Cap the valset to a small representative slice so
    base + per-rollout evals are cheap; the full trainset still feeds GEPA's
    minibatch reflection search. Env-tunable.
    """
    raw = os.environ.get("LEGBA_GEPA_VALSET_MAX", "40")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 40
    return max(5, min(n, 200))


def _gepa_num_threads() -> int:
    """Parallelism for GEPA's metric evals (dspy.GEPA ``num_threads``).

    The valset/minibatch evals are independent LLM calls; running them
    concurrently turns a serial base eval into a parallel one. Bounded so a
    compile doesn't hammer the shared provider endpoint. Env-tunable.
    """
    raw = os.environ.get("LEGBA_GEPA_NUM_THREADS", "4")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 4
    return max(1, min(n, 16))


def _gepa_rollout_headroom() -> int:
    """Metric-call budget GEPA gets for reflective rollouts ABOVE the base eval.

    dspy.GEPA always spends ``len(valset)`` metric calls on the mandatory base
    Pareto eval before any rollout. If ``max_metric_calls <= len(valset)`` the
    budget is exhausted by that base eval and GEPA emits the parent unchanged
    (0 rollouts) — it "runs" but never optimizes (the 0/30-rollouts symptom).
    The effective budget is therefore ``len(valset) + headroom``; the headroom
    funds the minibatch reflective search (≈ headroom / reflection_minibatch).
    Env-tunable.
    """
    raw = os.environ.get("LEGBA_GEPA_ROLLOUT_HEADROOM", "60")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 60
    return max(0, min(n, 460))


def _run_dspy_gepa_with_lm(
    workflow_input: OptimizerWorkflowInput,
    *,
    parent_text: str,
    baseline_score: float,
) -> OptimizerWorkflowResult | None:
    """Run :func:`_try_dspy_gepa` under an LM resolved from the analyzed
    analyst's own provider.

    GEPA's reflection + student calls need a ``dspy.settings.lm``. We supply
    a custom :class:`dspy.BaseLM` (``LegbaProviderLM``) that routes through the
    project's ``LLMProviderHandler`` + budget machinery — so litellm is NEVER
    invoked (operator hard rule). The LM is scoped to this compile via
    ``dspy.context`` (thread-local; the activity runs off the main thread) and
    torn down afterwards.

    Degrades gracefully: if dspy is absent, an LM is already configured (test
    injection), or the provider can't be resolved, falls through so the loop
    lands on the naive fallback rather than crashing the workflow.
    """
    try:
        import dspy  # type: ignore[import-not-found]
    except ImportError:
        return _try_dspy_gepa(
            workflow_input, parent_text=parent_text, baseline_score=baseline_score,
        )

    # An LM already in scope (a test or caller configured one) → run directly.
    if getattr(dspy.settings, "lm", None) is not None:
        return _try_dspy_gepa(
            workflow_input, parent_text=parent_text, baseline_score=baseline_score,
        )

    from .dspy_lm import configure_gepa_lm

    configured = configure_gepa_lm(workflow_input.analyst_id)
    if configured is None:
        # Provider unresolved — let the caller fall back to the naive path.
        logger.info(
            "optimizer.gepa.skipped reason=no LM (provider unresolved for %s)",
            workflow_input.analyst_id,
        )
        return None

    lm, cleanup = configured
    try:
        with dspy.context(lm=lm):
            return _try_dspy_gepa(
                workflow_input,
                parent_text=parent_text,
                baseline_score=baseline_score,
            )
    finally:
        cleanup()


def _try_dspy_gepa(
    workflow_input: OptimizerWorkflowInput,
    *,
    parent_text: str,
    baseline_score: float,
) -> OptimizerWorkflowResult | None:
    """Try the DSPy GEPA teleprompter; return None if unavailable.

    GEPA compiles a student :class:`dspy.Module` by repeatedly
    reflecting on per-trajectory scores and proposing new instruction
    text for each predictor.  We import the kind's
    :func:`build_prompt_module` to obtain the student module, then
    run GEPA against the (parent prompt → candidate prompt) optimization
    target.

    Why this is wrapped in a defensive ``try`` rather than a hard
    dependency: dspy.GEPA only landed in DSPy 3.0+ AND its construction
    requires a configured ``dspy.LM`` for the reflection_lm slot.  In a
    test environment without a real LLM key, this attempt fails cleanly
    and the loop drops to the naive fallback.
    """
    try:
        import dspy                                              # type: ignore[import-not-found]
        gepa_cls = getattr(dspy, "GEPA", None)
        if gepa_cls is None:
            logger.info("optimizer.gepa.skipped reason=dspy.GEPA not available")
            return None
    except ImportError:
        logger.info("optimizer.gepa.skipped reason=dspy not installed")
        return None

    # Require an LM to be configured globally — if dspy.settings.lm is
    # None, GEPA can't run.  Don't crash; return None so the caller
    # falls back to the naive path.
    try:
        if getattr(dspy.settings, "lm", None) is None:
            logger.info("optimizer.gepa.skipped reason=no dspy.settings.lm")
            return None
    except Exception:                                            # pragma: no cover
        return None

    # Build the parent student module from the kind's path so GEPA has
    # something to optimize.  Failure here is fatal — we can't proceed
    # without a base module to evolve.
    try:
        student = _import_prompt_module(workflow_input.parent_prompt_module_path)
    except ImportError as exc:
        # K-3: a dead reference must not degrade quietly into the naive
        # search. Returning None here made "the descriptor names a module that
        # does not exist" indistinguishable from "dspy could not build the
        # student", and the run continued either way.
        logger.error(
            "optimizer.gepa.dead_reference path=%s err=%s",
            workflow_input.parent_prompt_module_path, exc,
        )
        raise PromptModuleImportError(
            f"optimizer parent prompt module "
            f"{workflow_input.parent_prompt_module_path!r} does not import: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    except Exception as exc:
        # A real shape/dspy problem — the documented graceful degrade to the
        # naive candidate search still applies.
        logger.warning(
            "optimizer.gepa.parent_load_failed path=%s err=%s",
            workflow_input.parent_prompt_module_path, exc,
        )
        return None

    # Build Examples from the training set.  We require each row to
    # have an ``input`` (the prompt context) and a ``gold`` (the
    # reference answer the critique scored against).
    trainset: list[Any] = []
    for row in workflow_input.training_set:
        example_input = row.get("input")
        example_gold = row.get("gold") or row.get("expected_output", "")
        if example_input is None:
            continue
        try:
            ex = dspy.Example(input=example_input, gold=example_gold).with_inputs("input")
            trainset.append(ex)
        except Exception:                                        # pragma: no cover
            continue

    if not trainset:
        logger.info("optimizer.gepa.skipped reason=no usable examples")
        return None

    # Define the metric — uses the critic's score when present, else a
    # simple substring-overlap heuristic so the metric is non-zero even
    # in test setups without a real judge.
    def _metric(gold: Any, pred: Any, trace: Any = None, *args: Any, **kwargs: Any) -> float:
        gold_text = str(getattr(gold, "gold", "") or "")
        pred_text = str(getattr(pred, "answer", "") or getattr(pred, "output", "") or "")
        if not gold_text or not pred_text:
            return 0.0
        # Token-overlap Jaccard — bounded [0,1].  GEPA expects floats.
        gold_tokens = set(gold_text.lower().split())
        pred_tokens = set(pred_text.lower().split())
        if not gold_tokens:
            return 0.0
        union = gold_tokens | pred_tokens
        if not union:
            return 0.0
        return len(gold_tokens & pred_tokens) / len(union)

    # Snapshot the reflection LM's call history BEFORE the compile so we can
    # delta the real token usage GEPA burns (G5: the optimizer must report its
    # actual spend so country_optimizer's per-day token cap accrues).
    reflection_lm = dspy.settings.lm
    usage_since = _dspy_lm_usage_snapshot(reflection_lm)
    # COST BOUND — dspy.GEPA's ``auto`` modes ("light"≈hundreds of rollouts)
    # cost minutes of real LLM calls per tick; on a daily optimizer that is a
    # large, unbounded API spend that can blow the analyst's token cap. Drive
    # GEPA with an explicit ``max_metric_calls`` budget instead (auto and
    # max_metric_calls are mutually exclusive in dspy.GEPA), sized from the
    # descriptor so an operator can dial it up per analyst. See _gepa_budget.
    num_threads = _gepa_num_threads()
    # Cap the valset GEPA tracks Pareto scores over (see _gepa_valset_max):
    # passing no valset makes dspy use the WHOLE trainset (up to 500), so the
    # base eval + every per-rollout eval are hundreds of LLM calls — the reason
    # a daily compile never finished a rollout. A small representative slice
    # keeps base + candidate evals cheap; the full trainset still drives the
    # reflective minibatch search.
    valset = trainset[: _gepa_valset_max()]
    # The base Pareto eval costs len(valset) metric calls before ANY rollout,
    # so the budget must clear the valset + headroom or GEPA returns the parent
    # unchanged (0 rollouts). Honour a higher explicit descriptor budget but
    # never let it fall at/below the valset. Hard-capped so it can't run away.
    budget = min(
        max(_gepa_budget(workflow_input), len(valset) + _gepa_rollout_headroom()),
        500,
    )
    try:
        teleprompter = gepa_cls(
            metric=_metric,
            max_metric_calls=budget,
            reflection_minibatch_size=workflow_input.reflection_minibatch_size,
            reflection_lm=reflection_lm,
            num_threads=num_threads,
        )
        logger.info(
            "optimizer.gepa.compile.start analyst=%s max_metric_calls=%d "
            "trainset=%d valset=%d num_threads=%d",
            workflow_input.analyst_id, budget, len(trainset), len(valset),
            num_threads,
        )
        compiled = teleprompter.compile(student, trainset=trainset, valset=valset)
    except Exception as exc:
        logger.warning("optimizer.gepa.compile_failed err=%s", exc)
        return None
    logger.info(
        "optimizer.gepa.compile.complete analyst=%s trainset=%d valset=%d",
        workflow_input.analyst_id, len(trainset), len(valset),
    )
    usage = _dspy_usage_delta(reflection_lm, usage_since)

    # Extract the candidate prompt text — DSPy modules expose the
    # compiled instructions via ``module.signature.instructions`` on
    # each predictor.  We concatenate all predictors' instructions
    # (separated by a sentinel) so the candidate text is self-contained.
    candidate_text = _serialize_module_instructions(compiled)

    # Score on the same training set — gives us the delta vs baseline.
    candidate_score = _score_prompt_on_dataset(
        candidate_text, workflow_input.training_set,
    )

    return OptimizerWorkflowResult(
        candidate_prompt_module_text=candidate_text,
        training_set_size=len(workflow_input.training_set),
        eval_score=candidate_score,
        eval_score_delta=candidate_score - baseline_score,
        gepa_generation=int(getattr(compiled, "_gepa_generation", 1) or 1),
        parent_prompt_module_text=parent_text,
        diagnostics={
            "method": "dspy_gepa",
            "auto": workflow_input.auto,
            "baseline_score": baseline_score,
            "candidate_score": candidate_score,
            "parent_text_chars": len(parent_text),
            "candidate_text_chars": len(candidate_text),
            "reflection_minibatch_size": workflow_input.reflection_minibatch_size,
            # G5 — real observed token usage so the actor records spend.
            "usage": usage,
        },
    )


# ---------------------------------------------------------------------------
# Naive-search fallback (used when dspy.GEPA isn't viable)
# ---------------------------------------------------------------------------


def _naive_candidate_search(
    workflow_input: OptimizerWorkflowInput,
    *,
    parent_text: str,
    baseline_score: float,
) -> OptimizerWorkflowResult:
    """Generate a small set of light-mutation candidates, pick best.

    Mutations are deterministic (parameterized only by the generation
    index) so this path is replay-safe inside a durable workflow — even
    though the production path is dspy.GEPA, the fallback's determinism
    keeps the workflow correct under the no-LLM dev scenario.

    The mutations are pedagogical: add an explicit "be concise" hint,
    add a "cite evidence" hint, swap the role framing.  None of them
    are expected to outperform a real GEPA run — this is here to keep
    the contract honest in test environments.
    """
    mutations = [
        # Generation 1 — add concision hint.
        parent_text + "\n\nBe concise; prefer specific names + dates over generic phrasing.",
        # Generation 2 — add evidence-grounding hint.
        parent_text + "\n\nCite specific signal IDs inline as 'signal:<id>'. Never invent IDs.",
        # Generation 3 — add structural hint.
        parent_text + "\n\nStructure your response: claim, evidence, counterfactual, confidence.",
    ]

    best_text = parent_text
    best_score = baseline_score
    best_generation = 0
    per_generation_scores: list[dict[str, Any]] = [
        {"generation": 0, "score": baseline_score, "kind": "parent"},
    ]
    for gen_idx, candidate in enumerate(mutations[:workflow_input.max_generations], start=1):
        score = _score_prompt_on_dataset(candidate, workflow_input.training_set)
        per_generation_scores.append(
            {"generation": gen_idx, "score": score, "kind": "naive_mutation"},
        )
        if score > best_score:
            best_text = candidate
            best_score = score
            best_generation = gen_idx

    return OptimizerWorkflowResult(
        candidate_prompt_module_text=best_text,
        training_set_size=len(workflow_input.training_set),
        eval_score=best_score,
        eval_score_delta=best_score - baseline_score,
        gepa_generation=best_generation,
        parent_prompt_module_text=parent_text,
        diagnostics={
            "method": "naive_best_of_n",
            "baseline_score": baseline_score,
            "per_generation_scores": per_generation_scores,
            "mutations_tried": min(
                len(mutations), workflow_input.max_generations,
            ),
            # G5 — the naive fallback makes no LLM calls (deterministic
            # mutations + heuristic scoring); honest usage is zero.
            "usage": _zero_usage(),
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_parent_prompt_text(prompt_module_path: str) -> str:
    """Resolve a dotted import path → human-readable prompt text.

    Strategy:

      1. Try to import the module and call its ``build()`` function (if
         it exposes one — that's the prompts package convention).
      2. Read the resulting object's ``signature.instructions`` field if
         present (dspy.Module shape).
      3. Fall back to ``str(obj)``.
      4. On an unexpected *shape*, return a synthetic stub so the loop
         can still compute a sensible delta — degrading gracefully is
         the right shape there.

    Declared seam (docs/SEAMS.md #12): the ``<<no prompt text found>>``
    marker string is unmistakable in any audit of optimizer runs — never
    passed off as a real prompt.

    K-3 narrows that seam. A module that cannot be IMPORTED is a dead
    reference, not an unexpected shape, and the old behaviour was the
    worst available: return ``<<missing prompt module: ...>>`` at
    ``logger.debug`` (invisible in production) and then hand that marker
    to GEPA as the parent text — so the optimizer scored, mutated and
    could promote a candidate evolved from a placeholder. That is exactly
    the "deploy green and change what the system reasons with" failure the
    descriptor-reference work exists to prevent, so it now raises.

    Raises
    ------
    PromptModuleImportError
        ``prompt_module_path`` does not import.
    """
    try:
        import importlib
        mod = importlib.import_module(prompt_module_path)
    except Exception as exc:
        logger.error(
            "optimizer.parent_load.dead_reference path=%s err=%s — refusing to "
            "optimize a placeholder; fix the descriptor's prompt_module",
            prompt_module_path, exc,
        )
        raise PromptModuleImportError(
            f"optimizer parent prompt module {prompt_module_path!r} does not "
            f"import: {type(exc).__name__}: {exc}"
        ) from exc

    # Try the convention build() entry first.
    if hasattr(mod, "build"):
        try:
            obj = mod.build()
            return _extract_text_from_module(obj) or str(obj)
        except Exception as exc:
            logger.debug(
                "optimizer.parent_load.build_failed path=%s err=%s",
                prompt_module_path, exc,
            )

    # Some prompts expose a class directly named after the kind; pick
    # the first dspy.Module-shaped attribute.
    for attr in dir(mod):
        if attr.startswith("_"):
            continue
        try:
            candidate = getattr(mod, attr)
            text = _extract_text_from_module(candidate)
            if text:
                return text
        except Exception:                                       # pragma: no cover
            continue

    return f"<<no prompt text found in {prompt_module_path}>>"


def _extract_text_from_module(obj: Any) -> str | None:
    """Pull prompt text out of a dspy.Module-shaped object if possible."""
    # dspy.Predict / dspy.ChainOfThought: ``signature.instructions``.
    sig = getattr(obj, "signature", None)
    if sig is not None:
        instructions = getattr(sig, "instructions", None)
        if instructions:
            return str(instructions)

    # dspy.Module.predictors() — iterate predictors, concat instructions.
    predictors_fn = getattr(obj, "predictors", None)
    if callable(predictors_fn):
        try:
            parts: list[str] = []
            for predictor in predictors_fn():
                pred_sig = getattr(predictor, "signature", None)
                if pred_sig is not None:
                    inst = getattr(pred_sig, "instructions", None)
                    if inst:
                        parts.append(str(inst))
            if parts:
                return "\n---\n".join(parts)
        except Exception:                                       # pragma: no cover
            pass

    return None


def _import_prompt_module(prompt_module_path: str) -> Any:
    """Import + construct the student module for GEPA.

    The prompts package convention is module-level ``build()`` returning
    a fresh module instance.  Falls back to constructing the first
    public class in the module if no ``build`` is exported.
    """
    import importlib
    mod = importlib.import_module(prompt_module_path)
    if hasattr(mod, "build"):
        return mod.build()
    for attr in dir(mod):
        if attr.startswith("_"):
            continue
        candidate = getattr(mod, attr)
        if isinstance(candidate, type):
            try:
                return candidate()
            except Exception:                                   # pragma: no cover
                continue
    raise ImportError(
        f"prompt module {prompt_module_path} exposes no build() or "
        f"constructible class"
    )


def _serialize_module_instructions(module: Any) -> str:
    """Render a dspy.Module's predictor instructions as a single string."""
    text = _extract_text_from_module(module)
    return text or "<<compiled module has no extractable instructions>>"


def _score_prompt_on_dataset(
    prompt_text: str,
    training_set: list[dict[str, Any]],
) -> float:
    """Score a prompt against the training set.

    Strategy: each row already carries a ``critique_score`` from the
    critic (L-175) — use that as the gold-standard signal.  We weight
    the row's score by a small "prompt fit" heuristic (Jaccard overlap
    between the candidate prompt's keywords and the row's input keywords)
    so a candidate prompt that mentions terms appearing in the input
    gets a slight bias toward higher score.

    Returns a value in [0, 1].  Deterministic — same prompt + same
    training set always yields the same score, which is what durable-
    workflow determinism requires for replay.

    Why this matters: the score isn't a real LLM eval (no LLM call here)
    — it's a deterministic proxy that lets the workflow body stay
    pure-Python and replay-safe.  Real LLM-based scoring lives upstream
    in dspy.GEPA's metric callback when that path is taken.
    """
    if not training_set:
        return 0.0
    keywords = _extract_keywords(prompt_text)
    total = 0.0
    n = 0
    for row in training_set:
        row_score = row.get("critique_score")
        if row_score is None:
            # No critique → 0.5 baseline weight (uninformative).
            row_score = 0.5
        try:
            row_score = float(row_score)
        except (TypeError, ValueError):
            row_score = 0.5
        row_score = max(0.0, min(1.0, row_score))

        row_input = str(row.get("input") or "")
        row_keywords = _extract_keywords(row_input)
        if keywords and row_keywords:
            overlap = len(keywords & row_keywords) / len(keywords | row_keywords)
        else:
            overlap = 0.0
        # 80% row score, 20% prompt-fit overlap.  Bounded in [0, 1].
        weighted = 0.8 * row_score + 0.2 * overlap
        total += weighted
        n += 1
    if n == 0:
        return 0.0
    return total / n


def _extract_keywords(text: str) -> set[str]:
    """Tokenize → lowercase → drop short tokens.  Deterministic."""
    if not text:
        return set()
    tokens = []
    for raw in text.split():
        cleaned = "".join(c for c in raw.lower() if c.isalnum())
        if len(cleaned) >= 4:
            tokens.append(cleaned)
    return set(tokens)


# ---------------------------------------------------------------------------
# In-process entry point
# ---------------------------------------------------------------------------


async def run_optimizer_in_process(
    workflow_input: OptimizerWorkflowInput,
) -> OptimizerWorkflowResult:
    """Execute the optimizer loop in-process (no workflow engine).

    Used by :class:`InProcessWorkflowClient` + by the kind module's
    tests.  Returns the same shape the durable workflow would return.

    Validates the training set up front — under-trained analysts
    short-circuit with a zero-delta result (the kind module's
    promotion gate filters these out downstream).

    Production routes inline rows here (the in-process fallback has no gRPC
    hop), but honour a ``training_set_ref`` too: re-materialize it first so
    this path is correct regardless of how the input was shaped.
    """
    workflow_input = await materialize_training_set(workflow_input)
    validation = await validate_training_set_activity(workflow_input)
    if not validation.get("ok"):
        return OptimizerWorkflowResult(
            candidate_prompt_module_text=(
                f"<<skipped: {validation.get('reason', 'validation_failed')}>>"
            ),
            training_set_size=int(validation.get("training_size", 0)),
            eval_score=0.0,
            eval_score_delta=0.0,
            gepa_generation=0,
            diagnostics={
                "method": "skipped_validation",
                "reason": validation.get("reason", ""),
            },
        )
    return await _run_gepa_loop(workflow_input)


__all__ = [
    "InProcessWorkflowClient",
    "OptimizerWorkflowInput",
    "OptimizerWorkflowResult",
    "StubWorkflowHandle",
    "TrainingSetRef",
    "WorkflowHandleLike",
    "build_default_client",
    "materialize_training_set",
    "run_optimizer_in_process",
    "validate_training_set_activity",
]
