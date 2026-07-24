# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1 event-loop freeze fix — graph_mining proxy-chain bounding + off-loop run.

ROOT CAUSE (py-spy at the 2026-07-24 12:52 freeze): ``_proxy_chains`` calls
networkx ``all_simple_paths`` — worst-case EXPONENTIAL path enumeration —
SYNCHRONOUSLY on the asyncio event loop. On the dense live reified-nexus graph
(~1,980 vertices, max degree 474) it spun for ~33 min holding the GIL, starving
the reminder dispatch + healthchecks until the watchdog killed it. graph_mining's
cadence is ``52 */12`` → the twice-daily :52 freeze.

THE FIX (both belts):
  1. BOUND the work — ``_proxy_chains`` passes ``cutoff=`` AND caps total paths
     consumed via ``itertools.islice`` over the generators (worst case linear in
     the cap), stamping an honest ``truncated`` marker when the cap bites.
  2. OFF-LOOP — the three CPU-heavy phases run via ``asyncio.to_thread`` /
     ``run_in_executor`` so even bounded work can't block the plane; a wall-clock
     belt abandons a pathology with an honest-empty finding.

These tests are pure (``deps=None``) — no substrate, sub-second.
"""

from __future__ import annotations

import asyncio
import time

import networkx as nx
import pytest

from legba.data.analysts.deterministic_handlers import graph_mining as gm
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Fixtures: graphs dense enough that UNBOUNDED all_simple_paths explodes
# ---------------------------------------------------------------------------


def _dense_proxy_graph(n_hubs: int, n_leaves_per_hub: int = 3) -> nx.MultiDiGraph:
    """A hub-mesh + leaves graph that EXPLODES the pairwise proxy walk.

    The proxy miner only enumerates paths for pairs with NO direct edge (a
    cut-out has, by definition, no direct A->B link — this is exactly the live
    shape: ~1,980 vertices but only ~8,562 edges, so the vast majority of pairs
    are indirect). A complete graph K_n is therefore the WRONG stressor — every
    pair has a direct edge, so the walk explores nothing.

    This fixture instead builds a fully-connected mesh of ``n_hubs`` hubs
    (creating many multi-hop routes between any two hubs) plus leaf nodes hung
    off each hub. Two leaves on different hubs have NO direct edge but a
    combinatorial number of indirect paths through the hub mesh — so the
    UNBOUNDED miner would enumerate an explosive number of simple paths, while
    the bounded miner must stay fast. Alternating polarity keeps some chains
    negative (the headline cut-out shape).
    """
    g = nx.MultiDiGraph()
    hubs = [f"h{i:02d}" for i in range(n_hubs)]
    # Fully-connected directed hub mesh (both directions) → dense core.
    for i, u in enumerate(hubs):
        for j, v in enumerate(hubs):
            if i == j:
                continue
            g.add_edge(u, v, polarity=-1 if (i + j) % 2 else 1, confidence=1.0)
    # Leaves: each attached (in + out) to a couple of hubs, so leaf->leaf is
    # always indirect and routes through the mesh.
    for hi, hub in enumerate(hubs):
        for li in range(n_leaves_per_hub):
            leaf = f"l{hi:02d}_{li}"
            other = hubs[(hi + 1) % n_hubs]
            g.add_edge(leaf, hub, polarity=1, confidence=1.0)
            g.add_edge(hub, leaf, polarity=-1, confidence=1.0)
            g.add_edge(leaf, other, polarity=-1, confidence=1.0)
    return g


def _rows_from_multidigraph(g: nx.MultiDiGraph) -> list[dict]:
    """Project a graph into the input-row shape ``handle`` ingests."""
    rows: list[dict] = []
    for u, v, d in g.edges(data=True):
        rows.append({
            "source_entity": u,
            "target_entity": v,
            "edge_label": "HostileTo" if d.get("polarity", 0) < 0 else "AlliedWith",
            "polarity": d.get("polarity", 0),
            "confidence": d.get("confidence", 1.0),
        })
    return rows


# ---------------------------------------------------------------------------
# 1. The bound: a dense graph that would explode returns FAST with truncated
# ---------------------------------------------------------------------------


def test_dense_graph_bounded_returns_fast_and_truncates():
    """On a dense hub-mesh graph (where unbounded all_simple_paths would
    enumerate an explosive number of indirect paths) the miner must (a) return in
    well under a second and (b) honestly report ``truncated=True`` under a small
    scan cap."""
    g = _dense_proxy_graph(12)
    # A small explicit cap makes the truncation deterministic + the assertion
    # independent of the (large) production default.
    t0 = time.monotonic()
    chains, truncated = gm._proxy_chains(g, max_paths_scanned=2_000)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"bounded proxy mining took {elapsed:.2f}s (should be fast)"
    assert truncated is True, "dense graph under a 2k scan cap must report truncation"
    # It still returns a valid, bounded, scored top-K from the partial scan.
    assert 0 < len(chains) <= gm._MAX_PROXY_CHAINS
    for c in chains:
        assert 0.0 <= c["score"] <= 1.0
        assert c["length"] >= 2  # at least one intermediary


def test_dense_graph_would_explode_unbounded_sanity():
    """Sanity anchor: prove the fixture really is explosive by counting the
    indirect (no-direct-edge) simple paths the UNBOUNDED walk would yield. We
    count lazily via islice so this itself stays bounded + fast; hitting the
    islice ceiling proves the enumeration is far larger than our small scan cap.
    """
    import itertools

    g = _dense_proxy_graph(12)
    simple = nx.DiGraph()
    for u, v in g.edges():
        simple.add_edge(u, v)
    nodes = sorted(simple.nodes())

    def _all_indirect_paths():
        for a in nodes:
            for b in nodes:
                if a == b or simple.has_edge(a, b):
                    continue
                yield from nx.all_simple_paths(
                    simple, a, b, cutoff=gm._MAX_PROXY_PATH_LEN
                )

    # Count fully (bounded by a safety ceiling so the anchor itself stays fast).
    # The enumeration must dwarf the small scan caps the bound tests use (2k-3k)
    # so those tests genuinely exercise truncation — and this from a 48-vertex
    # graph, vs. the live ~1,980-vertex graph that froze the loop for 33 min.
    safety_ceiling = 500_000
    sampled = sum(1 for _ in itertools.islice(_all_indirect_paths(), safety_ceiling))
    assert sampled >= 10_000, (
        "fixture not dense enough to exercise the bound "
        f"(only {sampled} indirect paths — expected >> the 2k-3k scan caps used "
        "by the bound tests)"
    )


# ---------------------------------------------------------------------------
# 2. cutoff + cap behavior
# ---------------------------------------------------------------------------


def test_cutoff_bounds_chain_length():
    """``cutoff=max_path_len`` bounds every returned chain to <= max_path_len
    hops — a longer path is never emitted even if one exists."""
    # A long line a->b->c->d->e (4 hops) with no shortcut: at max_path_len=2 the
    # only 2-hop cut-outs (a->b->c, b->c->d, c->d->e) may surface but the full
    # 4-hop a..e must NOT.
    g = nx.MultiDiGraph()
    line = ["a", "b", "c", "d", "e"]
    for u, v in zip(line[:-1], line[1:]):
        g.add_edge(u, v, polarity=1, confidence=1.0)
    chains, truncated = gm._proxy_chains(g, max_path_len=2, max_paths_scanned=10_000)
    assert truncated is False
    assert chains, "expected some 2-hop cut-outs on the line"
    assert all(c["length"] <= 2 for c in chains)
    assert not any(c["actor"] == "a" and c["target"] == "e" for c in chains)


def test_scan_cap_makes_work_linear_not_exponential():
    """The total-paths-scanned cap is the load-bearing bound: raising graph
    density must NOT blow up runtime once the cap is fixed. A 10-hub and a
    14-hub dense graph both finish fast under the same small cap."""
    for n in (10, 14):
        g = _dense_proxy_graph(n)
        t0 = time.monotonic()
        chains, truncated = gm._proxy_chains(g, max_paths_scanned=3_000)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"{n}-hub dense graph took {elapsed:.2f}s under a fixed cap"
        assert truncated is True
        assert chains


def test_small_graph_not_truncated():
    """A tiny graph under the production default cap reports truncated=False —
    the marker is never a false positive."""
    g = nx.MultiDiGraph()
    g.add_edge("A", "M", polarity=-1, confidence=1.0)
    g.add_edge("M", "B", polarity=1, confidence=1.0)
    chains, truncated = gm._proxy_chains(g)  # production default cap
    assert truncated is False
    assert any(c["actor"] == "A" and c["target"] == "B" for c in chains)


def test_env_override_of_scan_cap(monkeypatch):
    """LEGBA_MAX_PROXY_PATHS overrides the cap at call time; a non-positive or
    junk value falls back to the safe default (the cap is never disabled)."""
    monkeypatch.setenv("LEGBA_MAX_PROXY_PATHS", "123")
    assert gm._max_proxy_paths_scanned() == 123
    monkeypatch.setenv("LEGBA_MAX_PROXY_PATHS", "0")   # never honor <= 0
    assert gm._max_proxy_paths_scanned() == gm._MAX_PROXY_PATHS_SCANNED
    monkeypatch.setenv("LEGBA_MAX_PROXY_PATHS", "not-a-number")
    assert gm._max_proxy_paths_scanned() == gm._MAX_PROXY_PATHS_SCANNED
    monkeypatch.delenv("LEGBA_MAX_PROXY_PATHS", raising=False)
    assert gm._max_proxy_paths_scanned() == gm._MAX_PROXY_PATHS_SCANNED


# ---------------------------------------------------------------------------
# 3. The truncation marker surfaces through handle() into the finding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_stamps_truncation_marker(monkeypatch):
    """When the cap bites, ``handle`` stamps ``data.proxy_chains_truncated=True``,
    a matching tag, and a warning — no silent truncation (the honesty rule)."""
    monkeypatch.setenv("LEGBA_MAX_PROXY_PATHS", "1500")
    rows = _rows_from_multidigraph(_dense_proxy_graph(12))
    result = await gm.handle(rows, {"sub_handler": "graph_mining"}, None)
    assert isinstance(result, AnalystMethodResult)
    data = result.finding.data
    assert data["proxy_chains_truncated"] is True
    assert "proxy_chains_truncated" in result.finding.tags
    assert any("proxy_chains_truncated" in w for w in data["warnings"])


@pytest.mark.asyncio
async def test_handle_no_marker_on_small_graph():
    """A small graph produces a finding with the marker present but False."""
    rows = [
        {"source_entity": "actor", "target_entity": "middleman",
         "edge_label": "SuppliesWeaponsTo", "polarity": -1, "confidence": 1.0},
        {"source_entity": "middleman", "target_entity": "mark",
         "edge_label": "HostileTo", "polarity": -1, "confidence": 1.0},
    ]
    result = await gm.handle(rows, {"sub_handler": "graph_mining"}, None)
    data = result.finding.data
    assert data["proxy_chains_truncated"] is False
    assert "proxy_chains_truncated" not in result.finding.tags


# ---------------------------------------------------------------------------
# 4. Off-loop execution — the event loop stays RESPONSIVE during mining
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_loop_responsive_during_mining():
    """The whole point of the fix: run ``handle`` over a dense graph CONCURRENTLY
    with a heartbeat coroutine and assert the heartbeat keeps ticking. If the CPU
    mining ran ON the loop (the bug), the heartbeat would stall for the duration
    of the enumeration and tick far fewer times.

    We use a LARGE scan cap so the CPU phase takes real (but bounded) wall time,
    giving the heartbeat something to race against.
    """
    ticks = 0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.005)  # 5ms cadence

    # A dense-ish graph + a big cap so the off-loop compute spends real time.
    rows = _rows_from_multidigraph(_dense_proxy_graph(16))

    hb = asyncio.create_task(heartbeat())
    # Let the heartbeat establish a baseline cadence first.
    await asyncio.sleep(0.05)
    baseline = ticks

    t0 = time.monotonic()
    result = await gm.handle(
        rows, {"sub_handler": "graph_mining"}, None,
    )
    mining_wall = time.monotonic() - t0

    stop.set()
    await hb

    assert isinstance(result, AnalystMethodResult)
    # The loop serviced the heartbeat DURING mining. Even a conservative floor:
    # if mining blocked the loop, ticks would barely advance past baseline.
    ticks_during = ticks - baseline
    # Expect at least ~1 tick per 20ms of mining wall time (heartbeat cadence is
    # 5ms; allow generous slack for scheduling). This FAILS loudly if the CPU
    # work ran on-loop.
    expected_floor = max(1, int(mining_wall / 0.020))
    assert ticks_during >= expected_floor, (
        f"event loop starved during mining: {ticks_during} ticks over "
        f"{mining_wall:.3f}s (floor {expected_floor}) — CPU phase not off-loop?"
    )


@pytest.mark.asyncio
async def test_offloop_runner_returns_same_as_inline():
    """``_run_mining_offloop`` produces the SAME mining products as calling the
    pure phases inline — the thread hand-off is behavior-preserving."""
    g = _dense_proxy_graph(8)
    cpu, warnings = await gm._run_mining_offloop(g, budget_s=30.0)
    assert warnings == []
    # Inline reference.
    ref = gm._mine_graph_cpu(g)
    assert cpu.communities == ref.communities
    assert cpu.centrality == ref.centrality
    assert cpu.proxy_chains == ref.proxy_chains
    assert cpu.proxy_chains_truncated == ref.proxy_chains_truncated


# ---------------------------------------------------------------------------
# 5. Wall-clock belt (belt 3) — a pathology is abandoned honestly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wall_clock_budget_abandons_with_honest_empty(monkeypatch):
    """If the CPU compute exceeds the wall-clock budget the run is abandoned with
    an honest-empty result + a warning — the loop is not left hanging.

    We force the pathology by monkeypatching the CPU bundle to sleep past a tiny
    budget (simulating a networkx pathology the path cap somehow missed).
    """
    def _slow_bundle(g):
        time.sleep(1.0)  # blows the 0.1s budget below
        return gm._CpuMiningResult([], None, {}, [], False)

    monkeypatch.setattr(gm, "_mine_graph_cpu", _slow_bundle)
    g = _dense_proxy_graph(5)
    t0 = time.monotonic()
    cpu, warnings = await gm._run_mining_offloop(g, budget_s=0.1)
    elapsed = time.monotonic() - t0
    # Returned promptly at the budget, not after the full 1.0s sleep.
    assert elapsed < 0.8, f"budget did not abandon promptly ({elapsed:.2f}s)"
    assert cpu.communities == [] and cpu.proxy_chains == []
    # Honesty: the abandon is flagged distinctly from a genuinely empty graph.
    assert cpu.mining_abandoned is True
    assert any("cpu_budget_exceeded" in w for w in warnings)


@pytest.mark.asyncio
async def test_handle_stamps_abandon_marker(monkeypatch):
    """When the CPU budget bites, ``handle`` stamps ``data.mining_abandoned=True``,
    a matching tag, and an ABANDONED title/body — so an operator never mistakes
    the empty result for a genuinely empty graph (the honesty rule)."""
    def _slow_bundle(g):
        time.sleep(1.0)
        return gm._CpuMiningResult([], None, {}, [], False)

    monkeypatch.setattr(gm, "_mine_graph_cpu", _slow_bundle)
    monkeypatch.setenv("LEGBA_GRAPH_MINING_BUDGET_S", "0.1")
    rows = _rows_from_multidigraph(_dense_proxy_graph(6))
    result = await gm.handle(rows, {"sub_handler": "graph_mining"}, None)
    data = result.finding.data
    assert data["mining_abandoned"] is True
    assert "mining_abandoned" in result.finding.tags
    assert "ABANDONED" in result.finding.title
    assert any("cpu_budget_exceeded" in w for w in data["warnings"])
    # The node_count still reflects the REAL graph (not zeroed) so the finding
    # is honest that a real graph was abandoned, not that the graph was empty.
    assert data["node_count"] > 0


@pytest.mark.asyncio
async def test_wall_clock_budget_disabled_still_offloop(monkeypatch):
    """A non-positive budget disables the abandon belt but STILL runs off-loop
    (to_thread) — the responsiveness guarantee does not depend on the belt."""
    monkeypatch.setenv("LEGBA_GRAPH_MINING_BUDGET_S", "0")
    assert gm._graph_mining_budget_s() == 0.0
    g = _dense_proxy_graph(6)
    cpu, warnings = await gm._run_mining_offloop(g)  # budget resolved from env → 0
    assert warnings == []
    ref = gm._mine_graph_cpu(g)
    assert cpu.proxy_chains == ref.proxy_chains
