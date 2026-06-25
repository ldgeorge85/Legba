# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deep-consult staged-workflow tests (anchor §5 PIECE 4).

Covers (per PLAN_DEEP_CONSULT_WORKFLOW.md §9):

  1. Stage chaining via the in-process fallback (scripted LLM + real-shaped
     substrate stub — no mocks at the substrate boundary).
  2. Each stage's reuse of its existing primitive (plan→_reason_via_llm,
     acquire→_dispatch_tool, analyze→consult.run_method, synthesize→writes).
  3. Determinism / instance-id ``::`` guard (D8).
  4. ``write_fact`` feature-detect SEAM (finding + hypotheses still land).
  5. Budget gate → high-uncertainty abort with no LLM spend past the gate.
  6. Worker registers BOTH workflows by function name (the #37 fix).

The LLM boundary is a scripted test double; the substrate boundary stays a
real-shaped stub (the no-mocks-at-substrate rule). The synthesize stage writes
through a recording fake pool so the provenance write paths are exercised
without a live Postgres in this unit suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest

from legba.runtime.dapr_workflow.deep_consult import (
    DeepConsultStageDeps,
    DeepConsultWorkflowInput,
    DeepConsultWorkflowResult,
    _run_acquire,
    _run_analyze,
    _run_plan,
    run_deep_consult_in_process,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _ScriptedLLM:
    """Scripted LLM double (mirrors the consult-test pattern)."""

    subprovider = "vllm-test"
    model_name = "test-model"

    def __init__(self, responses: list[str], *, default: str | None = None) -> None:
        self._responses = list(responses)
        self._default = default or json.dumps(
            {"final": True, "answer": "(default)", "uncertainty": 1.0,
             "cited_refs": [], "unanswered_aspects": ["exhausted"]}
        )
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": messages, "system": system})
        content = self._responses.pop(0) if self._responses else self._default
        return _Response(content=content, usage=_Usage())


@dataclass
class _SubstrateStub:
    """Real-shaped SubstrateQueryPort stub (returns fixed rows + refs)."""

    signal_refs: list[UUID] = field(default_factory=list)
    fact_refs: list[UUID] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def search_signals(self, *, query: str, category: str | None = None,
                             limit: int = 20, scope_predicate: str | None = None) -> dict[str, Any]:
        self.calls.append(("search_signals", {"query": query, "scope_predicate": scope_predicate}))
        return {"rows": [{"id": str(r), "title": "row"} for r in self.signal_refs],
                "refs": [str(r) for r in self.signal_refs]}

    async def query_facts(self, *, subject=None, predicate=None, value=None, limit=30) -> dict[str, Any]:
        self.calls.append(("query_facts", {"subject": subject}))
        return {"rows": [], "refs": [str(r) for r in self.fact_refs]}

    async def inspect_entity(self, *, name: str) -> dict[str, Any]:
        self.calls.append(("inspect_entity", {"name": name}))
        return {"found": False, "refs": []}

    async def vector_search(self, *, query: str, limit: int = 10) -> dict[str, Any]:
        self.calls.append(("vector_search", {"query": query}))
        return {"rows": [], "refs": [], "unavailable": True}


# --- Recording fake pool so synthesize exercises the real write paths ------


class _FakeConn:
    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink

    async def execute(self, sql: str, *args: Any) -> str:
        # Capture which table the write path INSERTed into (finding / fact /
        # hypothesis) by sniffing the SQL the real writes._insert_* emit.
        table = "?"
        for t in ("analyst_outputs", "facts", "hypotheses", "situations"):
            if f"INTO {t}" in sql:
                table = t
                break
        self._sink.append({"table": table, "args": args})
        return "INSERT 0 1"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return None


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.inserts: list[dict[str, Any]] = []

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(_FakeConn(self.inserts))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_json(tool: str = "search_signals") -> str:
    return json.dumps({
        "sub_queries": ["a", "b"],
        "tool_plan": [{"tool": tool, "args": {"query": "energy brazil"}}],
    })


def _final_json(answer: str = "Brazil's grid is strained.", refs: list[str] | None = None) -> str:
    return json.dumps({
        "final": True, "answer": answer, "uncertainty": 0.3,
        "cited_refs": refs or [], "unanswered_aspects": [],
    })


def _candidates_json() -> str:
    return json.dumps({
        "candidate_facts": [
            {"subject": "Brazil grid", "predicate": "status", "value": "strained", "confidence": 0.8}
        ],
        "candidate_hypotheses": [
            {"thesis": "Brazil will face blackouts", "counter_thesis": "Reserves hold"}
        ],
    })


def _input(**kw: Any) -> DeepConsultWorkflowInput:
    base = dict(question="What is the state of Brazil's energy grid?",
                run_id=str(uuid4()), analyst_id="deep_consult", analyst_version="v0")
    base.update(kw)
    return DeepConsultWorkflowInput(**base)


# ---------------------------------------------------------------------------
# 1 + 2. Stage chaining + per-stage primitive reuse (in-process fallback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_process_runs_four_stages_and_writes_finding() -> None:
    ref = uuid4()
    # plan turn → consult-loop final → candidate-extraction turn.
    llm = _ScriptedLLM([_plan_json(), _final_json(refs=[str(ref)]), _candidates_json()])
    substrate = _SubstrateStub(signal_refs=[ref])
    pool = _FakePool()
    deps = DeepConsultStageDeps(llm=llm, substrate=substrate, pg_pool=pool)

    result = await run_deep_consult_in_process(_input(), deps)

    assert isinstance(result, DeepConsultWorkflowResult)
    assert result.finding_id  # a finding row was written
    assert result.answer.startswith("Brazil")
    assert 0.0 <= result.uncertainty <= 1.0
    # acquire used the real dispatcher against the stub port (search_signals).
    assert any(c[0] == "search_signals" for c in substrate.calls)
    # derived_from threaded the acquired ref into the finding INSERT (lineage).
    finding_inserts = [i for i in pool.inserts if i["table"] == "analyst_outputs"]
    assert finding_inserts
    # The finding INSERT carries a derived_from list[UUID] positional arg
    # containing the acquired substrate ref.
    derived_lists = [
        a for i in finding_inserts for a in i["args"]
        if isinstance(a, list) and ref in a
    ]
    assert derived_lists, "acquired ref not threaded into finding derived_from"
    assert str(ref) in result.cited_substrate_refs


@pytest.mark.asyncio
async def test_acquire_reuses_dispatch_tool_and_collects_refs() -> None:
    ref = uuid4()
    llm = _ScriptedLLM([_plan_json()])
    substrate = _SubstrateStub(signal_refs=[ref])
    deps = DeepConsultStageDeps(llm=llm, substrate=substrate)

    plan = await _run_plan(_input(), deps)
    assert plan["ok"] is True
    acquired = await _run_acquire(plan, deps)

    assert str(ref) in acquired["cited_substrate_refs"]
    assert acquired["evidence"][0]["tool"] == "search_signals"


@pytest.mark.asyncio
async def test_analyze_reuses_consult_run_method() -> None:
    ref = uuid4()
    llm = _ScriptedLLM([_plan_json(), _final_json(refs=[str(ref)]), _candidates_json()])
    substrate = _SubstrateStub(signal_refs=[ref])
    deps = DeepConsultStageDeps(llm=llm, substrate=substrate)

    plan = await _run_plan(_input(), deps)
    acquired = await _run_acquire(plan, deps)
    analyzed = await _run_analyze(acquired, deps)

    assert analyzed["answer"].startswith("Brazil")
    assert analyzed["uncertainty"] == pytest.approx(0.3)
    # candidate extraction surfaced both legs.
    assert analyzed["candidate_facts"]
    assert analyzed["candidate_hypotheses"]


# ---------------------------------------------------------------------------
# 3. ``::`` guard (D8) — minted instance id has no ``::``
# ---------------------------------------------------------------------------


def test_instance_id_has_no_double_colon() -> None:
    from legba.data.analysts.deep_consult import _sanitize_scope

    # The kind mints ``deep_consult.<scope>.<run8>`` — scope is sanitized.
    scope = _sanitize_scope("country::ar.with.dots::x")
    assert "::" not in scope
    assert "." not in scope
    workflow_id = f"deep_consult.{scope}.{uuid4().hex[:8]}"
    assert "::" not in workflow_id


# ---------------------------------------------------------------------------
# 4. write_fact feature-detect SEAM — finding + hypotheses still land,
#    fact_ids == [] + a loud diagnostic flag when write_fact is absent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_fact_unavailable_seam(monkeypatch) -> None:
    import legba.runtime.dapr_workflow.deep_consult as dc

    monkeypatch.setattr(dc, "_have_write_fact", lambda: False)

    ref = uuid4()
    llm = _ScriptedLLM([_plan_json(), _final_json(refs=[str(ref)]), _candidates_json()])
    substrate = _SubstrateStub(signal_refs=[ref])
    pool = _FakePool()
    deps = DeepConsultStageDeps(llm=llm, substrate=substrate, pg_pool=pool)

    result = await run_deep_consult_in_process(_input(), deps)

    assert result.fact_ids == []
    assert result.stage_diagnostics["facts"] == "write_fact_unavailable"
    # The hypothesis leg still landed (its write path is independent).
    assert any(i["table"] == "hypotheses" for i in pool.inserts)


# ---------------------------------------------------------------------------
# 5. Budget gate — exhausted → high-uncertainty abort, no spend past the gate
# ---------------------------------------------------------------------------


@dataclass
class _Decision:
    outcome: str
    cause: str | None = None
    tokens_used_today: int = 0


class _ExhaustedBudget:
    def __init__(self) -> None:
        self.recorded = False

    async def precall_check(self, conn: Any, *, estimated_tokens: int = 0, **kw: Any) -> _Decision:
        return _Decision(outcome="exhausted", cause="per_analyst")

    async def record(self, conn: Any, **kw: Any) -> None:
        self.recorded = True


@pytest.mark.asyncio
async def test_budget_exhausted_aborts_analyze_before_llm() -> None:
    ref = uuid4()
    # plan turn only — the consult loop must NOT run when budget is exhausted.
    llm = _ScriptedLLM([_plan_json()])
    substrate = _SubstrateStub(signal_refs=[ref])
    pool = _FakePool()
    budget = _ExhaustedBudget()
    deps = DeepConsultStageDeps(llm=llm, substrate=substrate, pg_pool=pool, budget=budget)

    plan = await _run_plan(_input(), deps)
    acquired = await _run_acquire(plan, deps)
    # exactly one LLM call so far (the plan turn).
    assert len(llm.calls) == 1
    analyzed = await _run_analyze(acquired, deps)

    assert analyzed["uncertainty"] == 1.0
    assert "budget" in analyzed["answer"].lower()
    assert analyzed["analyze_diagnostics"]["aborted"] == "budget_exhausted"
    # No further LLM calls after the gate (consult loop never ran).
    assert len(llm.calls) == 1


# ---------------------------------------------------------------------------
# Empty-question plan-skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_question_plan_skip() -> None:
    llm = _ScriptedLLM([])
    substrate = _SubstrateStub()
    deps = DeepConsultStageDeps(llm=llm, substrate=substrate, pg_pool=_FakePool())
    result = await run_deep_consult_in_process(_input(question=""), deps)
    assert result.finding_id == ""
    assert "plan-skip" in result.answer
    assert len(llm.calls) == 0  # no LLM spend on an empty question


# ---------------------------------------------------------------------------
# Client contract — detached submit (no result() await) + get_status surface
# ---------------------------------------------------------------------------


def test_deep_consult_client_is_detached_and_pollable() -> None:
    import inspect

    from legba.runtime.dapr_workflow.deep_consult_client import (
        DaprDeepConsultWorkflowClient,
    )

    client = DaprDeepConsultWorkflowClient()
    # The submit method returns a task id (str), NOT a handle whose result()
    # is awaited — the schedule-vs-await split that keeps submit non-blocking.
    assert hasattr(client, "start_deep_consult_workflow")
    assert hasattr(client, "get_status")
    sig = inspect.signature(client.start_deep_consult_workflow)
    assert sig.parameters["workflow_id"].kind is inspect.Parameter.KEYWORD_ONLY
    # Its source must NOT call wait_for_workflow_completion (the optimizer's
    # blocking path) — detached by construction.
    src = inspect.getsource(client.start_deep_consult_workflow)
    assert "wait_for_workflow_completion" not in src
    # No await on a completion handle — the only awaits are the connect +
    # schedule bridges (asyncio.to_thread).
    assert "await handle.result()" not in src
    assert ".result()" not in src.split('"""')[-1]  # ignore the docstring


@pytest.mark.asyncio
async def test_deep_consult_client_get_status_maps_states() -> None:
    from legba.runtime.dapr_workflow.deep_consult_client import (
        DaprDeepConsultWorkflowClient,
    )

    class _State:
        def __init__(self, status: str, output: Any = None) -> None:
            self.runtime_status = status
            self.serialized_output = output
            self.failure_details = None

    class _FakeRawClient:
        def __init__(self, state: Any) -> None:
            self._state = state

        def get_workflow_state(self, task_id, *, fetch_payloads=True):
            return self._state

    completed_out = json.dumps({
        "finding_id": "f1", "answer": "done", "uncertainty": 0.2,
        "cited_substrate_refs": ["r1"], "fact_ids": [], "hypothesis_ids": [],
    })

    client = DaprDeepConsultWorkflowClient()
    client._client = _FakeRawClient(_State("WorkflowStatus.RUNNING"))
    running = await client.get_status("t1")
    assert running["status"] == "running"

    client._client = _FakeRawClient(_State("WorkflowStatus.COMPLETED", completed_out))
    completed = await client.get_status("t1")
    assert completed["status"] == "completed"
    assert completed["finding_id"] == "f1"
    assert completed["answer"] == "done"

    client._client = _FakeRawClient(_State("WorkflowStatus.FAILED"))
    failed = await client.get_status("t1")
    assert failed["status"] == "failed"


# ---------------------------------------------------------------------------
# 6. Worker registers BOTH workflows by function name (the #37 fix)
# ---------------------------------------------------------------------------


def test_worker_registers_both_workflows_by_function_name(monkeypatch) -> None:
    registered_workflows: list[str] = []
    registered_activities: list[str] = []

    class _ShimRuntime:
        def __init__(self, *, host: str, port: str) -> None:
            self.host = host
            self.port = port

        def register_workflow(self, fn: Any) -> None:
            registered_workflows.append(fn.__name__)

        def register_activity(self, fn: Any) -> None:
            registered_activities.append(fn.__name__)

    import legba.runtime.dapr_workflow.worker as worker_mod

    # Shim the WorkflowRuntime import inside build_workflow_runtime.
    import dapr.ext.workflow as dapr_wf
    monkeypatch.setattr(dapr_wf, "WorkflowRuntime", _ShimRuntime, raising=False)

    runtime = worker_mod.build_workflow_runtime(host="127.0.0.1", port="50001")
    assert isinstance(runtime, _ShimRuntime)

    # BOTH orchestrators registered by their function name.
    assert "optimizer_workflow" in registered_workflows
    assert "deep_consult_workflow" in registered_workflows
    # The four deep-consult activities registered by function name.
    for act in ("plan_activity", "acquire_activity", "analyze_activity",
                "synthesize_activity"):
        assert act in registered_activities, act
