# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S2-T3 — the WORLD compose over REGIONS (READ_SLICE + degrade + prose gaps).

The target-less world_assessor now composes the FIVE region_composition HEADS
(5-6 inputs) instead of the ~24 country_composition heads — structurally removing
the ``MAX_WORLD_INPUT_FINDINGS`` cap pressure. DEGRADE-NOT-DROP + absence-honest:

  * a region WITH a region_composition head this window feeds that head
    (mode ``region``);
  * a region with NO region head DEGRADES to that region's member
    country_composition heads (mode ``country_fallback``) — never silently dropped;
  * a region with NEITHER is a GAP (mode ``gap``, 0 inputs) — NAMED as an
    unassessed region in the world prose (appended REGION COVERAGE block), never
    silently missing.

The per-region MODE that ran is stamped into ``data.region_coverage``. The country
+ region READ_SLICE paths are UNCHANGED (this is the world path only).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import meta_findings_synthesizer as synth


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _WorldConn:
    """Fake asyncpg.Connection routing the S2-T3 world-assembly queries by SQL:

      * the region ROSTER query (``target_descriptors``, tag ``region``, no
        ``descriptor_id <>`` clause) → the region frames;
      * the region MEMBERS query (``target_descriptors`` WITH ``descriptor_id <>
        $1``) → that region's member country desks;
      * the slice query (``analyst_outputs``) → region_composition heads when the
        analyst set is ``[region_composition]``, else the member-country
        country_composition heads for the target-id SET (param index 2).
    """

    def __init__(
        self,
        *,
        roster: list[dict[str, str]] | None = None,
        members_by_region: dict[str, list[str]] | None = None,
        region_head_rows: list[dict[str, Any]] | None = None,
        country_rows_by_target: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._roster = roster or []
        self._members_by_region = members_by_region or {}
        self._region_head_rows = region_head_rows or []
        self._country_rows_by_target = country_rows_by_target or {}
        self.roster_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.member_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.slice_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        if "target_descriptors" in query:
            if "descriptor_id <> $1" in query:  # region MEMBERS query
                self.member_calls.append((query, params))
                region_id = params[0]
                return [
                    {"descriptor_id": m}
                    for m in self._members_by_region.get(region_id, [])
                ]
            # region ROSTER query
            self.roster_calls.append((query, params))
            return [dict(r) for r in self._roster]
        # analyst_outputs slice read
        self.slice_calls.append((query, params))
        analyst_ids = params[0] if params else []
        if "region_composition" in analyst_ids:
            return list(self._region_head_rows)
        if "country_composition" in analyst_ids:
            target_ids = params[2] if len(params) >= 3 and isinstance(params[2], list) else []
            out: list[dict[str, Any]] = []
            for t in target_ids:
                row = self._country_rows_by_target.get(t)
                if row is not None:
                    out.append(row)
            return out
        return []


def _world_descriptor(*, declares_verify: bool = True) -> SimpleNamespace:
    """A world_assessor-shaped descriptor: NO targets block, other_analysts =
    [region_composition], method.llm carrying (optionally) a verify block."""
    llm: dict[str, Any] = {"primary": {"raw": "llm.primary.openai_compat"}}
    if declares_verify:
        llm["verify"] = {"raw": "llm.verify.slm_8b"}
    return SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=[
                SimpleNamespace(id="region_composition", time_window="24h", data_types=[])
            ],
            targets=None,
        ),
        method=SimpleNamespace(llm=llm),
    )


def _region_head_row(*, uid: UUID, region_id: str, title: str) -> dict[str, Any]:
    """A verify-floored region_composition head row (as the world slice returns
    it — carries ``effective_confidence``)."""
    return {
        "id": uid,
        "kind": "finding",
        "title": title,
        "body": f"{title} region read body",
        "confidence": 0.7,
        "effective_confidence": 0.6,
        "faithfulness_score": 0.9,
        "severity": None,
        "data": {"evidence": [], "meta": True},
        "evidence": [],
        "target_id": region_id,
        "target_version": None,
        "analyst_id": "region_composition",
        "analyst_version": "vtest",
        "produced_at": "2026-07-01T00:00:00+00:00",
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }


def _country_head_row(*, uid: UUID, target_id: str, title: str) -> dict[str, Any]:
    row = _region_head_row(uid=uid, region_id=target_id, title=title)
    row["analyst_id"] = "country_composition"
    row["body"] = f"{title} country read body"
    return row


class _CannedLLM:
    subprovider = "world_test_double"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Any],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": list(messages), "system": system})

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50
            reasoning_tokens = 0

        resp = SimpleNamespace()
        resp.content = json.dumps(self._payload)
        resp.usage = _Usage()
        return resp


class _Deps:
    def __init__(self, llm: Any) -> None:
        self.llm = llm


_ROSTER = [
    {"descriptor_id": "region_americas", "name": "Americas"},
    {"descriptor_id": "region_europe", "name": "Europe"},
    {"descriptor_id": "region_mena", "name": "Middle East & North Africa"},
]


# ---------------------------------------------------------------------------
# _resolve_region_roster — the frame-tag roster read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_region_roster_queries_frame_tag_active_head_ordered():
    conn = _WorldConn(roster=_ROSTER)
    out = await synth._resolve_region_roster(conn)
    assert out == [
        {"region_id": "region_americas", "region_name": "Americas"},
        {"region_id": "region_europe", "region_name": "Europe"},
        {"region_id": "region_mena", "region_name": "Middle East & North Africa"},
    ]
    q, p = conn.roster_calls[0]
    assert "target_descriptors" in q
    assert "(body -> 'scope' -> 'tags') ? $1" in q
    assert "is_head = TRUE" in q and "state = 'active'" in q
    assert "ORDER BY descriptor_id" in q
    assert "descriptor_id <> $1" not in q            # NOT the members query
    assert p == (synth.REGION_FRAME_TAG,)            # keyed on the generic 'region' tag


@pytest.mark.asyncio
async def test_resolve_region_roster_falls_back_to_id_when_name_null():
    conn = _WorldConn(roster=[{"descriptor_id": "region_africa", "name": None}])
    out = await synth._resolve_region_roster(conn)
    assert out == [{"region_id": "region_africa", "region_name": "region_africa"}]


# ---------------------------------------------------------------------------
# _assemble_world_region_slice — the DEGRADE-NOT-DROP core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_regions_covered_by_region_heads_no_fallback():
    """Every roster region has a region head → mode 'region' for all; NO member
    resolution / country fallback fires."""
    heads = [
        _region_head_row(uid=uuid4(), region_id=r["descriptor_id"], title=r["name"])
        for r in _ROSTER
    ]
    conn = _WorldConn(roster=_ROSTER, region_head_rows=heads)
    rows = await synth._assemble_world_region_slice(
        conn, region_analyst_ids=["region_composition"],
        time_window_hours=24, limit=100, verify_floor=0.0,
    )
    assert len(rows) == 3
    assert all(r["_region_mode"] == synth.REGION_MODE_REGION for r in rows)
    assert {r["_region_id"] for r in rows} == {
        "region_americas", "region_europe", "region_mena"
    }
    # No degrade → the members query never fired.
    assert conn.member_calls == []
    # Coverage denormalized onto every row: all three regions, mode 'region'.
    coverage = rows[0]["_region_coverage"]
    assert {c["region_id"]: c["mode"] for c in coverage} == {
        "region_americas": "region",
        "region_europe": "region",
        "region_mena": "region",
    }
    assert all(c["input_count"] == 1 for c in coverage)


@pytest.mark.asyncio
async def test_headless_region_degrades_to_its_country_reads():
    """A region with NO region head degrades to its member country reads
    (mode 'country_fallback'); a covered region does NOT resolve members."""
    europe_head = _region_head_row(
        uid=uuid4(), region_id="region_europe", title="Europe"
    )
    sa, ir = uuid4(), uuid4()
    conn = _WorldConn(
        roster=[
            {"descriptor_id": "region_europe", "name": "Europe"},
            {"descriptor_id": "region_mena", "name": "MENA"},
        ],
        region_head_rows=[europe_head],
        members_by_region={"region_mena": ["country_watch_sa", "country_watch_ir"]},
        country_rows_by_target={
            "country_watch_sa": _country_head_row(uid=sa, target_id="country_watch_sa", title="Saudi Arabia"),
            "country_watch_ir": _country_head_row(uid=ir, target_id="country_watch_ir", title="Iran"),
        },
    )
    rows = await synth._assemble_world_region_slice(
        conn, region_analyst_ids=["region_composition"],
        time_window_hours=24, limit=100, verify_floor=0.0,
    )
    modes = {r["_region_mode"] for r in rows}
    assert modes == {synth.REGION_MODE_REGION, synth.REGION_MODE_COUNTRY_FALLBACK}
    # europe = one region head; mena = two country fallbacks.
    europe = [r for r in rows if r["_region_id"] == "region_europe"]
    mena = [r for r in rows if r["_region_id"] == "region_mena"]
    assert len(europe) == 1 and europe[0]["_region_mode"] == "region"
    assert len(mena) == 2 and all(r["_region_mode"] == "country_fallback" for r in mena)
    assert {r["id"] for r in mena} == {sa, ir}
    # Members resolved ONLY for the headless region.
    assert [p[0] for _, p in conn.member_calls] == ["region_mena"]
    # Coverage stamps the MODE per region.
    coverage = {c["region_id"]: c for c in rows[0]["_region_coverage"]}
    assert coverage["region_europe"]["mode"] == "region"
    assert coverage["region_mena"]["mode"] == "country_fallback"
    assert coverage["region_mena"]["input_count"] == 2


@pytest.mark.asyncio
async def test_region_with_no_region_and_no_country_is_a_named_gap():
    """A region with NO region head AND NO member country reads → mode 'gap',
    0 inputs — NAMED in coverage, never silently missing."""
    europe_head = _region_head_row(
        uid=uuid4(), region_id="region_europe", title="Europe"
    )
    conn = _WorldConn(
        roster=[
            {"descriptor_id": "region_europe", "name": "Europe"},
            {"descriptor_id": "region_mena", "name": "MENA"},
        ],
        region_head_rows=[europe_head],
        members_by_region={"region_mena": ["country_watch_sa"]},
        country_rows_by_target={},   # the member desk produced no verified read
    )
    rows = await synth._assemble_world_region_slice(
        conn, region_analyst_ids=["region_composition"],
        time_window_hours=24, limit=100, verify_floor=0.0,
    )
    # Only the europe head is an input row; the gap contributes NO citable row.
    assert [r["_region_id"] for r in rows] == ["region_europe"]
    coverage = {c["region_id"]: c for c in rows[0]["_region_coverage"]}
    assert coverage["region_mena"]["mode"] == "gap"
    assert coverage["region_mena"]["input_count"] == 0
    assert coverage["region_mena"]["region_name"] == "MENA"


@pytest.mark.asyncio
async def test_empty_roster_falls_back_to_plain_region_read_no_coverage():
    """A pre-S2-T1 topology (no region frames) → feed whatever region heads exist,
    NO gap/degrade frame, NO coverage stamp, NO members query."""
    heads = [
        _region_head_row(uid=uuid4(), region_id="region_europe", title="Europe"),
        _region_head_row(uid=uuid4(), region_id="region_mena", title="MENA"),
    ]
    conn = _WorldConn(roster=[], region_head_rows=heads)
    rows = await synth._assemble_world_region_slice(
        conn, region_analyst_ids=["region_composition"],
        time_window_hours=24, limit=100, verify_floor=0.0,
    )
    assert len(rows) == 2
    assert conn.member_calls == []
    assert all("_region_coverage" not in r for r in rows)
    # Region heads are still mode-stamped.
    assert all(r["_region_mode"] == "region" for r in rows)


@pytest.mark.asyncio
async def test_all_regions_gap_returns_empty_slice():
    """Every roster region is a gap (no heads, no country reads) → [] rows; the
    actor then NOOPs the world run (the standard empty-slice contract)."""
    conn = _WorldConn(
        roster=[{"descriptor_id": "region_mena", "name": "MENA"}],
        region_head_rows=[],
        members_by_region={"region_mena": []},   # no member desks
    )
    rows = await synth._assemble_world_region_slice(
        conn, region_analyst_ids=["region_composition"],
        time_window_hours=24, limit=100, verify_floor=0.0,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# READ_SLICE world branch dispatch — target None + declares verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_world_read_slice_dispatches_to_region_assembly():
    """READ_SLICE(target None) on a verify-declaring descriptor reads the region
    roster THEN the region_composition heads (the world-over-regions assembly)."""
    heads = [
        _region_head_row(uid=uuid4(), region_id=r["descriptor_id"], title=r["name"])
        for r in _ROSTER
    ]
    conn = _WorldConn(roster=_ROSTER, region_head_rows=heads)
    rows = await synth.READ_SLICE(
        conn, descriptor=_world_descriptor(declares_verify=True), target_filter=None
    )
    assert conn.roster_calls, "world branch must read the region roster"
    assert conn.slice_calls, "world branch must read the region heads"
    # The region-head read is over region_composition, meta-inclusive, verify-floored.
    sq, sp = conn.slice_calls[0]
    assert sp[0] == ["region_composition"]
    assert "JOIN LATERAL" in sq and "Faithfulness verify%" in sq
    assert "'meta'" not in sq
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_world_branch_only_fires_for_target_none(monkeypatch):
    """A per-COUNTRY / REGION target_filter never takes the world branch (no
    roster read) — the country + region READ_SLICE paths are unchanged."""
    desc = _world_descriptor(declares_verify=True)
    # per-country target → the per-country equality read, no roster.
    conn_c = _WorldConn(roster=_ROSTER)
    await synth.READ_SLICE(conn_c, descriptor=desc, target_filter="country_g20_in")
    assert conn_c.roster_calls == []
    cq, _ = conn_c.slice_calls[0]
    assert "f.target_id = $3" in cq
    # region frame target → the region members branch, no roster.
    conn_r = _WorldConn(roster=_ROSTER, members_by_region={"region_mena": ["country_watch_ir"]})
    await synth.READ_SLICE(conn_r, descriptor=desc, target_filter="region_mena")
    assert conn_r.roster_calls == []
    assert conn_r.member_calls, "region frame → members query"


# ---------------------------------------------------------------------------
# _render_region_coverage_block — gaps only
# ---------------------------------------------------------------------------


def test_render_region_coverage_block_lists_only_gaps():
    coverage = [
        {"region_id": "region_europe", "region_name": "Europe", "mode": "region", "input_count": 1},
        {"region_id": "region_mena", "region_name": "MENA", "mode": "country_fallback", "input_count": 3},
        {"region_id": "region_africa", "region_name": "Africa", "mode": "gap", "input_count": 0},
    ]
    block = synth._render_region_coverage_block(coverage)
    assert "REGION COVERAGE" in block
    assert "Africa (region_africa)" in block       # the gap IS named
    assert "Europe" not in block                    # covered regions are NOT listed
    assert "MENA" not in block


def test_render_region_coverage_block_blank_without_gaps():
    coverage = [
        {"region_id": "region_europe", "region_name": "Europe", "mode": "region", "input_count": 1},
    ]
    assert synth._render_region_coverage_block(coverage) == ""
    assert synth._render_region_coverage_block([]) == ""


# ---------------------------------------------------------------------------
# End-to-end _run — the world run selects the regions prompt, cites the region
# reads, stamps per-region coverage, and NAMES the gap in the prose.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_world_run_selects_regions_prompt_stamps_coverage_and_names_gaps():
    eu, me = uuid4(), uuid4()
    coverage = [
        {"region_id": "region_europe", "region_name": "Europe", "mode": "region", "input_count": 1},
        {"region_id": "region_mena", "region_name": "MENA", "mode": "country_fallback", "input_count": 1},
        {"region_id": "region_africa", "region_name": "Africa", "mode": "gap", "input_count": 0},
    ]
    eu_row = _region_head_row(uid=eu, region_id="region_europe", title="Europe read")
    me_row = _country_head_row(uid=me, target_id="country_watch_ir", title="Iran read")
    eu_row["_region_id"], eu_row["_region_mode"] = "region_europe", "region"
    me_row["_region_id"], me_row["_region_mode"] = "region_mena", "country_fallback"
    for r in (eu_row, me_row):
        r["_region_coverage"] = coverage

    body = (
        "BLUF: regions diverge. Europe is steady [[ref:1]], while MENA is "
        "escalating [[ref:2]]. Africa has no read this cycle."
    )
    llm = _CannedLLM(
        {"title": "World read", "body": body, "confidence": 0.5,
         "evidence": ["regions diverge"], "tags": ["world"]}
    )
    result = await synth.run_method(
        [eu_row, me_row],
        {"analyst_id": "world_assessor", "composition": True,
         "contention_groups": [], "run_id": uuid4()},
        _Deps(llm),
    )

    # Prompt: the WORLD-over-REGIONS prompt (not the region/per-country prompt).
    assert llm.calls[-1]["system"] == synth._WORLD_OVER_REGIONS_SYSTEM
    assert llm.calls[-1]["system"] != synth._WORLD_COMPOSITION_SYSTEM
    # The REGION COVERAGE gap block was injected into the user prompt.
    user = llm.calls[-1]["messages"][0]["content"]
    assert "REGION COVERAGE" in user
    assert "Africa (region_africa)" in user

    # Coverage + gaps stamped into data (the MODE that ran per region).
    assert result.finding.data["region_coverage"] == coverage
    assert result.finding.data["region_gaps"] == ["Africa"]

    # Citations resolve to the two cited reads (region head + country fallback).
    cited = {c["ref_id"] for c in result.finding.data["citations"]}
    assert cited == {str(eu), str(me)}
    assert set(result.derived_from) == {eu, me}


@pytest.mark.asyncio
async def test_world_run_no_gaps_stamps_coverage_without_block_or_gaps_key():
    eu = uuid4()
    coverage = [
        {"region_id": "region_europe", "region_name": "Europe", "mode": "region", "input_count": 1},
    ]
    eu_row = _region_head_row(uid=eu, region_id="region_europe", title="Europe read")
    eu_row["_region_id"], eu_row["_region_mode"] = "region_europe", "region"
    eu_row["_region_coverage"] = coverage
    llm = _CannedLLM(
        {"title": "World read", "body": "Europe steady [[ref:1]].", "confidence": 0.5,
         "evidence": [], "tags": ["world"]}
    )
    result = await synth.run_method(
        [eu_row],
        {"analyst_id": "world_assessor", "composition": True, "run_id": uuid4()},
        _Deps(llm),
    )
    # No gaps → no REGION COVERAGE block, no region_gaps key, but coverage stamped.
    assert "REGION COVERAGE" not in llm.calls[-1]["messages"][0]["content"]
    assert result.finding.data["region_coverage"] == coverage
    assert "region_gaps" not in result.finding.data
