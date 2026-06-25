# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trace-only output dispatch (the "Findings as a real output type" cleanup).

The actor's output-dispatch chokepoint
(:func:`legba.runtime.dapr_actors._resolve_effective_output_kind` + the
``trace_only`` branch in ``AnalystActor.run``) makes the META analysts
TRACE_ONLY: they write an ``analyst_traces`` receipt but NO ``analyst_outputs``
row, while their in-``run_method`` side-writes (write_nexus / write_hypothesis /
maintenance stamps) stay intact.

These tests pin the two invariants WITHOUT requiring Postgres / Dapr:

  * ``_resolve_effective_output_kind`` decides write-vs-skip correctly for the
    deterministic sub-handlers and the top-level META kinds (the single
    chokepoint the actor consults — see ``dapr_actors.py`` where it gates the
    ``write_analyst_output`` call on ``trace_only``);

  * the REAL :class:`RuntimeReceiptChain.record` (driven over a fake asyncpg
    pool) writes an ``analyst_traces`` row whose ``output_payload`` carries the
    run summary and whose ``output_row_refs`` is EMPTY for a trace-only run —
    proving the run summary survives in the trace with no analyst_outputs row.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from legba.data.provenance.kinds import TRACE_ONLY, OutputKind
from legba.data.provenance.models import FindingPayload
from legba.data.provenance.receipts import RuntimeReceiptChain
from legba.runtime.dapr_actors import (
    _payload_finding,
    _receipt_output_payload,
    _resolve_effective_output_kind,
)


# ---------------------------------------------------------------------------
# Fake asyncpg pool/connection — captures the analyst_traces INSERT so the
# real RuntimeReceiptChain.record() runs end-to-end with no live Postgres.
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, store: dict):
        self._store = store

    async def fetchrow(self, *_a, **_k):
        return None  # no prior head → ZERO_HASH

    async def fetchval(self, *_a, **_k):
        return 0  # zero existing traces

    async def execute(self, query: str, *args):
        # The ONLY execute the chain issues is the analyst_traces INSERT.
        assert "INSERT INTO analyst_traces" in query
        # Column order matches receipts.py: ... output_row_refs($13),
        # output_payload($14::jsonb) ...
        self._store["insert_args"] = args
        self._store["output_row_refs"] = args[12]
        self._store["output_payload"] = json.loads(args[13])


class _FakeAcquire:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self):
        self.store: dict = {}
        self._conn = _FakeConn(self.store)

    def acquire(self):
        return _FakeAcquire(self._conn)


# ---------------------------------------------------------------------------
# 1) The chokepoint decision: write-vs-skip.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sub_handler",
    ["nexus_decay", "fact_decay", "cross_source_dedup", "finding_supersession",
     "integrity_sweep", "entity_gc", "entity_resolution", "structural_balance",
     "cross_source_coalesce", "proposed_edge_governance"],
)
def test_deterministic_maintenance_resolves_trace_only(sub_handler):
    resolved = _resolve_effective_output_kind(
        kind="deterministic",
        bind_output_kind=OutputKind.FINDING,
        options={"sub_handler": sub_handler},
    )
    assert resolved is TRACE_ONLY


@pytest.mark.parametrize("sub_handler", ["graph_mining", "anomaly_detection",
                                         "situation_clustering"])
def test_deterministic_finding_sub_handlers_resolve_finding(sub_handler):
    resolved = _resolve_effective_output_kind(
        kind="deterministic",
        bind_output_kind=OutputKind.FINDING,
        options={"sub_handler": sub_handler},
    )
    assert resolved is OutputKind.FINDING


@pytest.mark.parametrize("kind", ["relationship_reifier", "competing_hypotheses"])
def test_top_level_meta_kinds_resolve_trace_only(kind):
    resolved = _resolve_effective_output_kind(
        kind=kind, bind_output_kind=TRACE_ONLY, options={},
    )
    assert resolved is TRACE_ONLY


# ---------------------------------------------------------------------------
# 2) The trace survives: a trace-only run records analyst_traces with the run
#    summary + EMPTY output_row_refs (no analyst_outputs row).
# ---------------------------------------------------------------------------


def _reifier_run_summary() -> FindingPayload:
    """A relationship_reifier-shaped per-run summary (candidates/typed/written)."""
    return FindingPayload(
        title="relationship_reifier run",
        body="typed 3 nexuses over 5 candidates",
        confidence=0.0,
        data={"sub_handler": "relationship_reifier", "candidates": 5,
              "typed": 3, "written": 3},
    )


@pytest.mark.asyncio
async def test_trace_only_run_records_trace_with_summary_and_no_output_refs():
    """Mirror the actor's trace-only branch: resolve TRACE_ONLY → skip the
    analyst_outputs write → STILL record the analyst_traces row, carrying the
    run summary and EMPTY output_row_refs. Proves no information is lost."""
    output_kind = _resolve_effective_output_kind(
        kind="relationship_reifier", bind_output_kind=TRACE_ONLY, options={},
    )
    trace_only = output_kind is TRACE_ONLY
    assert trace_only

    # The actor picks the FindingPayload summary for the trace via _payload_finding
    # when trace-only (it never calls _select_output_payload / write_analyst_output).

    class _Result:
        finding = _reifier_run_summary()
        intermediate_steps = None
        tool_calls = None
        prompt_module_hash = None
        prompt_rendered = None

    method_result = _Result()
    summary = _payload_finding(method_result)
    assert summary is method_result.finding

    pool = _FakePool()
    chain = RuntimeReceiptChain(pool)  # type: ignore[arg-type]
    now = datetime.now(timezone.utc)

    receipt, prev = await chain.record(
        run_id=uuid4(),
        analyst_id="relationship_reifier",
        analyst_version="v1",
        cadence_trigger="method",
        target_id=None,
        input_row_refs=[],
        input_payload=None,
        prompt_module_hash=None,
        prompt_rendered=None,
        # Trace-only: no analyst_outputs row → empty output_row_refs (this is
        # exactly what the actor passes when output_row is None).
        output_row_refs=[],
        output_payload=_receipt_output_payload(summary),
        run_started_at=now,
        run_ended_at=now,
        status="success",
    )

    assert receipt and isinstance(receipt, str)
    # The analyst_traces INSERT fired with the run summary + empty refs.
    assert pool.store["output_row_refs"] == []
    persisted = pool.store["output_payload"]
    assert persisted["data"]["written"] == 3, persisted
    assert persisted["data"]["typed"] == 3
    assert persisted["title"] == "relationship_reifier run"
