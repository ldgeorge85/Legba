# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-203 tests for the maintenance-migrated deterministic sub-handlers.

Each migrated sub-handler runs over real substrate when ``deps`` carries a
pg_pool, but for unit tests we exercise the no-deps path (``deps=None``) which
synthesizes from inputs or short-circuits to zero-counts. The shape contract
(``AnalystMethodResult`` with a ``FindingPayload`` whose ``data.sub_handler``
matches) is enforced for all of them.

The original L-203 set included six sub-handlers (lifecycle_decay,
state_propagation, corroboration_scoring, cooccurrence_edges,
situation_detection, integrity_verification) whose only backing tables were
dropped by migration 0030. Those handlers swallowed the missing-relation error
into a zeroed "success" finding (the N-1 fake-success class), so item 2.4
DELETED them; their tests are removed here accordingly.

Test groups:

  * adversarial_signals — synthetic echo cluster
  * entity_gc — empty-deps shape
  * fact_decay — empty-deps shape
  * nexus_decay — empty-deps shape
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.analysts.deterministic_handlers import (
    adversarial_signals,
    entity_gc,
    fact_decay,
    nexus_decay,
)
from legba.data.analysts.deterministic import SUB_HANDLERS, OUTPUT_KIND_BY_SUB_HANDLER
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Registry — confirm the surviving L-203 sub-handlers are wired in
# deterministic.py, and the 0030-orphaned ones are NOT dispatchable.
# ---------------------------------------------------------------------------


L203_SUB_HANDLERS = (
    "adversarial_signals",
    "entity_gc",
    "fact_decay",
    "nexus_decay",
)

# Sub-handlers item 2.4 deleted (their only backing tables were dropped by
# migration 0030). These must NOT be dispatchable — a registered entry would
# resurrect the fake-success-on-missing-relation behaviour.
DELETED_SUB_HANDLERS = (
    "lifecycle_decay",
    "state_propagation",
    "corroboration_scoring",
    "cooccurrence_edges",
    "situation_detection",
    "integrity_verification",
)


@pytest.mark.parametrize("name", L203_SUB_HANDLERS)
def test_l203_sub_handler_in_dispatch_table(name):
    """Every surviving L-203 sub-handler must be registered for dispatch."""
    assert name in SUB_HANDLERS, f"missing {name!r} in SUB_HANDLERS"
    assert name in OUTPUT_KIND_BY_SUB_HANDLER, f"missing {name!r} in OUTPUT_KIND_BY_SUB_HANDLER"


@pytest.mark.parametrize("name", DELETED_SUB_HANDLERS)
def test_l203_deleted_sub_handler_not_dispatchable(name):
    """The 0030-orphaned sub-handlers must be gone from the dispatch tables."""
    assert name not in SUB_HANDLERS, f"{name!r} still dispatchable in SUB_HANDLERS"
    assert name not in OUTPUT_KIND_BY_SUB_HANDLER, (
        f"{name!r} still present in OUTPUT_KIND_BY_SUB_HANDLER"
    )


# ---------------------------------------------------------------------------
# Shape contract — every L-203 sub-handler returns the same envelope shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handler,name", [
    (adversarial_signals, "adversarial_signals"),
    (entity_gc, "entity_gc"),
    (fact_decay, "fact_decay"),
    (nexus_decay, "nexus_decay"),
])
async def test_l203_handler_shape_contract(handler, name):
    result = await handler.handle(
        [], {"sub_handler": name, "analyst_id": "test", "run_id": uuid4()}, None,
    )
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    assert result.finding.data["sub_handler"] == name
    # Deterministic kind never spends tokens.
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


# ---------------------------------------------------------------------------
# adversarial_signals — synthetic echo detector
# ---------------------------------------------------------------------------


async def test_adversarial_echo_detects_cluster():
    """Three different-source signals with near-identical titles → echo flag."""
    rows = [
        {
            "signal_id": "s1",
            "title": "Massive coordinated cyber attack hits banking sector",
            "source_id": "src_a",
            "source_name": "src_a_name",
            "ownership_type": "independent",
            "geo_origin": "US",
        },
        {
            "signal_id": "s2",
            "title": "Massive coordinated cyber attack hits banking sector worldwide",
            "source_id": "src_b",
            "source_name": "src_b_name",
            "ownership_type": "independent",
            "geo_origin": "GB",
        },
        {
            "signal_id": "s3",
            "title": "Coordinated cyber attack hits banking sector massively",
            "source_id": "src_c",
            "source_name": "src_c_name",
            "ownership_type": "independent",
            "geo_origin": "DE",
        },
    ]
    result = await adversarial_signals.handle(
        rows, {"sub_handler": "adversarial_signals"}, None,
    )
    flags = result.finding.data["semantic_echo_flags"]
    assert flags, "expected at least one echo flag"
    flag = flags[0]
    assert set(flag["signal_ids"]) >= {"s1", "s2", "s3"}
    assert flag["source_count"] >= 3


async def test_adversarial_echo_shared_provenance_skipped():
    """Same ownership_type + geo_origin should NOT raise an echo flag."""
    rows = [
        {
            "signal_id": "s1",
            "title": "Massive coordinated cyber attack hits banking sector",
            "source_id": "src_a",
            "ownership_type": "state_media",
            "geo_origin": "RU",
        },
        {
            "signal_id": "s2",
            "title": "Massive coordinated cyber attack hits banking sector worldwide",
            "source_id": "src_b",
            "ownership_type": "state_media",
            "geo_origin": "RU",
        },
        {
            "signal_id": "s3",
            "title": "Coordinated cyber attack hits banking sector massively",
            "source_id": "src_c",
            "ownership_type": "state_media",
            "geo_origin": "RU",
        },
    ]
    result = await adversarial_signals.handle(
        rows, {"sub_handler": "adversarial_signals"}, None,
    )
    assert result.finding.data["semantic_echo_flags"] == []


async def test_adversarial_empty_inputs_no_flags():
    result = await adversarial_signals.handle(
        [], {"sub_handler": "adversarial_signals"}, None,
    )
    data = result.finding.data
    assert data["velocity_flags"] == []
    assert data["semantic_echo_flags"] == []
    assert data["provenance_flags"] == []
    assert data["signals_flagged"] == 0


# ---------------------------------------------------------------------------
# entity_gc — empty-deps path: all zero counters
# ---------------------------------------------------------------------------


async def test_entity_gc_no_deps_zero_actions():
    result = await entity_gc.handle(
        [], {"sub_handler": "entity_gc"}, None,
    )
    data = result.finding.data
    assert data["dormant_entities"] == 0
    assert data["duplicate_flags"] == 0
    assert data["orphan_edges"] == 0
    assert data["sources_paused"] == 0


# ---------------------------------------------------------------------------
# fact_decay — empty-deps path
# ---------------------------------------------------------------------------


async def test_fact_decay_no_deps_zero_actions():
    result = await fact_decay.handle(
        [], {"sub_handler": "fact_decay"}, None,
    )
    data = result.finding.data
    assert data["expired_count"] == 0
    assert data["decayed_count"] == 0


# ---------------------------------------------------------------------------
# nexus_decay — empty-deps path
# ---------------------------------------------------------------------------


async def test_nexus_decay_no_deps_zero_decayed():
    result = await nexus_decay.handle(
        [], {"sub_handler": "nexus_decay"}, None,
    )
    data = result.finding.data
    assert data["decayed_count"] == 0
