# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P3-T6 — the ``composition_lineage_sweep`` deterministic sub-handler.

Walks ``derived_from`` BACKWARD from each recent composition root
(world_assessor / country_composition) via ``validate_lineage`` and reports
per-floor integrity. Covers the properties that distinguish it from
``integrity_sweep``: it **refuses loud** (absent pool raises), it POST-FILTERS
single-table dangling against the full lineage catalog (a signal LEAF is valid,
not a break → a healthy tower reports 0), and it NAMES a root whose sub-claim
floor is broken (a deleted unit finding).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import composition_lineage_sweep
from legba.data.provenance.kinds import OutputKind
from legba.runtime.analyst_method import AnalystMethodResult


class _LineageConn:
    """Fake conn backing the roots query, the validate_lineage BFS fetchrow, and
    the catalog-resolve fetch — routed by SQL content.

    ``nodes``   : id -> {"derived_from": [UUID...], "analyst_id", "target_id"}
                  the analyst_outputs rows the single-table BFS can resolve.
    ``catalog`` : the set of ids that resolve in ANY lineage-catalog table (the
                  cross-table LEAVES — signals/facts/…). A ref in neither is a
                  TRUE dangling break.
    """

    def __init__(
        self,
        roots: list[dict[str, Any]],
        nodes: dict[UUID, dict[str, Any]],
        catalog: set[UUID],
        *,
        roots_raise: Exception | None = None,
    ):
        self._roots = roots
        self._nodes = nodes
        self._catalog = set(catalog)
        self._roots_raise = roots_raise

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "unnest($1::uuid[])" in sql:
            ids = args[0]
            return [{"ref": i} for i in ids if i in self._catalog]
        # the roots query
        if self._roots_raise is not None:
            raise self._roots_raise
        return list(self._roots)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        rid = args[0]
        node = self._nodes.get(rid)
        if node is None:
            return None  # single-table miss (dangling until catalog-resolved)
        return {
            "id": rid,
            "target_id": node.get("target_id"),
            "analyst_id": node.get("analyst_id"),
            "derived_from": list(node.get("derived_from", [])),
        }


class _Acquire:
    def __init__(self, conn: _LineageConn):
        self._conn = conn

    async def __aenter__(self) -> _LineageConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Pool:
    def __init__(self, conn: _LineageConn):
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _Deps:
    def __init__(self, pool: Any):
        self.pg_pool = pool


def _root_row(rid: UUID, analyst_id: str) -> dict[str, Any]:
    return {"id": rid, "analyst_id": analyst_id, "produced_at": "2026-06-30T00:00:00+00:00"}


def test_registered_in_dispatch_table():
    assert "composition_lineage_sweep" in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER["composition_lineage_sweep"] == OutputKind.FINDING


@pytest.mark.asyncio
async def test_absent_pool_refuses_loud():
    with pytest.raises(RuntimeError):
        await composition_lineage_sweep.handle([], {}, _Deps(None))
    with pytest.raises(RuntimeError):
        await composition_lineage_sweep.handle([], {}, None)


@pytest.mark.asyncio
async def test_healthy_tower_reports_zero_dangling_across_floors():
    """world -> country -> unit -> signal: the signal LEAF lives outside
    analyst_outputs (catalog-resolved), so the tower is CLEAN — 0 dangling."""
    world, country, unit = uuid4(), uuid4(), uuid4()
    signal = uuid4()
    nodes = {
        world: {"analyst_id": "world_assessor", "derived_from": [country]},
        country: {"analyst_id": "country_composition", "derived_from": [unit],
                  "target_id": "country_g20_br"},
        unit: {"analyst_id": "leadership_transition", "derived_from": [signal],
               "target_id": "country_g20_br"},
    }
    conn = _LineageConn(
        roots=[_root_row(world, "world_assessor")],
        nodes=nodes,
        catalog={signal},  # the signal resolves in the catalog → valid leaf
    )
    result = await composition_lineage_sweep.handle([], {}, _Deps(_Pool(conn)))
    assert isinstance(result, AnalystMethodResult)
    data = result.finding.data
    assert data["swept"] == 1
    assert data["ok"] == 1
    assert data["with_dangling"] == 0
    assert data["with_cycles"] == 0
    assert data["depth_exhausted"] == 0
    assert "composition_lineage_clean" in result.finding.tags
    assert data["offenders"] == []


@pytest.mark.asyncio
async def test_deleted_subclaim_flags_root_in_named_dangling_sample():
    """Delete the unit sub-claim under a live country read → the country read's
    derived_from ref resolves to nothing (not in analyst_outputs, not in the
    catalog) → TRUE dangling → the root is FLAGGED + NAMED."""
    world, country, deleted_unit = uuid4(), uuid4(), uuid4()
    nodes = {
        world: {"analyst_id": "world_assessor", "derived_from": [country]},
        country: {"analyst_id": "country_composition", "derived_from": [deleted_unit],
                  "target_id": "country_g20_br"},
        # deleted_unit is ABSENT from nodes AND from the catalog → a true break.
    }
    conn = _LineageConn(
        roots=[_root_row(world, "world_assessor")],
        nodes=nodes,
        catalog=set(),
    )
    result = await composition_lineage_sweep.handle([], {}, _Deps(_Pool(conn)))
    data = result.finding.data
    assert data["swept"] == 1
    assert data["ok"] == 0
    assert data["with_dangling"] == 1
    assert "composition_lineage_issues" in result.finding.tags
    offenders = data["offenders"]
    assert len(offenders) == 1
    assert offenders[0]["root_id"] == str(world)
    assert str(deleted_unit) in offenders[0]["dangling"]


@pytest.mark.asyncio
async def test_missing_relation_propagates_refuse_loud():
    """A roots query against a missing relation RAISES rather than emitting a
    zeroed clean finding."""
    conn = _LineageConn(
        roots=[], nodes={}, catalog=set(),
        roots_raise=RuntimeError("relation \"analyst_outputs\" does not exist"),
    )
    with pytest.raises(RuntimeError):
        await composition_lineage_sweep.handle([], {}, _Deps(_Pool(conn)))


@pytest.mark.asyncio
async def test_no_roots_in_window_is_clean_zeroed():
    """No composition roots in window → an HONEST 0/0 finding (the sweep ran, it
    just had nothing to grade) — not an error."""
    conn = _LineageConn(roots=[], nodes={}, catalog=set())
    result = await composition_lineage_sweep.handle([], {}, _Deps(_Pool(conn)))
    data = result.finding.data
    assert data["swept"] == 0
    assert data["ok"] == 0
    assert "composition_lineage_clean" in result.finding.tags
