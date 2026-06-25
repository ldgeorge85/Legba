# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-16 — Dapr-Workflow optimizer body + activities.

Shape
-----
* :func:`optimizer_workflow` is a deterministic **generator** body.  It
  ``yield``\\s ``ctx.call_activity(...)`` for each step; the engine
  records each result in workflow history and replays the generator
  deterministically.  Two-step structure:

    1. ``validate_training_set_activity`` — cheap, fail-fast.  If it
       rejects, the workflow short-circuits with a zero-delta "skipped"
       result (no candidate promoted; the kind still lands an audit row).
    2. ``compile_candidate_activity`` — THE LLM-bearing GEPA step.  Runs
       with a retry policy (2 attempts) so a wedged LLM call is retried
       by the engine rather than failing the whole run on the first
       transient error.

Determinism contract
--------------------
The generator body MUST NOT do wall-clock / RNG / I/O directly — use
``ctx.current_utc_datetime`` for time and push all non-determinism into
activities.  Activities can do anything.

Reuse of the GEPA algorithm
---------------------------
The activities here delegate to the shared loop in
:mod:`legba.runtime.dapr_workflow.gepa` —
:func:`~legba.runtime.dapr_workflow.gepa._run_gepa_loop` and
:func:`~legba.runtime.dapr_workflow.gepa.validate_training_set_activity`
— so the algorithm lives in exactly one place, shared with the
in-process fallback path.  Only the orchestration substrate differs.

Serialization
-------------
Dapr Workflow serializes workflow/activity inputs + outputs as JSON.
The activities accept/return plain ``dict`` (the
:class:`OptimizerWorkflowInput` / :class:`OptimizerWorkflowResult`
dataclasses round-trip via ``dataclasses.asdict`` at the client boundary)
so there are no custom-codec requirements.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from .gepa import (
    OptimizerWorkflowInput,
    OptimizerWorkflowResult,
    _run_gepa_loop,
    validate_training_set_activity as _validate_async,
)

logger = logging.getLogger(__name__)


# Registered names — kept stable so the worker + client agree on them.
WORKFLOW_NAME = "legba_optimizer_workflow"
VALIDATE_ACTIVITY_NAME = "legba_optimizer_validate_activity"
COMPILE_ACTIVITY_NAME = "legba_optimizer_compile_activity"


# ---------------------------------------------------------------------------
# Optional dependency probe — import-cleanly when dapr.ext.workflow absent
# ---------------------------------------------------------------------------


try:  # pragma: no cover - exercised on the live rig, shimmed in min envs
    from dapr.ext.workflow import (  # type: ignore[import-not-found]
        DaprWorkflowContext,
        RetryPolicy,
        WorkflowActivityContext,
    )

    _HAVE_DAPR_WF = True
except Exception:  # pragma: no cover
    DaprWorkflowContext = Any  # type: ignore[assignment,misc]
    WorkflowActivityContext = Any  # type: ignore[assignment,misc]
    RetryPolicy = None  # type: ignore[assignment,misc]
    _HAVE_DAPR_WF = False


# ---------------------------------------------------------------------------
# Activities — synchronous (run on the worker's activity thread pool).
#
# Dapr Workflow activities are plain callables ``(ctx, input) -> output``.
# Our GEPA work is async; activities run OFF the orchestrator on a thread
# pool, so blocking with ``asyncio.run`` here is correct (it does NOT
# block the deterministic workflow loop — that loop only sees the awaited
# Task result the engine feeds back on replay).
# ---------------------------------------------------------------------------


def validate_training_set_activity(
    ctx: "WorkflowActivityContext", wf_input: dict[str, Any],
) -> dict[str, Any]:
    """Validate the training set meets the descriptor's min thresholds.

    Delegates to the shared async validator so the validation logic is
    identical to the in-process path.  Returns the same status dict shape:
    ``{"ok": bool, "training_size": int, "reason": str}``.
    """
    payload = OptimizerWorkflowInput(**wf_input)
    return asyncio.run(_validate_async(payload))


def compile_candidate_activity(
    ctx: "WorkflowActivityContext", wf_input: dict[str, Any],
) -> dict[str, Any]:
    """Run the GEPA loop and return the resulting candidate as a dict.

    THE LLM-bearing activity.  Delegates to the shared
    :func:`legba.runtime.dapr_workflow.gepa._run_gepa_loop`, so the
    GEPA algorithm (dspy.GEPA path + naive fallback) is byte-for-byte the
    same as the in-process fallback's.  Any dspy/GEPA failure raises; the
    engine's retry policy (set on the ``call_activity`` in the workflow
    body) catches + retries.
    """
    payload = OptimizerWorkflowInput(**wf_input)
    result: OptimizerWorkflowResult = asyncio.run(_run_gepa_loop(payload))
    return asdict(result)


# ---------------------------------------------------------------------------
# Workflow body — deterministic generator.
# ---------------------------------------------------------------------------


def optimizer_workflow(
    ctx: "DaprWorkflowContext", wf_input: dict[str, Any],
):
    """Deterministic Dapr-Workflow body for the GEPA optimizer.

    Generator form: each ``yield ctx.call_activity(...)`` suspends the
    orchestration until the engine has the activity result, then resumes
    deterministically on replay from history.  Two-step structure:
    validate → compile.

    Returns the candidate as a dict (the client rehydrates it into an
    :class:`OptimizerWorkflowResult`).
    """
    # Retry policy for activities — 2 attempts, exponential backoff.
    # Guarded so the
    # module imports without dapr.ext.workflow (the body is never invoked
    # in that case — the client returns None and the kind falls back).
    retry = None
    if RetryPolicy is not None:  # pragma: no branch
        retry = RetryPolicy(
            first_retry_interval=timedelta(seconds=10),
            max_number_of_attempts=2,
            backoff_coefficient=2.0,
            max_retry_interval=timedelta(minutes=60),
        )

    # ---- Step 1: validate (cheap, fail-fast) --------------------------
    validation = yield ctx.call_activity(
        validate_training_set_activity, input=wf_input, retry_policy=retry,
    )
    if not validation.get("ok"):
        return {
            "candidate_prompt_module_text": (
                f"<<skipped: {validation.get('reason', 'validation_failed')}>>"
            ),
            "training_set_size": int(validation.get("training_size", 0)),
            "eval_score": 0.0,
            "eval_score_delta": 0.0,
            "gepa_generation": 0,
            "diagnostics": {
                "method": "skipped_validation",
                "reason": validation.get("reason", ""),
            },
        }

    # ---- Step 2: compile a candidate (the multi-hour LLM step) --------
    result = yield ctx.call_activity(
        compile_candidate_activity, input=wf_input, retry_policy=retry,
    )
    return result


__all__ = [
    "COMPILE_ACTIVITY_NAME",
    "VALIDATE_ACTIVITY_NAME",
    "WORKFLOW_NAME",
    "compile_candidate_activity",
    "optimizer_workflow",
    "validate_training_set_activity",
]
