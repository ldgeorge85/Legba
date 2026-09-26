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
    ``gauge``   : the FRAME GAUGE's rows (TITLE-FRAME-FIX, 2026-09-01) — the
                  title/body rows its own longer-window query returns. Routed
                  SEPARATELY from the roots query rather than reusing it,
                  because they are different queries over the same table with
                  different windows, caps and columns, and a fake that conflated
                  them would let a gauge reading the WRONG rows pass green.
    """

    def __init__(
        self,
        roots: list[dict[str, Any]],
        nodes: dict[UUID, dict[str, Any]],
        catalog: set[UUID],
        *,
        roots_raise: Exception | None = None,
        gauge: list[dict[str, Any]] | None = None,
        gauge_raise: Exception | None = None,
    ):
        self._roots = roots
        self._nodes = nodes
        self._catalog = set(catalog)
        self._roots_raise = roots_raise
        self._gauge = list(gauge or [])
        self._gauge_raise = gauge_raise
        self.gauge_args: tuple[Any, ...] | None = None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "unnest($1::uuid[])" in sql:
            ids = args[0]
            return [{"ref": i} for i in ids if i in self._catalog]
        if "ao.title" in sql:
            # the frame-gauge query
            if self._gauge_raise is not None:
                raise self._gauge_raise
            self.gauge_args = args
            return list(self._gauge)
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


# ---------------------------------------------------------------------------
# THE FRAME GAUGE (TITLE-FRAME-FIX, 2026-09-01)
# ---------------------------------------------------------------------------


def _gauge_row(analyst: str, title: str, body: str = "") -> dict[str, Any]:
    return {"analyst_id": analyst, "title": title, "body": body,
            "produced_at": "2026-09-01T12:00:00+00:00"}


@pytest.mark.asyncio
async def test_frame_gauge_lands_in_the_finding_data_and_body():
    """The gauge's numbers reach the sweep's EXISTING output surface.

    Review §5.2 puts the gauge here rather than in a new analyst — "Extend,
    don't build" — so the acceptance test is that the extension actually shows
    up where a reader of this finding will meet it: in ``data`` for machines and
    in the body lines for humans.
    """
    conn = _LineageConn(
        roots=[], nodes={}, catalog=set(),
        gauge=[
            _gauge_row("world_assessor", "Myanmar air strikes drive global escalation risk"),
            _gauge_row("world_assessor", "Sudan offensive eclipses other global escalation risks"),
        ],
    )
    result = await composition_lineage_sweep.handle([], {}, _Deps(_Pool(conn)))
    gauge = result.finding.data["frame_gauge"]
    assert set(gauge) == {"world_assessor"}
    world = gauge["world_assessor"]
    assert world["n"] == 2
    assert world["frame_rate"] == 1.0
    assert world["roll_call_rate"] == 0.0
    assert result.finding.data["frame_gauge_window_days"] == 14
    assert "frame_gauge (trailing 14d):" in result.finding.body
    assert "world_assessor n=2 frame=100.0%" in result.finding.body


@pytest.mark.asyncio
async def test_frame_gauge_queries_all_four_composition_tiers():
    """The gauge reads the region and thematic tiers the BFS does not walk.

    §2.b's cascade is world <- region <- country: a gauge blind to the middle
    floors could not tell a fixed headline from one whose input arrived already
    crowned, so the wider analyst list is load-bearing and pinned here.

    D-6 (2026-09-04) adds a FIFTH: the Assessment channel. Under the assembly a
    composition's title becomes DETERMINISTIC — masthead plus the lead span's
    own quoted fragment, or a shape line, or the masthead and two counts — so
    its frame rate stops measuring a model and starts measuring a format
    string. The Assessment's title is the model's, under the 2026-09-01
    contract unchanged, which makes it the only tier this gauge can still say
    anything about after the cutover.

    P3 LANE A adds a SIXTH, and it is the second half of that same sentence:
    after the demotion the gauge can only say something about a tier that still
    AUTHORS a title, and there are now exactly two of those — the world voice
    and the per-country one.
    """
    conn = _LineageConn(roots=[], nodes={}, catalog=set(), gauge=[])
    await composition_lineage_sweep.handle([], {}, _Deps(_Pool(conn)))
    assert conn.gauge_args is not None
    analysts, days, cap = conn.gauge_args
    assert set(analysts) == {
        "world_assessor",
        "region_composition",
        "country_composition",
        "escalation_composition",
        "world_assessment",
        "country_assessment",
    }
    assert days == 14
    assert cap == 1200


@pytest.mark.asyncio
async def test_frame_gauge_never_changes_the_lineage_verdict():
    """A locked headline is NOT a lineage break, and must not be reported as one.

    The gauge is explicitly non-gating (no new tag, no new alert kind). This is
    the test that keeps it that way: a 100%-frame, 100%-roll-call gauge over a
    provenance-clean tower still emits ``composition_lineage_clean``.
    """
    world, country, signal = uuid4(), uuid4(), uuid4()
    nodes = {
        world: {"analyst_id": "world_assessor", "derived_from": [country]},
        country: {"analyst_id": "country_composition", "derived_from": [signal],
                  "target_id": "country_g20_br"},
    }
    conn = _LineageConn(
        roots=[_root_row(world, "world_assessor")],
        nodes=nodes,
        catalog={signal},
        gauge=[_gauge_row("world_assessor", "World situational assessment - 2026-06-16")] * 3,
    )
    result = await composition_lineage_sweep.handle([], {}, _Deps(_Pool(conn)))
    assert result.finding.data["frame_gauge"]["world_assessor"]["roll_call_rate"] == 1.0
    assert "composition_lineage_clean" in result.finding.tags
    assert "composition_lineage_issues" not in result.finding.tags
    assert result.finding.data["ok"] == 1


@pytest.mark.asyncio
async def test_frame_gauge_missing_relation_propagates_refuse_loud():
    """The gauge inherits the module's refuse-loud contract.

    A gauge that reported ``frame_rate 0.0%`` after failing to read a single
    title would be worse than no gauge — it would report the defect CURED.
    """
    conn = _LineageConn(
        roots=[], nodes={}, catalog=set(),
        gauge_raise=RuntimeError('relation "analyst_outputs" does not exist'),
    )
    with pytest.raises(RuntimeError):
        await composition_lineage_sweep.handle([], {}, _Deps(_Pool(conn)))


@pytest.mark.asyncio
async def test_frame_gauge_is_empty_not_zero_when_no_titles():
    """No rows → an ABSENT gauge, never a confident zero."""
    conn = _LineageConn(roots=[], nodes={}, catalog=set(), gauge=[])
    result = await composition_lineage_sweep.handle([], {}, _Deps(_Pool(conn)))
    assert result.finding.data["frame_gauge"] == {}
    assert "frame_gauge" not in result.finding.body
