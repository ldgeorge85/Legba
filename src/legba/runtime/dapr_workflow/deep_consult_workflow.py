# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deep-consult Dapr-Workflow body + the 4 stage activities.

Shape — mirrors :mod:`legba.runtime.dapr_workflow.workflow` (the optimizer
workflow) exactly:

  * :func:`deep_consult_workflow` is a deterministic **generator** body. It
    ``yield``\\s ``ctx.call_activity(...)`` for each of the four stages
    (plan → acquire → analyze → synthesize); the engine records each result
    in history and replays deterministically.  Stage N's output dict is stage
    N+1's input dict (chained), so the body never does I/O.

  * The four activities (:func:`plan_activity` / :func:`acquire_activity` /
    :func:`analyze_activity` / :func:`synthesize_activity`) are plain sync
    ``(ctx, dict) -> dict`` callables.  They run OFF the orchestrator on the
    worker's activity thread pool, so blocking with ``asyncio.run`` is correct
    (it does NOT block the deterministic loop).  Each delegates to the SHARED
    stage body in :mod:`legba.runtime.dapr_workflow.deep_consult`, resolving
    its deps inside itself (activities own all I/O).

Determinism contract (verbatim from the optimizer): the generator body MUST
NOT do wall-clock / RNG / I/O directly — all non-determinism lives in the
activities.

Registration: the worker registers the orchestrator + the four activities BY
FUNCTION NAME (no ``name=`` override), so the client schedules via the function
object and durabletask resolves by ``__name__`` (the #37 fix — see
``worker.py``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from .deep_consult import (
    DeepConsultStageDeps,
    DeepConsultWorkflowInput,
    DeepConsultWorkflowResult,
    _run_acquire,
    _run_analyze,
    _run_plan,
    _run_synthesize,
)

logger = logging.getLogger(__name__)


# Registered names — kept stable so the worker + client agree on them.
WORKFLOW_NAME = "legba_deep_consult_workflow"
PLAN_ACTIVITY_NAME = "legba_deep_consult_plan_activity"
ACQUIRE_ACTIVITY_NAME = "legba_deep_consult_acquire_activity"
ANALYZE_ACTIVITY_NAME = "legba_deep_consult_analyze_activity"
SYNTHESIZE_ACTIVITY_NAME = "legba_deep_consult_synthesize_activity"


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
# Stage-deps resolver — the activities call this to build the LLM handler +
# substrate port + pool + budget enforcer inside the activity (I/O is the
# activity's job, never the orchestrator's).
# ---------------------------------------------------------------------------


async def resolve_deep_consult_stage_deps(
    wf_input: DeepConsultWorkflowInput,
) -> DeepConsultStageDeps:
    """Resolve the live stage deps from env + the registry.

    Self-standing so it works both in the embedded worker (in-process in
    ``legba-runtime-dapr``) and a standalone worker container.  Reuses the
    SAME builders the host uses:

      * the substrate Postgres pool (``PostgresStore`` / ``PostgresConfig``),
      * the qdrant client + :class:`PostgresQdrantSubstrateQueryPort` (the LIVE
        production ``SubstrateQueryPort`` — the same instance shape the host
        builds),
      * the LLM handler via
        :func:`legba.runtime.analyst_deps_builder.build_llm_handler_from_stack_component`
        over ``wf_input.llm_component_id``,
      * a :class:`legba.runtime.budget.BudgetEnforcer` keyed to the
        ``deep_consult`` analyst id (the cadence-actor budget plane).

    The pool is owned by the caller (the activity) and closed when the
    activity's ``asyncio.run`` loop tears down.
    """
    import os

    from ...data.postgres import PostgresStore
    from ..analyst_deps_builder import build_llm_handler_from_stack_component
    from ..budget import BudgetEnforcer
    from ..registry_client import RegistryHTTPClient
    from ..substrate_query_port import PostgresQdrantSubstrateQueryPort

    pg_store = PostgresStore.from_env()
    await pg_store.connect()

    registry_client = RegistryHTTPClient()

    # Qdrant is optional — vector_search degrades to its documented
    # ``unavailable`` shape without it (same as the host path,
    # ``dapr_host.py:927-943``).
    qdrant_client: Any | None = None
    try:
        from ..qdrant_factory import build_qdrant_client_from_stack_component

        qdrant_client = await build_qdrant_client_from_stack_component(
            os.environ.get(
                "LEGBA_DATA_DEFAULT_VECTOR_STORE",
                "vector.qdrant.cluster_main",
            ),
            registry_client=registry_client,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("deep_consult.deps.qdrant_unavailable err=%s", exc)
        qdrant_client = None

    # Stage 1 — the OpenSearch full-text corpus store for search_corpus /
    # read_document. Built GUARDED (opensearch-py may be absent, or the index
    # unprovisioned); None keeps the honest no_corpus_wired fallback.
    try:
        from ...data.opensearch import OpenSearchStore

        _os_store: Any | None = OpenSearchStore.from_env()
    except Exception:
        _os_store = None

    substrate = PostgresQdrantSubstrateQueryPort(
        pg_pool=pg_store.pool,
        qdrant_client=qdrant_client,
        opensearch_store=_os_store,
    )

    vault = _build_vault(pg_store)

    async def _secrets_resolve(secret_id: str) -> bytes:
        return await vault.resolve(secret_id)

    llm = await build_llm_handler_from_stack_component(
        wf_input.llm_component_id,
        registry_client=registry_client,
        secrets_resolve=_secrets_resolve,
    )

    budget = BudgetEnforcer(
        analyst_id=wf_input.analyst_id,
        analyst_version=wf_input.analyst_version,
        budget_tokens_per_day=wf_input.budget_tokens_per_day,
        provider=getattr(llm, "subprovider", "vllm"),
        model=getattr(llm, "model_name", "") or "",
        estimated_tokens_per_run=int(wf_input.max_analyze_tokens),
    )

    async def _publish(subject: str, payload: bytes) -> None:  # best-effort
        return None

    # W1-T1 — the WORKER-LOCAL agency plane. deep_consult runs in the standalone
    # workflow worker (LEGBA_EMBED_WORKFLOW_WORKER=0), where the runtime's
    # AGENCY_HOLDER is EMPTY — so we build the Agency here rather than fetch a
    # runtime-held one (the consult recipe in dapr_host._analyst_deps_resolver
    # reaches into AGENCY_HOLDER, which is wrong for this worker). Every acquire /
    # re-entrant-synthesis tool call then routes through Agency.run_pack_tool
    # (resolve ∩ allow ∩ applicability → governor → the action_pack_invocations
    # ledger) instead of the ungoverned direct dispatch.
    agency_binding = await _build_worker_agency_binding(
        registry_client=registry_client,
        substrate=substrate,
        pg_pool=pg_store.pool,
    )

    return DeepConsultStageDeps(
        llm=llm,
        substrate=substrate,
        pg_pool=pg_store.pool,
        budget=budget,
        publish_fn=_publish,
        agency_binding=agency_binding,
    )


async def _build_worker_agency_binding(
    *,
    registry_client: Any,
    substrate: Any,
    pg_pool: Any,
) -> Any:
    """Build the worker-local ``substrate_read`` :class:`AgencyToolBinding`.

    Mirrors the consult binding in
    :func:`legba.runtime.dapr_host._analyst_deps_resolver` (the self-allow model
    for a no-target operator surface) but constructs the :class:`Agency` LOCALLY
    from :func:`default_tool_registry` — the standalone workflow worker's
    ``AGENCY_HOLDER`` is empty, so there is no runtime-held plane to fetch.

    FAIL-LOUD: if the ``substrate_read`` pack can't be fetched / validated we
    RAISE. deep_consult is an operator-billed surface; silently continuing to the
    ungoverned direct dispatch is exactly the orphaned-agency-plane bug W1-T1
    closes.
    """
    from ...data.analysts.agency import Agency
    from ...data.analysts.agency.binding import (
        AgencyToolBinding,
        GLOBAL_SCOPE,
        fetch_action_pack,
    )
    from ...data.analysts.agency.substrate_read import SUBSTRATE_READ_PACK_ID
    from ...data.analysts.agency.tools import ToolContext, default_tool_registry
    from ...data.schemas.action_pack import ActionPackRef

    pack = await fetch_action_pack(registry_client, SUBSTRATE_READ_PACK_ID)
    if pack is None:
        raise RuntimeError(
            "deep_consult.deps.substrate_read_pack_unavailable — the "
            f"{SUBSTRATE_READ_PACK_ID!r} action pack could not be fetched from "
            "the registry; register it (bringup_register_action_packs) before "
            "running deep_consult. Refusing to fall back to ungoverned dispatch.",
        )

    return AgencyToolBinding(
        agency=Agency(tool_registry=default_tool_registry()),
        pack=pack,
        pg_pool=pg_pool,
        # No queue / emit — the read-only substrate_read pack only needs the
        # SubstrateQueryPort already built here (the same instance acquire reads).
        tool_context=ToolContext(substrate=substrate),
        # The no-target operator surface self-satisfies BOTH legs of the gate:
        # the grant leg is a real ActionPackRef (so resolve_pack sees it granted,
        # not a resolution bypass) and the read-only pack is allowed by
        # construction under the synthetic GLOBAL scope (mirrors the consult
        # binding's self-allow — deep_consult, like consult, carries no target).
        analyst_grants=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        target_allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        scope=GLOBAL_SCOPE,
        requested_by="analyst::deep_consult",
        budget_account="deep_consult",
    )


def _build_vault(pg_store: Any) -> Any:
    from ...data.registry.credentials import CredentialVault

    return CredentialVault(pg_store)


# ---------------------------------------------------------------------------
# Activities — synchronous (run on the worker's activity thread pool).
# Each resolves its own deps + delegates to the shared stage body.
# ---------------------------------------------------------------------------


def plan_activity(
    ctx: "WorkflowActivityContext", wf_input: dict[str, Any],
) -> dict[str, Any]:
    """plan — decompose the question into a tool plan (one LLM turn)."""
    payload = DeepConsultWorkflowInput(**wf_input)

    async def _go() -> dict[str, Any]:
        deps = await resolve_deep_consult_stage_deps(payload)
        return await _run_plan(payload, deps)

    return asyncio.run(_go())


def acquire_activity(
    ctx: "WorkflowActivityContext", plan: dict[str, Any],
) -> dict[str, Any]:
    """acquire — run the plan's tool calls against the LIVE substrate port."""
    payload = DeepConsultWorkflowInput(**plan["wf_input"])

    async def _go() -> dict[str, Any]:
        deps = await resolve_deep_consult_stage_deps(payload)
        return await _run_acquire(plan, deps)

    return asyncio.run(_go())


def analyze_activity(
    ctx: "WorkflowActivityContext", acquired: dict[str, Any],
) -> dict[str, Any]:
    """analyze — bounded synthesis over the evidence (budget-gated)."""
    payload = DeepConsultWorkflowInput(**acquired["wf_input"])

    async def _go() -> dict[str, Any]:
        deps = await resolve_deep_consult_stage_deps(payload)
        return await _run_analyze(acquired, deps)

    return asyncio.run(_go())


def synthesize_activity(
    ctx: "WorkflowActivityContext", analyzed: dict[str, Any],
) -> dict[str, Any]:
    """synthesize — write the finding (+ optional facts/hypotheses)."""
    payload = DeepConsultWorkflowInput(**analyzed["wf_input"])

    async def _go() -> dict[str, Any]:
        deps = await resolve_deep_consult_stage_deps(payload)
        result: DeepConsultWorkflowResult = await _run_synthesize(analyzed, deps)
        return asdict(result)

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# Workflow body — deterministic generator (mirrors optimizer_workflow).
# ---------------------------------------------------------------------------


def deep_consult_workflow(
    ctx: "DaprWorkflowContext", wf_input: dict[str, Any],
):
    """Deterministic Dapr-Workflow body for the deep consult.

    Generator form: each ``yield ctx.call_activity(...)`` suspends until the
    engine has the activity result, then resumes deterministically on replay.
    Four-step structure: plan → acquire → analyze → synthesize.  Stage N's
    output dict is stage N+1's input dict, so the body only chains dicts.

    Returns the :class:`DeepConsultWorkflowResult` as a dict (the client
    rehydrates it).
    """
    retry = None
    if RetryPolicy is not None:  # pragma: no branch
        retry = RetryPolicy(
            first_retry_interval=timedelta(seconds=10),
            max_number_of_attempts=2,
            backoff_coefficient=2.0,
            max_retry_interval=timedelta(minutes=30),
        )

    # ---- Stage 1: plan (cheap; one LLM turn) --------------------------
    plan = yield ctx.call_activity(plan_activity, input=wf_input, retry_policy=retry)
    if not plan.get("ok"):
        return {
            "finding_id": "",
            "answer": f"<<plan-skip: {plan.get('reason')}>>",
            "cited_substrate_refs": [],
            "fact_ids": [],
            "hypothesis_ids": [],
            "uncertainty": 1.0,
            "run_id": str(wf_input.get("run_id", "")),
            "stage_diagnostics": {
                "stage": "plan",
                "reason": plan.get("reason"),
            },
        }

    # ---- Stage 2: acquire (substrate reads) ---------------------------
    acquired = yield ctx.call_activity(acquire_activity, input=plan, retry_policy=retry)
    # ---- Stage 3: analyze (LLM synthesis under the budget plane) -------
    analyzed = yield ctx.call_activity(analyze_activity, input=acquired, retry_policy=retry)
    # ---- Stage 4: synthesize (provenance writes) ----------------------
    result = yield ctx.call_activity(synthesize_activity, input=analyzed, retry_policy=retry)
    return result


__all__ = [
    "ACQUIRE_ACTIVITY_NAME",
    "ANALYZE_ACTIVITY_NAME",
    "PLAN_ACTIVITY_NAME",
    "SYNTHESIZE_ACTIVITY_NAME",
    "WORKFLOW_NAME",
    "acquire_activity",
    "analyze_activity",
    "deep_consult_workflow",
    "plan_activity",
    "resolve_deep_consult_stage_deps",
    "synthesize_activity",
]
