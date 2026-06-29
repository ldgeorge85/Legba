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
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


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
    # doesn't decide promotion — that's done back in the kind module via
    # :func:`legba.data.analysts.optimizer.should_auto_promote`.  But the
    # workflow surfaces the policy declared on the descriptor so the
    # eventual row can carry it forward.
    promotion_policy: str = "human_gated"

    # Per-2026-05-16 decision: 50 GT rows default per analyst.
    min_traces_required: int = 50
    min_critiques_required: int = 0

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
    """
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
    """
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
    gepa_result = _run_dspy_gepa_with_lm(
        workflow_input,
        parent_text=parent_text,
        baseline_score=baseline_score,
    )
    if gepa_result is not None:
        return gepa_result

    # Fallback — naive instruction search.  Generates a small set of
    # candidate variants by light-touch mutation, scores each, returns
    # the best one (or the parent if none beats the baseline).
    return _naive_candidate_search(
        workflow_input,
        parent_text=parent_text,
        baseline_score=baseline_score,
    )


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
    except Exception as exc:
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
      4. On any failure, return a synthetic stub so the loop can still
         compute a sensible delta — degrading gracefully is the right
         shape here, the alternative is the optimizer crashing whenever
         a prompt module has a slightly different shape.

    Declared seam (docs/SEAMS.md #12): the ``<<missing prompt module>>``
    / ``<<no prompt text found>>`` marker strings are unmistakable in
    any audit of optimizer runs — never passed off as a real prompt.
    """
    try:
        import importlib
        mod = importlib.import_module(prompt_module_path)
    except Exception as exc:
        logger.debug(
            "optimizer.parent_load.import_failed path=%s err=%s",
            prompt_module_path, exc,
        )
        return f"<<missing prompt module: {prompt_module_path}>>"

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
