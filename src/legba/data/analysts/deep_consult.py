# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``deep_consult`` analyst kind — the detached submit half of PIECE 4.

This kind is the **actor→workflow bridge** for the deep-consult staged Dapr
Workflow (anchor §2.2 / §5 PIECE 4 — "scheduled via the actor→workflow bridge,
optimizer pattern").  It is deliberately THIN:

  * its ``run_method`` SCHEDULES :func:`deep_consult_workflow` via the
    runtime-resident :class:`DaprDeepConsultWorkflowClient` and returns the
    DETACHED task id IMMEDIATELY — it does NOT await ``result()``.  So the
    actor method returns in <1s and the registry's submit POST is never pinned
    by the 180s blocking envelope the consult invoke path uses
    (``consult_api.py:259-283``).

  * the analytical work (plan → acquire → analyze → synthesize) lives in the
    workflow stages (:mod:`legba.runtime.dapr_workflow.deep_consult`), each of
    which is itself thin orchestration over an existing primitive.  This kind
    adds NO analysis logic — it is purely the schedule shim, mirroring
    ``optimizer._dispatch_workflow`` but WITHOUT the ``handle.result()`` await
    (the optimizer awaits because GEPA is a blocking analyst run; deep consult
    is fire-and-forget and read later).

The produced FINDING (+ optional facts/hypotheses) is written by the workflow's
synthesize stage under THIS kind's ``analyst_id`` / ``analyst_version`` (stamped
into the workflow input from ``options``), so lineage + the eval scorecard treat
deep consult as a first-class producer.

Why no analysis here / why ``OUTPUT_KIND = FINDING``: the actor's run path
short-circuits on ``kind == "deep_consult"`` (``dapr_actors.py``) and returns
the task id without writing a row — the row is the WORKFLOW's product.
``OUTPUT_KIND`` is declared FINDING for registry/eval identity consistency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4

from ..provenance.kinds import OutputKind as _OutputKind

logger = logging.getLogger(__name__)


KIND_NAME = "deep_consult"
SCHEMA_VERSION = "legba/analyst.deep_consult/1-0-0"
HANDLER_VERSION = "0.1.0"
PROMPT_MODULE_PATH = "legba.prompts.consult_on_demand.v1"

OUTPUT_KIND: _OutputKind = _OutputKind.FINDING
# On-demand only — the registry route invokes the actor directly with the
# question; there is no cadence slice to read.
READ_SLICE = None


@dataclass
class DeepConsultKindDeps:
    """Deps the runtime binds for the ``deep_consult`` kind.

    ``workflow_client`` is the runtime-resident
    :class:`legba.runtime.dapr_workflow.deep_consult_client.DaprDeepConsultWorkflowClient`
    (the host builds it once and threads it here).  ``llm_component_id`` is the
    descriptor's ``method.llm.primary`` StackRef the workflow's plan/analyze
    stages resolve their LLM handler from.
    """

    workflow_client: Any
    llm_component_id: str = ""
    budget_tokens_per_day: int | None = None
    max_acquire_rounds: int = 12
    max_analyze_tokens: int = 8192
    emit_facts: bool = True
    emit_hypotheses: bool = True


@dataclass
class DeepConsultSubmitResult:
    """Result of one ``deep_consult`` submit.

    The actor's ``deep_consult`` short-circuit (``dapr_actors.py``) reads
    ``task_id`` + ``submit_status`` off this and returns them in the envelope
    WITHOUT writing a row.  ``finding``/``usage`` are empty — the workflow's
    synthesize stage writes the finding + meters its own spend.
    """

    task_id: str
    submit_status: str = "running"
    usage: dict[str, int] = field(default_factory=dict)
    derived_from: list[Any] = field(default_factory=list)
    intermediate_steps: list[dict[str, Any]] = field(default_factory=list)


def _sanitize_scope(raw: str | None) -> str:
    """Build a '::'-free, dot-free scope tag for the instance id grammar (D8).

    Dapr derives ``<instance>::<taskId>::<gen>`` and a ``::`` (or, for the
    activity-result strip, a ``.``-collision) in the instance id mis-parses →
    the workflow hangs forever (the optimizer hang GOTCHA,
    ``optimizer.py:508-519``). We strip both separators from the tag.
    """
    if not raw:
        return "global"
    cleaned = "".join(
        c for c in str(raw) if c.isalnum() or c in "-_"
    )
    return cleaned[:32] or "global"


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: DeepConsultKindDeps,
) -> DeepConsultSubmitResult:
    """Schedule the deep-consult workflow (detached) and return the task id.

    ``inputs[0]`` MUST carry ``question``; ``scope_predicate`` is optional.
    ``options`` carries ``analyst_id`` / ``analyst_version`` / ``run_id`` the
    actor stamped.  We build the workflow input, mint a '::'-free instance id
    (D8), schedule via the client WITHOUT awaiting, and return the task id.
    """
    from ...runtime.dapr_workflow.deep_consult import DeepConsultWorkflowInput

    if not inputs:
        raise ValueError("deep_consult requires inputs[0] with 'question'")
    first = inputs[0]
    if not isinstance(first, Mapping) or "question" not in first:
        raise ValueError("deep_consult requires inputs[0]['question']")
    question = str(first["question"]).strip()
    if not question:
        raise ValueError("deep_consult 'question' must be non-empty")
    scope_predicate = first.get("scope_predicate")
    if scope_predicate is not None:
        scope_predicate = str(scope_predicate)

    if deps.workflow_client is None:
        raise RuntimeError(
            "deep_consult kind has no workflow client wired — the Dapr "
            "Workflow engine is required for the detached submit (no "
            "in-process fallback; the run is durable by design)",
        )

    run_id = str(options.get("run_id") or uuid4())
    analyst_id = str(options.get("analyst_id") or KIND_NAME)
    analyst_version = str(options.get("analyst_version") or "")

    wf_input = DeepConsultWorkflowInput(
        question=question,
        scope_predicate=scope_predicate,
        submitted_by=str(options.get("submitted_by") or ""),
        analyst_id=analyst_id,
        analyst_version=analyst_version,
        run_id=run_id,
        llm_component_id=deps.llm_component_id,
        budget_tokens_per_day=deps.budget_tokens_per_day,
        max_acquire_rounds=int(
            first.get("max_acquire_rounds") or deps.max_acquire_rounds
        ),
        max_analyze_tokens=int(deps.max_analyze_tokens),
        emit_facts=bool(deps.emit_facts),
        emit_hypotheses=bool(deps.emit_hypotheses),
    )

    # Instance id grammar: deep_consult.<scope>.<short> — NO '::' (D8).
    scope_tag = _sanitize_scope(scope_predicate or first.get("scope_tag"))
    workflow_id = f"deep_consult.{scope_tag}.{run_id.replace('-', '')[:8]}"

    task_id = await deps.workflow_client.start_deep_consult_workflow(
        wf_input, workflow_id=workflow_id,
    )
    logger.info(
        "deep_consult.submit task_id=%s analyst_id=%s run_id=%s",
        task_id, analyst_id, run_id,
    )
    return DeepConsultSubmitResult(task_id=str(task_id), submit_status="running")


def build_prompt_module() -> Any:
    """Deep consult reuses the consult prompt module for identity parity.

    The plan/analyze stages render their own stage prompts; this hook exists so
    the kind satisfies the optional ``build_prompt_module`` contract the host's
    optimizer-promotion path probes (``analyst_deps_builder.py``).
    """
    from .consult_on_demand import build_prompt_module as _consult_build

    return _consult_build()


__all__ = [
    "DeepConsultKindDeps",
    "DeepConsultSubmitResult",
    "HANDLER_VERSION",
    "KIND_NAME",
    "OUTPUT_KIND",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "SCHEMA_VERSION",
    "run_method",
]
