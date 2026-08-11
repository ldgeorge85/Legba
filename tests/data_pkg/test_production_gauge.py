# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-1 — the expected-vs-actual production gauge.

Two tiers.

**Pure** (no DB): the expectation model itself — cron-derived cadence bars,
trailing-baseline drought bars, the severity ramp, and every quiet-by-design
exemption. These are the tests that keep the gauge PRECISE; each one encodes a
specific way an earlier watch would have false-alarmed.

**Acceptance** (ephemeral DB): the three documented incidents from
ENGINE_REVIEW_2026-08-02 replayed as substrate, asserting the gauge would have
caught each. These are the reason the module exists and they are written as
history, not as synthetic shapes:

  1. **The frozen AP feeds.** Five ``rsshub.apnews.*`` sources polled ~130
     times over six days, every poll ``outcome='success'`` with
     ``health_state='healthy'``, writing zero signals. No alert ever fired.
     The fixture reproduces the exact mechanism — including the bumped
     ``fetched_at`` that made the existing watchdog read them as seconds
     fresh — and asserts the gauge fires anyway because it keys on
     ``created_at``.
  2. **The 38h journal death.** ``journal_assessor`` on a 12h cadence went
     silent for 38 hours behind a moved actor id and nothing paged.
  3. **Forecast resolution, 0 of 38 ever.** Overdue ``acute_forecasts`` rows
     with ``forecast_scoreboard`` running daily and resolving nothing, errors
     swallowed at DEBUG.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.registry import production_gauge as pg

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
CFG = pg.GaugeConfig()


# ---------------------------------------------------------------------------
# Pure — the severity ramp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ratio,severity",
    [
        (0.0, "low"),
        (0.99, "low"),
        (1.0, "medium"),
        (1.99, "medium"),
        (2.0, "high"),
        (3.99, "high"),
        (4.0, "critical"),
        (40.0, "critical"),
    ],
)
def test_severity_ramp(ratio, severity):
    assert pg.severity_for_ratio(ratio) == severity


def test_only_medium_and_worse_pages():
    """The precision floor: a deficit under `medium` is a number, not a page."""
    low = pg.LoopGauge(
        loop_class=pg.LOOP_SOURCE_PRODUCTION,
        loop_id="s",
        label="s",
        state="deficit",
        severity="low",
    )
    med = pg.LoopGauge(
        loop_class=pg.LOOP_SOURCE_PRODUCTION,
        loop_id="s",
        label="s",
        state="deficit",
        severity="medium",
    )
    ok = pg.LoopGauge(
        loop_class=pg.LOOP_SOURCE_PRODUCTION,
        loop_id="s",
        label="s",
        state="ok",
        severity="critical",
    )
    assert low.pages is False
    assert med.pages is True
    # State wins over severity — an `ok` row never pages whatever its rung.
    assert ok.pages is False


# ---------------------------------------------------------------------------
# Pure — analyst cadence
# ---------------------------------------------------------------------------


def _cadence_row(**over):
    row = {
        "analyst_id": "some_analyst",
        "state": "active",
        "cron": "0 0,12 * * *",  # every 12h
        "last_run_at": NOW - timedelta(hours=2),
        "head_created_at": NOW - timedelta(days=30),
        "runs": 40,
        "failed_runs": 0,
    }
    row.update(over)
    return row


def test_cadence_ok_inside_the_bar():
    g = pg.judge_analyst_cadence(_cadence_row(), now=NOW, cfg=CFG)
    assert g.state == "ok"
    assert g.loop_class == pg.LOOP_ANALYST_CADENCE
    assert g.evidence["interval_minutes"] == 720.0


def test_cadence_deficit_needs_three_whole_intervals():
    """The bar is 3 intervals, not 2 — deliberately SLOWER than the liveness
    watchdog's 2x edge alert, so this tier means "still dead three periods
    later" rather than duplicating a page the watchdog already sent."""
    just_under = pg.judge_analyst_cadence(
        _cadence_row(last_run_at=NOW - timedelta(hours=35)), now=NOW, cfg=CFG
    )
    just_over = pg.judge_analyst_cadence(
        _cadence_row(last_run_at=NOW - timedelta(hours=37)), now=NOW, cfg=CFG
    )
    assert just_under.state == "ok"
    assert just_over.state == "deficit"
    assert just_over.severity == "medium"


def test_cadence_escalates_with_multiples_of_the_bar():
    g = pg.judge_analyst_cadence(
        _cadence_row(last_run_at=NOW - timedelta(days=7)), now=NOW, cfg=CFG
    )
    assert g.state == "deficit"
    assert g.severity == "critical"
    assert g.evidence["missed_periods"] == pytest.approx(14.0, rel=0.01)


def test_cadence_absolute_floor_protects_a_fast_analyst():
    """A */10 analyst's 3 intervals is 30 minutes — far too twitchy. The
    absolute floor (3h) governs instead, so jitter and a late cooldown cannot
    page."""
    row = _cadence_row(cron="*/10 * * * *", last_run_at=NOW - timedelta(hours=1))
    g = pg.judge_analyst_cadence(row, now=NOW, cfg=CFG)
    assert g.state == "ok"
    assert g.evidence["bar_minutes"] == CFG.analyst_min_absence_minutes


@pytest.mark.parametrize("state", ["draft", "configured", "paused", "retired"])
def test_cadence_quiet_when_not_active(state):
    g = pg.judge_analyst_cadence(_cadence_row(state=state), now=NOW, cfg=CFG)
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_NOT_ACTIVE
    # An ungauged loop NEVER carries a ratio — no reader can mistake "no
    # expectation" for a measured 0.0.
    assert g.ratio is None


def test_cadence_quiet_for_on_demand_analysts():
    """consult_default / deep_consult declare no cron. No promise, no debt."""
    g = pg.judge_analyst_cadence(_cadence_row(cron=None), now=NOW, cfg=CFG)
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_NO_CADENCE


def test_cadence_unparsable_cron_is_ungauged_never_ok():
    g = pg.judge_analyst_cadence(
        _cadence_row(cron="not a cron"), now=NOW, cfg=CFG
    )
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_UNPARSABLE_CADENCE


def test_cadence_never_ran_gets_an_activation_grace_then_fires():
    """A descriptor registered a minute ago has not had its chance yet; one
    registered a week ago and never run is a real, and severe, deficit."""
    fresh = pg.judge_analyst_cadence(
        _cadence_row(last_run_at=None, head_created_at=NOW - timedelta(hours=1)),
        now=NOW,
        cfg=CFG,
    )
    assert fresh.state == "ungauged"
    assert fresh.quiet_reason == pg.QUIET_ACTIVATION_GRACE

    stale = pg.judge_analyst_cadence(
        _cadence_row(last_run_at=None, head_created_at=NOW - timedelta(days=7)),
        now=NOW,
        cfg=CFG,
    )
    assert stale.state == "deficit"
    assert stale.severity == "critical"
    assert stale.evidence["never_ran"] is True
    assert "NEVER ran" in stale.actual


# ---------------------------------------------------------------------------
# Pure — analyst production
# ---------------------------------------------------------------------------


def _production_row(**over):
    row = {
        "analyst_id": "some_analyst",
        "state": "active",
        "gather_only": False,
        "runs": 100,
        "failed_runs": 0,
        "producing_runs": 50,          # writes a row every other run
        "last_producing_run_at": NOW - timedelta(hours=1),
        "runs_since_production": 2,
    }
    row.update(over)
    return row


def test_production_ok_within_its_own_rate():
    g = pg.judge_analyst_production(_production_row(), now=NOW, cfg=CFG)
    assert g.state == "ok"
    assert g.evidence["runs_per_output"] == 2.0
    assert g.evidence["bar_runs"] == 12.0  # 6x its own rate


def test_production_drought_is_relative_to_the_analysts_own_rate():
    """The same 20 barren runs is a drought for a per-run producer and
    perfectly normal for a sparse one. That relativity is the whole design —
    a global "must produce every N runs" would page situation_clustering
    forever."""
    dense = pg.judge_analyst_production(
        _production_row(producing_runs=100, runs_since_production=20),
        now=NOW,
        cfg=CFG,
    )
    sparse = pg.judge_analyst_production(
        _production_row(runs=1000, producing_runs=5, runs_since_production=20),
        now=NOW,
        cfg=CFG,
    )
    assert dense.state == "deficit"
    assert sparse.state == "ok"


def test_production_never_produced_reads_trace_only_not_deficit():
    """The auto-exemption that covers every side-effect sweep in the fleet —
    cross_source_dedup, integrity_sweep, alert_trigger_scan itself — WITHOUT
    naming any of them. A maintained list would rot; observation cannot."""
    g = pg.judge_analyst_production(
        _production_row(producing_runs=0, last_producing_run_at=None),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_TRACE_ONLY
    assert g.evidence["runs_in_window"] == 100


def test_production_gather_only_is_quiet_by_descriptor():
    """A gather_only analyst NOOPs whenever there is nothing to gather; its
    own descriptor says so, so the exemption is data-driven too."""
    g = pg.judge_analyst_production(
        _production_row(gather_only=True, runs_since_production=999),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_GATHER_ONLY


def test_production_thin_baseline_is_ungauged_not_a_deficit():
    g = pg.judge_analyst_production(
        _production_row(producing_runs=2, runs_since_production=500),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_INSUFFICIENT_HISTORY


def test_production_absolute_floor_protects_a_per_run_producer():
    """A 1-row-per-run analyst's 6x bar is 6 runs; the floor keeps it from
    paging after two missed rows."""
    g = pg.judge_analyst_production(
        _production_row(producing_runs=100, runs_since_production=4),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "ok"


# ---------------------------------------------------------------------------
# Pure — source production
# ---------------------------------------------------------------------------


def _source_row(**over):
    row = {
        "source_id": "source.example.feed",
        "state": "active",
        "cron": "17 * * * *",  # hourly
        "head_created_at": NOW - timedelta(days=60),
        "signals": 100,
        "last_created_at": NOW - timedelta(hours=1),
        "max_gap_minutes": 240.0,
        "polls_ok": 400,
        "polls_error": 0,
        "polls_since_production": 1,
    }
    row.update(over)
    return row


def test_source_ok_within_its_own_worst_gap():
    g = pg.judge_source_production(_source_row(), now=NOW, cfg=CFG)
    assert g.state == "ok"
    assert g.evidence["sub_state"] == "drought"


def test_source_drought_bar_rises_with_a_bursty_feeds_own_history():
    """USGS earthquakes and NASA disaster feeds are legitimately bursty. Their
    OWN worst observed gap raises their bar, so the same 4-day silence is a
    deficit for a steady wire and nothing at all for an event feed."""
    steady = pg.judge_source_production(
        _source_row(
            max_gap_minutes=120.0, last_created_at=NOW - timedelta(days=4)
        ),
        now=NOW,
        cfg=CFG,
    )
    bursty = pg.judge_source_production(
        _source_row(
            max_gap_minutes=4_320.0,  # a 3-day quiet spell is normal for it
            last_created_at=NOW - timedelta(days=4),
        ),
        now=NOW,
        cfg=CFG,
    )
    assert steady.state == "deficit"
    assert bursty.state == "ok"


def test_source_absolute_floor_no_page_inside_a_day():
    """However tight a feed's own history, nothing pages on a few hours."""
    g = pg.judge_source_production(
        _source_row(max_gap_minutes=1.0, last_created_at=NOW - timedelta(hours=20)),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "ok"
    assert g.evidence["bar_minutes"] == CFG.source_min_drought_minutes


def test_source_cadence_floor_covers_a_single_backfill_burst():
    """The AP shape in miniature: every signal arrived in one burst, so the
    observed max gap is ~0 and the gap statistic alone would set a bar of
    zero. The declared poll cadence floors it instead."""
    g = pg.judge_source_production(
        _source_row(
            max_gap_minutes=0.0,
            signals=39,
            last_created_at=NOW - timedelta(days=6),
        ),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "deficit"
    # 24 hourly intervals = 1440 min, above the 1-day absolute floor.
    assert g.evidence["bar_minutes"] == 1440.0
    assert g.severity == "critical"


def test_source_silent_substate_for_a_source_that_never_produced():
    g = pg.judge_source_production(
        _source_row(signals=0, last_created_at=None, polls_ok=75),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "deficit"
    assert g.evidence["sub_state"] == "silent"
    assert "SILENT" in g.actual


def test_source_silent_needs_enough_polls_first():
    g = pg.judge_source_production(
        _source_row(signals=0, last_created_at=None, polls_ok=3),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_INSUFFICIENT_HISTORY


def test_a_freshly_registered_source_gets_an_activation_grace():
    """A feed's first item can legitimately be a day out; registering a source
    at 9am must not page at noon. The grace is the SAME floor the drought
    branch uses, so a new source and an old one are held to one patience."""
    fresh = pg.judge_source_production(
        _source_row(
            signals=0,
            last_created_at=None,
            polls_ok=60,
            head_created_at=NOW - timedelta(hours=6),
        ),
        now=NOW,
        cfg=CFG,
    )
    assert fresh.state == "ungauged"
    assert fresh.quiet_reason == pg.QUIET_ACTIVATION_GRACE

    settled = pg.judge_source_production(
        _source_row(
            signals=0,
            last_created_at=None,
            polls_ok=60,
            head_created_at=NOW - timedelta(days=5),
        ),
        now=NOW,
        cfg=CFG,
    )
    assert settled.state == "deficit"
    assert settled.evidence["sub_state"] == "silent"


def test_forecast_overdue_keys_on_resolution_not_outcome():
    """A voided forecast (resolved_by 'voided:*') has resolved_at set and NO
    outcome. Keying overdue on the outcome column counted every void as
    permanently overdue — a false CRITICAL that paged for weeks (2026-08-09)."""
    fc = next(
        b for b in pg.BACKLOG_DRAINS if b.backlog_id == "acute_forecast_resolution"
    )
    assert "resolved_at IS NULL" in fc.overdue_sql
    assert "resolved_outcome" not in fc.overdue_sql


def test_source_with_no_polls_at_all_pages_instead_of_vanishing():
    """The gdelt.doc_api shape (2026-08-09): an ACTIVE descriptor whose actor
    stopped polling entirely used to slide into ungauged/insufficient-history
    and disappear. The descriptor says run; nothing running must page."""
    g = pg.judge_source_production(
        _source_row(polls_ok=0, polls_error=0, signals=0, last_created_at=None),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "deficit"
    assert g.evidence["sub_state"] == "no_polls"

    fresh = pg.judge_source_production(
        _source_row(
            polls_ok=0,
            polls_error=0,
            signals=0,
            last_created_at=None,
            head_created_at=NOW - timedelta(hours=2),
        ),
        now=NOW,
        cfg=CFG,
    )
    assert fresh.state == "ungauged"
    assert fresh.quiet_reason == pg.QUIET_ACTIVATION_GRACE


def test_source_sparse_veteran_is_upstream_quiet_not_silent():
    """The eia.press shape: a ~monthly publisher has zero signals in ANY 21d
    window, but the poll rows prove the FEED's newest entry is one we already
    ingested. Upstream quiet — not our deficit."""
    life_last = NOW - timedelta(days=33)
    g = pg.judge_source_production(
        _source_row(
            signals=0,
            last_created_at=None,
            polls_ok=80,
            lifetime_signals=9,
            lifetime_last_created_at=life_last,
            feed_newest_entry_ts=life_last - timedelta(hours=5),
        ),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "ok"
    assert g.evidence["sub_state"] == "upstream_quiet"


def test_source_sparse_veteran_with_fresh_feed_content_is_a_conversion_stall():
    """Same sparse veteran, but the feed DOES carry entries newer than our
    last ingest and healthy polls converted none of them. That is the real
    fault the silent page was invented for — it must still fire."""
    g = pg.judge_source_production(
        _source_row(
            signals=0,
            last_created_at=None,
            polls_ok=80,
            lifetime_signals=9,
            lifetime_last_created_at=NOW - timedelta(days=33),
            feed_newest_entry_ts=NOW - timedelta(days=2),
        ),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "deficit"
    assert g.evidence["sub_state"] == "conversion_stall"


def test_source_weekend_drought_is_upstream_quiet_when_feed_holds_nothing_new():
    """The weekday-publisher weekend (2026-08-09): a would-be drought deficit
    is honest quiet when the feed's own newest entry is the one we already
    ingested."""
    last = NOW - timedelta(days=4)
    would_page = pg.judge_source_production(
        _source_row(max_gap_minutes=120.0, last_created_at=last),
        now=NOW,
        cfg=CFG,
    )
    assert would_page.state == "deficit"  # control: this shape pages…

    quiet = pg.judge_source_production(
        _source_row(
            max_gap_minutes=120.0,
            last_created_at=last,
            feed_newest_entry_ts=last - timedelta(minutes=30),
        ),
        now=NOW,
        cfg=CFG,
    )
    assert quiet.state == "ok"  # …until the feed observation vouches for it
    assert quiet.evidence["sub_state"] == "upstream_quiet"


def test_source_erroring_feed_is_the_watchdogs_beat_not_ours():
    """An erroring source already pages through source_cadence_stall /
    source_degraded. Reporting it here too would be one fault under two
    names."""
    g = pg.judge_source_production(
        _source_row(
            polls_ok=10,
            polls_error=90,
            last_created_at=NOW - timedelta(days=10),
        ),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_POLLING_ERRORS


@pytest.mark.parametrize("state", ["draft", "configured", "paused", "retired"])
def test_source_quiet_when_not_active(state):
    """The operator pausing a broken feed must SILENCE it, not keep paging."""
    g = pg.judge_source_production(
        _source_row(state=state, last_created_at=NOW - timedelta(days=30)),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_NOT_ACTIVE


def test_source_thin_history_is_ungauged():
    g = pg.judge_source_production(
        _source_row(signals=2, last_created_at=NOW - timedelta(days=15)),
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_INSUFFICIENT_HISTORY


# ---------------------------------------------------------------------------
# Pure — backlog drain
# ---------------------------------------------------------------------------

_DRAIN = pg.BACKLOG_DRAINS[0]


def test_backlog_quiet_when_nothing_is_overdue():
    g = pg.judge_backlog_drain(
        _DRAIN, {"overdue": 0, "resolved": 0, "owner_runs": 20}, now=NOW, cfg=CFG
    )
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_NO_BACKLOG


def test_backlog_deficit_when_the_owner_runs_and_nothing_drains():
    g = pg.judge_backlog_drain(
        _DRAIN,
        {
            "overdue": 19,
            "resolved": 0,
            "owner_runs": 21,
            "oldest_due_at": NOW - timedelta(days=28),
        },
        now=NOW,
        cfg=CFG,
    )
    assert g.state == "deficit"
    assert g.severity == "critical"
    assert g.evidence["overdue"] == 19
    assert g.evidence["oldest_overdue_age_days"] == 28.0


def test_backlog_ok_once_anything_drains():
    g = pg.judge_backlog_drain(
        _DRAIN, {"overdue": 5, "resolved": 1, "owner_runs": 20}, now=NOW, cfg=CFG
    )
    assert g.state == "ok"


def test_backlog_defers_to_the_cadence_class_when_the_owner_is_not_running():
    """If the resolver is not firing that is a CADENCE deficit under its own
    analyst id. Attributing it here as well would report one fault twice."""
    g = pg.judge_backlog_drain(
        _DRAIN, {"overdue": 19, "resolved": 0, "owner_runs": 0}, now=NOW, cfg=CFG
    )
    assert g.state == "ungauged"
    assert g.quiet_reason == pg.QUIET_OWNER_NOT_RUNNING


# ---------------------------------------------------------------------------
# Pure — config
# ---------------------------------------------------------------------------


def test_config_from_options_coerces_and_ignores_junk():
    cfg = pg.GaugeConfig.from_options(
        {"window_days": "7", "source_gap_multiple": 9, "nonsense": 1}
    )
    assert cfg.window_days == 7
    assert cfg.source_gap_multiple == 9.0


def test_config_bad_value_keeps_the_default_rather_than_exploding():
    """A mistyped knob must not take the whole gauge offline."""
    cfg = pg.GaugeConfig.from_options({"window_days": "not-a-number"})
    assert cfg.window_days == pg.GaugeConfig().window_days


def test_report_totals_separate_gauged_from_ungauged():
    """"We cannot say" and "it is fine" are different statements; blurring
    them into one health percentage is how the twelve got missed."""
    report = pg.GaugeReport(
        generated_at=NOW,
        window_days=21,
        loops=[
            pg.LoopGauge(pg.LOOP_SOURCE_PRODUCTION, "a", "a", "deficit", "high"),
            pg.LoopGauge(pg.LOOP_SOURCE_PRODUCTION, "b", "b", "ok"),
            pg.LoopGauge(
                pg.LOOP_ANALYST_CADENCE, "c", "c", "ungauged",
                quiet_reason=pg.QUIET_NOT_ACTIVE,
            ),
        ],
    )
    totals = report.totals()
    assert totals == {
        "loops": 3,
        "gauged": 2,
        "ok": 1,
        "deficit": 1,
        "ungauged": 1,
        "paging": 1,
        "by_severity": {"high": 1},
        "by_class": {
            pg.LOOP_SOURCE_PRODUCTION: {
                "gauged": 2, "ok": 1, "deficit": 1, "ungauged": 0,
            },
            pg.LOOP_ANALYST_CADENCE: {
                "gauged": 0, "ok": 0, "deficit": 0, "ungauged": 1,
            },
        },
    }


def test_junk_source_prefixes_match_the_ledgers():
    """The gauge must exclude exactly the scaffolding descriptors the
    source-quality ledger and /system/source-firing exclude, or it would
    manufacture permanent deficits out of registration templates."""
    from legba.data.registry.source_quality_api import JUNK_DESCRIPTOR_PREFIXES

    assert pg.JUNK_SOURCE_PREFIXES == JUNK_DESCRIPTOR_PREFIXES


def test_declared_backlogs_are_unique_and_named():
    ids = [d.backlog_id for d in pg.BACKLOG_DRAINS]
    assert len(ids) == len(set(ids))
    assert all(d.owner_analyst_id and d.label and d.unit for d in pg.BACKLOG_DRAINS)


# ---------------------------------------------------------------------------
# Ephemeral-DB rig
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def blank(pool):
    """An empty engine: no descriptors, no traces, no signals, no polls."""
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE analyst_descriptors, source_descriptors, analyst_traces, "
            "analyst_outputs, signals, source_poll_outcomes, acute_forecasts "
            "RESTART IDENTITY CASCADE"
        )
    yield


async def _register_source(conn, source_id: str, cron: str, state: str = "active"):
    body = {
        "identity": {"id": source_id, "kind": "rss", "state": state},
        "acquisition": "poll",
        "cadence": {"schedule": {"raw": cron, "ui_hint": {}}, "cooldown_seconds": 60},
    }
    await conn.execute(
        """
        INSERT INTO source_descriptors
            (descriptor_id, version, schema_uri, is_head, kind, state, owner,
             name, body, created_at)
        VALUES ($1, 'v1', 'legba/source/1.0.0', TRUE, 'rss', $2, 'test', $1,
                $3::jsonb, now() - interval '60 days')
        """,
        source_id,
        state,
        json.dumps(body),
    )


async def _register_analyst(
    conn, analyst_id: str, cron: str | None, state: str = "active", *, age_days=60
):
    body = {
        "identity": {"id": analyst_id, "kind": "deterministic", "state": state},
        "cadence": {"fallback_schedule": cron, "cooldown_seconds": 0},
        "subscription": {"substrate": {"direct_queries": True}},
    }
    await conn.execute(
        """
        INSERT INTO analyst_descriptors
            (descriptor_id, version, schema_uri, is_head, kind, state, owner,
             name, body, created_at)
        VALUES ($1, 'v1', 'legba/analyst/1.0.0', TRUE, 'deterministic', $2,
                'test', $1, $3::jsonb, now() - make_interval(days => $4))
        """,
        analyst_id,
        state,
        json.dumps(body),
        age_days,
    )


async def _trace(conn, analyst_id: str, started_at, *, output_refs=()):
    await conn.execute(
        """
        INSERT INTO analyst_traces
            (run_id, analyst_id, analyst_version, cadence_trigger,
             output_row_refs, status, run_started_at, run_ended_at, receipt_hash)
        VALUES ($1, $2, 'v1', 'cadence', $3::uuid[], 'success', $4, $4, $5)
        """,
        uuid4(),
        analyst_id,
        list(output_refs),
        started_at,
        uuid4().hex,
    )


async def _signal(conn, source_id: str, created_at, fetched_at):
    await conn.execute(
        """
        INSERT INTO signals
            (id, source_id, source_version, payload, content_hash, fetched_at,
             created_at, updated_at, schema_uri)
        VALUES ($1, $2, 'v1', '{}'::jsonb, $3, $4, $5, $5,
                'iglu:legba/signal/jsonschema/1-0-0')
        """,
        uuid4(),
        source_id,
        uuid4().hex,
        fetched_at,
        created_at,
    )


async def _poll(conn, source_id: str, occurred_at, *, outcome="success", written=0):
    await conn.execute(
        """
        INSERT INTO source_poll_outcomes
            (source_id, source_version, outcome, health_state, signals_written,
             occurred_at)
        VALUES ($1, 'v1', $2, 'healthy', $3, $4)
        """,
        source_id,
        outcome,
        written,
        occurred_at,
    )


# ---------------------------------------------------------------------------
# ACCEPTANCE CASE 1 — the frozen AP feeds (2026-07-28 .. 08-03)
# ---------------------------------------------------------------------------

_AP = "source.rsshub.apnews.taiwan"


async def _seed_frozen_ap_feed(conn, now: datetime) -> None:
    """The incident, reproduced exactly.

    39 signals arrived in one burst on 07-28, then 130 hourly polls over six
    days each recording ``outcome='success'``, ``health_state='healthy'``,
    ``signals_written=0`` — and each bumping ``fetched_at`` on the existing
    rows, which is what made the feed read as seconds-fresh to every reader
    keyed on that column.
    """
    await _register_source(conn, _AP, "13 * * * *")
    burst = now - timedelta(days=6)
    for i in range(39):
        await _signal(
            conn,
            _AP,
            created_at=burst + timedelta(minutes=i),
            # THE LIE: bumped by the most recent no-op poll.
            fetched_at=now - timedelta(minutes=5),
        )
    for h in range(130):
        await _poll(conn, _AP, occurred_at=now - timedelta(hours=h), written=0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acceptance_frozen_ap_feed_is_caught(pool, blank):
    """ACCEPTANCE 1. Six days frozen, 130 healthy polls, zero alerts ever.

    The gauge fires CRITICAL. Also asserted here: the exact reason the
    existing watchdog could not — ``max(fetched_at)`` reads minutes old while
    ``max(created_at)`` reads six days old, so a freshness check keyed on
    ``fetched_at`` is structurally incapable of seeing this.
    """
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        await _seed_frozen_ap_feed(conn, now)

        lied, truth = await conn.fetchrow(
            "SELECT max(fetched_at) AS a, max(created_at) AS b "
            "FROM signals WHERE source_id = $1",
            _AP,
        )
        assert (now - lied) < timedelta(hours=1), "fixture must reproduce the lie"
        assert (now - truth) > timedelta(days=5)

        report = await pg.read_gauge(conn)

    row = next(g for g in report.loops if g.loop_id == _AP)
    assert row.loop_class == pg.LOOP_SOURCE_PRODUCTION
    assert row.state == "deficit"
    assert row.severity == "critical"
    assert row.pages is True
    assert row.evidence["polls_ok"] == 130
    assert row.evidence["signals_in_window"] == 39
    # The verdict rests on row birth, never on the bumped timestamp.
    assert row.evidence["drought_minutes"] > 5 * 24 * 60


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acceptance_pausing_the_frozen_feed_silences_it(pool, blank):
    """The operator's remediation (B-0 ran ``ops_pause_apnews_frozen.py``) must
    STOP the paging, not keep it. Quiet-by-design is a real answer."""
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        await _seed_frozen_ap_feed(conn, now)
        await conn.execute(
            "UPDATE source_descriptors SET state = 'paused' WHERE descriptor_id = $1",
            _AP,
        )
        report = await pg.read_gauge(conn)

    row = next(g for g in report.loops if g.loop_id == _AP)
    assert row.state == "ungauged"
    assert row.quiet_reason == pg.QUIET_NOT_ACTIVE


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_healthy_feed_beside_the_frozen_one_stays_ok(pool, blank):
    """The precision half of acceptance 1: an identically-configured feed that
    is actually producing must NOT be swept up."""
    now = datetime.now(tz=timezone.utc)
    healthy = "source.example.healthy"
    async with pool.acquire() as conn:
        await _seed_frozen_ap_feed(conn, now)
        await _register_source(conn, healthy, "13 * * * *")
        for h in range(130):
            at = now - timedelta(hours=h)
            await _signal(conn, healthy, created_at=at, fetched_at=at)
            await _poll(conn, healthy, occurred_at=at, written=1)
        report = await pg.read_gauge(conn)

    frozen_row = next(g for g in report.loops if g.loop_id == _AP)
    healthy_row = next(g for g in report.loops if g.loop_id == healthy)
    assert frozen_row.state == "deficit"
    assert healthy_row.state == "ok"


# ---------------------------------------------------------------------------
# ACCEPTANCE CASE 2 — the 38h journal death
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acceptance_38h_journal_death_is_caught(pool, blank):
    """ACCEPTANCE 2. journal_assessor on ``0 0,12 * * *`` went silent 38h
    behind a moved actor id and no watchdog paged.

    The bar is 3 x 12h = 36h, so 38h clears it and 30h does not — an exact
    boundary, asserted both ways so a future threshold change cannot quietly
    drop the case this test exists for.
    """
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        await _register_analyst(conn, "journal_assessor", "0 0,12 * * *")
        # Ten healthy 12h legs, then silence: the newest run is 38h old.
        for leg in range(10):
            await _trace(
                conn,
                "journal_assessor",
                now - timedelta(hours=38 + 12 * leg),
                output_refs=[uuid4()],
            )
        report = await pg.read_gauge(conn)

    row = next(
        g
        for g in report.loops
        if g.loop_id == "journal_assessor"
        and g.loop_class == pg.LOOP_ANALYST_CADENCE
    )
    assert row.state == "deficit"
    assert row.pages is True
    assert row.evidence["bar_minutes"] == 2160.0  # 3 x 12h
    assert row.evidence["missed_periods"] > 3.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_journal_thirty_hours_quiet_does_not_page(pool, blank):
    """The precision half of acceptance 2 — two missed 12h legs is not yet a
    page; that window belongs to the liveness watchdog's faster edge alert."""
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        await _register_analyst(conn, "journal_assessor", "0 0,12 * * *")
        await _trace(
            conn, "journal_assessor", now - timedelta(hours=30),
            output_refs=[uuid4()],
        )
        report = await pg.read_gauge(conn)

    row = next(
        g
        for g in report.loops
        if g.loop_id == "journal_assessor"
        and g.loop_class == pg.LOOP_ANALYST_CADENCE
    )
    assert row.state == "ok"


# ---------------------------------------------------------------------------
# ACCEPTANCE CASE 3 — forecast resolution, 0 of 38, ever
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acceptance_forecast_resolution_never_drains_is_caught(pool, blank):
    """ACCEPTANCE 3. 19 forecasts overdue since 07-06 against a 1-day grace;
    forecast_scoreboard head-active, actor registered, running daily, every
    receipt ``resolved=0`` — and the per-row failure logged at DEBUG."""
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        await _register_analyst(conn, "forecast_scoreboard", "50 2 * * *")
        for d in range(1, 21):
            await _trace(conn, "forecast_scoreboard", now - timedelta(days=d))
        for i in range(19):
            await conn.execute(
                """
                INSERT INTO acute_forecasts
                    (region, event_class, window_start, window_end, p, p_base,
                     method, issued_at)
                VALUES ($1, 'hazard_severe', $2, $3, 0.2, 0.1, 'poisson', $2)
                """,
                f"country_test_{i}",
                now - timedelta(days=35),
                now - timedelta(days=28),
            )
        report = await pg.read_gauge(conn)

    row = next(
        g for g in report.loops if g.loop_class == pg.LOOP_BACKLOG_DRAIN
    )
    assert row.loop_id == "acute_forecast_resolution"
    assert row.state == "deficit"
    assert row.pages is True
    assert row.evidence["overdue"] == 19
    assert row.evidence["resolved_in_window"] == 0
    assert row.evidence["owner_runs_in_window"] == 20
    assert row.evidence["oldest_overdue_age_days"] >= 27


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forecast_backlog_clears_once_one_resolves(pool, blank):
    """One honest resolution is enough to say the drain works. The gauge asks
    "does this loop produce at all", never "is the backlog empty" — a growing
    backlog on a working resolver is a capacity question, not a silent death."""
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        await _register_analyst(conn, "forecast_scoreboard", "50 2 * * *")
        for d in range(1, 21):
            await _trace(conn, "forecast_scoreboard", now - timedelta(days=d))
        await conn.execute(
            """
            INSERT INTO acute_forecasts
                (region, event_class, window_start, window_end, p, p_base,
                 method, issued_at)
            VALUES ('r_open', 'hazard_severe', $1, $2, 0.2, 0.1, 'poisson', $1)
            """,
            now - timedelta(days=35),
            now - timedelta(days=28),
        )
        await conn.execute(
            """
            INSERT INTO acute_forecasts
                (region, event_class, window_start, window_end, p, p_base,
                 method, issued_at, resolved_outcome, resolved_at, resolved_by)
            VALUES ('r_done', 'hazard_severe', $1, $2, 0.2, 0.1, 'poisson', $1,
                    0, $3, 'forecast_scoreboard')
            """,
            now - timedelta(days=35),
            now - timedelta(days=28),
            now - timedelta(days=2),
        )
        report = await pg.read_gauge(conn)

    row = next(g for g in report.loops if g.loop_class == pg.LOOP_BACKLOG_DRAIN)
    assert row.state == "ok"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_declared_backlog_sql_runs_against_the_live_schema(pool, blank):
    """The anti-rot guard for the one non-auto-covering class.

    A renamed column in ``acute_forecasts`` must break this suite, not zero
    the gauge silently — the exact failure mode ("we measured and found
    nothing") the whole module exists to prevent.
    """
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        rows = await pg.read_backlog_loops(conn, now=now, cfg=CFG)
    assert len(rows) == len(pg.BACKLOG_DRAINS)
    for row in rows:
        assert row.quiet_reason != "backlog_query_failed", row.evidence


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_broken_declared_backlog_degrades_loud_never_silent(pool, blank):
    now = datetime.now(tz=timezone.utc)
    broken = pg.BacklogDrain(
        backlog_id="ghost",
        label="Ghost backlog",
        owner_analyst_id="nobody",
        unit="thing",
        overdue_sql="SELECT count(*)::int AS overdue FROM table_that_is_not_there",
        resolved_sql="SELECT 0::int AS resolved WHERE $1::timestamptz IS NOT NULL",
    )
    async with pool.acquire() as conn:
        rows = await pg.read_backlog_loops(conn, now=now, cfg=CFG, drains=[broken])
    assert rows[0].state == "ungauged"
    assert rows[0].quiet_reason == "backlog_query_failed"
    assert "table_that_is_not_there" in rows[0].evidence["error"]


# ---------------------------------------------------------------------------
# Whole-engine reads
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_empty_engine_gauges_nothing_and_says_so(pool, blank):
    """An empty substrate must read as "nothing to measure", never as "all
    clear" — the totals distinguish the two."""
    async with pool.acquire() as conn:
        report = await pg.read_gauge(conn)
    totals = report.totals()
    assert totals["deficit"] == 0
    assert totals["gauged"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gauge_sorts_worst_first(pool, blank):
    """The one-screen table property: line one is always the thing most worth
    reading."""
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        await _seed_frozen_ap_feed(conn, now)
        await _register_analyst(conn, "healthy_analyst", "*/15 * * * *")
        await _trace(conn, "healthy_analyst", now - timedelta(minutes=5))
        report = await pg.read_gauge(conn)

    assert report.loops[0].state == "deficit"
    states = [g.state for g in report.loops]
    assert states.index("deficit") < states.index("ungauged")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_new_analyst_is_gauged_the_moment_its_descriptor_activates(
    pool, blank
):
    """THE auto-covering property, asserted directly: nothing in this module
    names an analyst, so registering one and letting it fall behind produces a
    deficit with no code change anywhere. A gauge that needed a per-component
    list would rot the first time somebody added a desk."""
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        await _register_analyst(
            conn, "brand_new_analyst_nobody_coded_for", "0 * * * *"
        )
        await _trace(
            conn, "brand_new_analyst_nobody_coded_for", now - timedelta(days=2)
        )
        report = await pg.read_gauge(conn)

    row = next(
        g
        for g in report.loops
        if g.loop_id == "brand_new_analyst_nobody_coded_for"
        and g.loop_class == pg.LOOP_ANALYST_CADENCE
    )
    assert row.state == "deficit"
    assert row.severity == "critical"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_production_actual_covers_side_written_output_kinds(pool, blank):
    """The union that keeps the production class kind-agnostic: a run that
    wrote nothing into ``output_row_refs`` but side-wrote an
    ``analyst_outputs`` row under its run id still counts as producing. Without
    it every TRACE_ONLY side-writer (scorecard_producer, alert_trigger_scan)
    would read as a dead producer."""
    now = datetime.now(tz=timezone.utc)
    async with pool.acquire() as conn:
        await _register_analyst(conn, "side_writer", "0 * * * *")
        run_ids = []
        for h in range(30):
            rid = uuid4()
            run_ids.append(rid)
            await conn.execute(
                """
                INSERT INTO analyst_traces
                    (run_id, analyst_id, analyst_version, cadence_trigger,
                     output_row_refs, status, run_started_at, run_ended_at,
                     receipt_hash)
                VALUES ($1, 'side_writer', 'v1', 'cadence', '{}'::uuid[],
                        'success', $2, $2, $3)
                """,
                rid,
                now - timedelta(hours=h),
                uuid4().hex,
            )
        for rid in run_ids:
            await conn.execute(
                """
                INSERT INTO analyst_outputs
                    (kind, title, body, analyst_id, analyst_version, run_id,
                     schema_uri)
                VALUES ('scorecard', 't', '', 'side_writer', 'v1', $1,
                        'iglu:legba/scorecard/jsonschema/1-0-0')
                """,
                rid,
            )
        report = await pg.read_gauge(conn)

    row = next(
        g
        for g in report.loops
        if g.loop_id == "side_writer"
        and g.loop_class == pg.LOOP_ANALYST_PRODUCTION
    )
    assert row.state == "ok"
    assert row.evidence["producing_runs"] == 30
