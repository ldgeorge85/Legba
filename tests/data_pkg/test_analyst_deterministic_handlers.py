# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-173 per-sub-handler tests for the deterministic analyst kind.

One synthetic fixture per handler:

  * graph_mining — small synthetic AGE-shaped edge list with one obvious
    community and one proxy chain.
  * anomaly_detection — synthetic per-bucket time series with a planted
    rate spike and a sentiment shift.
  * structural_balance — synthetic signed graph with one balanced triad
    and one unbalanced (frustrated) triad.
  * calibration_tracking — synthetic resolved-claim list with known
    Brier score + a drift week.

All tests run with ``deps=None`` so no substrate is touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic_handlers import (
    anomaly_detection,
    calibration_tracking,
    graph_mining,
    structural_balance,
)
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# graph_mining
# ---------------------------------------------------------------------------


def _synthetic_graph_rows() -> list[dict]:
    """Two triangles + one cut-out shape.

    Cluster A: a1 -- a2 -- a3 -- a1 (triangle, AlliedWith).
    Cluster B: b1 -- b2 -- b3 -- b1 (triangle, AlliedWith).
    Bridge:   a1 -> b1 (AlliedWith).
    Proxy:    actor -> middleman -> mark, no direct actor->mark edge.
    """
    return [
        # Cluster A — alliance triangle.
        {"source_entity": "a1", "target_entity": "a2",
         "edge_label": "AlliedWith", "polarity": 1, "confidence": 1.0},
        {"source_entity": "a2", "target_entity": "a3",
         "edge_label": "AlliedWith", "polarity": 1, "confidence": 1.0},
        {"source_entity": "a3", "target_entity": "a1",
         "edge_label": "AlliedWith", "polarity": 1, "confidence": 1.0},
        # Cluster B — separate alliance triangle.
        {"source_entity": "b1", "target_entity": "b2",
         "edge_label": "AlliedWith", "polarity": 1, "confidence": 1.0},
        {"source_entity": "b2", "target_entity": "b3",
         "edge_label": "AlliedWith", "polarity": 1, "confidence": 1.0},
        {"source_entity": "b3", "target_entity": "b1",
         "edge_label": "AlliedWith", "polarity": 1, "confidence": 1.0},
        # Bridge between clusters.
        {"source_entity": "a1", "target_entity": "b1",
         "edge_label": "AlliedWith", "polarity": 1, "confidence": 1.0},
        # Proxy chain: actor -> middleman -> mark (no actor->mark direct).
        {"source_entity": "actor", "target_entity": "middleman",
         "edge_label": "SuppliesWeaponsTo", "polarity": -1, "confidence": 1.0},
        {"source_entity": "middleman", "target_entity": "mark",
         "edge_label": "HostileTo", "polarity": -1, "confidence": 1.0},
    ]


@pytest.mark.asyncio
async def test_graph_mining_detects_two_communities():
    rows = _synthetic_graph_rows()
    result = await graph_mining.handle(rows, {"sub_handler": "graph_mining"}, None)
    assert isinstance(result, AnalystMethodResult)
    data = result.finding.data
    assert data["sub_handler"] == "graph_mining"
    # Two alliance triangles + proxy linelet → at least 2 communities.
    assert len(data["communities"]) >= 2
    # Modularity should be positive (real community structure).
    assert data["modularity"] is None or data["modularity"] > 0


@pytest.mark.asyncio
async def test_graph_mining_finds_proxy_chain():
    rows = _synthetic_graph_rows()
    result = await graph_mining.handle(rows, {"sub_handler": "graph_mining"}, None)
    chains = result.finding.data["proxy_chains"]
    matching = [
        c for c in chains
        if c["actor"] == "actor" and c["target"] == "mark"
    ]
    assert matching, f"expected proxy chain actor->mark, got {chains!r}"
    # Path through middleman.
    assert "middleman" in matching[0]["via"]
    # Two negative edges along the path → product is +1 (enemy of enemy).
    assert matching[0]["polarity_sign"] == 1


@pytest.mark.asyncio
async def test_graph_mining_empty_inputs():
    """Empty input → empty findings, no crash."""
    result = await graph_mining.handle([], {"sub_handler": "graph_mining"}, None)
    data = result.finding.data
    assert data["node_count"] == 0
    assert data["edge_count"] == 0
    assert data["communities"] == []
    assert data["proxy_chains"] == []
    assert result.usage["prompt_tokens"] == 0


@pytest.mark.asyncio
async def test_graph_mining_centrality_top_node_is_bridge():
    """The a1 node bridges two triangles + has 3 incident edges → high degree."""
    rows = _synthetic_graph_rows()
    result = await graph_mining.handle(rows, {"sub_handler": "graph_mining"}, None)
    centrality = result.finding.data["centrality"]
    # a1 is in both clusters' triangles + bridge → degree >= 3.
    assert "a1" in centrality
    assert centrality["a1"]["degree"] >= 3


@pytest.mark.asyncio
async def test_graph_mining_interesting_shortlist_contract():
    """#99: graph_mining emits a scored `interesting` shortlist following the
    shared contract (kind/label/score/rationale/entities), sorted by score."""
    rows = _synthetic_graph_rows()
    result = await graph_mining.handle(rows, {"sub_handler": "graph_mining"}, None)
    interesting = result.finding.data["interesting"]
    assert isinstance(interesting, list)
    # Contract keys present on every item.
    for it in interesting:
        assert set(it) >= {"kind", "label", "score", "rationale", "entities"}
        assert it["kind"] in {"broker", "new_hostile_edge", "proxy_chain"}
        assert 0.0 <= float(it["score"]) <= 1.0
        assert isinstance(it["entities"], list)
    # Sorted by score desc.
    scores = [it["score"] for it in interesting]
    assert scores == sorted(scores, reverse=True)


def test_graph_mining_proxy_chains_deterministic_and_scored():
    """#99: the proxy-chain miner returns a deterministic, score-ranked top-K
    (replacing the arbitrary first-200 early break)."""
    import networkx as nx

    g = nx.MultiDiGraph()
    # Hostile cut-out: A -> M -> B (negative product), no direct A->B.
    g.add_edge("A", "M", polarity=-1)
    g.add_edge("M", "B", polarity=1)
    # Mundane positive chain: X -> Y -> Z.
    g.add_edge("X", "Y", polarity=1)
    g.add_edge("Y", "Z", polarity=1)
    # P1: _proxy_chains now returns (chains, truncated); a tiny graph never
    # trips the path-scan cap.
    first, first_trunc = graph_mining._proxy_chains(g)
    second, second_trunc = graph_mining._proxy_chains(g)
    assert first == second, "proxy-chain mining must be deterministic"
    assert first_trunc is False and second_trunc is False
    # Every chain carries a 0..1 score; ranked desc.
    for c in first:
        assert 0.0 <= c["score"] <= 1.0
    assert [c["score"] for c in first] == sorted(
        (c["score"] for c in first), reverse=True
    )
    # The negative cut-out outranks the positive chain.
    neg = next(c for c in first if c["polarity_sign"] < 0)
    pos = next(c for c in first if c["polarity_sign"] > 0)
    assert neg["score"] > pos["score"]


# ---------------------------------------------------------------------------
# anomaly_detection
# ---------------------------------------------------------------------------


def _synthetic_anomaly_series() -> list[dict]:
    """30 buckets: stable baseline ~10 events, then one giant spike at idx 28.

    Includes a sentiment shift on the same spike bucket.
    """
    rows: list[dict] = []
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i in range(30):
        ts = (base + timedelta(hours=i)).isoformat()
        if i == 28:
            count, sentiment = 100, -0.9
        else:
            count, sentiment = 10, 0.1
        rows.append({
            "bucket_ts": ts,
            "category": "news",
            "count": count,
            "sentiment_mean": sentiment,
            "entities": ["alpha", "beta"] if i < 25 else (
                ["alpha", "beta", "novel_x"] if i == 29 else ["alpha", "beta"]
            ),
        })
    return rows


@pytest.mark.asyncio
async def test_anomaly_detection_finds_planted_spike():
    rows = _synthetic_anomaly_series()
    result = await anomaly_detection.handle(
        rows,
        {"sub_handler": "anomaly_detection", "z_threshold": 2.5},
        None,
    )
    data = result.finding.data
    assert data["sub_handler"] == "anomaly_detection"
    # Should have caught the bucket at index 28 (one of the largest z-scores).
    spikes = data["rate_spikes"]
    assert spikes, "expected at least one rate spike"
    assert any(s["count"] == 100.0 for s in spikes)
    assert spikes[0]["z"] > 2.5


@pytest.mark.asyncio
async def test_anomaly_detection_finds_sentiment_shift():
    rows = _synthetic_anomaly_series()
    result = await anomaly_detection.handle(
        rows,
        {"sub_handler": "anomaly_detection", "z_threshold": 2.0},
        None,
    )
    shifts = result.finding.data["sentiment_shifts"]
    assert shifts, "expected at least one sentiment shift"
    # The -0.9 reading should be the most extreme.
    assert any(abs(s["z"]) > 2.0 for s in shifts)


@pytest.mark.asyncio
async def test_anomaly_detection_finds_novel_entity():
    """The latest bucket (idx 29) has 'novel_x' which doesn't appear earlier."""
    rows = _synthetic_anomaly_series()
    result = await anomaly_detection.handle(
        rows,
        {"sub_handler": "anomaly_detection"},
        None,
    )
    novel = result.finding.data["novel_entities"]
    novel_ids = {n["entity"] for n in novel}
    assert "novel_x" in novel_ids, f"expected novel_x in {novel_ids}"
    # alpha and beta are NOT novel.
    assert "alpha" not in novel_ids
    assert "beta" not in novel_ids


@pytest.mark.asyncio
async def test_anomaly_detection_empty_inputs():
    """Empty input + no deps → empty findings."""
    result = await anomaly_detection.handle(
        [], {"sub_handler": "anomaly_detection"}, None,
    )
    data = result.finding.data
    assert data["rate_spikes"] == []
    assert data["sentiment_shifts"] == []
    assert data["novel_entities"] == []
    assert data["bucket_count"] == 0


@pytest.mark.asyncio
async def test_anomaly_detection_quiet_series_no_false_positives():
    """30 identical buckets should produce zero spikes (std=0 short-circuits)."""
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    rows = [
        {
            "bucket_ts": (base + timedelta(hours=i)).isoformat(),
            "category": "news",
            "count": 10,
            "sentiment_mean": 0.0,
            "entities": ["a"],
        }
        for i in range(30)
    ]
    result = await anomaly_detection.handle(
        rows, {"sub_handler": "anomaly_detection"}, None,
    )
    assert result.finding.data["rate_spikes"] == []
    assert result.finding.data["sentiment_shifts"] == []


# ---------------------------------------------------------------------------
# structural_balance
# ---------------------------------------------------------------------------


def _balanced_triad_rows() -> list[dict]:
    """A-B-C: all AlliedWith (+,+,+) → product +1 = balanced."""
    return [
        {"source_entity": "A", "target_entity": "B", "edge_label": "AlliedWith"},
        {"source_entity": "B", "target_entity": "C", "edge_label": "AlliedWith"},
        {"source_entity": "A", "target_entity": "C", "edge_label": "AlliedWith"},
    ]


def _unbalanced_triad_rows() -> list[dict]:
    """A-B alliance, B-C alliance, A-C hostile → -1 = unbalanced."""
    return [
        {"source_entity": "A", "target_entity": "B", "edge_label": "AlliedWith"},
        {"source_entity": "B", "target_entity": "C", "edge_label": "AlliedWith"},
        {"source_entity": "A", "target_entity": "C", "edge_label": "HostileTo"},
    ]


@pytest.mark.asyncio
async def test_structural_balance_balanced_triad_ratio_one():
    rows = _balanced_triad_rows()
    result = await structural_balance.handle(
        rows, {"sub_handler": "structural_balance"}, None,
    )
    data = result.finding.data
    assert data["balanced_count"] == 1
    assert data["unbalanced_count"] == 0
    assert data["balance_ratio"] == 1.0


@pytest.mark.asyncio
async def test_structural_balance_unbalanced_triad_ratio_zero():
    rows = _unbalanced_triad_rows()
    result = await structural_balance.handle(
        rows, {"sub_handler": "structural_balance"}, None,
    )
    data = result.finding.data
    assert data["balanced_count"] == 0
    assert data["unbalanced_count"] == 1
    assert data["balance_ratio"] == 0.0
    # The frustration counter should record A, B, C each participating
    # in one unbalanced triad.
    assert data["frustration"] == {"A": 1, "B": 1, "C": 1}
    # The unbalanced triad list should carry one entry.
    triads = data["unbalanced_triads"]
    assert len(triads) == 1
    signs = triads[0]["signs"]
    # A-B and B-C are positive; A-C is negative.
    assert signs["bc"] == 1
    # The triad is keyed alphabetically (a<b<c) per the enumerator.


@pytest.mark.asyncio
async def test_structural_balance_mixed_balanced_unbalanced():
    """Two triads: one balanced (A-B-C all+), one unbalanced (D-E-F)."""
    rows = _balanced_triad_rows() + [
        {"source_entity": "D", "target_entity": "E", "edge_label": "AlliedWith"},
        {"source_entity": "E", "target_entity": "F", "edge_label": "AlliedWith"},
        {"source_entity": "D", "target_entity": "F", "edge_label": "HostileTo"},
    ]
    result = await structural_balance.handle(
        rows, {"sub_handler": "structural_balance"}, None,
    )
    data = result.finding.data
    assert data["balanced_count"] == 1
    assert data["unbalanced_count"] == 1
    assert data["balance_ratio"] == 0.5


@pytest.mark.asyncio
async def test_structural_balance_neutral_edge_excludes_triad():
    """Mixing a neutral edge (LocatedIn) into a triad → incomplete, not counted."""
    rows = [
        {"source_entity": "A", "target_entity": "B", "edge_label": "AlliedWith"},
        {"source_entity": "B", "target_entity": "C", "edge_label": "LocatedIn"},
        {"source_entity": "A", "target_entity": "C", "edge_label": "AlliedWith"},
    ]
    result = await structural_balance.handle(
        rows, {"sub_handler": "structural_balance"}, None,
    )
    data = result.finding.data
    assert data["balanced_count"] == 0
    assert data["unbalanced_count"] == 0
    assert data["incomplete_count"] == 1
    assert data["balance_ratio"] is None


@pytest.mark.asyncio
async def test_structural_balance_empty_inputs():
    result = await structural_balance.handle(
        [], {"sub_handler": "structural_balance"}, None,
    )
    data = result.finding.data
    assert data["node_count"] == 0
    assert data["edge_count"] == 0
    assert data["balance_ratio"] is None


@pytest.mark.asyncio
async def test_structural_balance_interesting_shortlist_contract():
    """#99: structural_balance emits a scored `interesting` shortlist following
    the shared contract, surfacing tense actors + sign-imbalanced triads."""
    # One unbalanced triad D-E-F (two allies split by one hostility).
    rows = _balanced_triad_rows() + [
        {"source_entity": "D", "target_entity": "E", "edge_label": "AlliedWith"},
        {"source_entity": "E", "target_entity": "F", "edge_label": "AlliedWith"},
        {"source_entity": "D", "target_entity": "F", "edge_label": "HostileTo"},
    ]
    result = await structural_balance.handle(
        rows, {"sub_handler": "structural_balance"}, None,
    )
    interesting = result.finding.data["interesting"]
    assert isinstance(interesting, list) and interesting
    for it in interesting:
        assert set(it) >= {"kind", "label", "score", "rationale", "entities"}
        assert it["kind"] in {"tense_actor", "sign_imbalanced_triad"}
        assert 0.0 <= float(it["score"]) <= 1.0
    # Sorted by score desc.
    scores = [it["score"] for it in interesting]
    assert scores == sorted(scores, reverse=True)
    # The unbalanced triad surfaces; its three nodes appear as tense actors.
    kinds = {it["kind"] for it in interesting}
    assert "sign_imbalanced_triad" in kinds
    assert "tense_actor" in kinds


def test_structural_balance_build_interesting_scoring():
    """#99 unit: a single-hostility unbalanced triad outscores an all-hostile
    one; tense-actor score is frustration normalised to the busiest node."""
    metrics = {
        "frustration": {"Iran": 3, "US": 1},
        "unbalanced_triads": [
            {"a": "A", "b": "B", "c": "C", "signs": {"ab": 1, "bc": 1, "ac": -1}},
            {"a": "D", "b": "E", "c": "F", "signs": {"ab": -1, "bc": -1, "ac": -1}},
        ],
    }
    items = structural_balance._build_interesting(metrics)
    tense = {it["label"]: it["score"] for it in items if it["kind"] == "tense_actor"}
    assert tense["Iran"] == 1.0  # busiest → normalised to 1.0
    assert tense["US"] < tense["Iran"]
    triads = {
        tuple(it["entities"]): it["score"]
        for it in items
        if it["kind"] == "sign_imbalanced_triad"
    }
    assert triads[("A", "B", "C")] > triads[("D", "E", "F")]


# ---------------------------------------------------------------------------
# calibration_tracking
# ---------------------------------------------------------------------------


def _perfect_calibration_rows() -> list[dict]:
    """4 rows: claimed conf vs outcome give Brier = ((0.9-1)^2 + (0.1-0)^2 + (0.8-1)^2 + (0.2-0)^2)/4."""
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return [
        {"analyst_id": "alpha", "claim_id": "c1", "claim_kind": "prediction",
         "claimed_confidence": 0.9, "outcome": 1, "resolved_at": base.isoformat()},
        {"analyst_id": "alpha", "claim_id": "c2", "claim_kind": "prediction",
         "claimed_confidence": 0.1, "outcome": 0, "resolved_at": base.isoformat()},
        {"analyst_id": "beta", "claim_id": "c3", "claim_kind": "prediction",
         "claimed_confidence": 0.8, "outcome": 1, "resolved_at": base.isoformat()},
        {"analyst_id": "beta", "claim_id": "c4", "claim_kind": "prediction",
         "claimed_confidence": 0.2, "outcome": 0, "resolved_at": base.isoformat()},
    ]


@pytest.mark.asyncio
async def test_calibration_brier_known_value():
    rows = _perfect_calibration_rows()
    result = await calibration_tracking.handle(
        rows, {"sub_handler": "calibration_tracking"}, None,
    )
    data = result.finding.data
    # Brier MATH is preserved in brier_pooled = (0.01+0.01+0.04+0.04)/4 = 0.025.
    assert abs(data["brier_pooled"] - 0.025) < 1e-9
    assert data["sample_size"] == 4
    # DQ-H2 honest contract: these synthetic rows carry no exogenous
    # resolved_by, so the HEADLINE brier is withheld (insufficient exogenous)
    # instead of presenting a self-consistency number as calibration.
    assert data["brier"] is None
    assert data["insufficient_exogenous"] is True


@pytest.mark.asyncio
async def test_calibration_per_analyst_breakdown():
    rows = _perfect_calibration_rows()
    result = await calibration_tracking.handle(
        rows, {"sub_handler": "calibration_tracking"}, None,
    )
    per = result.finding.data["per_analyst"]
    assert set(per) == {"alpha", "beta"}
    # alpha had two claims, each with squared-err 0.01 → brier 0.01.
    assert abs(per["alpha"]["brier"] - 0.01) < 1e-9
    assert per["alpha"]["sample_size"] == 2
    # beta had two claims, each squared-err 0.04 → brier 0.04.
    assert abs(per["beta"]["brier"] - 0.04) < 1e-9
    assert per["beta"]["sample_size"] == 2


@pytest.mark.asyncio
async def test_calibration_reliability_bins_shape():
    rows = _perfect_calibration_rows()
    result = await calibration_tracking.handle(
        rows, {"sub_handler": "calibration_tracking", "bin_count": 10}, None,
    )
    bins = result.finding.data["reliability_bins"]
    assert len(bins) == 10
    # Each bin must have lo + hi in [0, 1] and monotonic.
    for i, b in enumerate(bins):
        assert 0.0 <= b["lo"] <= 1.0
        assert 0.0 <= b["hi"] <= 1.0
        if i > 0:
            assert b["lo"] == bins[i - 1]["hi"]
    # Non-empty bins should have mean_claimed in their range.
    populated = [b for b in bins if b["count"] > 0]
    assert populated, "expected at least one populated bin"
    for b in populated:
        assert b["lo"] <= b["mean_claimed"] <= b["hi"] + 1e-9


@pytest.mark.asyncio
async def test_calibration_rolling_and_drift():
    """Build 14 weekly buckets with a clear drift in the final week."""
    base = datetime(2026, 2, 2, tzinfo=timezone.utc)  # Monday
    rows: list[dict] = []
    for week in range(14):
        wk_start = base + timedelta(days=7 * week)
        # Weeks 0..12: low-Brier, ~0.04.
        # Week 13: drift — high Brier (~0.49).
        if week < 13:
            rows.append({
                "analyst_id": "alpha", "claim_id": f"w{week}-1",
                "claim_kind": "prediction",
                "claimed_confidence": 0.8, "outcome": 1,
                "resolved_at": wk_start.isoformat(),
            })
            rows.append({
                "analyst_id": "alpha", "claim_id": f"w{week}-2",
                "claim_kind": "prediction",
                "claimed_confidence": 0.2, "outcome": 0,
                "resolved_at": wk_start.isoformat(),
            })
        else:
            rows.append({
                "analyst_id": "alpha", "claim_id": f"w{week}-drift",
                "claim_kind": "prediction",
                "claimed_confidence": 0.95, "outcome": 0,
                "resolved_at": wk_start.isoformat(),
            })

    result = await calibration_tracking.handle(
        rows,
        {"sub_handler": "calibration_tracking", "drift_threshold": 1.0},
        None,
    )
    data = result.finding.data
    rolling = data["rolling_brier"]
    # Should yield up to 12 weeks; latest week should be the drift one.
    assert rolling, "expected at least one rolling-Brier bucket"
    # Last bucket Brier should be high (~0.9025).
    assert rolling[-1]["brier"] is not None
    assert rolling[-1]["brier"] > 0.5
    # Drift z-score should clearly exceed threshold.
    assert data["drift_z"] is not None
    assert data["drift_alert"] is True


@pytest.mark.asyncio
async def test_calibration_drops_invalid_rows():
    """Confidence out of range or outcome != 0/1 → dropped + warning."""
    rows = [
        {"analyst_id": "a", "claimed_confidence": 0.5, "outcome": 1},
        # Bad: confidence > 1.
        {"analyst_id": "a", "claimed_confidence": 1.5, "outcome": 1},
        # Bad: outcome not in {0,1}.
        {"analyst_id": "a", "claimed_confidence": 0.5, "outcome": 2},
        # Bad: missing confidence.
        {"analyst_id": "a", "outcome": 1},
    ]
    result = await calibration_tracking.handle(
        rows, {"sub_handler": "calibration_tracking"}, None,
    )
    data = result.finding.data
    assert data["sample_size"] == 1
    assert any("dropped_invalid" in w for w in data["warnings"])


@pytest.mark.asyncio
async def test_calibration_empty_inputs():
    result = await calibration_tracking.handle(
        [], {"sub_handler": "calibration_tracking"}, None,
    )
    data = result.finding.data
    assert data["brier"] is None
    assert data["sample_size"] == 0
    assert data["reliability_bins"] == []
    assert data["per_analyst"] == {}
    assert data["rolling_brier"] == []
    assert data["drift_z"] is None
    assert data["drift_alert"] is False


# ---------------------------------------------------------------------------
# Shape contract — every handler returns a FindingPayload with sub_handler set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_module,name",
    [
        (graph_mining, "graph_mining"),
        (anomaly_detection, "anomaly_detection"),
        (structural_balance, "structural_balance"),
        (calibration_tracking, "calibration_tracking"),
    ],
)
async def test_handler_shape_contract(handler_module, name):
    result = await handler_module.handle(
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
# DQ-H2: honest exogenous/self-consistency split + horizon-end resolver
# ---------------------------------------------------------------------------


def test_calibration_is_exogenous_classifier():
    ex = calibration_tracking._is_exogenous
    assert ex({"resolved_by": "forecast_vs_actual"}) is True
    assert ex({"resolved_by": "operator:alice"}) is True
    assert ex({"resolved_by": "forecast_acute_exogenous"}) is True
    # D16 — subsequent_facts is now the WEAK/LEXICAL tier, NOT headline-exogenous.
    assert ex({"resolved_by": "subsequent_facts"}) is False
    # D28 — a naive_mean prediction is a non-forecaster: even with an exogenous
    # CI-vs-actual resolution it is DEMOTED out of the headline exogenous tier.
    assert ex({"resolved_by": "forecast_vs_actual",
               "forecast_method": "naive_mean"}) is False
    assert ex({"resolved_by": "forecast_vs_actual",
               "forecast_method": "auto_arima"}) is True
    # Self-consistency + unlabeled are NOT exogenous (conservative).
    assert ex({"resolved_by": "status_transition"}) is False
    assert ex({"resolved_by": "unknown"}) is False
    assert ex({"resolved_by": None}) is False
    assert ex({}) is False


def test_calibration_is_weak_tier_classifier():
    """D16/D28 — the WEAK tier: lexical `subsequent_facts` + `naive_mean`
    non-forecaster predictions. Weak ⇒ NOT headline-exogenous, NOT self-
    consistency (the system did not grade itself)."""
    weak = calibration_tracking._is_weak_tier
    ex = calibration_tracking._is_exogenous
    # D16 — lexical proxy is weak.
    assert weak({"resolved_by": "subsequent_facts"}) is True
    # D28 — naive_mean is weak regardless of the resolution source.
    assert weak({"resolved_by": "forecast_vs_actual",
                 "forecast_method": "naive_mean"}) is True
    # A genuinely falsifiable, fitted forecast is NOT weak.
    assert weak({"resolved_by": "forecast_vs_actual",
                 "forecast_method": "auto_arima"}) is False
    assert weak({"resolved_by": "operator:alice"}) is False
    assert weak({"resolved_by": "forecast_acute_exogenous"}) is False
    # Self-consistency is NOT weak (it is its own tier).
    assert weak({"resolved_by": "status_transition"}) is False
    assert weak({}) is False
    # Mutual exclusivity: a weak row is never headline-exogenous.
    for r in ({"resolved_by": "subsequent_facts"},
              {"resolved_by": "forecast_vs_actual", "forecast_method": "naive_mean"}):
        assert weak(r) is True and ex(r) is False


@pytest.mark.asyncio
async def test_calibration_weak_tier_demoted_from_headline():
    """A mixed sample with falsifiable exogenous rows AND weak (lexical /
    naive_mean) rows: the headline is the EXOGENOUS-only Brier; the weak rows
    land in the weak bucket and never touch the headline."""
    base = datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat()

    def _row(cid, conf, out, by, method=None):
        r = {"analyst_id": "a", "claim_id": cid, "claim_kind": "prediction",
             "claimed_confidence": conf, "outcome": out, "resolved_at": base,
             "resolved_by": by}
        if method is not None:
            r["forecast_method"] = method
        return r

    rows = [
        _row("e1", 0.9, 1, "operator:alice"),        # exogenous, err 0.01
        _row("e2", 0.1, 0, "operator:alice"),        # exogenous, err 0.01
        _row("w1", 0.95, 0, "subsequent_facts"),     # D16 weak (lexical)
        _row("w2", 0.2, 1, "forecast_vs_actual",     # D28 weak (naive_mean)
             method="naive_mean"),
    ]
    result = await calibration_tracking.handle(
        rows, {"sub_handler": "calibration_tracking", "min_exogenous": 1}, None,
    )
    data = result.finding.data
    # Headline = exogenous-only = (0.01 + 0.01)/2 = 0.01.
    assert abs(data["brier"] - 0.01) < 1e-9
    assert data["exogenous_sample_size"] == 2
    assert data["insufficient_exogenous"] is False
    # Weak tier holds the 2 demoted rows with its own diagnostic Brier.
    assert data["weak_sample_size"] == 2
    assert data["brier_weak"] is not None
    assert abs(data["weak_fraction"] - 0.5) < 1e-9
    # The weak rows must NOT be in the headline exogenous Brier.
    assert abs(data["brier_exogenous"] - 0.01) < 1e-9
    assert "brier_weak_tier_present" in result.finding.tags


@pytest.mark.asyncio
async def test_calibration_all_weak_withholds_headline():
    """When EVERY resolved row is weak-tier (no falsifiable exogenous rows), the
    headline Brier is withheld (insufficient_exogenous) even though a number
    could be computed — the honest outcome is INSUFFICIENT sample."""
    base = datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat()
    rows = [
        {"analyst_id": "a", "claim_id": f"w{i}", "claim_kind": "hypothesis",
         "claimed_confidence": 0.8, "outcome": i % 2, "resolved_at": base,
         "resolved_by": "subsequent_facts"}
        for i in range(6)
    ]
    result = await calibration_tracking.handle(
        rows, {"sub_handler": "calibration_tracking", "min_exogenous": 1}, None,
    )
    data = result.finding.data
    assert data["sample_size"] == 6
    assert data["exogenous_sample_size"] == 0
    assert data["insufficient_exogenous"] is True
    assert data["brier"] is None              # headline withheld
    assert data["weak_sample_size"] == 6
    assert data["brier_weak"] is not None     # diagnostic only


@pytest.mark.asyncio
async def test_calibration_honest_split_headline():
    """A mixed sample: the HEADLINE brier is exogenous-only; self-consistency
    rows are quarantined into the fraction + diagnostic, never the headline."""
    base = datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat()
    def _row(cid, conf, out, by):
        return {"analyst_id": "a", "claim_id": cid, "claim_kind": "prediction",
                "claimed_confidence": conf, "outcome": out, "resolved_at": base,
                "resolved_by": by}
    rows = [
        _row("e1", 0.9, 1, "forecast_vs_actual"),   # exogenous, err 0.01
        _row("e2", 0.1, 0, "forecast_vs_actual"),   # exogenous, err 0.01
        _row("s1", 0.95, 1, "status_transition"),   # self-consistency
        _row("s2", 0.95, 1, "status_transition"),   # self-consistency
    ]
    result = await calibration_tracking.handle(
        rows, {"sub_handler": "calibration_tracking", "min_exogenous": 1}, None,
    )
    data = result.finding.data
    # Headline = exogenous-only Brier = (0.01 + 0.01)/2 = 0.01.
    assert abs(data["brier"] - 0.01) < 1e-9
    assert abs(data["brier_exogenous"] - 0.01) < 1e-9
    assert data["exogenous_sample_size"] == 2
    assert data["insufficient_exogenous"] is False
    # Half the sample is self-consistency.
    assert abs(data["self_consistency_fraction"] - 0.5) < 1e-9
    assert data["brier_self_consistency"] is not None
    # Pooled (all 4) is the diagnostic, NOT the headline.
    assert data["brier_pooled"] is not None


class _GradeConn:
    """Fake conn for _grade_one_prediction: a target geo + an actual count."""
    def __init__(self, geo, count):
        self._geo, self._count = geo, count
    async def fetchrow(self, query, *args):
        if "target_descriptors" in query:
            return {"body": {"scope": {"geo": self._geo}}}
        if "FROM signals" in query:
            return {"cnt": float(self._count)}
        return None


@pytest.mark.asyncio
async def test_grade_one_prediction_hit_and_miss():
    # 70 signals over a 7-day horizon = 10/day actual.
    conn = _GradeConn(geo=["US"], count=70)
    base_data = {"region": "country_g20_us", "horizon_end": "2026-06-20",
                 "horizon_days": 7}
    # CI [5,15] contains 10 → hit.
    hit = await calibration_tracking._grade_one_prediction(
        conn, {**base_data, "ci_lower": 5.0, "ci_upper": 15.0}, {},
    )
    assert hit is not None and hit["hit"] is True and abs(hit["actual"] - 10.0) < 1e-9
    # CI [20,30] misses 10 → miss.
    miss = await calibration_tracking._grade_one_prediction(
        conn, {**base_data, "ci_lower": 20.0, "ci_upper": 30.0}, {},
    )
    assert miss is not None and miss["hit"] is False
