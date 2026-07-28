# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P3-7 — the ``desk_baseline`` CAST-recipe per-desk statistical baseline.

Pure tests (no DB): the static land-border adjacency + neighbour-desk mapping,
the CAST feature recipe (lags / rolling means), the robust estimator (band,
Poisson-floored sigma, within/above/below deviation with the trigger's ABSOLUTE
floors so a quiet desk's σ≈0 blip never fires), the insufficient-history
honesty flag, and the no-forecast summary honesty. Route tests (fake conn): the
``/eval/desk_baselines`` divergence surfacing shape + honest-empty states.
Ephemeral-DB tests (``migrated_pg``): compute → store → deviation end-to-end,
the zero/insufficient-history summary, neighbour spillover, and the wholesale
prune, all against live SQL + the 0103 sidecar.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import alert_trigger_scan as ats
from legba.data.analysts.deterministic_handlers import desk_baseline as db
from legba.data.config import PostgresConfig
from legba.data.provenance.kinds import (
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
    OutputKind,
)
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_registered_as_finding_sub_handler_and_structural_exempt():
    """The daily distribution readout is a genuine FINDING (the source_track_
    record / fact_decay_scan precedent), so the handler sits in the
    STRUCTURAL_VERIFY_EXEMPT registry and the drift guard's FINDING-set equality
    holds."""
    assert SUB_HANDLERS["desk_baseline"] is db.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER["desk_baseline"] is OutputKind.FINDING
    assert "desk_baseline" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


async def test_refuses_loud_without_pool():
    with pytest.raises(RuntimeError, match="pg_pool"):
        await db.handle([], {"sub_handler": "desk_baseline"}, None)


def test_floors_are_the_trigger_floors():
    """The absolute floors are imported from the P1-3 trigger, in lockstep."""
    assert db.MIN_CURRENT_SIGNALS is ats.MIN_CURRENT_SIGNALS
    assert db.MIN_CURRENT_FINDINGS is ats.MIN_CURRENT_FINDINGS
    assert db.METRIC_SIGNAL_VOLUME == ats.METRIC_SIGNAL_VOLUME
    assert db.METRIC_HIGH_SEV_FINDINGS == ats.METRIC_HIGH_SEV_FINDINGS


# ---------------------------------------------------------------------------
# Pure — static adjacency + neighbour mapping
# ---------------------------------------------------------------------------


def test_land_adjacency_is_symmetric():
    """Every edge is bidirectional (built from canonical edges) — an asymmetry
    would silently drop a spillover contribution."""
    for a, neighbours in db.LAND_ADJACENCY.items():
        for b in neighbours:
            assert a in db.LAND_ADJACENCY[b], f"{a}->{b} not mirrored"


def test_build_adjacency_expands_both_directions():
    adj = db.build_adjacency([("US", "CA"), ("US", "MX")])
    assert adj["US"] == frozenset({"CA", "MX"})
    assert adj["CA"] == frozenset({"US"})
    assert adj["MX"] == frozenset({"US"})


def test_neighbor_desks_maps_iso2_and_excludes_self():
    iso2_to_desk = {
        "US": "country_g20_us",
        "CA": "country_g20_ca",
        "MX": "country_g20_mx",
    }
    assert db.neighbor_desks(
        ["US"], iso2_to_desk, self_desk="country_g20_us"
    ) == ["country_g20_ca", "country_g20_mx"]
    # Self never counts; an unmapped neighbour ISO2 just contributes nothing.
    assert db.neighbor_desks(
        ["CA"], iso2_to_desk, self_desk="country_g20_ca"
    ) == ["country_g20_us"]
    # A desk with no desk-neighbours (island / non-desk borders) → empty.
    assert db.neighbor_desks(["AU"], iso2_to_desk, self_desk="x") == []


# ---------------------------------------------------------------------------
# Pure — the CAST feature recipe (lags + rolling means)
# ---------------------------------------------------------------------------


def test_lag_features_lags_and_rolling_means():
    # buckets[0]=30 (current), buckets[k]=k days ago; 30..1.
    buckets = list(range(30, 0, -1))
    feats = db.lag_features(buckets)
    assert feats["lag_1"] == 29.0
    assert feats["lag_7"] == 23.0
    assert feats["lag_28"] == 2.0
    # roll_mean_7 = mean(buckets[1..7]) = mean(29..23) = 26.
    assert feats["roll_mean_7"] == 26.0
    # roll_mean_28 = mean(buckets[1..28]) = mean(29..2) = 15.5.
    assert feats["roll_mean_28"] == 15.5


def test_lag_features_short_vector_is_honest_none():
    feats = db.lag_features([5, 4, 3])
    assert feats["lag_1"] == 4.0
    assert feats["lag_7"] is None       # unreachable → honest None, never a 0
    assert feats["lag_28"] is None
    assert feats["roll_mean_7"] == 3.5  # mean of the two available baseline days


# ---------------------------------------------------------------------------
# Pure — the robust estimator
# ---------------------------------------------------------------------------


def test_estimate_within_band():
    est = db.estimate_baseline(
        [5.0] * 7, 5.0, min_current=db.MIN_CURRENT_SIGNALS
    )
    assert est.expected == 5.0
    assert est.center_median == 5.0
    assert est.deviation == "within"
    assert est.deviation_sigma == 0.0
    assert est.insufficient_history is False


def test_estimate_above_requires_the_absolute_floor():
    # Clear statistical + absolute exceedance (signals floor = 10).
    est = db.estimate_baseline(
        [1.0] * 28, 15.0, min_current=db.MIN_CURRENT_SIGNALS
    )
    assert est.deviation == "above"
    assert est.band_high == pytest.approx(3.0)  # 1 + 2*sqrt(1)
    assert est.deviation_sigma == pytest.approx(14.0)


def test_estimate_quiet_desk_sigma_zero_never_fires():
    """The σ≈0 quiet-desk guard: a 2-vs-0 blip on a zero baseline is
    statistically 'infinitely' above 0 but stays WITHIN — the absolute floor
    (>=10 signals) is the guard, exactly mirroring the P1-3 trigger."""
    est = db.estimate_baseline(
        [0.0] * 28, 2.0, min_current=db.MIN_CURRENT_SIGNALS
    )
    assert est.robust_sigma == 0.0
    assert est.deviation == "within"
    assert est.deviation_sigma is None
    assert est.insufficient_history is True  # no active days → thin


def test_estimate_spike_over_floor_on_zero_history_still_fires_and_flags_thin():
    """A no-history desk that suddenly clears the floor DOES read 'above' (a real
    spike over the floor is a real deviation) AND carries the thin-history flag —
    the flag never suppresses the absolute-floor exceedance."""
    est = db.estimate_baseline(
        [0.0] * 28, 12.0, min_current=db.MIN_CURRENT_SIGNALS
    )
    assert est.deviation == "above"
    assert est.insufficient_history is True


def test_estimate_below_only_when_baseline_clears_floor():
    # Findings floor = 3. A busy desk (mean 5) going silent → 'below'.
    est = db.estimate_baseline(
        [5.0] * 28, 0.0, min_current=db.MIN_CURRENT_FINDINGS
    )
    assert est.deviation == "below"
    # A perennially-quiet desk (mean 1 < floor 3) dropping to 0 is NOT a signal.
    quiet = db.estimate_baseline(
        [1.0] * 28, 0.0, min_current=db.MIN_CURRENT_FINDINGS
    )
    assert quiet.deviation == "within"


def test_estimate_poisson_floor_prevents_band_collapse():
    """A perfectly steady desk (sample stddev 0) still gets a sensible band from
    the sqrt(mean) Poisson floor — the one robustness gain over the trigger's
    raw stddev."""
    est = db.estimate_baseline(
        [4.0] * 28, 4.0, min_current=db.MIN_CURRENT_SIGNALS
    )
    assert est.robust_sigma == pytest.approx(2.0)  # sqrt(4), NOT 0
    assert est.band_high == pytest.approx(8.0)     # 4 + 2*2


def test_estimate_insufficient_history_flag():
    # Events on only 2 of 28 days → below MIN_ACTIVE_DAYS (3) → thin.
    counts = [3.0, 3.0] + [0.0] * 26
    est = db.estimate_baseline(counts, 0.0, min_current=db.MIN_CURRENT_FINDINGS)
    assert est.active_days == 2
    assert est.insufficient_history is True


def test_estimate_is_deterministic():
    counts = [2.0, 7.0, 0.0, 3.0, 9.0, 1.0, 4.0]
    a = db.estimate_baseline(counts, 5.0, min_current=db.MIN_CURRENT_SIGNALS)
    b = db.estimate_baseline(counts, 5.0, min_current=db.MIN_CURRENT_SIGNALS)
    assert a == b


@pytest.mark.parametrize(
    "baseline,current",
    [
        ([1.0] * 28, 15.0),   # above
        ([0.0] * 28, 2.0),    # floor-guarded within
        ([5.0] * 7, 5.0),     # within
        ([3.0] * 28, 30.0),   # above
    ],
)
def test_estimate_above_matches_trigger_baseline_exceeds(baseline, current):
    """The 'above' call is byte-for-byte the trigger's exceedance test over the
    SAME robust sigma — the persistent baseline can never disagree with the
    ephemeral trigger about what 'above' means."""
    import statistics as _stats
    import math as _math

    mean = _stats.fmean(baseline)
    sample_sigma = _stats.stdev(baseline) if len(baseline) >= 2 else 0.0
    robust_sigma = max(sample_sigma, _math.sqrt(mean) if mean > 0 else 0.0)
    est = db.estimate_baseline(
        baseline, current, min_current=db.MIN_CURRENT_SIGNALS
    )
    expected_above = ats.baseline_exceeds(
        current, mean, robust_sigma,
        min_current=float(db.MIN_CURRENT_SIGNALS),
        n_sigma=db.DEFAULT_N_SIGMA,
    )
    assert (est.deviation == "above") == expected_above


# ---------------------------------------------------------------------------
# Pure — build_record + summary honesty
# ---------------------------------------------------------------------------


def test_build_record_carries_features_and_spillover():
    rec = db.build_record(
        "country_g20_us",
        ["US"],
        db.METRIC_SIGNAL_VOLUME,
        [15.0] + [1.0] * 28,
        n_sigma=db.DEFAULT_N_SIGMA,
        baseline_days=28,
        min_current=float(db.MIN_CURRENT_SIGNALS),
        spillover_current=7.0,
        neighbors=["country_g20_ca", "country_g20_mx"],
        hours_since_last_high_sev=12.5,
        now=datetime.now(timezone.utc),
    )
    assert rec.current == 15.0
    assert rec.deviation == "above"
    assert rec.spillover_current == 7.0
    assert rec.features["spillover_neighbors"] == [
        "country_g20_ca",
        "country_g20_mx",
    ]
    assert rec.features["neighbor_count"] == 2
    assert rec.features["hours_since_last_high_sev"] == 12.5
    assert rec.features["lag_1"] == 1.0
    assert rec.key == "country_g20_us|signal_volume_24h"


def _rec(desk, metric, deviation, *, current, expected, sigma, thin=False):
    return db.DeskBaseline(
        desk_id=desk, metric=metric, geo=["US"], baseline_days=28,
        n_sigma=2.0, expected=expected, center_median=expected,
        robust_sigma=2.0, band_low=0.0, band_high=expected + 4.0,
        current=current, deviation=deviation, deviation_sigma=sigma,
        min_current_floor=10.0, sample_days=28, active_days=(0 if thin else 28),
        insufficient_history=thin, spillover_current=0.0,
    )


def test_build_summary_is_honest_and_not_a_forecast():
    records = [
        _rec("d_a", db.METRIC_SIGNAL_VOLUME, "above", current=40, expected=5, sigma=8.0),
        _rec("d_b", db.METRIC_HIGH_SEV_FINDINGS, "below", current=0, expected=6, sigma=-3.0),
        _rec("d_c", db.METRIC_SIGNAL_VOLUME, "within", current=5, expected=5, sigma=0.0),
        _rec("d_d", db.METRIC_HIGH_SEV_FINDINGS, "within", current=0, expected=0, sigma=None, thin=True),
    ]
    fp = db.build_summary(records, baseline_days=28, n_sigma=2.0)
    data = fp.data
    assert data["not_a_forecast"] is True
    assert "honesty_note" in data and "NOT a forecast" in data["honesty_note"]
    assert data["above_band"] == 1
    assert data["below_band"] == 1
    assert data["insufficient_history"] == 1
    # No skill/Brier/prediction vocabulary is asserted anywhere.
    assert "brier" not in data and "skill" not in data
    # The most-deviating list ranks by |sigma| and names the desk + metric.
    top = data["top_deviating"]
    assert top and top[0]["desk_id"] == "d_a"  # |8| ahead of |-3|
    assert {t["desk_id"] for t in top} == {"d_a", "d_b"}


def test_build_summary_empty_is_honest():
    fp = db.build_summary([], baseline_days=28, n_sigma=2.0)
    assert "no g20/watch desks" in fp.title
    assert fp.data["desks_metrics"] == 0
    assert fp.data["not_a_forecast"] is True


# ---------------------------------------------------------------------------
# Route — /eval/desk_baselines divergence surfacing (fake conn)
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, rows=None, raise_exc=False):
        self._rows = rows or []
        self._raise = raise_exc
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        if self._raise:
            raise RuntimeError("relation \"desk_baselines\" does not exist")
        return self._rows


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _fake_deps(conn):
    pg = SimpleNamespace(acquire=lambda: _FakeAcquire(conn))
    return SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pg))


def _endpoint(deps):
    from legba.data.registry.v3_api import build_v3_router

    router = build_v3_router(deps=deps)
    (route,) = [
        r for r in router.routes  # type: ignore[attr-defined]
        if r.path == "/eval/desk_baselines"
    ]
    return route.endpoint


def _row(desk, metric, deviation, *, current, sigma, geo='["US"]', thin=False):
    return {
        "desk_id": desk,
        "metric": metric,
        "geo": geo,  # jsonb arrives as str — exercises the reducer's _jsonish
        "baseline_days": 28,
        "n_sigma": 2.0,
        "expected": 5.0,
        "center_median": 5.0,
        "robust_sigma": 2.0,
        "band_low": 0.5,
        "band_high": 9.5,
        "current": current,
        "deviation": deviation,
        "deviation_sigma": sigma,
        "min_current_floor": 10.0,
        "sample_days": 28,
        "active_days": 0 if thin else 28,
        "insufficient_history": thin,
        "spillover_current": 3.0,
        "features": {"lag_1": 1.0, "neighbor_count": 2},
        "computed_at": datetime(2026, 7, 24, tzinfo=timezone.utc),
    }


def test_eval_desk_baselines_route_registered():
    from legba.data.registry.v3_api import build_v3_router

    router = build_v3_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/eval/desk_baselines" in paths


def test_desk_baseline_board_absent_defaults():
    from legba.data.registry.v3_api import DeskBaselineBoard

    b = DeskBaselineBoard(available=False)
    assert b.available is False
    assert b.rows == []
    assert "NOT a forecast" in b.note


async def test_eval_desk_baselines_projects_rows_and_counts():
    conn = _FakeConn(rows=[
        _row("country_g20_us", db.METRIC_SIGNAL_VOLUME, "above", current=40, sigma=8.0),
        _row("country_g20_ca", db.METRIC_HIGH_SEV_FINDINGS, "within", current=0, sigma=0.0, thin=True),
    ])
    board = await _endpoint(_fake_deps(conn))(principal="t")
    assert board.available is True
    assert board.counts == {
        "total": 2, "above": 1, "below": 0, "insufficient_history": 1,
    }
    assert board.computed_at == "2026-07-24T00:00:00+00:00"
    assert "NOT a forecast" in board.note
    first = board.rows[0]
    assert first.desk_id == "country_g20_us"
    assert first.deviation == "above"
    assert first.geo == ["US"]                 # jsonb str parsed
    assert first.features["neighbor_count"] == 2
    # The SQL orders most-deviating first (surfaced honestly).
    sql = conn.queries[0][0]
    assert "deviation <> 'within'" in sql and "abs(deviation_sigma) DESC" in sql


async def test_eval_desk_baselines_filters_build_where():
    conn = _FakeConn(rows=[])
    await _endpoint(_fake_deps(conn))(
        desk="country_g20_us", deviating_only=True, principal="t"
    )
    sql, args = conn.queries[0]
    assert "desk_id = $1" in sql
    assert "deviation <> 'within'" in sql
    assert args == ("country_g20_us",)


async def test_eval_desk_baselines_empty_is_available_false():
    board = await _endpoint(_fake_deps(_FakeConn(rows=[])))(principal="t")
    assert board.available is False
    assert board.rows == []


async def test_eval_desk_baselines_read_failure_is_honest_empty():
    board = await _endpoint(_fake_deps(_FakeConn(raise_exc=True)))(principal="t")
    assert board.available is False


# ---------------------------------------------------------------------------
# Ephemeral-DB rig
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    """A clean desk universe + empty sidecar so each test's compute sees ONLY
    its own desks (the session DB is per-session + disposable)."""
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM desk_baselines")
        await conn.execute("DELETE FROM target_descriptors")
        await conn.execute("DELETE FROM signals WHERE source_id = 'test_p3_7_src'")
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id IN "
            "('desk_baseline', 'test_p3_7_escalation')"
        )
    yield


class _Deps:
    def __init__(self, pool):
        self.pg_pool = pool
        self.extras = {}


async def _run(pool, **opts):
    options = {
        "sub_handler": "desk_baseline",
        "analyst_id": "desk_baseline",
        "run_id": str(uuid4()),
        **opts,
    }
    result = await db.handle([], options, _Deps(pool))
    assert isinstance(result, AnalystMethodResult)
    return result


async def _insert_desk(conn, desk, geo, tags=("g20",)):
    body = {"scope": {"geo": list(geo), "tags": list(tags)}}
    await conn.execute(
        "INSERT INTO target_descriptors (descriptor_id, version, schema_uri, "
        "  is_head, state, owner, name, body) "
        "VALUES ($1, 'v1', 'legba/target/2.0.0', TRUE, 'active', 'test_p3_7', "
        "        $1, $2::jsonb) ON CONFLICT DO NOTHING",
        desk,
        json.dumps(body),
    )


async def _insert_signals(conn, geo, n, *, days_ago=0.0):
    for _ in range(n):
        await conn.execute(
            "INSERT INTO signals (id, source_id, geo, fetched_at, content_hash) "
            "VALUES ($1, 'test_p3_7_src', $2::text[], "
            "        now() - make_interval(secs => $3), $4)",
            uuid4(),
            list(geo),
            days_ago * 86400.0,
            uuid4().hex,
        )


async def _insert_finding(conn, desk, *, sev="high", days_ago=0.0):
    fid = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, severity, data, target_id, "
        "   analyst_id, produced_at, schema_uri) "
        "VALUES ($1, 'finding', $2, '', 0.9, $3, $4::jsonb, $5, "
        "        'test_p3_7_escalation', now() - make_interval(secs => $6), "
        "        'iglu:legba/finding/jsonschema/1-0-0')",
        fid,
        f"finding {fid}",
        sev,
        json.dumps({"tags": [f"severity:{sev}"]}),
        desk,
        days_ago * 86400.0,
    )
    return fid


async def _baseline_rows(conn, desk):
    return {
        r["metric"]: r
        for r in await conn.fetch(
            "SELECT * FROM desk_baselines WHERE desk_id = $1", desk
        )
    }


# ---------------------------------------------------------------------------
# DB — compute → store → deviation
# ---------------------------------------------------------------------------


async def test_compute_store_and_signal_deviation(pg_pool, clean_slate):
    desk = "country_g20_us"
    geo = ["US"]
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, geo)
        # Trailing baseline: 1 signal/day for the previous 28 days; current
        # window quiet.
        for day in range(1, 29):
            await _insert_signals(conn, geo, 1, days_ago=day + 0.5)

    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _baseline_rows(conn, desk)
    # Both metrics stored; signal baseline within-band while quiet.
    assert db.METRIC_SIGNAL_VOLUME in rows
    assert db.METRIC_HIGH_SEV_FINDINGS in rows
    sig = rows[db.METRIC_SIGNAL_VOLUME]
    assert sig["current"] == pytest.approx(0.0)
    assert sig["deviation"] == "within"
    assert sig["expected"] == pytest.approx(1.0, abs=0.05)
    assert sig["baseline_days"] == 28

    # Spike the current 24h window well over the absolute floor (>=10).
    async with pg_pool.acquire() as conn:
        await _insert_signals(conn, geo, 15, days_ago=0.01)
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _baseline_rows(conn, desk)
    sig = rows[db.METRIC_SIGNAL_VOLUME]
    assert sig["current"] == pytest.approx(15.0)
    assert sig["deviation"] == "above"
    assert sig["deviation_sigma"] is not None and sig["deviation_sigma"] > 2.0


async def test_high_sev_finding_deviation(pg_pool, clean_slate):
    desk = "country_g20_br"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["BR"])
        # Baseline high-sev findings across many days (a genuinely active desk).
        for day in range(1, 29):
            await _insert_finding(conn, desk, sev="high", days_ago=day + 0.5)
        # Current window: a spike over the high-sev floor (>=3).
        for _ in range(6):
            await _insert_finding(conn, desk, sev="critical", days_ago=0.02)

    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _baseline_rows(conn, desk)
    fnd = rows[db.METRIC_HIGH_SEV_FINDINGS]
    assert fnd["current"] == pytest.approx(6.0)
    assert fnd["deviation"] == "above"
    assert fnd["insufficient_history"] is False


# ---------------------------------------------------------------------------
# DB — zero / insufficient-history summary honesty
# ---------------------------------------------------------------------------


async def test_insufficient_history_and_summary_honesty(pg_pool, clean_slate):
    desk = "country_watch_ht"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, ["HT"], tags=("watch",))
        # No signals, no findings at all — nothing to be a baseline of.

    result = await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _baseline_rows(conn, desk)
    # Both metrics stored, both flagged thin-history, none deviating.
    assert rows[db.METRIC_SIGNAL_VOLUME]["insufficient_history"] is True
    assert rows[db.METRIC_HIGH_SEV_FINDINGS]["insufficient_history"] is True
    assert rows[db.METRIC_SIGNAL_VOLUME]["deviation"] == "within"

    # The summary states the honest thin state and NEVER claims a forecast.
    data = result.finding.data
    assert data["not_a_forecast"] is True
    assert "honesty_note" in data
    assert data["insufficient_history"] >= 2
    assert data["above_band"] == 0 and data["below_band"] == 0
    # The finding is a genuine (structural) FINDING carrying the distribution.
    assert result.finding.confidence == 1.0
    assert "desk_baseline" in result.finding.tags


# ---------------------------------------------------------------------------
# DB — neighbour-desk spillover feature
# ---------------------------------------------------------------------------


async def test_spillover_from_adjacent_desk(pg_pool, clean_slate):
    # US and CA are adjacent desks; CA has current-window signal volume.
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, "country_g20_us", ["US"])
        await _insert_desk(conn, "country_g20_ca", ["CA"])
        await _insert_signals(conn, ["CA"], 5, days_ago=0.02)

    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        us = await _baseline_rows(conn, "country_g20_us")
    sig = us[db.METRIC_SIGNAL_VOLUME]
    # US's spillover = CA's current signal volume (5); CA is named as a neighbour.
    assert sig["spillover_current"] == pytest.approx(5.0)
    feats = sig["features"]
    feats = json.loads(feats) if isinstance(feats, str) else feats
    assert "country_g20_ca" in feats["spillover_neighbors"]


# ---------------------------------------------------------------------------
# DB — wholesale refresh prunes desks no longer present
# ---------------------------------------------------------------------------


async def test_store_prunes_removed_desks(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, "country_g20_za", ["ZA"])
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _baseline_rows(conn, "country_g20_za")
        # Retire the desk out of the g20/watch set, add a different one.
        await conn.execute("DELETE FROM target_descriptors")
        await _insert_desk(conn, "country_g20_in", ["IN"])
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert not await _baseline_rows(conn, "country_g20_za")  # pruned
        assert await _baseline_rows(conn, "country_g20_in")      # present
