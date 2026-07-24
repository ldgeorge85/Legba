# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-tool unit tests for the Journal Assessor's ~9 net-new self-instrument
reads (plan §5 / §13 — "a per-instrument-tool unit test for each of the ~9 new
tools").

Each test seeds the disposable DB with the minimal rows the instrument reads and
asserts the port method returns the right shape, including the HONESTY contract
(get_calibration forces ``forecast_unproven`` / ``calibration_thin`` from the
real substrate metrics). DB = the DISPOSABLE container only (see conftest).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# get_assessments — country/world_assessor findings, critic-folded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_assessments_returns_assessor_findings(pg_pool, port):
    fid = uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            # B0 red-test fix (2026-07-10): country_assessor was RETIRED and
            # 98bb4dc removed it from _ASSESSMENT_PRODUCER_ANALYSTS — seed a
            # LIVE producer so the default read can actually return the row.
            "INSERT INTO analyst_outputs (id, kind, title, body, confidence, "
            "analyst_id, produced_at, schema_uri) VALUES "
            "($1,'finding','BR energy','body',0.8,'country_composition',$2,'u')",
            fid, NOW,
        )
        # a non-assessor finding must NOT surface by default
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, body, confidence, "
            "analyst_id, produced_at, schema_uri) VALUES "
            "($1,'finding','noise','b',0.5,'cross_target_raw',$2,'u')",
            uuid4(), NOW,
        )
    out = await port.get_assessments(since_hours=48, limit=10)
    ids = {r["id"] for r in out["rows"]}
    assert str(fid) in ids
    # every returned row must come from the LIVE producer set
    from legba.runtime.substrate_query_port import _ASSESSMENT_PRODUCER_ANALYSTS
    assert all(
        r["analyst_id"] in _ASSESSMENT_PRODUCER_ANALYSTS for r in out["rows"]
    )
    assert str(fid) in out["refs"]


@pytest.mark.asyncio
async def test_get_assessments_filters_by_analyst(pg_pool, port):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, confidence, analyst_id, "
            "produced_at, schema_uri) VALUES ($1,'finding','w',0.7,'world_assessor',$2,'u')",
            uuid4(), NOW,
        )
    out = await port.get_assessments(analyst_id="world_assessor", limit=10)
    assert all(r["analyst_id"] == "world_assessor" for r in out["rows"])


# ---------------------------------------------------------------------------
# get_graph_structure / get_structural_balance — graph_metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_graph_structure_reads_latest_graph_mining(pg_pool, port):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO graph_metrics (metric_kind, computed_at, payload) "
            "VALUES ('graph_mining', $1, $2::jsonb)",
            NOW,
            json.dumps({
                "community_count": 4, "modularity": 0.42, "node_count": 50,
                "edge_count": 120, "top_centrality": {"Iran": {"degree": 0.9}},
                "interesting": [{"kind": "broker", "entity": "Turkey"}],
            }),
        )
    out = await port.get_graph_structure(limit=10)
    assert out["available"] is True
    assert out["community_count"] == 4
    assert out["modularity"] == 0.42
    assert "Iran" in out["top_centrality"]
    assert out["refs"] == []


@pytest.mark.asyncio
async def test_get_graph_structure_unavailable_when_no_metric(pg_pool, port):
    out = await port.get_graph_structure()
    assert out["available"] is False
    assert out["refs"] == []


@pytest.mark.asyncio
async def test_get_structural_balance_reads_unstable_triads(pg_pool, port):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO graph_metrics (metric_kind, computed_at, payload) "
            "VALUES ('structural_balance', $1, $2::jsonb)",
            NOW,
            json.dumps({
                "balance_ratio": 0.6, "balanced_count": 3, "unbalanced_count": 2,
                "unbalanced_triads": [
                    {"a": "US", "b": "Iran", "c": "Israel",
                     "signs": {"ab": -1, "bc": -1, "ac": 1}},
                ],
                "frustration": {"Iran": 5, "US": 3},
            }),
        )
    out = await port.get_structural_balance(limit=10)
    assert out["available"] is True
    assert out["unbalanced_count"] == 2
    assert len(out["unstable_triads"]) == 1
    assert out["frustration"]["Iran"] == 5
    assert "prediction" in out["note"]
    assert out["refs"] == []


# ---------------------------------------------------------------------------
# get_critic_scores — analyst_outputs kind='critique'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_critic_scores_reads_critiques(pg_pool, port):
    cid = uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, analyst_id, produced_at, "
            "schema_uri, data) VALUES ($1,'critique','c','critic',$2,'u',$3::jsonb)",
            cid, NOW,
            json.dumps({
                "overall_score": 0.48, "scores": {"grounding": 0.5},
                "analyzed_analyst_id": "country_assessor",
                "analyzed_output_id": str(uuid4()),
                "revision_delta": "tighten the grounding",
            }),
        )
    out = await port.get_critic_scores(limit=10)
    assert out["count"] == 1
    row = out["rows"][0]
    assert row["overall_score"] == 0.48
    assert row["analyzed_analyst_id"] == "country_assessor"
    assert out["mean_overall_score"] == 0.48
    assert "NON-ACTUATING" in out["actuation_note"]
    assert str(cid) in out["refs"]


# ---------------------------------------------------------------------------
# get_calibration — the HONESTY contract (segregated acute pilot)
#
# B0-3 (read-truth): seeded EXACTLY as the writer writes — calibration_tracking
# emits OutputKind.FINDING (kind='finding', analyst_id='calibration_tracking';
# NOTHING writes kind='calibration'), and the row's ``data`` column holds the
# WHOLE FindingPayload dump with the metrics one level down at ``data.data``.
# ---------------------------------------------------------------------------


async def _insert_calibration_finding(conn, metrics: dict, *, superseded_by=None):
    """Insert an ``analyst_outputs`` row shaped exactly like the live
    calibration_tracking writer: kind='finding', analyst_id='calibration_tracking',
    ``data`` = FindingPayload dump with the metrics dict NESTED under data.data."""
    oid = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs (id, kind, title, analyst_id, produced_at, "
        "schema_uri, data, superseded_by) VALUES "
        "($1,'finding','Calibration','calibration_tracking',$2,'u',$3::jsonb,$4)",
        oid, NOW,
        json.dumps({
            "title": "Calibration", "body": "…", "confidence": 1.0,
            "evidence": [], "tags": ["deterministic", "calibration_tracking"],
            "data": {"sub_handler": "calibration_tracking", **metrics},
        }),
        superseded_by,
    )
    return oid


@pytest.mark.asyncio
async def test_get_calibration_forces_unproven_on_thin_pilot(pg_pool, port):
    """A calibration finding with a NOT-ready acute pilot (n<30) + thin exogenous
    sample must report forecast_unproven=True + calibration_thin=True — the
    deterministic verdict the journal's §10 honesty post-step keys off."""
    async with pg_pool.acquire() as conn:
        await _insert_calibration_finding(conn, {
            "brier": None, "exogenous_sample_size": 2, "sample_size": 10,
            "brier_forecast_acute": None, "brier_skill_score": None,
            "forecast_acute_sample_size": 12, "forecast_acute_ready": False,
            "forecast_acute_degenerate": False,
            "forecast_acute_status": "accumulating (n=12/30)",
        })
    out = await port.get_calibration()
    assert out["available"] is True
    assert out["forecast_unproven"] is True   # not ready → unproven
    assert out["calibration_thin"] is True     # exogenous n<5 → thin
    assert out["forecast_acute_ready"] is False
    assert str(out["id"]) in out["refs"]


@pytest.mark.asyncio
async def test_get_calibration_unproven_when_degenerate_even_if_positive_bss(pg_pool, port):
    """Ready + positive BSS but DEGENERATE (geography-dominated) is still unproven
    — the honesty guard refuses the skill claim when the pilot isn't probabilistic."""
    async with pg_pool.acquire() as conn:
        await _insert_calibration_finding(conn, {
            "brier": 0.1, "exogenous_sample_size": 40, "sample_size": 80,
            "brier_forecast_acute": 0.12, "brier_skill_score": 0.3,
            "forecast_acute_sample_size": 40, "forecast_acute_ready": True,
            "forecast_acute_degenerate": True,
            "forecast_acute_status": "degenerate — geography-dominated",
        })
    out = await port.get_calibration()
    assert out["forecast_unproven"] is True     # degenerate → still unproven
    assert out["calibration_thin"] is False      # exogenous n=40 → not thin


@pytest.mark.asyncio
async def test_get_calibration_proven_when_ready_nondegenerate_positive_bss(pg_pool, port):
    async with pg_pool.acquire() as conn:
        await _insert_calibration_finding(conn, {
            "brier": 0.08, "exogenous_sample_size": 40, "sample_size": 80,
            "brier_forecast_acute": 0.1, "brier_skill_score": 0.25,
            "forecast_acute_sample_size": 35, "forecast_acute_ready": True,
            "forecast_acute_degenerate": False,
            "forecast_acute_status": "ready",
        })
    out = await port.get_calibration()
    assert out["forecast_unproven"] is False     # ready + non-degenerate + BSS>0
    assert out["calibration_thin"] is False


@pytest.mark.asyncio
async def test_get_calibration_reads_nested_metrics_from_live_writer_shape(pg_pool, port):
    """B0-3 core: the metrics surface from the NESTED data.data (the FindingPayload
    dump), the numbers matching the live substrate (brier_exogenous 0.3976,
    n_exo=537), and refs carries the row id so the journal can cite it."""
    async with pg_pool.acquire() as conn:
        oid = await _insert_calibration_finding(conn, {
            "brier": 0.3976, "brier_exogenous": 0.3976,
            "exogenous_sample_size": 537, "sample_size": 600,
            "insufficient_exogenous": False, "self_consistency_only": False,
            "brier_forecast_acute": None, "brier_skill_score": None,
            "forecast_acute_sample_size": 3, "forecast_acute_ready": False,
            "forecast_acute_degenerate": False,
            "forecast_acute_status": "accumulating (n=3/30)",
        })
    out = await port.get_calibration()
    assert out["available"] is True
    assert out["brier"] == 0.3976
    assert out["brier_exogenous"] == 0.3976
    assert out["exogenous_sample_size"] == 537
    assert out["sample_size"] == 600
    assert out["calibration_thin"] is False
    assert out["forecast_unproven"] is True
    assert out["refs"] == [str(oid)]
    assert out["id"] == str(oid)


@pytest.mark.asyncio
async def test_get_calibration_ignores_legacy_kind_and_superseded_rows(pg_pool, port):
    """The read is re-pointed: a legacy flat ``kind='calibration'`` row (which no
    writer produces) is INVISIBLE, and a superseded calibration finding never
    shadows the live head."""
    async with pg_pool.acquire() as conn:
        # legacy-shaped row alone → NOT read (available stays False)
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, analyst_id, produced_at, "
            "schema_uri, data) VALUES ($1,'calibration','cal','calibration_tracking',"
            "$2,'u',$3::jsonb)",
            uuid4(), NOW, json.dumps({"brier": 0.9, "exogenous_sample_size": 99}),
        )
    out = await port.get_calibration()
    assert out["available"] is False

    async with pg_pool.acquire() as conn:
        live = await _insert_calibration_finding(conn, {
            "brier": 0.2, "exogenous_sample_size": 10, "sample_size": 20,
        })
        # a SUPERSEDED calibration finding (points at the live head) is skipped
        await _insert_calibration_finding(
            conn,
            {"brier": 0.99, "exogenous_sample_size": 1, "sample_size": 1},
            superseded_by=live,
        )
    out = await port.get_calibration()
    assert out["available"] is True
    assert out["id"] == str(live)
    assert out["brier"] == 0.2


@pytest.mark.asyncio
async def test_get_calibration_conservative_when_no_data(pg_pool, port):
    out = await port.get_calibration()
    assert out["available"] is False
    assert out["forecast_unproven"] is True      # absence of proof ≠ proof of skill
    assert out["calibration_thin"] is True


# ---------------------------------------------------------------------------
# get_run_health — analyst_traces (what fired vs went quiet)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_health_flags_quiet_analysts(pg_pool, port):
    fresh = NOW - timedelta(hours=2)
    stale = NOW - timedelta(hours=72)
    async with pg_pool.acquire() as conn:
        for aid, started in (("world_assessor", fresh), ("country_assessor", stale)):
            await conn.execute(
                "INSERT INTO analyst_traces (run_id, analyst_id, analyst_version, "
                "cadence_trigger, status, run_started_at, run_ended_at, receipt_hash) "
                "VALUES ($1,$2,'v','schedule','success',$3,$3,$4)",
                uuid4(), aid, started, "h" + uuid4().hex[:8],
            )
    out = await port.get_run_health(quiet_hours=24, limit=20)
    by_id = {r["analyst_id"]: r for r in out["rows"]}
    assert by_id["world_assessor"]["quiet"] is False
    assert by_id["country_assessor"]["quiet"] is True
    assert "country_assessor" in out["quiet_analysts"]
    assert "world_assessor" not in out["quiet_analysts"]
    assert out["refs"] == []


@pytest.mark.asyncio
async def test_get_run_health_surfaces_error_flag(pg_pool, port):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO analyst_traces (run_id, analyst_id, analyst_version, "
            "cadence_trigger, status, run_started_at, run_ended_at, receipt_hash, "
            "error_payload) VALUES ($1,'predictor','v','schedule','success',$2,$2,$3,"
            "$4::jsonb)",
            uuid4(), NOW - timedelta(hours=1), "h" + uuid4().hex[:8],
            json.dumps({"error": "boom"}),
        )
    out = await port.get_run_health(analyst_id="predictor", limit=5)
    assert out["rows"][0]["had_error"] is True


# ---------------------------------------------------------------------------
# get_source_health — source_poll_outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_source_health_reports_silence_and_errors(pg_pool, port):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO source_descriptors (descriptor_id, version, schema_uri, "
            "is_head, kind, state, owner, name, body) VALUES "
            "('src_quiet','v','u',TRUE,'rss','active','o','Quiet Feed',"
            "$1::jsonb)",
            json.dumps({"identity": {"name": "Quiet Feed"}}),
        )
        await conn.execute(
            "INSERT INTO source_poll_outcomes (source_id, outcome, health_state, "
            "occurred_at) VALUES ('src_quiet','error','unhealthy',$1)",
            NOW - timedelta(hours=5),
        )
    out = await port.get_source_health(limit=20)
    rows = {r["source_id"]: r for r in out["rows"]}
    assert "src_quiet" in rows
    assert rows["src_quiet"]["last_poll_outcome"] == "error"
    assert out["error_count"] >= 1
    assert out["refs"] == []


# ---------------------------------------------------------------------------
# get_budget_status — budget_ledger + budget_demotion_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_budget_status_reads_consumption_and_demotions(pg_pool, port):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO budget_ledger (analyst_id, analyst_version, bucket, "
            "tokens_used, runs, cost_usd) VALUES "
            "('journal_assessor','v',CURRENT_DATE,12345,2,0.5)",
        )
        await conn.execute(
            "INSERT INTO budget_demotion_events (analyst_id, analyst_version, "
            "bucket, cause, tokens_used_at_demote, tokens_cap_at_demote, "
            "primary_llm, fallback_llm, occurred_at) VALUES "
            "('journal_assessor','v',CURRENT_DATE,'per_analyst',9,10,'opus','oss',$1)",
            NOW - timedelta(hours=2),
        )
    out = await port.get_budget_status(analyst_id="journal_assessor")
    assert out["today_consumption"][0]["tokens_used"] == 12345
    assert out["demotion_count"] == 1
    assert out["recent_demotions"][0]["cause"] == "per_analyst"
    assert "plumbing" in out["note"] or "cap" in out["note"]
    assert out["refs"] == []


# ---------------------------------------------------------------------------
# get_journal_delta — prior entry + consolidation + change counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_journal_delta_returns_prior_entry_and_counts(pg_pool, port):
    prior_end = NOW - timedelta(hours=12)
    eid = uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO journal_entries (id, entry_kind, title, body, period_start, "
            "period_end, analyst_id, produced_at) VALUES "
            "($1,'entry','prior','reflecting',$2,$3,'journal_assessor',$3)",
            eid, prior_end - timedelta(hours=24), prior_end,
        )
        # a finding produced AFTER the prior entry's period_end (in the delta window)
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, confidence, analyst_id, "
            "produced_at, schema_uri) VALUES ($1,'finding','new','0.5'::real,"
            "'world_assessor',$2,'u')",
            uuid4(), NOW - timedelta(hours=1),
        )
    out = await port.get_journal_delta()
    assert out["prior_entry"] is not None
    assert out["prior_entry"]["id"] == str(eid)
    assert out["delta"]["new_findings"] >= 1
    assert str(eid) in out["refs"]


@pytest.mark.asyncio
async def test_get_journal_delta_bootstrap_no_prior_entry(pg_pool, port):
    out = await port.get_journal_delta()
    assert out["prior_entry"] is None
    assert out["current_consolidation"] is None
    assert out["refs"] == []
    assert "new_findings" in out["delta"]
