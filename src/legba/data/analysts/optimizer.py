# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-176 optimizer analyst kind — DSPy + GEPA self-improvement loop.

The single analyst kind in Legba that uses a durable workflow (per L-103
hybrid runtime design; Dapr Workflow since P-16/P-CUT).  Reads another
analyst's recorded ``analyst_traces`` + ``analyst_critiques`` rows, runs
the GEPA evolutionary loop (arxiv 2507.19457) over them, and emits a
candidate ``prompt_module`` whose promotion to live is gated by the
descriptor's ``eval.promotion`` policy.

Per L-105 §4 (eval-loop spec) and the L-176 brief
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Read:    (a) ``analyst_traces`` rows for the analyst being optimized,
         (b) ``analyst_critiques`` rows joined by trace_id.
Method:  ``dspy_compile`` — GEPA reflective Pareto-frontier prompt
         evolution.  Multi-hour deterministic outer loop + non-
         deterministic LLM activities → dispatched as a Dapr Workflow
         (see :mod:`legba.runtime.dapr_workflow`).
Write:   :class:`PromptModuleCandidatePayload` to ``analyst_outputs``
         with ``kind = OutputKind.PROMPT_MODULE_CANDIDATE``.
Promote: gated by ``descriptor.eval.promotion`` — default
         ``human_gated``; ``auto_with_threshold`` becomes eligible
         after 5 successful manual promotions (per 2026-05-16 decision).

Wave B prereqs (commit ``8a1fd5c``):
  * ``MethodBlock.kind`` has the ``dspy_compile`` literal.
  * ``dspy_compile`` is exempt from the ``prompt_module`` requirement
    on :class:`MethodBlock` — the optimizer COMPILES prompts; it
    doesn't have a static one of its own.

For the in-process / no-sidecar path used by tests, see
:class:`legba.runtime.dapr_workflow.gepa.InProcessWorkflowClient`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

import asyncpg

from ..provenance.kinds import OutputKind
from ..provenance.models import FindingPayload, PromptModuleCandidatePayload
from ...runtime.analyst_method import AnalystMethodResult
from ...runtime.dapr_workflow.gepa import (
    OptimizerWorkflowInput,
    OptimizerWorkflowResult,
    build_default_client,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind identity — host's discover_analyst_kinds picks these up
# ---------------------------------------------------------------------------


KIND_NAME: str = "optimizer"
SCHEMA_VERSION: str = "legba/analyst.optimizer/1-0-0"
HANDLER_VERSION: str = "0.1.0"

# The kind writes PROMPT_MODULE_CANDIDATE rows.  Lands in the generic
# `analyst_outputs` table per the new spec in
# :mod:`legba.data.provenance.kinds`.
OUTPUT_KIND: OutputKind = OutputKind.PROMPT_MODULE_CANDIDATE

# Wave B prereq: the optimizer kind is exempt from build_prompt_module()
# because its job is to COMPILE prompts, not to carry a static one.  The
# host's discover layer still calls this (defensive) — returning None is
# the contract for kinds that don't expose a prompt module.
PROMPT_MODULE_PATH: str | None = None


def build_prompt_module() -> Any:
    """Optimizer is exempt from carrying a prompt module (Wave B prereq).

    Returns None so the host's discover layer can still look this
    attribute up without crashing.  The optimizer compiles OTHER
    kinds' prompt modules; it doesn't have one of its own.
    """
    return None


# ---------------------------------------------------------------------------
# Tunables — module-level so tests can override
# ---------------------------------------------------------------------------


# Per 2026-05-16 ratified decision: 50 ground-truth rows per analyst by
# default.  Descriptors override via ``eval.optimizer.min_traces_required``.
DEFAULT_MIN_TRACES_REQUIRED: int = 50
DEFAULT_MIN_CRITIQUES_REQUIRED: int = 0

# Per L-176 §"Promotion gates": 5 successful manual promotions before an
# analyst becomes eligible for `auto_with_threshold` promotion.
AUTO_PROMOTION_SUCCESS_THRESHOLD: int = 5

# GEPA hyperparameters — descriptors override via ``eval.optimizer.*``.
DEFAULT_MAX_GENERATIONS: int = 5
DEFAULT_REFLECTION_MINIBATCH_SIZE: int = 3
DEFAULT_AUTO_MODE: str = "light"

# Per-run trace + critique read window.  Default 30 days — generous
# enough to accumulate 50 critiqued runs for most analysts on a daily
# cadence.
DEFAULT_READ_WINDOW_DAYS: int = 30
MAX_TRAINING_ROWS: int = 500     # hard cap regardless of window

# Outer wall-clock bound on awaiting the GEPA workflow result. The silent-
# death class that left the optimizer leg dormant ~4 days was the workflow's
# ``compile()`` hanging → ``handle.result()`` never returning → ``run_method``
# never completing → NO ``analyst_trace`` ever written. Bounding the await
# means the actor ALWAYS records a trace (a visible ``workflow_timeout`` row)
# rather than looking dormant. 30 min — comfortably above the worker's own
# per-LM-call bound, so a healthy run returns its real result first. Env-tunable.
DEFAULT_DISPATCH_TIMEOUT_S: float = 1800.0


def _dispatch_timeout_s() -> float:
    """Resolve the optimizer dispatch wall-clock bound from the environment."""
    raw = os.environ.get(
        "LEGBA_OPTIMIZER_DISPATCH_TIMEOUT_S", str(DEFAULT_DISPATCH_TIMEOUT_S),
    )
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DISPATCH_TIMEOUT_S
    return v if v > 0 else DEFAULT_DISPATCH_TIMEOUT_S


# ---------------------------------------------------------------------------
# READ_SLICE — joins analyst_traces + analyst_critiques for the target analyst
# ---------------------------------------------------------------------------


async def read_traces_and_critiques(
    conn: asyncpg.Connection,
    *,
    analyzed_analyst_id: str,
    analyzed_analyst_version: str | None = None,
    read_window_days: int = DEFAULT_READ_WINDOW_DAYS,
    limit: int = MAX_TRAINING_ROWS,
) -> list[dict[str, Any]]:
    """Fetch the GEPA training set for the analyst being optimized.

    Joins ``analyst_traces`` LEFT JOIN ``analyst_critiques`` on
    ``trace.run_id = critique.trace_id`` — LEFT so traces without a
    critique still land (they're informative even uncritiqued, just
    weighted lower by the workflow's metric).

    Returns row dicts with the columns the GEPA loop's metric needs:

      * ``run_id`` — trace run UUID (also the row's natural primary key
        in our derived_from list).
      * ``input`` — the trace's ``prompt_rendered`` (what the analyst
        was asked to reason over).
      * ``gold`` — the critic's narrative-corrected output if present,
        else the original ``output_payload`` text.
      * ``critique_score`` — the critic's ``overall_score``, NULL when
        the trace was never critiqued.
      * ``analyzed_analyst_id`` / ``analyzed_analyst_version`` — pass-
        through so the workflow can audit-trail.

    Empty result is meaningful — the workflow will short-circuit with
    a "no training data" candidate (zero delta) which the kind module
    then writes as an audit row anyway, so an operator can see the
    optimizer fired and bailed.
    """
    if not analyzed_analyst_id:
        return []

    params: list[Any] = [analyzed_analyst_id, int(read_window_days)]
    version_filter = ""
    if analyzed_analyst_version:
        params.append(analyzed_analyst_version)
        version_filter = f" AND t.analyst_version = ${len(params)}"

    query = f"""
        SELECT
            t.run_id              AS run_id,
            t.analyst_id          AS analyzed_analyst_id,
            t.analyst_version     AS analyzed_analyst_version,
            t.prompt_rendered     AS input,
            t.output_payload      AS gold,
            t.status              AS trace_status,
            c.overall_score       AS critique_score,
            c.scores              AS critique_scores,
            c.revision_delta      AS critique_revision_delta,
            c.id                  AS critique_id,
            t.run_started_at      AS run_started_at
        FROM analyst_traces t
        LEFT JOIN analyst_critiques c ON c.trace_id = t.run_id
        WHERE t.analyst_id = $1
          AND t.run_started_at > NOW() - make_interval(days => $2)
          {version_filter}
        ORDER BY t.run_started_at DESC
        LIMIT {int(limit)}
    """
    rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


async def READ_SLICE(  # noqa: N802 — host-discovered constant alias
    conn,  # type: ignore[no-untyped-def]
    *,
    descriptor,  # type: ignore[no-untyped-def]
    target_filter,  # type: ignore[no-untyped-def]
    analyzed_analyst_id: str | None = None,
    analyzed_analyst_version: str | None = None,
    read_window_days: int = DEFAULT_READ_WINDOW_DAYS,
    limit: int = MAX_TRAINING_ROWS,
) -> list[dict[str, Any]]:
    """Host-dispatcher adapter for :func:`read_traces_and_critiques`.

    Resolves the analyzed-analyst id in this priority order:

      1. ``analyzed_analyst_id=`` argument (test path),
      2. the descriptor's ``eval.optimizer['analyzed_analyst_id']``
         dict entry (production path — the optimizer descriptor names
         which analyst it optimizes via its eval block),
      3. ``target_filter`` cast to string (fallback for the rare case
         where the runtime passes the analyst id through target_filter,
         e.g. on a per-run override).

    Returns ``[]`` if no analyzed-analyst id resolves — the run_method
    then emits a "noop, no target analyst" candidate row.
    """
    analyst_id = analyzed_analyst_id
    analyst_version = analyzed_analyst_version

    if not analyst_id:
        eval_block = getattr(descriptor, "eval", None) if descriptor else None
        optimizer_cfg = getattr(eval_block, "optimizer", None) if eval_block else None
        if isinstance(optimizer_cfg, dict):
            analyst_id = optimizer_cfg.get("analyzed_analyst_id")
            analyst_version = analyst_version or optimizer_cfg.get(
                "analyzed_analyst_version"
            )

    if not analyst_id and target_filter:
        analyst_id = str(target_filter)

    if not analyst_id:
        logger.info(
            "optimizer.read_slice.no_target_analyst descriptor=%s",
            getattr(getattr(descriptor, "identity", None), "id", None),
        )
        return []

    return await read_traces_and_critiques(
        conn,
        analyzed_analyst_id=str(analyst_id),
        analyzed_analyst_version=(
            str(analyst_version) if analyst_version else None
        ),
        read_window_days=read_window_days,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Workflow-client resolution (P-16 — Dapr Workflow port)
# ---------------------------------------------------------------------------


def _resolve_workflow_client() -> Any:
    """Pick the durable-workflow client backing the optimizer's GEPA loop.

    Resolution order (P-16 cutover-aware):

      1. ``LEGBA_OPTIMIZER_DAPR_WORKFLOW=1`` → the Dapr-Workflow client
         (:func:`legba.runtime.dapr_workflow.build_dapr_workflow_client`).
         When that returns ``None`` (dep missing / in-process forced) we
         fall through so the optimizer still runs.
      2. Otherwise → the in-process GEPA fallback
         (:func:`legba.runtime.dapr_workflow.gepa.build_default_client`).

    The returned object satisfies the same
    ``start_optimizer_workflow(wf_input, *, workflow_id) -> handle``
    contract regardless of backend, so the kind's ``run_method`` is
    backend-agnostic — this is what keeps the cutover from touching the
    kind's external interface.
    """
    if os.environ.get("LEGBA_OPTIMIZER_DAPR_WORKFLOW") == "1":
        try:
            from ...runtime.dapr_workflow import build_dapr_workflow_client

            client = build_dapr_workflow_client()
            if client is not None:
                logger.info("optimizer.workflow_client backend=dapr_workflow")
                return client
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "optimizer.dapr_workflow_client.unavailable err=%s "
                "(falling back to the in-process GEPA path)", exc,
            )
    return build_default_client()


# ---------------------------------------------------------------------------
# Deps surface
# ---------------------------------------------------------------------------


@dataclass
class OptimizerDeps:
    """Dependency bundle the optimizer kind needs.

    Most kinds wire an LLM here; the optimizer's LLM access is mediated
    by dspy.GEPA + dspy.settings.lm, so the kind itself doesn't need a
    direct LLM port.  Instead it carries:

      * a durable-workflow client (production:
        :class:`legba.runtime.dapr_workflow.client.DaprOptimizerWorkflowClient`,
        tests: :class:`legba.runtime.dapr_workflow.gepa.InProcessWorkflowClient`
        or a stub).  Defaults to :func:`_resolve_workflow_client` which
        auto-picks based on environment.  The field name ``temporal_client``
        is historical and kept stable — it is just "the workflow client".
      * GEPA tuning knobs (max_generations, auto mode, etc.) — same
        defaults as the workflow input; descriptors override via
        ``eval.optimizer.*``.
    """

    temporal_client: Any = None
    max_generations: int = DEFAULT_MAX_GENERATIONS
    reflection_minibatch_size: int = DEFAULT_REFLECTION_MINIBATCH_SIZE
    auto_mode: str = DEFAULT_AUTO_MODE
    min_traces_required: int = DEFAULT_MIN_TRACES_REQUIRED
    min_critiques_required: int = DEFAULT_MIN_CRITIQUES_REQUIRED

    def __post_init__(self) -> None:
        if self.temporal_client is None:
            self.temporal_client = _resolve_workflow_client()


# ---------------------------------------------------------------------------
# Promotion gate — consults descriptor + historic outcomes
# ---------------------------------------------------------------------------


async def resolve_promoted_system_prompt(
    pg_pool: Any,
    analyst_id: str,
    *,
    default: str | None,
) -> str | None:
    """Return the live system prompt for ``analyst_id`` — closing the loop.

    A GEPA candidate whose ``data->>'promotion_gate'`` an operator flipped to
    ``'promoted'`` becomes the analyst's live system prompt: this returns the
    newest such candidate's ``candidate_prompt_module_text``, else ``default``
    (the descriptor/baseline prompt). This is what makes the optimizer's
    evolved instructions actually reach inference (the analyst still runs its
    own direct ``chat_complete`` — no dspy on the hot path).

    Best-effort by design: any error (no pool, query failure, malformed row)
    returns ``default`` so a promotion-lookup hiccup can never break an
    analyst run.
    """
    if pg_pool is None:
        return default
    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT data->>'candidate_prompt_module_text' AS text
                FROM analyst_outputs
                WHERE kind = 'prompt_module_candidate'
                  AND data->>'analyst_id' = $1
                  AND data->>'promotion_gate' = 'promoted'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(analyst_id),
            )
    except Exception as exc:  # noqa: BLE001 — never break inference on lookup
        logger.warning(
            "optimizer.promoted_prompt.lookup_failed analyst=%s err=%r",
            analyst_id, exc,
        )
        return default
    if row and row["text"]:
        logger.info(
            "optimizer.promoted_prompt.active analyst=%s chars=%d",
            analyst_id, len(row["text"]),
        )
        return str(row["text"])
    return default


async def should_auto_promote(
    conn: asyncpg.Connection,
    *,
    analyzed_analyst_id: str,
    candidate_score: float,
    parent_score: float,
    promotion_policy: str,
    min_promotion_threshold: int = AUTO_PROMOTION_SUCCESS_THRESHOLD,
) -> tuple[bool, str]:
    """Decide whether a candidate should auto-promote.

    Returns ``(eligible, reason)``.

    Policy resolution per L-176 §"Promotion gates":

      * ``human_gated`` (default) → never auto-promotes; returns
        ``(False, "human_gated")``.
      * ``auto_with_threshold`` → counts historic manual promotions
        for this analyst's prior candidates (rows in ``analyst_outputs``
        with ``kind='prompt_module_candidate'`` AND
        ``data ->> 'promotion_gate' = 'promoted'``).  When that count
        reaches ``min_promotion_threshold`` (5 per the 2026-05-16
        decision), AND the candidate's score exceeds the parent's,
        return ``(True, "auto_promoted")``.

    The "promoted" promotion_gate value is set externally by the
    operator's promotion action (or by this function itself when it
    returns ``(True, ...)``); the optimizer kind writes
    ``human_gated`` / ``auto_with_threshold`` on the row at write
    time.  An operator UI / CLI flips it to ``promoted`` after manual
    review.
    """
    if candidate_score <= parent_score:
        return False, "score_did_not_improve"

    if promotion_policy == "human_gated":
        return False, "human_gated"

    if promotion_policy != "auto_with_threshold":
        return False, f"unknown_policy:{promotion_policy}"

    # Count historic "promoted" rows for this analyst's candidates.
    # The audit semantics are: a row's data->promotion_gate is updated
    # to 'promoted' when the operator promotes it; we count those.
    row = await conn.fetchrow(
        """
        SELECT COUNT(*) AS n
        FROM analyst_outputs
        WHERE kind = 'prompt_module_candidate'
          AND (data ->> 'analyst_id') = $1
          AND (data ->> 'promotion_gate') = 'promoted'
        """,
        analyzed_analyst_id,
    )
    n_prior_promotions = int(row["n"]) if row else 0
    if n_prior_promotions < min_promotion_threshold:
        return False, (
            f"insufficient_history: {n_prior_promotions} prior promotions, "
            f"need {min_promotion_threshold}"
        )
    return True, f"auto_promoted_after_{n_prior_promotions}_priors"


# ---------------------------------------------------------------------------
# Public entry — run_method
# ---------------------------------------------------------------------------


async def run_method(
    inputs: list[Mapping[str, Any]],
    options: Mapping[str, Any],
    deps: OptimizerDeps | None = None,
) -> AnalystMethodResult:
    """Execute one optimizer run.

    Inputs are the rows :func:`READ_SLICE` produced — joined trace +
    critique rows for the analyzed analyst.  Options carry:

      * ``analyst_id`` / ``analyst_version`` — the OPTIMIZER analyst's
        own identity (not the analyzed one).  The runtime stamps these
        from the descriptor.
      * ``analyzed_analyst_id`` — the analyst being optimized.  Pulled
        from the row when present (since READ_SLICE projects it), or
        from options as a runtime-passthrough.
      * ``parent_prompt_module_path`` — optional explicit override;
        otherwise resolved from the descriptor's
        ``eval.optimizer.parent_prompt_module_path``.
      * ``promotion_policy`` — override; defaults to the descriptor's
        ``eval.promotion`` value.
      * ``run_id`` — supplied by the runtime; we forward it so the
        candidate row's run_id matches the actor's trace.

    Returns an :class:`AnalystMethodResult` whose ``finding`` field
    carries a :class:`FindingPayload` with the
    :class:`PromptModuleCandidatePayload` stashed under ``data['candidate']``.
    The actor's ``_select_output_payload`` reads the kind's OUTPUT_KIND
    and the candidate payload from this slot.
    """
    deps = deps or OptimizerDeps()

    analyzed_analyst_id, analyzed_analyst_version = _resolve_analyzed_identity(
        inputs, options,
    )
    if not analyzed_analyst_id:
        return _no_op_result(
            reason="no_analyzed_analyst",
            options=options,
            training_size=len(inputs),
        )

    parent_path = options.get("parent_prompt_module_path") or (
        f"legba.prompts.{analyzed_analyst_id}.v1"
    )
    promotion_policy = str(options.get("promotion_policy") or "human_gated")

    workflow_input = OptimizerWorkflowInput(
        analyst_id=str(analyzed_analyst_id),
        analyst_version=str(analyzed_analyst_version or ""),
        parent_prompt_module_path=str(parent_path),
        training_set=_shape_training_set(inputs),
        max_generations=int(deps.max_generations),
        reflection_minibatch_size=int(deps.reflection_minibatch_size),
        auto=str(deps.auto_mode),
        promotion_policy=promotion_policy,
        min_traces_required=int(deps.min_traces_required),
        min_critiques_required=int(deps.min_critiques_required),
    )

    # Dispatch the workflow.  In production this returns a real
    # workflow handle whose result() blocks until the engine completes
    # the run; in test + dev (no sidecar) it returns a StubWorkflowHandle
    # with the result already computed.
    # NB: separator is '.', NOT '::'. Dapr Workflow derives each activity's
    # internal actor id as "<workflow_instance_id>::<taskId>::<gen>" and strips
    # that '::' suffix to recover the parent workflow when an activity reports
    # its result. A '::' inside the instance id itself makes that strip mis-parse
    # (it recovered just "optimizer"), so the activity result is reported to a
    # non-existent workflow → "workflow actor instance not found" → the
    # orchestrator never resumes and the GEPA run hangs forever. Keep this id
    # free of '::'.
    workflow_id = (
        f"optimizer.{analyzed_analyst_id}."
        f"{str(options.get('run_id') or uuid4())[:8]}"
    )
    workflow_result, workflow_meta = await _dispatch_workflow(
        deps.temporal_client, workflow_input, workflow_id=workflow_id,
    )

    # G5 — surface the workflow's REAL token usage so the actor records spend
    # against country_optimizer's per-day token cap. The GEPA workflow deltas
    # ``dspy.settings.lm`` history across the compile and stamps it into
    # ``diagnostics['usage']`` (zero on the no-LLM naive / empty paths).
    usage = _usage_from_workflow_result(workflow_result)

    # The REAL method the workflow took (dspy_gepa / naive_best_of_n /
    # noop_empty_training / skipped_validation) — read from the workflow's own
    # diagnostics rather than hardcoding. A deploy without the GEPA worker (or
    # any dspy-unavailable path) silently runs the naive fallback; surfacing
    # the true method lets the UI flag it instead of falsely labelling every
    # row 'dspy_gepa'. Degrades to 'unknown' if diagnostics lacks the key.
    diagnostics = dict(workflow_result.diagnostics or {})
    actual_method = str(diagnostics.get("method") or "unknown")

    # Build the candidate payload.
    derived_from = _collect_derived_from(inputs)
    candidate = PromptModuleCandidatePayload(
        analyst_id=str(analyzed_analyst_id),
        analyst_version=str(analyzed_analyst_version or ""),
        parent_prompt_module_path=str(parent_path),
        candidate_prompt_module_text=str(workflow_result.candidate_prompt_module_text),
        # Snapshot the parent text the candidate was scored against so the
        # operator diff route renders current-vs-candidate with no dspy import
        # (capped to the model's 128KiB ceiling; empty on the skip path).
        parent_prompt_module_text=str(
            getattr(workflow_result, "parent_prompt_module_text", "") or "",
        )[:131072],
        training_set_size=int(workflow_result.training_set_size),
        eval_score=float(workflow_result.eval_score),
        eval_score_delta=float(workflow_result.eval_score_delta),
        gepa_generation=int(workflow_result.gepa_generation),
        promotion_gate=promotion_policy if promotion_policy in (
            "human_gated", "auto_with_threshold", "rejected",
        ) else "human_gated",
        temporal_workflow_id=str(workflow_meta.get("workflow_id") or ""),
        temporal_run_id=str(workflow_meta.get("run_id") or ""),
        data={
            "diagnostics": diagnostics,
            "method": actual_method,
            "derived_trace_ids": [str(u) for u in derived_from],
            "optimizer_options": {
                "max_generations": deps.max_generations,
                "auto_mode": deps.auto_mode,
                "reflection_minibatch_size": deps.reflection_minibatch_size,
            },
        },
    )

    # The actor's ``_select_output_payload`` doesn't yet know about
    # PROMPT_MODULE_CANDIDATE; it returns ``method_result.finding``.
    # We hide the candidate payload inside a FindingPayload-shaped
    # wrapper for transport, and the actor's existing dispatch will
    # validate it against PROMPT_MODULE_CANDIDATE's spec via
    # ``write_analyst_output(kind=OUTPUT_KIND, output_payload=...)``.
    #
    # To make that path work cleanly with the actor's current
    # ``_select_output_payload`` (which only branches on PREDICTION),
    # we stash the candidate in finding.data['candidate'] AND surface
    # the candidate directly via a custom ``candidate`` attribute on
    # the result.  The runtime in dapr_actors._select_output_payload
    # currently returns ``method_result.finding`` for non-PREDICTION
    # kinds, so we make the finding's body equal a serialized JSON
    # representation of the candidate.  The substrate write helper
    # then validates whatever the actor passes to
    # ``write_analyst_output`` against PromptModuleCandidatePayload's
    # schema.  See "Bug surfaced" in the L-176 report — the
    # ``_select_output_payload`` dispatch needs a clean extension
    # point for kinds beyond PREDICTION.
    summary_body = (
        f"Optimizer candidate for {analyzed_analyst_id} "
        f"(generation {workflow_result.gepa_generation}, "
        f"score {workflow_result.eval_score:.3f}, "
        f"Δ {workflow_result.eval_score_delta:+.3f})"
    )
    finding = FindingPayload(
        title=summary_body[:200],
        body=summary_body[:65000],
        confidence=max(0.0, min(1.0, float(workflow_result.eval_score))),
        evidence=[str(u) for u in derived_from[:50]],
        tags=[
            "optimizer",
            f"analyzed:{analyzed_analyst_id}",
            f"generation:{workflow_result.gepa_generation}",
            promotion_policy,
        ],
        data={
            # Hold the candidate payload dump here so the actor + a
            # future _select_output_payload extension can pivot off it
            # without re-doing the workflow.
            "candidate": candidate.model_dump(),
            "temporal_workflow_id": workflow_meta.get("workflow_id"),
            "temporal_run_id": workflow_meta.get("run_id"),
            # The REAL method the GEPA workflow took (not always dspy_gepa — a
            # worker-less deploy or dspy-unavailable path runs naive_best_of_n).
            "method": actual_method,
        },
    )
    return AnalystMethodResult(
        finding=finding,
        usage=usage,
        derived_from=derived_from,
        intermediate_steps=[
            {"phase": "read", "kind": "trace_critique_join",
             "input_rows": len(inputs)},
            {"phase": "dispatch", "kind": "temporal_workflow",
             "workflow_id": workflow_meta.get("workflow_id"),
             "in_process": workflow_meta.get("in_process", False)},
            {"phase": "candidate", "kind": "build_payload",
             "score": float(workflow_result.eval_score),
             "delta": float(workflow_result.eval_score_delta),
             "generation": int(workflow_result.gepa_generation)},
        ],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_analyzed_identity(
    inputs: list[Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    options: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Pick (analyzed_analyst_id, analyzed_analyst_version) from inputs+options.

    Inputs are the trace/critique join rows; each carries the analyzed
    analyst's id + version (the optimizer's READ_SLICE projects them).
    Options can override (used by the on-demand path).
    """
    aid = options.get("analyzed_analyst_id")
    aver = options.get("analyzed_analyst_version")
    if aid:
        return str(aid), str(aver) if aver else None
    for row in inputs:
        aid = row.get("analyzed_analyst_id") or row.get("analyst_id")
        if aid:
            aver = (
                row.get("analyzed_analyst_version")
                or row.get("analyst_version")
            )
            return str(aid), str(aver) if aver else None
    return None, None


def _shape_training_set(
    inputs: list[Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project the trace+critique rows into the workflow's input shape.

    Workflow needs JSON-serializable dicts (the engine's JSON
    serialization).  We keep the load-bearing fields and drop the rest.
    """
    out: list[dict[str, Any]] = []
    for row in inputs:
        input_text = row.get("input") or row.get("prompt_rendered") or ""
        gold = row.get("gold") or row.get("output_payload") or ""
        if not isinstance(input_text, str):
            input_text = str(input_text)
        if not isinstance(gold, str):
            gold = str(gold)
        score = row.get("critique_score")
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = None
        out.append({
            "run_id": str(row.get("run_id") or ""),
            "input": input_text[:8000],
            "gold": gold[:8000],
            "critique_score": score,
            "trace_status": row.get("trace_status"),
        })
    return out


def _usage_from_workflow_result(workflow_result: Any) -> dict[str, int]:
    """Lift the workflow's observed token usage into a budget-shaped dict (G5).

    The GEPA workflow stamps real reflection-LM token usage into
    ``diagnostics['usage']`` (zero on the no-LLM naive / empty-training paths).
    Returning it as the method result's ``usage`` makes
    :meth:`dapr_actors` record spend — previously this returned ``{}`` so the
    optimizer was silently exempt from its OWN per-day token cap. Defensive: a
    missing / malformed diagnostics blob yields an all-zero dict rather than
    raising (the run still produced a candidate; we just observed no tokens).
    """
    diagnostics = getattr(workflow_result, "diagnostics", None) or {}
    raw = diagnostics.get("usage") if isinstance(diagnostics, dict) else None
    if not isinstance(raw, dict):
        return {
            "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "total_tokens": 0,
        }

    def _int(key: str) -> int:
        try:
            return int(raw.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    prompt = _int("prompt_tokens")
    completion = _int("completion_tokens")
    reasoning = _int("reasoning_tokens")
    total = _int("total_tokens") or (prompt + completion + reasoning)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
    }


def _collect_derived_from(
    inputs: list[Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> list[UUID]:
    """Extract trace + critique UUIDs for the candidate's derived_from."""
    out: list[UUID] = []
    for row in inputs:
        for key in ("run_id", "critique_id"):
            raw = row.get(key)
            if raw is None:
                continue
            if isinstance(raw, UUID):
                out.append(raw)
                continue
            try:
                out.append(UUID(str(raw)))
            except (ValueError, AttributeError):
                continue
    return out


async def _dispatch_workflow(
    temporal_client: Any,
    workflow_input: OptimizerWorkflowInput,
    *,
    workflow_id: str,
) -> tuple[OptimizerWorkflowResult, dict[str, Any]]:
    """Start the workflow + await its result.

    Returns ``(result, meta)`` where ``meta`` carries the
    ``workflow_id`` + ``run_id`` for the candidate payload to record.
    Failures bubble up — the actor's failure-classification path
    handles them.
    """
    handle = await temporal_client.start_optimizer_workflow(
        workflow_input, workflow_id=workflow_id,
    )
    in_process = type(temporal_client).__name__ == "InProcessWorkflowClient"
    timeout_s = _dispatch_timeout_s()
    try:
        result = await asyncio.wait_for(handle.result(), timeout=timeout_s)
    except (asyncio.TimeoutError, TimeoutError):
        # The GEPA workflow exceeded its wall-clock bound — the silent-death
        # class: compile() hangs → result() never returns → run_method never
        # completes → NO analyst_trace is written, so the leg looks dormant
        # rather than broken. Synthesize a timeout result so run_method
        # finishes and the actor records a trace: silence becomes an
        # observable 'workflow_timeout' row. The orphaned worker-side workflow
        # is bounded independently by the per-LM-call timeout in dspy_lm.
        logger.warning(
            "optimizer.dispatch.timeout workflow_id=%s timeout=%ss "
            "— recording a timeout trace instead of hanging",
            workflow_id, timeout_s,
        )
        timeout_result = OptimizerWorkflowResult(
            candidate_prompt_module_text=(
                f"<<workflow_timeout: exceeded {timeout_s}s>>"
            ),
            training_set_size=len(workflow_input.training_set),
            eval_score=0.0,
            eval_score_delta=0.0,
            gepa_generation=0,
            diagnostics={
                "method": "workflow_timeout",
                "reason": f"workflow exceeded {timeout_s}s",
                "usage": {
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "reasoning_tokens": 0, "total_tokens": 0,
                },
            },
        )
        return timeout_result, {
            "workflow_id": getattr(handle, "id", workflow_id),
            "run_id": getattr(handle, "result_run_id", ""),
            "in_process": in_process,
            "timed_out": True,
        }
    return result, {
        "workflow_id": getattr(handle, "id", workflow_id),
        "run_id": getattr(handle, "result_run_id", ""),
        "in_process": in_process,
    }


def _no_op_result(
    *,
    reason: str,
    options: Mapping[str, Any],
    training_size: int,
) -> AnalystMethodResult:
    """Emit an audit-only result when the optimizer has nothing to do."""
    analyst_id = str(options.get("analyst_id") or "optimizer")
    finding = FindingPayload(
        title=f"Optimizer noop: {reason}"[:200],
        body=(
            f"Optimizer for {analyst_id} ran but emitted no candidate: {reason}. "
            f"Training rows examined: {training_size}."
        )[:65000],
        confidence=0.0,
        evidence=[],
        tags=["optimizer", "noop", reason],
        data={
            "reason": reason,
            "training_size": training_size,
            "method": "noop",
        },
    )
    return AnalystMethodResult(
        finding=finding,
        usage={},
        derived_from=[],
        intermediate_steps=[
            {"phase": "noop", "reason": reason, "training_size": training_size},
        ],
    )


__all__ = [
    "AUTO_PROMOTION_SUCCESS_THRESHOLD",
    "DEFAULT_MAX_GENERATIONS",
    "DEFAULT_MIN_CRITIQUES_REQUIRED",
    "DEFAULT_MIN_TRACES_REQUIRED",
    "DEFAULT_READ_WINDOW_DAYS",
    "DEFAULT_REFLECTION_MINIBATCH_SIZE",
    "HANDLER_VERSION",
    "KIND_NAME",
    "MAX_TRAINING_ROWS",
    "OUTPUT_KIND",
    "OptimizerDeps",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "SCHEMA_VERSION",
    "build_prompt_module",
    "read_traces_and_critiques",
    "run_method",
    "should_auto_promote",
]
