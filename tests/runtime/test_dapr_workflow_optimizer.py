# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-16 — Dapr-Workflow optimizer port tests.

Two surfaces (the loop itself is covered by
``tests/runtime/test_optimizer_gepa_loop.py``):

  1. **Contract / interface tests** (always run) — confirm the
     :class:`DaprOptimizerWorkflowClient` + :class:`DaprWorkflowHandle`
     satisfy the SAME ``start_optimizer_workflow`` /
     :class:`WorkflowHandleLike` surface the optimizer kind calls, that
     the factory env-gating resolves correctly, and that the activities
     reuse the shared GEPA loop in
     :mod:`legba.runtime.dapr_workflow.gepa` (so the algorithm is
     byte-identical to the in-process fallback).  No daprd sidecar
     required.

  2. **Live-sidecar end-to-end** (gated by ``LEGBA_TEST_DAPR_WORKFLOW=1``)
     — start a real :class:`WorkflowRuntime` against the dev-rig daprd
     sidecar gRPC, dispatch the real ``optimizer_workflow`` via the
     production client, await an :class:`OptimizerWorkflowResult`, and
     assert it round-trips into a ``PromptModuleCandidatePayload``.  No
     mocks: this exercises the actual Dapr Workflow engine.

The live path is skipped unless ``LEGBA_TEST_DAPR_WORKFLOW=1`` so the
default suite doesn't need a sidecar.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from legba.runtime.dapr_workflow import (
    DAPR_WORKFLOW_AVAILABLE,
    DaprOptimizerWorkflowClient,
    DaprWorkflowClientConfig,
    build_dapr_workflow_client,
)
from legba.runtime.dapr_workflow.client import DaprWorkflowHandle
from legba.runtime.dapr_workflow.workflow import (
    COMPILE_ACTIVITY_NAME,
    VALIDATE_ACTIVITY_NAME,
    WORKFLOW_NAME,
    compile_candidate_activity,
    validate_training_set_activity,
)
from legba.runtime.dapr_workflow.gepa import (
    OptimizerWorkflowInput,
    OptimizerWorkflowResult,
    WorkflowHandleLike,
)


def _build_input(*, n_rows: int = 8, min_traces_required: int = 1) -> OptimizerWorkflowInput:
    return OptimizerWorkflowInput(
        analyst_id="inline_target.test",
        analyst_version="v" + "0" * 16,
        parent_prompt_module_path="legba.prompts.inline_target.v1",
        training_set=[
            {
                "run_id": str(uuid4()),
                "input": f"signal context row {i} energy infrastructure brazil",
                "gold": f"finding {i} about brazil energy",
                "critique_score": 0.5 + (i % 3) * 0.05,
            }
            for i in range(n_rows)
        ],
        max_generations=3,
        min_traces_required=min_traces_required,
    )


# ---------------------------------------------------------------------------
# Contract / interface tests (always run)
# ---------------------------------------------------------------------------


def test_registered_names_are_stable() -> None:
    """Worker + client must agree on the registered workflow name."""
    assert WORKFLOW_NAME == "legba_optimizer_workflow"
    assert VALIDATE_ACTIVITY_NAME == "legba_optimizer_validate_activity"
    assert COMPILE_ACTIVITY_NAME == "legba_optimizer_compile_activity"


def test_factory_returns_none_when_in_process_forced(monkeypatch) -> None:
    """LEGBA_OPTIMIZER_IN_PROCESS=1 → factory returns None so the kind
    keeps its in-process GEPA fallback (factory is purely additive)."""
    monkeypatch.setenv("LEGBA_OPTIMIZER_IN_PROCESS", "1")
    assert build_dapr_workflow_client() is None


def test_factory_returns_client_when_available(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_OPTIMIZER_IN_PROCESS", raising=False)
    client = build_dapr_workflow_client()
    if DAPR_WORKFLOW_AVAILABLE:
        assert isinstance(client, DaprOptimizerWorkflowClient)
        assert hasattr(client, "start_optimizer_workflow")
    else:
        assert client is None


def test_client_exposes_kind_stable_surface() -> None:
    """The kind calls ``await client.start_optimizer_workflow(wf, workflow_id=...)``
    — the Dapr client must expose that exact method, like the in-process
    fallback (``InProcessWorkflowClient``)."""
    client = DaprOptimizerWorkflowClient(
        config=DaprWorkflowClientConfig(host="127.0.0.1", port="50001"),
    )
    assert hasattr(client, "start_optimizer_workflow")
    # Same param surface the in-process fallback client exposes.
    import inspect

    sig = inspect.signature(client.start_optimizer_workflow)
    assert "workflow_input" in sig.parameters
    assert "workflow_id" in sig.parameters
    assert sig.parameters["workflow_id"].kind is inspect.Parameter.KEYWORD_ONLY


def test_handle_satisfies_workflow_handle_like() -> None:
    """DaprWorkflowHandle must satisfy WorkflowHandleLike so the kind's
    _dispatch_workflow reads .id / .result_run_id unchanged."""
    handle = DaprWorkflowHandle(
        id="optimizer-x", result_run_id="dapr_wf::optimizer-x",
        _client=None, _config=DaprWorkflowClientConfig(),
    )
    assert handle.id == "optimizer-x"
    assert handle.result_run_id == "dapr_wf::optimizer-x"
    assert hasattr(handle, "result")
    # WorkflowHandleLike is a runtime_checkable Protocol.
    assert isinstance(handle, WorkflowHandleLike)


def test_activities_reuse_shared_gepa_loop() -> None:
    """The Dapr activities delegate to the SAME shared loop the
    in-process fallback uses — proven by the algorithm module being
    :mod:`legba.runtime.dapr_workflow.gepa` (single source of truth)."""
    import legba.runtime.dapr_workflow.workflow as wf

    src = wf.validate_training_set_activity.__module__
    assert src == "legba.runtime.dapr_workflow.workflow"
    # The delegated callables come from the gepa module (single source
    # of truth for the GEPA algorithm; moved there from the deleted
    # temporal package by P-CUT/C-3).
    from legba.runtime.dapr_workflow import gepa as shared

    assert wf._validate_async is shared.validate_training_set_activity
    assert wf._run_gepa_loop is shared._run_gepa_loop


def test_validate_activity_runs_synchronously() -> None:
    """The sync activity wrapper drives the async validator; reject path."""
    wf_in = _build_input(n_rows=2, min_traces_required=10)
    out = validate_training_set_activity(None, wf_in.__dict__)
    assert out["ok"] is False
    assert "insufficient_traces" in out["reason"]


@pytest.mark.skip(reason="optimizer plane mothballed 2026-08-21 (RUST-4)")
def test_compile_activity_returns_candidate_dict() -> None:
    """The sync compile activity returns the result as a JSON-able dict
    with the load-bearing OptimizerWorkflowResult fields."""
    wf_in = _build_input(n_rows=6, min_traces_required=1)
    out = compile_candidate_activity(None, wf_in.__dict__)
    for key in (
        "candidate_prompt_module_text", "training_set_size",
        "eval_score", "eval_score_delta", "gepa_generation", "diagnostics",
    ):
        assert key in out, key
    assert out["training_set_size"] == 6
    assert 0.0 <= out["eval_score"] <= 1.0


# ---------------------------------------------------------------------------
# Live-sidecar end-to-end (gated by LEGBA_TEST_DAPR_WORKFLOW=1)
# ---------------------------------------------------------------------------


_LIVE_GATE = os.environ.get("LEGBA_TEST_DAPR_WORKFLOW") == "1"

live_only = pytest.mark.skipif(
    not _LIVE_GATE,
    reason="LEGBA_TEST_DAPR_WORKFLOW=1 not set; skipping live daprd-sidecar test",
)


@live_only
@pytest.mark.integration
@pytest.mark.asyncio
async def test_optimizer_workflow_end_to_end_on_daprd() -> None:
    """Dispatch the real optimizer workflow on the live daprd sidecar and
    await a candidate — the strongest end-to-end statement for the port.
    """
    import time

    from legba.runtime.dapr_workflow.worker import build_workflow_runtime

    host = os.environ.get("DAPR_RUNTIME_HOST", "127.0.0.1")
    port = os.environ.get("DAPR_GRPC_PORT", "50001")

    runtime = build_workflow_runtime(host=host, port=port)
    runtime.start()
    try:
        time.sleep(3)  # let the worker register with the engine
        client = DaprOptimizerWorkflowClient(
            config=DaprWorkflowClientConfig(
                host=host, port=port, completion_timeout_seconds=120,
            ),
        )
        wf_in = _build_input(n_rows=8, min_traces_required=1)
        wf_id = f"optimizer-pytest-{uuid4().hex[:8]}"
        handle = await client.start_optimizer_workflow(wf_in, workflow_id=wf_id)
        assert handle.id == wf_id
        result = await handle.result()
        assert isinstance(result, OptimizerWorkflowResult)
        assert result.training_set_size == 8
        assert 0.0 <= result.eval_score <= 1.0
        assert 0 <= result.gepa_generation <= wf_in.max_generations
    finally:
        runtime.shutdown()


@live_only
@pytest.mark.integration
@pytest.mark.asyncio
async def test_optimizer_workflow_validation_short_circuit_on_daprd() -> None:
    """Workflow honors the validation reject branch on the live engine
    without running the compile activity."""
    import time

    from legba.runtime.dapr_workflow.worker import build_workflow_runtime

    host = os.environ.get("DAPR_RUNTIME_HOST", "127.0.0.1")
    port = os.environ.get("DAPR_GRPC_PORT", "50001")

    runtime = build_workflow_runtime(host=host, port=port)
    runtime.start()
    try:
        time.sleep(3)
        client = DaprOptimizerWorkflowClient(
            config=DaprWorkflowClientConfig(
                host=host, port=port, completion_timeout_seconds=60,
            ),
        )
        wf_in = _build_input(n_rows=2, min_traces_required=10)
        handle = await client.start_optimizer_workflow(
            wf_in, workflow_id=f"optimizer-pytest-sc-{uuid4().hex[:8]}",
        )
        result = await handle.result()
        assert result.gepa_generation == 0
        assert result.eval_score_delta == 0.0
        assert result.candidate_prompt_module_text.startswith("<<skipped:")
    finally:
        runtime.shutdown()
