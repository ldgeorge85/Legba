# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deep-consult workflow core — I/O dataclasses + the 4 shared stage bodies.

This is the altitude-3 (on-demand deep) sibling of
:mod:`legba.runtime.dapr_workflow.gepa`.  It holds:

  * **Workflow I/O dataclasses** (:class:`DeepConsultWorkflowInput` /
    :class:`DeepConsultWorkflowResult`) — plain JSON-serializable
    dataclasses that round-trip through Dapr Workflow's JSON serialization
    via ``dataclasses.asdict`` (mirrors ``gepa.py``'s I/O split).

  * **The four shared stage bodies** (:func:`_run_plan` / :func:`_run_acquire`
    / :func:`_run_analyze` / :func:`_run_synthesize`) executed either inside a
    Dapr Workflow activity (see
    :mod:`legba.runtime.dapr_workflow.deep_consult_workflow`) or synchronously
    via :func:`run_deep_consult_in_process` (the no-sidecar fallback + test
    path).  Both paths share these bodies so the staged logic lives in exactly
    one place.

THE DISCIPLINE RULE (anchor §7) — each stage is THIN orchestration over an
EXISTING primitive, never a re-implementation.  A reviewer can point at each
new line and name the primitive it orchestrates:

  * **plan**     → ``consult_on_demand._reason_via_llm`` + ``_extract_json``
                   (one LLM turn that decomposes the question; the SAME
                   ``chat_complete`` plane every analyst uses).
  * **acquire**  → ``consult_on_demand._dispatch_tool`` over the LIVE
                   :class:`legba.data.analysts.consult_on_demand.SubstrateQueryPort`
                   (``search_signals`` / ``query_facts`` / ``inspect_entity`` /
                   ``vector_search``).  NO new query code.
  * **analyze**  → ``consult_on_demand.run_method`` (the canonical ReAct
                   synthesis loop) gated by
                   :meth:`legba.runtime.budget.BudgetEnforcer.precall_check`
                   — the SAME LLM + budget plane the cadence actor uses.
  * **synthesize** → ``write_finding`` / ``write_fact`` / ``write_hypothesis``
                   (the provenance write paths).  NO raw INSERT.

The ONLY genuinely-new logic here is the plan-prompt string, the
stage-chaining glue (dict in → dict out), and the candidate fact/hypothesis
extraction from the analyze JSON.  Search, inference, budget, and persistence
are CALLED, not re-implemented.

Dependency injection
--------------------
The activities own all I/O (the deterministic orchestrator body must not).
So each stage takes its deps — the substrate port, an LLM handler, a Postgres
pool, a budget enforcer — as plain args.  The activity resolves them inside
itself via :func:`resolve_deep_consult_stage_deps` (Postgres pool + qdrant +
LLM handler from the descriptor's ``method.llm.primary``).  Tests pass
hand-built deps so the stages run with a scripted LLM + a real-shaped
substrate stub (no mocks at the substrate boundary).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workflow I/O dataclasses (shared with the orchestrator + the client)
# ---------------------------------------------------------------------------


@dataclass
class DeepConsultWorkflowInput:
    """Input dataclass for the deep-consult workflow.

    All fields are JSON-serializable so the workflow engine can round-trip the
    payload.  UUIDs are stored as strings for the same reason.
    """

    question: str
    scope_predicate: str | None = None
    submitted_by: str = ""                 # principal (audit only)
    analyst_id: str = "deep_consult"       # producing analyst id (provenance)
    analyst_version: str = ""              # descriptor content-hash (stamped at submit)
    run_id: str = ""                       # uuid str; provenance run id
    llm_component_id: str = ""             # method.llm.primary StackRef (stamped at submit)
    budget_tokens_per_day: int | None = None
    max_acquire_rounds: int = 12           # acquire/analyze tool-call cap (> chat default)
    max_analyze_tokens: int = 8192
    emit_facts: bool = True                # gated by write_fact existence (PIECE 2)
    emit_hypotheses: bool = True


@dataclass
class DeepConsultWorkflowResult:
    """Output dataclass returned by the deep-consult workflow."""

    finding_id: str                        # the produced finding row id ("" on skip)
    answer: str
    cited_substrate_refs: list[str] = field(default_factory=list)  # uuid strs (lineage)
    fact_ids: list[str] = field(default_factory=list)              # [] if write_fact absent
    hypothesis_ids: list[str] = field(default_factory=list)
    uncertainty: float = 1.0
    run_id: str = ""
    stage_diagnostics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stage deps — what each stage body needs.  Activities resolve these; tests
# construct them by hand (scripted LLM + real-shaped substrate stub).
# ---------------------------------------------------------------------------


@dataclass
class DeepConsultStageDeps:
    """Bundle the stage bodies need.  ``llm`` + ``substrate`` are the SAME
    primitives the consult kind binds; ``pg_pool`` is the substrate pool the
    synthesize stage writes through; ``budget`` is the cadence-actor enforcer.
    """

    llm: Any                                # LLMHandlerLike (chat_complete)
    substrate: Any                          # SubstrateQueryPort
    pg_pool: Any | None = None              # asyncpg.Pool — synthesize writes
    budget: Any | None = None               # BudgetEnforcer | None — analyze gates
    publish_fn: Any | None = None           # NatsPublishFn | None — write-path emit
    # W1-T1: the worker-local agency binding for the ``substrate_read`` pack.
    # When set, acquire routes EVERY tool call through ``Agency.run_pack_tool``
    # (resolve ∩ allow ∩ applicability → governor → dispatch → settle →
    # ``action_pack_invocations`` ledger) instead of the direct port dispatcher,
    # and analyze threads it into the re-entrant consult synthesis loop so its
    # tools are governed too. None = direct port dispatch (tests / non-runtime
    # embedders that hand-build deps; the production resolver ALWAYS binds it and
    # fails loud if it can't). Mirrors ConsultOnDemandDeps.agency_binding.
    agency_binding: Any | None = None


# ---------------------------------------------------------------------------
# Stage 1 — plan.  REUSES consult_on_demand._reason_via_llm + _extract_json.
# ---------------------------------------------------------------------------


from ...data.analysts._tradecraft import with_preamble  # noqa: E402

_PLAN_SYSTEM_PROMPT = with_preamble(
    """TASK — you are the PLAN stage of a deep intelligence analysis job. Decompose the operator's question into a broad-first acquisition plan over the substrate.
Respond with ONLY strict JSON, no prose:
  {"sub_queries": ["...", ...],
   "tool_plan": [{"tool": "search_signals", "args": {"query": "..."}}, ...]}
Tools available to the acquire stage:
  Finished intelligence — the platform's OWN prior products; reach for these FIRST so the analysis builds on (and reconciles against) earlier work rather than re-deriving from the raw firehose:
  - list_findings([target_id], [analyst_id], [severity], [since_hours], [include_superseded], [limit]) — recent LIVE assessments/findings (analyst products; superseded revisions excluded unless include_superseded=true; effective_confidence already folds in the critic). Cite the output_id.
  - list_situations([status], [target_id], [since_hours], [limit]) — ongoing clustered situation frames (analysis-derived). A situation_id pairs with query_hypotheses to pull its ACH rows.
  - query_predictions([target_id], [status], [limit]) — event-volume forecasts (forecast_method='naive_mean' = no trend fit, low-confidence; 'auto_arima' = fitted). The feed is FROZEN (writer retired 2026-07-01) — rows are historical, never a current forecast. Cite the output_id.
  - list_targets() — the monitored targets and their ids (e.g. country_g20_ir); resolve a place/topic to a valid target_id before list_findings / query_hypotheses.
  - list_sources([active_only], [silent_only]) — the ingest sources and their freshness/coverage; use to tell 'no source coverage on X' apart from 'a quiet feed' before concluding the substrate is silent.
  Raw substrate:
  - search_signals(query, [limit]) — full-text signal search (title + summary).
  - query_facts([subject], [predicate], [value], [limit]) — fact store.
  - inspect_entity(name) — entity profile + recent facts.
  - vector_search(query, [limit]) — semantic signal search.
Plan FINISHED-INTELLIGENCE calls first (list_findings / list_situations, resolving ids via list_targets), THEN survey the raw substrate BROADLY (2-4 wide calls) to verify and fill gaps, then drill. Emit 3-8 tool-plan entries. Each entry MUST name a tool above and a JSON args object."""
)


async def _run_plan(
    wf_input: DeepConsultWorkflowInput, deps: DeepConsultStageDeps,
) -> dict[str, Any]:
    """plan — one LLM turn that decomposes the question into a tool plan.

    REUSES :func:`consult_on_demand._reason_via_llm` (the SAME
    ``chat_complete`` turn helper the consult ReAct loop uses) +
    :func:`consult_on_demand._extract_json`.  No new planner, no new inference
    code — this is one call onto the existing LLM plane.
    """
    from ...data.analysts.consult_on_demand import _extract_json, _reason_via_llm

    started = time.monotonic()
    question = (wf_input.question or "").strip()
    if not question:
        return {
            "ok": False,
            "reason": "empty_question",
            "wf_input": asdict(wf_input),
        }

    user = f"Operator question:\n{question}"
    if wf_input.scope_predicate:
        user += f"\n\nScope predicate (apply to queries): {wf_input.scope_predicate}"

    try:
        content, usage = await _reason_via_llm(
            deps.llm,
            messages=[{"role": "user", "content": user}],
            max_tokens=min(2048, wf_input.max_analyze_tokens),
            temperature=0.2,
            system_prompt=_PLAN_SYSTEM_PROMPT,
        )
    except Exception as exc:  # noqa: BLE001 — surface as a clean plan-skip
        logger.warning("deep_consult.plan.llm_error err=%s", exc)
        return {
            "ok": False,
            "reason": f"plan_llm_error: {exc!s}",
            "wf_input": asdict(wf_input),
        }

    parsed = _extract_json(content) or {}
    tool_plan_raw = parsed.get("tool_plan")
    tool_plan = _coerce_tool_plan(tool_plan_raw)
    sub_queries = [
        str(s) for s in (parsed.get("sub_queries") or []) if s
    ][:12]

    # If the planner produced no usable tool plan, fall back to a single broad
    # search_signals over the question — never abort the run for a thin plan.
    if not tool_plan:
        tool_plan = [{"tool": "search_signals", "args": {"query": question[:512]}}]

    return {
        "ok": True,
        "wf_input": asdict(wf_input),
        "sub_queries": sub_queries,
        "tool_plan": tool_plan,
        "plan_usage": usage,
        "plan_seconds": round(time.monotonic() - started, 3),
    }


def _coerce_tool_plan(raw: Any) -> list[dict[str, Any]]:
    """Coerce the planner's tool_plan into a clean ``[{tool, args}]`` list,
    keeping only the tools consult exposes (membership in consult's
    ``_KNOWN_TOOLS`` — the full raw + graph + finished-intelligence palette,
    not the original four).
    """
    from ...data.analysts.consult_on_demand import _KNOWN_TOOLS

    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        tool = str(entry.get("tool") or "")
        if tool not in _KNOWN_TOOLS:
            continue
        args = entry.get("args")
        out.append({"tool": tool, "args": dict(args) if isinstance(args, Mapping) else {}})
        if len(out) >= 8:
            break
    return out


# ---------------------------------------------------------------------------
# Stage 2 — acquire.  REUSES consult_on_demand._dispatch_tool + the LIVE port.
# ---------------------------------------------------------------------------


async def _acquire_one_call(
    deps: DeepConsultStageDeps,
    *,
    name: str,
    args: Mapping[str, Any],
    scope_predicate: str | None,
    dispatch: Any,
) -> dict[str, Any]:
    """Execute ONE acquire tool call — governed via the binding when present,
    else the direct port dispatcher (the binding-None fallback).

    Mirrors :func:`consult_on_demand._run_one_call`'s governed branch: the
    binding shapes the :class:`ToolCall` and runs the full hard-gate pipeline
    (resolve ∩ allow ∩ applicability → governor → ledger); ``scope_predicate``
    is INJECTED into the args here (caller-pinned) so the planner cannot override
    an operator scope. NEVER raises — any failure (block / dispatch error) is
    folded into the returned dict as ``{"error": ...}`` so one call cannot abort
    the acquire round (exactly as the historical direct dispatcher behaved).
    """
    if deps.agency_binding is not None:
        try:
            outcome = await deps.agency_binding.run_tool(
                name,
                {**dict(args), "scope_predicate": scope_predicate},
            )
        except Exception as exc:  # noqa: BLE001 — one call's failure ≠ round failure
            logger.warning(
                "deep_consult.acquire.governed_error tool=%s err=%s", name, exc,
            )
            return {"error": f"tool_failed: {exc!s}"}
        if not outcome.admitted:
            return {"error": f"tool_blocked: {outcome.block_cause}: {outcome.detail}"}
        if outcome.tool_result is None or outcome.tool_result.status == "failed":
            err = (
                outcome.tool_result.error
                if outcome.tool_result is not None
                else "tool produced no result"
            )
            return {"error": f"tool_failed: {err}"}
        return dict(outcome.tool_result.output)

    # UNGOVERNED direct-port dispatch. Reachable only from hand-constructed deps
    # (tests / non-runtime embedders) — the production resolver ALWAYS binds the
    # substrate_read pack and fails loud when it can't. Log at WARNING so this can
    # never be a *silent* bypass if it ever appears on a production path.
    logger.warning(
        "deep_consult.acquire.UNGOVERNED tool=%s — agency_binding not wired; "
        "dispatching direct at the substrate port (expected only for "
        "tests/embedders, never the runtime)",
        name,
    )
    return await dispatch(
        deps.substrate,
        name=name,
        args=args,
        scope_predicate=scope_predicate,
    )


async def _run_acquire(
    plan: dict[str, Any], deps: DeepConsultStageDeps,
) -> dict[str, Any]:
    """acquire — run the plan's tool calls against the LIVE substrate port.

    REUSES :func:`consult_on_demand._dispatch_tool` VERBATIM (the same
    per-tool dispatcher the consult ReAct loop drives) over the LIVE
    :class:`PostgresQdrantSubstrateQueryPort`.  ``scope_predicate`` threads
    through unchanged.  NO new query code — every row comes back through the
    existing four-tool surface.

    W1-T1: when ``deps.agency_binding`` is wired (the runtime path), every call
    routes through the governed binding (:meth:`AgencyToolBinding.run_tool` →
    ``Agency.run_pack_tool`` → the ``action_pack_invocations`` ledger), mirroring
    :func:`consult_on_demand._run_one_call`'s governed branch — result-shape
    handling included. The direct :func:`_dispatch_tool` stays ONLY as the
    binding-None fallback (tests / non-runtime embedders), exactly as consult.
    """
    from ...data.analysts.consult_on_demand import _dispatch_tool

    started = time.monotonic()
    wf_input = DeepConsultWorkflowInput(**plan["wf_input"])
    scope_predicate = wf_input.scope_predicate
    tool_plan = plan.get("tool_plan") or []

    collected_refs: list[str] = []
    seen_refs: set[str] = set()
    evidence: list[dict[str, Any]] = []

    for call in tool_plan[: wf_input.max_acquire_rounds]:
        name = str(call.get("tool") or "")
        args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
        result = await _acquire_one_call(
            deps,
            name=name,
            args=args,
            scope_predicate=scope_predicate,
            dispatch=_dispatch_tool,
        )
        # Lift any substrate refs (lineage) — same shape consult reads.
        # W2-T4 ref honesty: only REAL UUIDs enter substrate lineage — a
        # non-UUID ref (e.g. search_context's ``ctx:``-prefixed background-
        # chunk refs) is excluded, mirroring consult's ``_coerce_uuid_list``,
        # so a Qdrant chunk id can never masquerade as substrate here either.
        for ref in (result.get("refs") or []) if isinstance(result, Mapping) else []:
            sref = str(ref)
            if not sref or sref in seen_refs:
                continue
            try:
                UUID(sref)
            except (ValueError, AttributeError):
                continue
            seen_refs.add(sref)
            collected_refs.append(sref)
        evidence.append({"tool": name, "args": dict(args), "result": result})

    return {
        "ok": True,
        "wf_input": plan["wf_input"],
        "sub_queries": plan.get("sub_queries") or [],
        "tool_plan": tool_plan,
        "evidence": evidence,
        "cited_substrate_refs": collected_refs,
        "acquire_seconds": round(time.monotonic() - started, 3),
        "plan_diagnostics": {
            "plan_usage": plan.get("plan_usage"),
            "plan_seconds": plan.get("plan_seconds"),
        },
    }


# ---------------------------------------------------------------------------
# Stage 3 — analyze.  REUSES consult_on_demand.run_method (the ReAct loop)
#                     gated by BudgetEnforcer.precall_check.
# ---------------------------------------------------------------------------


async def _run_analyze(
    acquired: dict[str, Any], deps: DeepConsultStageDeps,
) -> dict[str, Any]:
    """analyze — bounded synthesis over the acquired evidence.

    REUSES :func:`consult_on_demand.run_method` — the canonical ReAct
    synthesis loop — seeded with the plan's evidence summary.  Before driving
    the loop we gate via
    :meth:`legba.runtime.budget.BudgetEnforcer.precall_check`, the SAME budget
    plane the cadence actor uses; on ``exhausted`` / ``global_exhausted`` we
    abort with a high-uncertainty answer + a diagnostic and DO NOT spend.

    Driving the consult ``run_method`` (rather than a second loop) is the
    discipline rule in action: acquire + analyze are the consult loop split
    across two stages; the model can still issue further drill-down tool calls
    here against the SAME substrate port, but it opens with the broad evidence
    the acquire stage already gathered.
    """
    from ...data.analysts.consult_on_demand import (
        ConsultOnDemandDeps,
        run_method as consult_run_method,
    )

    started = time.monotonic()
    wf_input = DeepConsultWorkflowInput(**acquired["wf_input"])
    evidence = acquired.get("evidence") or []
    acquired_refs = [str(r) for r in (acquired.get("cited_substrate_refs") or [])]

    # ---- Budget gate (the cadence-actor plane) -----------------------------
    budget_decision: dict[str, Any] | None = None
    if deps.budget is not None and deps.pg_pool is not None:
        try:
            async with deps.pg_pool.acquire() as conn:
                decision = await deps.budget.precall_check(
                    conn,
                    estimated_tokens=int(wf_input.max_analyze_tokens),
                )
            budget_decision = {
                "outcome": decision.outcome,
                "cause": decision.cause,
                "tokens_used_today": decision.tokens_used_today,
            }
            if decision.outcome in ("exhausted", "global_exhausted"):
                logger.warning(
                    "deep_consult.analyze.budget_abort outcome=%s analyst_id=%s",
                    decision.outcome, wf_input.analyst_id,
                )
                return {
                    "ok": True,
                    "wf_input": acquired["wf_input"],
                    "answer": (
                        "Analysis aborted before LLM synthesis: the budget "
                        f"envelope is {decision.outcome}."
                    ),
                    "uncertainty": 1.0,
                    "cited_substrate_refs": acquired_refs,
                    "candidate_facts": [],
                    "candidate_hypotheses": [],
                    "analyze_diagnostics": {
                        "budget": budget_decision,
                        "aborted": "budget_exhausted",
                    },
                    "acquire_diagnostics": _acquire_diag(acquired),
                }
        except Exception as exc:  # noqa: BLE001 — never let the gate crash the run
            logger.warning("deep_consult.analyze.budget_check_failed err=%s", exc)
            budget_decision = {"outcome": "check_failed", "error": str(exc)}

    # ---- Drive the consult ReAct loop over the acquired evidence ----------
    evidence_brief = _evidence_brief(evidence)
    question = wf_input.question
    if evidence_brief:
        question = (
            f"{wf_input.question}\n\n"
            "Pre-acquired substrate evidence (from the broad-first plan):\n"
            f"{evidence_brief}\n\n"
            "Synthesize a thorough analytical answer using this evidence; you "
            "MAY drill further with tool calls before answering.")

    consult_deps = ConsultOnDemandDeps(
        llm=deps.llm,
        substrate=deps.substrate,
        max_tokens=int(wf_input.max_analyze_tokens),
        max_rounds=int(wf_input.max_acquire_rounds),
        # W1-T1: govern the re-entrant synthesis loop's tool calls too — the SAME
        # worker-local substrate_read binding acquire uses. None (tests) ⇒ the
        # consult loop falls back to its own direct-port dispatch, unchanged.
        agency_binding=deps.agency_binding,
    )
    try:
        method_result = await consult_run_method(
            inputs=[{
                "question": question,
                "scope_predicate": wf_input.scope_predicate,
            }],
            options={"analyst_id": wf_input.analyst_id},
            deps=consult_deps,
        )
    except Exception as exc:  # noqa: BLE001 — surface as a low-confidence answer
        logger.warning("deep_consult.analyze.run_method_error err=%s", exc)
        return {
            "ok": True,
            "wf_input": acquired["wf_input"],
            "answer": f"Analysis failed during synthesis: {exc!s}",
            "uncertainty": 1.0,
            "cited_substrate_refs": acquired_refs,
            "candidate_facts": [],
            "candidate_hypotheses": [],
            "analyze_diagnostics": {"budget": budget_decision, "error": str(exc)},
            "acquire_diagnostics": _acquire_diag(acquired),
        }

    consult = method_result.consult_response
    # Union the acquire-stage refs with the loop's own collected refs (lineage).
    loop_refs = [str(r) for r in (consult.cited_substrate_refs or [])]
    all_refs: list[str] = []
    seen: set[str] = set()
    for r in acquired_refs + loop_refs:
        if r and r not in seen:
            seen.add(r)
            all_refs.append(r)

    # The consult ConsultResponsePayload.data does NOT preserve arbitrary
    # final-JSON extras, so candidate facts/hypotheses are surfaced by ONE
    # dedicated extraction turn — the SAME chat_complete + _extract_json
    # primitive, no new inference machinery — over the produced answer +
    # evidence. Only runs when synthesize will actually emit them.
    cand_facts: list[dict[str, Any]] = []
    cand_hyps: list[dict[str, Any]] = []
    extract_usage: dict[str, int] = {}
    if (wf_input.emit_facts or wf_input.emit_hypotheses) and consult.answer:
        cand_facts, cand_hyps, extract_usage = await _extract_candidates_via_llm(
            deps.llm,
            answer=consult.answer,
            evidence_brief=evidence_brief,
            max_tokens=min(2048, int(wf_input.max_analyze_tokens)),
            want_facts=wf_input.emit_facts,
            want_hypotheses=wf_input.emit_hypotheses,
        )

    # Record actual spend against the analyst's budget (the post-call leg of
    # the same enforcer the cadence actor uses) — best-effort. Fold the
    # extraction turn's tokens into the metered usage.
    metered_usage = dict(method_result.usage)
    for k, v in extract_usage.items():
        metered_usage[k] = int(metered_usage.get(k, 0)) + int(v)
    await _record_budget_best_effort(deps, metered_usage)

    return {
        "ok": True,
        "wf_input": acquired["wf_input"],
        "answer": consult.answer,
        "uncertainty": float(consult.uncertainty),
        "unanswered_aspects": list(consult.unanswered_aspects),
        "cited_substrate_refs": all_refs,
        "candidate_facts": cand_facts,
        "candidate_hypotheses": cand_hyps,
        "analyze_diagnostics": {
            "budget": budget_decision,
            "usage": metered_usage,
            "rounds": consult.data.get("rounds_used"),
            "candidate_facts": len(cand_facts),
            "candidate_hypotheses": len(cand_hyps),
            "analyze_seconds": round(time.monotonic() - started, 3),
        },
        "acquire_diagnostics": _acquire_diag(acquired),
    }


_CANDIDATE_SYSTEM_PROMPT = (
    "You extract structured atomic claims from an analytical answer. Respond "
    "with ONLY strict JSON, no prose:\n"
    '  {"candidate_facts": [{"subject": "...", "predicate": "...", '
    '"value": "...", "confidence": 0.0-1.0}, ...],\n'
    '   "candidate_hypotheses": [{"thesis": "...", "counter_thesis": "..."}, ...]}\n'
    "Facts are present-tense, evidence-grounded (subject, predicate, value) "
    "triples. Hypotheses are FORWARD claims (what may happen / is likely). "
    "Emit ONLY claims the answer actually supports; emit empty lists if none. "
    "Do not invent. At most 10 of each."
)


async def _extract_candidates_via_llm(
    llm: Any,
    *,
    answer: str,
    evidence_brief: str,
    max_tokens: int,
    want_facts: bool,
    want_hypotheses: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """One extraction turn — REUSES consult's ``_reason_via_llm`` + ``_extract_json``.

    This is the SAME single-``chat_complete``-turn primitive the plan stage
    uses; it surfaces the atomic facts / forward hypotheses the synthesize
    stage emits.  No new inference machinery.
    """
    from ...data.analysts.consult_on_demand import _extract_json, _reason_via_llm

    user = (
        f"Analytical answer:\n{answer[:8000]}\n\n"
        f"Supporting evidence:\n{evidence_brief[:4000]}\n\n"
        "Extract the candidate facts and forward hypotheses as strict JSON."
    )
    try:
        content, usage = await _reason_via_llm(
            llm,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0.1,
            system_prompt=_CANDIDATE_SYSTEM_PROMPT,
        )
    except Exception as exc:  # noqa: BLE001 — extraction is best-effort
        logger.warning("deep_consult.analyze.candidate_extract_failed err=%s", exc)
        return [], [], {}

    parsed = _extract_json(content) or {}
    facts, hyps = _extract_candidates(parsed)
    if not want_facts:
        facts = []
    if not want_hypotheses:
        hyps = []
    return facts, hyps, usage


def _acquire_diag(acquired: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_calls": len(acquired.get("evidence") or []),
        "refs": len(acquired.get("cited_substrate_refs") or []),
        "acquire_seconds": acquired.get("acquire_seconds"),
        "plan": acquired.get("plan_diagnostics"),
    }


def _evidence_brief(evidence: list[dict[str, Any]], *, cap: int = 6000) -> str:
    """Compact the acquired tool results into a prompt-sized brief."""
    parts: list[str] = []
    for ev in evidence:
        tool = ev.get("tool")
        result = ev.get("result")
        rows = result.get("rows") if isinstance(result, Mapping) else None
        n = len(rows) if isinstance(rows, list) else 0
        snippet = json.dumps(result, default=str)[:1200]
        parts.append(f"[{tool}] ({n} rows) {snippet}")
    brief = "\n".join(parts)
    return brief[:cap]


def _extract_candidates(
    data: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pull candidate facts/hypotheses the analyze JSON surfaced.

    The consult ``run_method`` stashes the raw final payload's extras under
    ``data`` only when present; we read ``candidate_facts`` /
    ``candidate_hypotheses`` defensively (the synthesize stage validates each).
    """
    facts_raw = data.get("candidate_facts") if isinstance(data, Mapping) else None
    hyps_raw = data.get("candidate_hypotheses") if isinstance(data, Mapping) else None
    facts: list[dict[str, Any]] = []
    hyps: list[dict[str, Any]] = []
    if isinstance(facts_raw, list):
        for f in facts_raw[:50]:
            if isinstance(f, Mapping) and f.get("subject") and f.get("predicate"):
                facts.append(dict(f))
    if isinstance(hyps_raw, list):
        for h in hyps_raw[:50]:
            if isinstance(h, Mapping) and h.get("thesis"):
                hyps.append(dict(h))
    return facts, hyps


async def _record_budget_best_effort(
    deps: DeepConsultStageDeps, usage: Mapping[str, int],
) -> None:
    if deps.budget is None or deps.pg_pool is None:
        return
    try:
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        async with deps.pg_pool.acquire() as conn:
            await deps.budget.record(
                conn,
                prompt_tokens=prompt,
                completion_tokens=completion,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("deep_consult.analyze.budget_record_failed err=%s", exc)


# ---------------------------------------------------------------------------
# Stage 4 — synthesize.  REUSES write_finding / write_fact / write_hypothesis.
# ---------------------------------------------------------------------------


def _have_write_fact() -> bool:
    """Feature-detect ``write_fact`` (PIECE 2 prerequisite, anchor §6).

    Until PIECE 2 lands ``OutputKind.FACT`` + ``write_fact``, the fact-emit leg
    is a DECLARED SEAM that fails loud (a diagnostic flag), not a stub. Once it
    lands, the leg activates with zero code change here.
    """
    try:
        from ...data.provenance.writes import write_fact  # noqa: F401
        from ...data.provenance.kinds import OutputKind

        return hasattr(OutputKind, "FACT")
    except Exception:  # noqa: BLE001
        return False


async def _run_synthesize(
    analyzed: dict[str, Any], deps: DeepConsultStageDeps,
) -> DeepConsultWorkflowResult:
    """synthesize — write the finding (+ optional facts/hypotheses).

    REUSES the provenance write paths VERBATIM: :func:`write_finding`,
    :func:`write_fact`, :func:`write_hypothesis`.  Each already validates the
    payload, builds provenance via ``from_analyst``, INSERTs, and publishes
    NATS.  NO raw INSERT here — only payload assembly + the calls.
    """
    from ...data.provenance._core import AnalystContext
    from ...data.provenance.models import FindingPayload
    from ...data.provenance.writes import write_finding, write_hypothesis

    wf_input = DeepConsultWorkflowInput(**analyzed["wf_input"])
    answer = str(analyzed.get("answer") or "")
    uncertainty = float(analyzed.get("uncertainty", 1.0))
    refs = [str(r) for r in (analyzed.get("cited_substrate_refs") or [])]
    derived_from = _to_uuids(refs)

    run_id = _coerce_uuid(wf_input.run_id) or uuid4()
    analyst_ctx = AnalystContext(
        analyst_id=wf_input.analyst_id,
        analyst_version=wf_input.analyst_version,
        run_id=run_id,
    )

    stage_diag: dict[str, Any] = {
        "acquire": analyzed.get("acquire_diagnostics"),
        "analyze": analyzed.get("analyze_diagnostics"),
    }

    if deps.pg_pool is None:
        # No write substrate (a degraded / pre-wired env) — return the answer
        # without persisting; loud diagnostic, not a silent skip.
        logger.warning("deep_consult.synthesize.no_pg_pool — finding NOT persisted")
        stage_diag["synthesize"] = "no_pg_pool"
        return DeepConsultWorkflowResult(
            finding_id="", answer=answer, cited_substrate_refs=refs,
            uncertainty=uncertainty, run_id=str(run_id), stage_diagnostics=stage_diag,
        )

    confidence = max(0.0, min(1.0, 1.0 - uncertainty))
    finding = FindingPayload(
        title=f"Deep consult: {wf_input.question}"[:2048],
        body=(answer or "(no answer produced)")[:65000],
        confidence=confidence,
        evidence=refs[:50],
        tags=["deep_consult", f"analyst:{wf_input.analyst_id}"][:50],
        data={
            "deep_consult": {
                "question": wf_input.question,
                "uncertainty": uncertainty,
                "unanswered_aspects": analyzed.get("unanswered_aspects") or [],
                "scope_predicate": wf_input.scope_predicate,
            },
        },
    )

    fact_ids: list[str] = []
    hypothesis_ids: list[str] = []
    publish = deps.publish_fn

    async with deps.pg_pool.acquire() as conn:
        finding_row, _dlq = await write_finding(
            conn,
            analyst_ctx=analyst_ctx,
            payload=finding,
            derived_from=derived_from,
            publish_fn=publish,
        )
        finding_id = str(finding_row.id) if finding_row is not None else ""

        # ---- Optional facts leg (gated on write_fact existence) -----------
        if wf_input.emit_facts and analyzed.get("candidate_facts"):
            if _have_write_fact():
                from ...data.provenance.writes import write_fact

                for cf in analyzed["candidate_facts"]:
                    payload = _fact_payload(cf)
                    if payload is None:
                        continue
                    try:
                        row, _ = await write_fact(
                            conn,
                            analyst_ctx=analyst_ctx,
                            payload=payload,
                            derived_from=derived_from,
                            publish_fn=publish,
                        )
                        if row is not None:
                            fact_ids.append(str(row.id))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("deep_consult.synthesize.write_fact_failed err=%s", exc)
                stage_diag["facts"] = f"wrote {len(fact_ids)}"
            else:
                # DECLARED SEAM — fail loud, do not silently skip, do not stub.
                logger.warning(
                    "deep_consult.synthesize.write_fact_unavailable — "
                    "facts NOT emitted (PIECE 2 prerequisite absent)",
                )
                stage_diag["facts"] = "write_fact_unavailable"

        # ---- Optional hypotheses leg --------------------------------------
        if wf_input.emit_hypotheses and analyzed.get("candidate_hypotheses"):
            for ch in analyzed["candidate_hypotheses"]:
                payload = _hypothesis_payload(ch)
                if payload is None:
                    continue
                try:
                    row, _ = await write_hypothesis(
                        conn,
                        analyst_ctx=analyst_ctx,
                        payload=payload,
                        derived_from=derived_from,
                        publish_fn=publish,
                    )
                    if row is not None:
                        hypothesis_ids.append(str(row.id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("deep_consult.synthesize.write_hypothesis_failed err=%s", exc)
            stage_diag["hypotheses"] = f"wrote {len(hypothesis_ids)}"

    return DeepConsultWorkflowResult(
        finding_id=finding_id,
        answer=answer,
        cited_substrate_refs=refs,
        fact_ids=fact_ids,
        hypothesis_ids=hypothesis_ids,
        uncertainty=uncertainty,
        run_id=str(run_id),
        stage_diagnostics=stage_diag,
    )


def _fact_payload(cf: Mapping[str, Any]) -> Any:
    from ...data.provenance.models import FactPayload

    try:
        return FactPayload(
            subject=str(cf["subject"])[:2048],
            predicate=str(cf["predicate"])[:512],
            value=str(cf.get("value") or "")[:4096] or "unspecified",
            confidence=float(cf.get("confidence", 0.5)),
            source_type="agent",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("deep_consult.synthesize.bad_fact_candidate err=%s", exc)
        return None


def _hypothesis_payload(ch: Mapping[str, Any]) -> Any:
    from ...data.provenance.models import HypothesisPayload

    try:
        return HypothesisPayload(
            thesis=str(ch["thesis"])[:4096],
            counter_thesis=str(ch.get("counter_thesis") or "")[:4096],
            status="active",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("deep_consult.synthesize.bad_hypothesis_candidate err=%s", exc)
        return None


def _to_uuids(refs: list[str]) -> list[UUID]:
    out: list[UUID] = []
    for r in refs:
        u = _coerce_uuid(r)
        if u is not None:
            out.append(u)
    return out


def _coerce_uuid(raw: Any) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# In-process fallback — runs the 4 stages synchronously (test + no-sidecar).
# Mirrors gepa.InProcessWorkflowClient: deterministic, shares the SAME stage
# bodies the Dapr activities call, so the wiring is identical.
# ---------------------------------------------------------------------------


async def run_deep_consult_in_process(
    wf_input: DeepConsultWorkflowInput, deps: DeepConsultStageDeps,
) -> DeepConsultWorkflowResult:
    """Run plan → acquire → analyze → synthesize synchronously in-process.

    The same four stage bodies the Dapr activities call, chained the same way
    the orchestrator chains them.  Used by tests + the no-sidecar dev path.
    """
    plan = await _run_plan(wf_input, deps)
    if not plan.get("ok"):
        return DeepConsultWorkflowResult(
            finding_id="",
            answer=f"<<plan-skip: {plan.get('reason')}>>",
            uncertainty=1.0,
            run_id=wf_input.run_id,
            stage_diagnostics={"stage": "plan", **{k: v for k, v in plan.items() if k != "wf_input"}},
        )
    acquired = await _run_acquire(plan, deps)
    analyzed = await _run_analyze(acquired, deps)
    return await _run_synthesize(analyzed, deps)


__all__ = [
    "DeepConsultStageDeps",
    "DeepConsultWorkflowInput",
    "DeepConsultWorkflowResult",
    "_run_acquire",
    "_run_analyze",
    "_run_plan",
    "_run_synthesize",
    "run_deep_consult_in_process",
]
