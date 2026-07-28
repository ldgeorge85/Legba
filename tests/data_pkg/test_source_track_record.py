# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A6 P3-3 — the EARNED source track record (layer 3), pure-logic coverage.

No live Postgres. Covers:

  * the win-rate math — Beta-Bernoulli smoothed rate + neutral prior (a
    2-contest source is never rated extreme), the Wilson lower bound, and the
    damped, non-negative arbiter side-weight;
  * the SourceRecord derived fields (contested_total / raw rate / low-sample /
    corroboration rate);
  * the CIRCULARITY GUARD threading — the lag cutoff (surfaced < now-lag) and
    the acyclicity exclusion (the contention being decided is passed through as
    ``exclude_contention``) reach the substrate query verbatim;
  * the arbiter OFF-flag invariant — with ``LEGBA_CONTENTION_EARNED_WEIGHT``
    unset the deterministic tie-break weight is BYTE-IDENTICAL to the P3-2
    formula even when a side carries a populated ``earned_weight``; and the
    consumption path (``_attach_earned_weights``) only runs / matters ON-flag;
  * summary honesty — an honest zero-state and correct distribution counts.

The real Postgres round-trip (win/loss detection over resolved contentions via
fact->signal->source lineage, the lag-window exclusion, the route + projection)
lives in the sibling integration test ``test_source_track_record_db.py``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb
from legba.data.analysts.deterministic_handlers import source_track_record as str_mod


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _rec(source_id: str, wins: int, losses: int, *, corr: int = 0, corr_total: int = 0,
         lag: float = 72.0) -> str_mod.SourceRecord:
    return str_mod.SourceRecord(
        source_id=source_id,
        wins=wins,
        losses=losses,
        corroborated=corr,
        corroboration_total=corr_total,
        lag_hours=lag,
        sample_as_of=NOW - timedelta(hours=lag),
        computed_at=NOW,
    )


# ===========================================================================
# 1. Win-rate math — smoothing + prior.
# ===========================================================================


def test_beta_neutral_at_zero_sample():
    """No sample -> exactly the neutral 0.5 prior (never a fabricated extreme)."""
    assert str_mod.beta_smoothed_rate(0, 0) == 0.5


def test_beta_prior_damps_small_samples_away_from_extreme():
    """A 2-0 source is NOT rated 1.0 — the Beta(2,2) prior pulls it to ~0.667
    (the operator's "a source with 2 contests isn't rated extreme")."""
    r = str_mod.beta_smoothed_rate(2, 0)
    assert r == pytest.approx(4 / 6)   # (2+2)/(2+0+4)
    assert r < 0.9                     # nowhere near the raw 1.0


def test_beta_symmetric_around_half():
    """Swapping wins<->losses reflects the rate around 0.5 (unbiased prior)."""
    assert str_mod.beta_smoothed_rate(3, 1) + str_mod.beta_smoothed_rate(1, 3) == pytest.approx(1.0)


def test_beta_approaches_raw_rate_with_large_sample():
    """A big sample overwhelms the prior — 90/10 smooths close to 0.9."""
    assert str_mod.beta_smoothed_rate(90, 10) == pytest.approx((90 + 2) / (100 + 4))
    assert str_mod.beta_smoothed_rate(90, 10) > 0.87


def test_wilson_zero_at_no_sample():
    """No evidence -> no earned floor (the conservative bound is 0.0)."""
    assert str_mod.wilson_lower_bound(0, 0) == 0.0


def test_wilson_is_below_raw_rate_and_grows_with_sample():
    """The lower bound sits below the point estimate and tightens upward as the
    (same-rate) sample grows."""
    small = str_mod.wilson_lower_bound(4, 1)    # 0.8 raw
    big = str_mod.wilson_lower_bound(40, 10)     # 0.8 raw
    assert small < 0.8 and big < 0.8
    assert big > small                            # more evidence -> higher floor
    assert 0.0 <= small <= 1.0 and 0.0 <= big <= 1.0


def test_wilson_clamped_to_unit_interval():
    assert 0.0 <= str_mod.wilson_lower_bound(1, 0) <= 1.0
    assert 0.0 <= str_mod.wilson_lower_bound(0, 1) <= 1.0


# ===========================================================================
# 2. Damped arbiter side-weight (circularity guard (b)).
# ===========================================================================


def test_side_weight_zero_at_neutral_and_below():
    """A source with no / a losing record adds NOTHING (non-negative seam — it
    never pushes a side below its corroboration floor)."""
    assert str_mod.earned_side_weight(0, 0) == 0.0     # neutral
    assert str_mod.earned_side_weight(0, 5) == 0.0     # all losses -> below 0.5 -> 0
    assert str_mod.earned_side_weight(1, 9) == 0.0


def test_side_weight_damped_by_sample_size():
    """Same raw rate, more evidence -> a STRONGER signal (small n stays damped
    toward the neutral prior -> ~0 signal)."""
    thin = str_mod.earned_side_weight(2, 0)     # smoothed 0.667
    thick = str_mod.earned_side_weight(40, 0)    # smoothed ~0.955
    assert 0.0 < thin < thick <= 1.0
    assert thin == pytest.approx(2.0 * (4 / 6 - 0.5))


# ===========================================================================
# 3. SourceRecord derived fields.
# ===========================================================================


def test_record_derived_fields():
    r = _rec("source.x", 6, 2, corr=5, corr_total=8)
    assert r.contested_total == 8
    assert r.win_rate_raw == pytest.approx(6 / 8)
    assert r.win_rate_smoothed == pytest.approx((6 + 2) / (8 + 4))
    assert r.corroboration_rate == pytest.approx(5 / 8)
    assert r.low_sample is False               # 8 >= LOW_SAMPLE_THRESHOLD (5)


def test_record_zero_sample_fields_are_honest_nulls():
    r = _rec("source.y", 0, 0, corr=0, corr_total=0)
    assert r.contested_total == 0
    assert r.win_rate_raw is None              # undefined, not fabricated
    assert r.corroboration_rate is None
    assert r.low_sample is True
    assert r.earned_signal == 0.0


# ===========================================================================
# 4. Circularity guard threading — lag cutoff + acyclicity exclusion.
# ===========================================================================


class _RecordingConn:
    """Records the params passed to ``fetch`` and replays a scripted result."""

    def __init__(self, result: list[Any]) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._result = result

    async def fetch(self, sql: str, *params: Any) -> Any:
        self.calls.append((sql, params))
        return self._result


def test_lag_cutoff_and_exclusion_reach_the_query():
    """The lag (guard a) becomes ``surfaced_at < now-lag`` and the contention
    being decided (guard c) is threaded as the exclusion param, verbatim."""
    conn = _RecordingConn([])
    cid = uuid4()
    asyncio.run(
        str_mod.compute_source_records(
            conn, now=NOW, lag_hours=72.0, exclude_contention=cid,
            source_ids=["source.a", "source.b"],
        )
    )
    sql, params = conn.calls[0]
    assert "surfaced_at < $1" in sql
    assert "id <> $2" in sql                       # the acyclicity exclusion
    assert params[0] == NOW - timedelta(hours=72)  # the settled cutoff
    assert params[1] == cid                         # exclude the current contention
    assert params[2] == ["source.a", "source.b"]    # source allowlist


def test_earned_weights_for_sources_excludes_current_contention():
    """The arbiter-facing helper passes the exclusion through and maps each
    source to its damped side-weight."""
    cid = uuid4()
    conn = _RecordingConn([
        {"source_id": "source.a", "wins": 40, "losses": 0,
         "corroborated": 0, "corroboration_total": 0},
        {"source_id": "source.b", "wins": 0, "losses": 8,
         "corroborated": 0, "corroboration_total": 0},
    ])
    weights = asyncio.run(
        str_mod.earned_weights_for_sources(
            conn, ["source.a", "source.b"], now=NOW, exclude_contention=cid,
            lag_hours=72.0,
        )
    )
    _, params = conn.calls[0]
    assert params[1] == cid
    assert weights["source.a"] == pytest.approx(str_mod.earned_side_weight(40, 0))
    assert weights["source.b"] == 0.0             # a losing record -> no bonus


def test_earned_weights_empty_for_no_sources():
    conn = _RecordingConn([])
    weights = asyncio.run(
        str_mod.earned_weights_for_sources(
            conn, [], now=NOW, exclude_contention=None,
        )
    )
    assert weights == {}
    assert conn.calls == []                        # never touches the substrate


# ===========================================================================
# 5. Arbiter OFF-flag invariant + the consumption path.
# ===========================================================================


def _agg(value_key: str, *, distinct: int, cred: float, types: int,
         fact_ids: list[UUID] | None = None) -> arb._ValueAgg:
    a = arb._ValueAgg(value_key)
    a.representative_fact_id = uuid4()
    a.representative_value = value_key
    a.distinct_lineage = {f"src:{value_key}:{i}" for i in range(distinct)}
    a.cred_sum = cred
    a.source_types = {f"type{i}" for i in range(types)}
    a.supporting_fact_ids = list(fact_ids or [a.representative_fact_id])
    return a


def test_off_flag_weight_is_byte_identical_to_p3_2_formula(monkeypatch):
    """With LEGBA_CONTENTION_EARNED_WEIGHT unset, the tie-break weight equals
    EXACTLY the P3-2 formula (distinct sources + type diversity + cred sum) —
    even when a side already carries a populated earned_weight. This is the
    OFF invariant: shipping this task changes ZERO arbiter behavior by default."""
    monkeypatch.delenv(arb.EARNED_WEIGHT_ENV, raising=False)
    a = _agg("x", distinct=3, cred=2.4, types=2)
    a.earned_weight = 0.9                          # would-be bonus, ignored OFF
    p3_2 = float(a.distinct_source_count) + float(len(a.source_types)) + float(a.cred_sum)
    assert arb._tiebreak_weight(a) == p3_2 == pytest.approx(3 + 2 + 2.4)
    assert arb._earned_track_record_weight(a) == 0.0


def test_on_flag_seam_adds_scaled_earned_weight(monkeypatch):
    monkeypatch.setenv(arb.EARNED_WEIGHT_ENV, "2.0")
    a = _agg("x", distinct=3, cred=2.4, types=2)
    a.earned_weight = 0.5
    assert arb._earned_track_record_weight(a) == pytest.approx(1.0)   # 2.0 * 0.5
    assert arb._tiebreak_weight(a) == pytest.approx(3 + 2 + 2.4 + 1.0)


def test_negative_env_disables_the_seam(monkeypatch):
    monkeypatch.setenv(arb.EARNED_WEIGHT_ENV, "-5")
    a = _agg("x", distinct=2, cred=1.0, types=1)
    a.earned_weight = 1.0
    assert arb._earned_weight_scale() == 0.0
    assert arb._earned_track_record_weight(a) == 0.0


class _EarnedConn:
    """Fake conn for the ON-flag consumption path: dispatches the fact->source
    lineage query and the per-source aggregate query by SQL shape."""

    def __init__(self, fact_sources: dict[UUID, str], aggregates: list[dict]) -> None:
        self._fact_sources = fact_sources
        self._aggregates = aggregates

    async def fetch(self, sql: str, *params: Any) -> Any:
        if "unnest(f.derived_from)" in sql and "f.id = ANY" in sql:
            want = set(params[0])
            return [
                {"fact_id": fid, "source_id": sid}
                for fid, sid in self._fact_sources.items()
                if fid in want
            ]
        # the _RECORDS_SQL aggregate query — narrow to the requested sources.
        allow = params[2]
        return [
            r for r in self._aggregates
            if allow is None or r["source_id"] in allow
        ]


def test_attach_earned_weights_populates_side_from_best_carrier(monkeypatch):
    """ON-flag end to end: resolve each side's sources, look up their live
    earned records (excluding the current contention), and stamp the side with
    the strongest carrier's damped signal."""
    monkeypatch.setenv(arb.EARNED_WEIGHT_ENV, "1.0")
    fa, fb = uuid4(), uuid4()
    proven = _agg("de-escalating", distinct=2, cred=1.0, types=1, fact_ids=[fa])
    weak = _agg("clashes ongoing", distinct=2, cred=1.0, types=1, fact_ids=[fb])
    conn = _EarnedConn(
        fact_sources={fa: "source.proven", fb: "source.weak"},
        aggregates=[
            {"source_id": "source.proven", "wins": 40, "losses": 0,
             "corroborated": 0, "corroboration_total": 0},
            {"source_id": "source.weak", "wins": 0, "losses": 6,
             "corroborated": 0, "corroboration_total": 0},
        ],
    )
    cid = uuid4()
    asyncio.run(
        arb._attach_earned_weights(conn, [proven, weak], contention_id=cid, now=NOW)
    )
    assert proven.earned_weight == pytest.approx(str_mod.earned_side_weight(40, 0))
    assert weak.earned_weight == 0.0
    # And it shows up additively in the weight now that the flag is ON.
    assert arb._tiebreak_weight(proven) > arb._tiebreak_weight(weak)


def test_attach_earned_weights_degrades_on_error(monkeypatch):
    """A lineage-resolution failure DEGRADES to no bonus (earned_weight stays
    0.0) — the advisory seam never breaks the deterministic tie-break."""
    monkeypatch.setenv(arb.EARNED_WEIGHT_ENV, "1.0")

    class _BoomConn:
        async def fetch(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("substrate down")

    a = _agg("x", distinct=2, cred=1.0, types=1)
    asyncio.run(
        arb._attach_earned_weights(_BoomConn(), [a], contention_id=uuid4(), now=NOW)
    )
    assert a.earned_weight == 0.0


# ===========================================================================
# 6. Summary honesty.
# ===========================================================================


def test_summary_zero_state_is_honest():
    """No contested sources -> the finding SAYS so (never a fabricated rate)."""
    finding = str_mod.build_summary(
        [_rec("source.a", 0, 0), _rec("source.b", 0, 0)], lag_hours=72.0,
    )
    assert "0 with a resolved-contest sample" in finding.title
    d = finding.data
    assert d["contested_sources"] == 0
    assert d["mean_win_rate_smoothed"] is None
    assert d["top_earned"] == []
    assert finding.confidence == 1.0
    # The hard-rule + OFF-by-default reminders are stamped in the body.
    assert "never faithfulness" in finding.body
    assert "OFF by default" in finding.body


def test_summary_distribution_counts_and_ranking():
    records = [
        _rec("source.proven", 40, 5, corr=30, corr_total=40),   # well-sampled, strong
        _rec("source.weak", 2, 18, corr=1, corr_total=20),       # well-sampled, weak
        _rec("source.thin", 2, 0),                                # low-sample
        _rec("source.unseen", 0, 0),                              # no sample
    ]
    finding = str_mod.build_summary(records, lag_hours=72.0)
    d = finding.data
    assert d["sources_total"] == 4
    assert d["contested_sources"] == 3          # proven + weak + thin
    assert d["well_sampled_sources"] == 2       # proven + weak
    assert d["low_sample_sources"] == 1         # thin
    assert d["mean_win_rate_smoothed"] is not None
    # Ranked by the conservative lower bound: the proven source tops it.
    assert d["top_earned"][0]["source_id"] == "source.proven"
    assert d["lag_hours"] == 72.0
