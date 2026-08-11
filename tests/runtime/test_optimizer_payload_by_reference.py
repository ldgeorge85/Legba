# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GEPA-workflow payload pass-by-reference tests.

The optimizer's GEPA loop runs as a Dapr Workflow. The runtime used to inline
the FULL training set (up to ~500 joined trace+critique rows of ~8 KiB each)
into ``OptimizerWorkflowInput`` and serialize that across the Dapr Workflow
internal gRPC channel — overflowing the default 4 MB cap
(``RESOURCE_EXHAUSTED: message larger than max 4234332 vs 4194304``), so the
orchestrator never resumed and the wedged workflow leaked orphan reminders.

The fix passes a small :class:`TrainingSetRef` instead and re-materializes the
identical rows inside the workflow worker (mirroring deep_consult's
``resolve_*_stage_deps``). These tests prove:

  (a) the by-reference workflow input is SMALL (well under 4 MB),
  (b) the worker-side re-materialization reproduces the SAME training set
      (the fetch + shape mocked),
  (c) the empty-trainset path still works,
  (d) the build site routes inline-vs-ref by backend, and
  (e) the nested ``TrainingSetRef`` survives the asdict→JSON→**dict round-trip
      the engine performs.

No daprd sidecar / live substrate required — the substrate fetch is mocked.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from legba.data.analysts import optimizer as opt
from legba.runtime.dapr_workflow import gepa
from legba.runtime.dapr_workflow.gepa import (
    OptimizerWorkflowInput,
    OptimizerWorkflowResult,
    TrainingSetRef,
    materialize_training_set,
)


# ---------------------------------------------------------------------------
# Helpers — a realistic "big" fetched row set (the shape READ_SLICE returns)
# ---------------------------------------------------------------------------


def _fetched_rows(n: int) -> list[dict]:
    """Rows as ``read_traces_and_critiques`` returns them (pre-shaping)."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return [
        {
            "run_id": uuid4(),
            "analyzed_analyst_id": "country_g20_ir",
            "analyzed_analyst_version": "vdeadbeef",
            "input": "x" * 8000,
            "gold": "y" * 8000,
            "trace_status": "completed",
            "output_row_refs": [uuid4()],
            "critique_score": 0.5,
            "critique_id": uuid4(),
            # DESC by run_started_at — newest first, like the real query.
            "run_started_at": datetime(2026, 6, 1 + (n - i) % 27, tzinfo=timezone.utc),
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# (a) the by-reference workflow input is small
# ---------------------------------------------------------------------------


def test_by_reference_input_is_small_inline_input_overflows() -> None:
    """The by-ref input must serialize well under 4 MB; the OLD inline shape
    (what caused RESOURCE_EXHAUSTED) must exceed it — proving the fix removes
    the bulk rows from the transported payload."""
    ref_input = OptimizerWorkflowInput(
        analyst_id="country_g20_ir",
        analyst_version="vdeadbeef",
        parent_prompt_module_path="legba.prompts.country_g20_ir.v1",
        training_set=[],
        training_set_ref=TrainingSetRef(
            analyzed_analyst_id="country_g20_ir",
            analyzed_analyst_version="vdeadbeef",
            until_ts="2026-06-29T00:00:00+00:00",
        ),
    )
    ref_bytes = len(json.dumps(asdict(ref_input)))
    assert ref_bytes < 4096, f"by-ref input unexpectedly large: {ref_bytes}"

    shaped = opt._shape_training_set(_fetched_rows(500))
    inline_input = OptimizerWorkflowInput(
        analyst_id="country_g20_ir",
        analyst_version="vdeadbeef",
        parent_prompt_module_path="legba.prompts.country_g20_ir.v1",
        training_set=shaped,
    )
    inline_bytes = len(json.dumps(asdict(inline_input)))
    assert inline_bytes > 4 * 1024 * 1024, (
        "expected the inline 500-row shape to exceed the 4 MB gRPC cap "
        f"(the bug); got {inline_bytes}"
    )


# ---------------------------------------------------------------------------
# (e) the nested TrainingSetRef survives the engine's serialization round-trip
# ---------------------------------------------------------------------------


def test_training_set_ref_survives_asdict_json_roundtrip() -> None:
    """``client.asdict`` flattens the nested ref to a dict; the engine JSON-
    round-trips it; ``workflow.OptimizerWorkflowInput(**wf_input)`` rehydrates
    it. __post_init__ must coerce the dict back to a TrainingSetRef so the
    worker reads ``ref.analyzed_analyst_id`` (attr, not key)."""
    wfi = OptimizerWorkflowInput(
        analyst_id="a",
        analyst_version="v",
        parent_prompt_module_path="p",
        training_set_ref=TrainingSetRef(
            analyzed_analyst_id="a",
            analyzed_analyst_version="v",
            read_window_days=14,
            limit=250,
            until_ts="2026-06-29T00:00:00+00:00",
        ),
    )
    wire = json.loads(json.dumps(asdict(wfi)))  # client → engine → worker
    rehydrated = OptimizerWorkflowInput(**wire)
    assert isinstance(rehydrated.training_set_ref, TrainingSetRef)
    assert rehydrated.training_set_ref.analyzed_analyst_id == "a"
    assert rehydrated.training_set_ref.read_window_days == 14
    assert rehydrated.training_set_ref.limit == 250
    assert rehydrated.training_set_ref.until_ts == "2026-06-29T00:00:00+00:00"


def test_inline_input_roundtrip_keeps_ref_none() -> None:
    """The in-process / test path inlines rows and carries NO ref — that must
    survive the round-trip too (ref stays None, rows stay inlined)."""
    wfi = OptimizerWorkflowInput(
        analyst_id="a",
        analyst_version="v",
        parent_prompt_module_path="p",
        training_set=[{"input": "i", "gold": "g", "critique_score": 0.5}],
    )
    rehydrated = OptimizerWorkflowInput(**json.loads(json.dumps(asdict(wfi))))
    assert rehydrated.training_set_ref is None
    assert len(rehydrated.training_set) == 1


# ---------------------------------------------------------------------------
# (b) the worker-side re-materialization reproduces the SAME training set
# ---------------------------------------------------------------------------


class _FakePool:
    """Minimal async-context pool whose acquire() yields a sentinel conn."""

    def acquire(self):  # noqa: ANN201 - test double
        pool = self

        class _Ctx:
            async def __aenter__(self):  # noqa: ANN001
                return object()

            async def __aexit__(self, *exc):  # noqa: ANN001
                return False

        return _Ctx()


class _FakeStore:
    def __init__(self) -> None:
        self.pool = _FakePool()
        self.closed = False

    async def connect(self) -> None:  # noqa: D401 - test double
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_materialize_reproduces_same_training_set(monkeypatch) -> None:
    """The worker-side ``materialize_training_set`` must produce EXACTLY the
    rows ``_shape_training_set(read_traces_and_critiques(...))`` produces — the
    invariant that keeps this a transport refactor, not a behaviour change."""
    fetched = _fetched_rows(60)
    expected = opt._shape_training_set(fetched)

    captured = {}

    async def _fake_read(conn, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return fetched

    store = _FakeStore()
    # Patch on the optimizer module (where materialize imports them from).
    monkeypatch.setattr(opt, "read_traces_and_critiques", _fake_read)
    # PostgresStore.from_env is imported INSIDE materialize from data.postgres.
    import legba.data.postgres as pg_mod

    monkeypatch.setattr(pg_mod.PostgresStore, "from_env", classmethod(lambda cls: store))

    wf_in = OptimizerWorkflowInput(
        analyst_id="country_g20_ir",
        analyst_version="vdeadbeef",
        parent_prompt_module_path="legba.prompts.country_g20_ir.v1",
        training_set=[],
        training_set_ref=TrainingSetRef(
            analyzed_analyst_id="country_g20_ir",
            analyzed_analyst_version="vdeadbeef",
            read_window_days=30,
            limit=500,
            until_ts="2026-06-29T00:00:00+00:00",
        ),
    )
    out = await materialize_training_set(wf_in)

    # Same rows, same order, same per-field shaping.
    assert out.training_set == expected
    # The ref params were threaded through to the re-fetch verbatim.
    assert captured["analyzed_analyst_id"] == "country_g20_ir"
    assert captured["analyzed_analyst_version"] == "vdeadbeef"
    assert captured["read_window_days"] == 30
    assert captured["limit"] == 500
    # until_ts pins the recent end of the window for re-fetch determinism.
    assert captured["until_ts"] == datetime(2026, 6, 29, tzinfo=timezone.utc)
    # Ref cleared so a re-entry can't double-fetch.
    assert out.training_set_ref is None
    assert store.closed is True


@pytest.mark.asyncio
async def test_materialize_noop_when_rows_already_inlined() -> None:
    """If training_set is already populated (inline / in-process path),
    materialize is a no-op — it must NOT re-fetch and clobber the rows."""
    inline = [{"input": "i", "gold": "g", "critique_score": 0.5}]
    wf_in = OptimizerWorkflowInput(
        analyst_id="a", analyst_version="v", parent_prompt_module_path="p",
        training_set=list(inline), training_set_ref=None,
    )
    out = await materialize_training_set(wf_in)
    assert out.training_set == inline


@pytest.mark.asyncio
async def test_materialize_degrades_to_empty_on_fetch_failure(monkeypatch) -> None:
    """A re-fetch failure must NOT crash the activity (that would recreate the
    silent-death class) — it degrades to an empty training set, which the loop
    turns into a visible noop candidate."""

    def _boom(cls):  # noqa: ANN001
        raise RuntimeError("no substrate in this env")

    import legba.data.postgres as pg_mod

    monkeypatch.setattr(pg_mod.PostgresStore, "from_env", classmethod(_boom))

    wf_in = OptimizerWorkflowInput(
        analyst_id="a", analyst_version="v", parent_prompt_module_path="p",
        training_set=[],
        training_set_ref=TrainingSetRef(analyzed_analyst_id="a"),
    )
    out = await materialize_training_set(wf_in)
    assert out.training_set == []
    assert out.training_set_ref is None


# ---------------------------------------------------------------------------
# (c) the empty-trainset path still works (back-compat invariant)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_training_set_still_noops(monkeypatch) -> None:
    """gepa.py L283 ``if not workflow_input.training_set`` — the noop_empty
    path must survive the by-reference change (an empty re-fetch lands here)."""
    # No ref, no rows → straight to the empty path.
    wf_in = OptimizerWorkflowInput(
        analyst_id="a", analyst_version="v",
        parent_prompt_module_path="legba.prompts.inline_target.v1",
        training_set=[], training_set_ref=None,
        min_traces_required=0,
    )
    result = await gepa.run_optimizer_in_process(wf_in)
    assert isinstance(result, OptimizerWorkflowResult)
    assert result.training_set_size == 0
    assert result.diagnostics.get("method") == "noop_empty_training"


# ---------------------------------------------------------------------------
# (d) the build site routes inline-vs-ref by backend
# ---------------------------------------------------------------------------


def test_build_ref_pins_until_ts_to_newest_fetched_row() -> None:
    """_build_training_set_ref pins until_ts to the NEWEST run_started_at in
    the fetched rows (the window's recent edge), so the worker re-fetch returns
    the identical set even if newer traces land in between."""
    rows = _fetched_rows(40)
    newest = max(r["run_started_at"] for r in rows)
    ref = opt._build_training_set_ref(
        rows, analyzed_analyst_id="country_g20_ir", analyzed_analyst_version="v",
    )
    assert ref.analyzed_analyst_id == "country_g20_ir"
    assert ref.read_window_days == opt.DEFAULT_READ_WINDOW_DAYS
    assert ref.limit == opt.MAX_TRAINING_ROWS
    assert datetime.fromisoformat(ref.until_ts) == newest


@pytest.mark.asyncio
async def test_run_method_dapr_backend_passes_ref_not_rows(monkeypatch) -> None:
    """With a NON-in-process (Dapr) client, run_method must dispatch a workflow
    input carrying the REF and an EMPTY inline training_set — the core of the
    fix (no bulk rows on the gRPC channel)."""
    captured_input = {}

    class _FakeDaprClient:  # NOT InProcessWorkflowClient → ref path
        async def start_optimizer_workflow(self, workflow_input, *, workflow_id):
            captured_input["wf"] = workflow_input

            class _H:
                id = workflow_id
                result_run_id = "dapr_wf::" + workflow_id

                async def result(self_inner):  # noqa: ANN001
                    return OptimizerWorkflowResult(
                        candidate_prompt_module_text="cand",
                        training_set_size=3,
                        eval_score=0.6,
                        eval_score_delta=0.1,
                        gepa_generation=1,
                        diagnostics={"method": "dspy_gepa", "usage": {}},
                    )

            return _H()

    deps = opt.OptimizerDeps(temporal_client=_FakeDaprClient())
    inputs = _fetched_rows(3)
    options = {
        "analyst_id": "optimizer",
        "analyzed_analyst_id": "country_g20_ir",
        "analyzed_analyst_version": "vdeadbeef",
        "run_id": str(uuid4()),
        # The analyzed analyst's KIND — what the parent prompt-module
        # convention derives from (the actor plumbs it from that analyst's
        # head descriptor). Without a declared path or a kind the optimizer
        # refuses to invent one and no-ops.
        "analyzed_analyst_kind": "inline_target",
    }
    await opt.run_method(inputs, options, deps)

    wf = captured_input["wf"]
    assert isinstance(wf, OptimizerWorkflowInput)
    # The bulk rows are NOT inlined on the Dapr path.
    assert wf.training_set == []
    # A small ref carries the re-fetch params instead.
    assert isinstance(wf.training_set_ref, TrainingSetRef)
    assert wf.training_set_ref.analyzed_analyst_id == "country_g20_ir"
    # And the serialized input is tiny (well under the 4 MB cap).
    assert len(json.dumps(asdict(wf))) < 4096


@pytest.mark.asyncio
async def test_run_method_in_process_backend_inlines_rows() -> None:
    """The in-process backend (tests / no sidecar) has no gRPC hop, so
    run_method KEEPS inlining rows there (ref stays None) — preserving the
    byte-identical legacy behaviour on that path.

    The backend is detected by ``isinstance(..., InProcessWorkflowClient)``, so
    we subclass it (still in-process) and return a controlled result — the GEPA
    loop itself is covered elsewhere; here we only assert the build-site routing
    (rows inlined, ref None) on the in-process branch."""
    captured_input = {}

    class _CapturingInProcess(gepa.InProcessWorkflowClient):
        async def start_optimizer_workflow(self, workflow_input, *, workflow_id):
            captured_input["wf"] = workflow_input
            return gepa.StubWorkflowHandle(
                id=workflow_id,
                result_run_id=f"in_process::{workflow_id}",
                _result=OptimizerWorkflowResult(
                    candidate_prompt_module_text="candidate prompt text",
                    training_set_size=2,
                    eval_score=0.6,
                    eval_score_delta=0.1,
                    gepa_generation=1,
                    parent_prompt_module_text="parent prompt text",
                    diagnostics={"method": "dspy_gepa", "usage": {}},
                ),
            )

    deps = opt.OptimizerDeps(temporal_client=_CapturingInProcess())
    inputs = _fetched_rows(2)
    options = {
        "analyst_id": "optimizer",
        "analyzed_analyst_id": "inline_target.test",
        "analyzed_analyst_version": "vdeadbeef0123456",
        "run_id": str(uuid4()),
        "analyzed_analyst_kind": "inline_target",
    }
    await opt.run_method(inputs, options, deps)

    wf = captured_input["wf"]
    assert wf.training_set_ref is None
    assert len(wf.training_set) == 2
