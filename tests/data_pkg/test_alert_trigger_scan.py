# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1-3 — the ``alert_trigger_scan`` deterministic trigger set v1.

Pure tests (no DB): band-transition classification, the baseline exceedance
test, and the per-desk cap / rollup fold. Ephemeral-DB tests (the
``migrated_pg`` fixture): per trigger class, the seed-silently → fire-once →
never-refire watermark property against live SQL, the verified bar
(faithfulness present + effective_confidence >= floor + structural-exempt
exclusion), the contention verified-tie gate, the baseline rising edge, and
the per-desk cap with an honest rollup whose members' watermarks still
advance.

Trigger 6 (geo_convergence) — 2026-07-29 alert-plane consolidation: these
tests were ported from the former standalone ``geo_convergence_scan``
analyst's own DB-lifecycle suite (``test_geo_convergence_scan.py``), now
exercising the SAME stimuli through ``alert_trigger_scan.handle()`` and
asserting payload-content equivalence (not just "an alert exists") — proving
the fold preserved firing conditions, payload content, and watermark dedup
byte-for-byte.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    TRACE_ONLY,
)
from legba.data.analysts.deterministic_handlers import alert_trigger_scan as ats
from legba.data.config import PostgresConfig
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_registered_as_trace_only_sub_handler():
    """The receipt is TRACE_ONLY (real product = side-written alert rows), so
    the STRUCTURAL_VERIFY_EXEMPT drift guard's FINDING-set equality holds."""
    assert SUB_HANDLERS["alert_trigger_scan"] is ats.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER["alert_trigger_scan"] is TRACE_ONLY


async def test_refuses_loud_without_pool():
    with pytest.raises(RuntimeError, match="pg_pool"):
        await ats.handle([], {"sub_handler": "alert_trigger_scan"}, None)


# ---------------------------------------------------------------------------
# Pure — band transition classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frm,to,direction,severity",
    [
        ("watch", "high", "deterioration", "high"),
        ("low", "watch", "deterioration", "high"),
        ("high", "critical", "deterioration", "high"),
        ("high", "watch", "improvement", "medium"),
        ("critical", "low", "improvement", "medium"),
        ("insufficient-evidence", "high", "evidence-gained", "medium"),
        ("elevated", "insufficient-evidence", "evidence-lost", "medium"),
        ("garbage", "high", "indeterminate", "medium"),
    ],
)
def test_classify_band_transition(frm, to, direction, severity):
    assert ats.classify_band_transition(frm, to) == (direction, severity)


# ---------------------------------------------------------------------------
# Pure — baseline exceedance
# ---------------------------------------------------------------------------


def test_baseline_exceeds_mean_plus_two_sigma_with_floor():
    # Clear exceedance above both the stat threshold and the floor.
    assert ats.baseline_exceeds(30, 5.0, 2.0, min_current=10) is True
    # Below the absolute floor NEVER fires, however extreme statistically
    # (the σ≈0 quiet-desk guard).
    assert ats.baseline_exceeds(4, 0.1, 0.0, min_current=10) is False
    # At/below mean+2σ does not fire.
    assert ats.baseline_exceeds(9.0, 5.0, 2.0, min_current=5) is False
    assert ats.baseline_exceeds(9.1, 5.0, 2.0, min_current=5) is True


# ---------------------------------------------------------------------------
# Pure — per-desk cap + rollup
# ---------------------------------------------------------------------------


def _cand(desk: str, sev: str, title: str, wm_key: str) -> ats.AlertCandidate:
    return ats.AlertCandidate(
        trigger_class=ats.TRIGGER_BAND,
        severity=sev,
        title=title,
        body="",
        target_id=desk,
        watermarks=[(ats.TRIGGER_BAND, wm_key, {"band": "high"})],
    )


def test_apply_desk_cap_keeps_worst_and_rolls_up_honestly():
    cands = [
        _cand("d1", "critical", "c1", "k1"),
        _cand("d1", "high", "c2", "k2"),
        _cand("d1", "high", "c3", "k3"),
        _cand("d1", "high", "c4", "k4"),
        _cand("d1", "medium", "c5", "k5"),
        _cand("d2", "medium", "other-desk", "k6"),
    ]
    kept, rollups = ats.apply_desk_cap(cands, 3)

    d1_kept = [c for c in kept if c.target_id == "d1"]
    assert len(d1_kept) == 3
    # Worst-first: the critical survives, the medium never outranks a high.
    assert d1_kept[0].severity == "critical"
    assert all(c.severity in ("critical", "high") for c in d1_kept)
    # d2 is under its own cap — no rollup for it.
    assert [c.target_id for c in kept if c.target_id == "d2"] == ["d2"]

    assert len(rollups) == 1
    roll = rollups[0]
    assert roll.target_id == "d1"
    assert roll.data["suppressed_count"] == 2
    assert "2 further trigger alert(s)" in roll.title
    # The rollup's severity is the WORST of the suppressed (one high, one medium).
    assert roll.severity == "high"
    # Watermarks of the summarized transitions ride the rollup (no refire).
    rolled_keys = {k for _, k, _ in roll.watermarks}
    kept_keys = {k for c in d1_kept for _, k, _ in c.watermarks}
    assert rolled_keys | kept_keys == {"k1", "k2", "k3", "k4", "k5"}
    assert rolled_keys.isdisjoint(kept_keys)


def test_apply_desk_cap_global_bucket_for_deskless_alerts():
    cands = [
        ats.AlertCandidate(
            trigger_class=ats.TRIGGER_CONTENTION,
            severity="medium",
            title=f"t{i}",
            body="",
            target_id=None,
            watermarks=[(ats.TRIGGER_CONTENTION, f"c{i}", {})],
        )
        for i in range(5)
    ]
    kept, rollups = ats.apply_desk_cap(cands, 3)
    assert len(kept) == 3 and len(rollups) == 1
    assert rollups[0].target_id is None
    assert rollups[0].data["suppressed_count"] == 2


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
    """Fresh watermark table + no leftover trigger alerts, so every test gets
    its own seed → fire cycle (foreign substrate rows just seed silently)."""
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE alert_trigger_watermarks")
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = 'alert_trigger_scan'"
        )
    yield


class _FakeDispatcher:
    def __init__(self) -> None:
        self.payloads: list[Any] = []

    async def fan_out(self, payload: Any) -> list[Any]:
        self.payloads.append(payload)
        return []


class _Deps:
    def __init__(self, pool: Any, dispatcher: Any) -> None:
        self.pg_pool = pool
        self.extras = {"alert_sink_dispatcher": dispatcher}


async def _run(pool: Any, dispatcher: Any | None = None, **opts: Any):
    deps = _Deps(pool, dispatcher if dispatcher is not None else _FakeDispatcher())
    options = {
        "sub_handler": "alert_trigger_scan",
        "analyst_id": "alert_trigger_scan",
        "run_id": str(uuid4()),
        **opts,
    }
    result = await ats.handle([], options, deps)
    assert isinstance(result, AnalystMethodResult)
    return result


async def _alert_rows(conn: Any) -> list[Any]:
    return await conn.fetch(
        "SELECT id, title, severity, target_id, data, derived_from "
        "FROM analyst_outputs "
        "WHERE kind = 'alert' AND analyst_id = 'alert_trigger_scan' "
        "ORDER BY produced_at, id"
    )


def _row_data(row: Any) -> dict[str, Any]:
    """The alert payload's own nested ``data`` dict (the ``data`` COLUMN
    stores the full payload dump — see writes._insert_analyst_output)."""
    d = row["data"]
    full = json.loads(d) if isinstance(d, str) else dict(d)
    inner = full.get("data")
    return inner if isinstance(inner, dict) else full


# -- insert helpers ---------------------------------------------------------


async def _insert_scorecard(
    conn: Any, desk: str, bands: dict[str, str], basis: dict[str, list] | None = None
) -> UUID:
    row_id = uuid4()
    dims = {
        dim: {
            "band": band,
            "basis": [str(b) for b in (basis or {}).get(dim, [])],
            "reason": "qualified",
            "effective_confidence": 0.8,
        }
        for dim, band in bands.items()
    }
    # Mirror the LIVE column shape: the data COLUMN carries the full payload
    # dump, so the producer's payload `data` (with `bands`) is NESTED.
    data = {
        "kind_marker": "scorecard",
        "tags": ["deterministic", "scorecard"],
        "data": {
            "sub_handler": "scorecard_producer",
            "bands": {"target_id": desk, "dimensions": dims},
        },
    }
    await conn.execute(
        "UPDATE analyst_outputs SET superseded_by = $1 "
        "WHERE kind = 'scorecard' AND target_id = $2 AND superseded_by IS NULL",
        row_id,
        desk,
    )
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, data, target_id, analyst_id, "
        "   schema_uri) "
        "VALUES ($1, 'scorecard', $2, '', 1.0, $3::jsonb, $4, "
        "        'scorecard_producer', 'iglu:legba/scorecard/jsonschema/1-0-0')",
        row_id,
        f"Scorecard {desk}",
        json.dumps(data),
        desk,
    )
    return row_id


async def _insert_finding(
    conn: Any,
    *,
    analyst_id: str = "escalation",
    target: str | None = "country_g20_us",
    confidence: float = 0.9,
    sev_tag: str | None = "high",
    derived: list[UUID] | None = None,
) -> UUID:
    fid = uuid4()
    tags = [f"severity:{sev_tag}"] if sev_tag else []
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, severity, data, target_id, "
        "   analyst_id, derived_from, schema_uri) "
        "VALUES ($1, 'finding', $2, '', $3, $4, $5::jsonb, $6, $7, "
        "        $8::uuid[], 'iglu:legba/finding/jsonschema/1-0-0')",
        fid,
        f"Test finding {fid}",
        confidence,
        sev_tag,
        json.dumps({"tags": tags}),
        target,
        analyst_id,
        derived or [],
    )
    return fid


async def _insert_faith_critique(conn: Any, finding_id: UUID, score: float) -> UUID:
    cid = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs (id, kind, title, body, confidence, data, "
        "                             schema_uri) "
        "VALUES ($1, 'critique', $2, '', $3, $4::jsonb, "
        "        'iglu:legba/critique/jsonschema/1-0-0')",
        cid,
        "Faithfulness verify — test",
        score,
        json.dumps({"analyzed_output_id": str(finding_id), "overall_score": score}),
    )
    return cid


async def _insert_contention(
    conn: Any,
    *,
    subject: str,
    predicate: str = "located in",
    status: str = "contested",
    value_count: int = 2,
) -> UUID:
    cid = uuid4()
    await conn.execute(
        "INSERT INTO fact_contention "
        "  (id, subject_key, predicate_key, status, value_count) "
        "VALUES ($1, $2, $3, $4, $5)",
        cid,
        subject,
        predicate,
        status,
        value_count,
    )
    return cid


async def _insert_contention_value(
    conn: Any, contention_id: UUID, fact_ids: list[UUID], value_key: str
) -> None:
    await conn.execute(
        "INSERT INTO fact_contention_values "
        "  (contention_id, value_key, supporting_fact_ids) "
        "VALUES ($1, $2, $3::uuid[])",
        contention_id,
        value_key,
        fact_ids,
    )


async def _insert_desk(conn: Any, desk: str, geo: list[str]) -> None:
    body = {"scope": {"geo": geo, "tags": ["g20"]}}
    await conn.execute(
        "INSERT INTO target_descriptors "
        "  (descriptor_id, version, schema_uri, is_head, state, owner, name, "
        "   body) "
        "VALUES ($1, 'v1', 'legba/target/2.0.0', TRUE, 'active', "
        "        'test_p1_3', $1, $2::jsonb) "
        "ON CONFLICT DO NOTHING",
        desk,
        json.dumps(body),
    )


async def _insert_signals(
    conn: Any, geo: list[str], n: int, *, days_ago: float = 0.0
) -> None:
    for _ in range(n):
        await conn.execute(
            "INSERT INTO signals (id, source_id, geo, fetched_at, content_hash) "
            "VALUES ($1, 'test_p1_3_source', $2::text[], "
            "        now() - make_interval(secs => $3), $4)",
            uuid4(),
            geo,
            days_ago * 86400.0,
            uuid4().hex,
        )


# ---------------------------------------------------------------------------
# DB — trigger 1: band crossing
# ---------------------------------------------------------------------------


async def test_band_crossing_seeds_then_fires_once(pg_pool, clean_slate):
    desk = f"desk_band_{uuid4().hex[:8]}"
    basis_id = uuid4()
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(
            conn,
            desk,
            {"escalation": "watch", "military_posture": "low"},
            basis={"escalation": [basis_id]},
        )

    # Scan 1 — first-ever: seeds silently, fires nothing.
    r1 = await _run(pg_pool)
    assert ats.TRIGGER_BAND in r1.finding.data["seeded_classes"]
    async with pg_pool.acquire() as conn:
        assert await _alert_rows(conn) == []
        seeded = await conn.fetchval(
            "SELECT count(*) FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key <> $2",
            ats.TRIGGER_BAND,
            ats.SEED_KEY,
        )
        assert seeded == 2  # both dimensions watermarked

        # New scorecard head: escalation watch → high (deterioration),
        # military_posture unchanged.
        new_basis = uuid4()
        sc2 = await _insert_scorecard(
            conn,
            desk,
            {"escalation": "high", "military_posture": "low"},
            basis={"escalation": [new_basis]},
        )

    # Scan 2 — exactly ONE alert, direction + severity per the mapping.
    dispatcher = _FakeDispatcher()
    r2 = await _run(pg_pool, dispatcher)
    assert r2.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    data = _row_data(row)
    assert data["trigger_class"] == ats.TRIGGER_BAND
    assert data["from_band"] == "watch" and data["to_band"] == "high"
    assert data["direction"] == "deterioration"
    assert row["severity"] == "high"
    assert row["target_id"] == desk
    # Lineage names the scorecard row + the dimension's basis finding.
    assert sc2 in list(row["derived_from"])
    assert new_basis in list(row["derived_from"])
    # Outward fan-out carried the converged payload with an alert receipt.
    assert len(dispatcher.payloads) == 1
    p = dispatcher.payloads[0]
    assert p.severity == "high" and p.target_id == desk
    assert p.receipt_path == f"/api/v1/lineage/alert/{row['id']}"
    assert p.verify_state.startswith("unverified")

    # Scan 3 — the SAME transition never refires.
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 0
    async with pg_pool.acquire() as conn:
        assert len(await _alert_rows(conn)) == 1


async def test_band_improvement_fires_medium(pg_pool, clean_slate):
    desk = f"desk_band_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(conn, desk, {"escalation": "high"})
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(conn, desk, {"escalation": "watch"})
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    assert rows[0]["severity"] == "medium"
    assert _row_data(rows[0])["direction"] == "improvement"


# ---------------------------------------------------------------------------
# DB — trigger 2: new verified high-severity finding
# ---------------------------------------------------------------------------


async def test_verified_finding_bar_and_no_refire(pg_pool, clean_slate):
    await _run(pg_pool)  # seed all classes

    async with pg_pool.acquire() as conn:
        # Qualifies: high severity, verified at 0.8, non-exempt analyst.
        good = await _insert_finding(conn, sev_tag="high", confidence=0.9)
        await _insert_faith_critique(conn, good, 0.80)
        # Below the effective-confidence floor (0.30 < 0.50): no alert.
        low = await _insert_finding(conn, sev_tag="critical", confidence=0.9)
        await _insert_faith_critique(conn, low, 0.30)
        # Verify-exempt structural analyst: excluded even with a critique.
        exempt = await _insert_finding(
            conn, analyst_id="graph_mining", sev_tag="high", confidence=0.9
        )
        await _insert_faith_critique(conn, exempt, 0.90)
        # No faithfulness verdict at all: cannot meet the verified bar.
        await _insert_finding(conn, sev_tag="high", confidence=0.95)

    dispatcher = _FakeDispatcher()
    r2 = await _run(pg_pool, dispatcher)
    assert r2.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["trigger_class"] == ats.TRIGGER_FINDING
    assert data["finding_id"] == str(good)
    assert data["effective_confidence"] == pytest.approx(0.80)
    assert rows[0]["severity"] == "high"
    assert good in list(rows[0]["derived_from"])
    # The outward payload states the REAL faithfulness verdict.
    assert dispatcher.payloads[0].verify_state == "faithfulness=0.80"
    assert dispatcher.payloads[0].effective_confidence == pytest.approx(0.80)

    # No refire on the next scan.
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 0
    async with pg_pool.acquire() as conn:
        assert len(await _alert_rows(conn)) == 1


async def test_late_verify_still_fires_within_window(pg_pool, clean_slate):
    """A finding whose critique lands AFTER a scan has passed it still fires —
    the per-finding-id watermark (not a time cursor) is the no-refire key."""
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        fid = await _insert_finding(conn, sev_tag="high", confidence=0.9)
    r = await _run(pg_pool)  # no critique yet — not verified, nothing fires
    assert r.finding.data["fired"] == 0
    async with pg_pool.acquire() as conn:
        await _insert_faith_critique(conn, fid, 0.75)
    r = await _run(pg_pool)
    assert r.finding.data["fired"] == 1


# ---------------------------------------------------------------------------
# DB — trigger 3: contention flip
# ---------------------------------------------------------------------------


async def test_contention_flip_fires_on_verified_tied_state_change(
    pg_pool, clean_slate
):
    fact_a, fact_b = uuid4(), uuid4()
    async with pg_pool.acquire() as conn:
        cid = await _insert_contention(conn, subject=f"subj_{uuid4().hex[:8]}")
        await _insert_contention_value(conn, cid, [fact_a], "value-a")
        await _insert_contention_value(conn, cid, [fact_b], "value-b")
        # The verified tie: a non-superseded finding CITES fact_a and met the
        # bar (no high-severity tag, so trigger 2 stays out of this test).
        tied = await _insert_finding(
            conn, sev_tag=None, confidence=0.9, derived=[fact_a]
        )
        await _insert_faith_critique(conn, tied, 0.85)

    await _run(pg_pool)  # seed — the existing contention fires nothing

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE fact_contention SET status = 'surfaced', "
            "surfaced_value = 'value-a', surfaced_fact_id = $2, "
            "updated_at = now() WHERE id = $1",
            cid,
            fact_a,
        )

    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["trigger_class"] == ats.TRIGGER_CONTENTION
    assert data["change"] == "contested->surfaced"
    assert data["verified_finding_id"] == str(tied)
    assert rows[0]["severity"] == "medium"
    assert rows[0]["target_id"] is None
    assert tied in list(rows[0]["derived_from"])

    # Same state → no refire.
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 0


async def test_new_contention_requires_verified_tie(pg_pool, clean_slate):
    await _run(pg_pool)  # seed
    fact_v, fact_u = uuid4(), uuid4()
    async with pg_pool.acquire() as conn:
        # New contention WITH a verified tie → fires.
        c_tied = await _insert_contention(conn, subject=f"tied_{uuid4().hex[:8]}")
        await _insert_contention_value(conn, c_tied, [fact_v], "v")
        tied = await _insert_finding(
            conn, sev_tag=None, confidence=0.9, derived=[fact_v]
        )
        await _insert_faith_critique(conn, tied, 0.9)
        # New contention with NO verified finding on its facts → silent.
        c_untied = await _insert_contention(
            conn, subject=f"untied_{uuid4().hex[:8]}"
        )
        await _insert_contention_value(conn, c_untied, [fact_u], "u")

    r = await _run(pg_pool)
    assert r.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["change"] == "new-contention"
    assert data["contention_id"] == str(c_tied)
    # The untied contention was WATERMARKED (observed) even though it did not
    # fire — its unchanged state can never fire later.
    r_again = await _run(pg_pool)
    assert r_again.finding.data["fired"] == 0


# ---------------------------------------------------------------------------
# DB — trigger 4: baseline deviation (rising edge)
# ---------------------------------------------------------------------------


async def test_baseline_deviation_rising_edge_once(pg_pool, clean_slate):
    desk = f"desk_base_{uuid4().hex[:8]}"
    geo = [f"Z{uuid4().hex[:6].upper()}"]
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, geo)
        # Trailing baseline: 1 signal/day for the previous 28 days; the
        # current 24h window stays quiet.
        for day in range(1, 29):
            await _insert_signals(conn, geo, 1, days_ago=day + 0.5)

    r1 = await _run(pg_pool)  # seed (not exceeding)
    assert ats.TRIGGER_BASELINE in r1.finding.data["seeded_classes"]

    async with pg_pool.acquire() as conn:
        await _insert_signals(conn, geo, 15, days_ago=0.01)  # the spike

    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["trigger_class"] == ats.TRIGGER_BASELINE
    assert data["metric"] == ats.METRIC_SIGNAL_VOLUME
    assert data["desk"] == desk
    assert data["current_24h"] == pytest.approx(15.0)
    assert rows[0]["severity"] == "medium"

    # Still exceeding on the next scan → rising-edge only, no refire.
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 0
    async with pg_pool.acquire() as conn:
        assert len(await _alert_rows(conn)) == 1


# ---------------------------------------------------------------------------
# E-1 (2026-07-27 sweep item 4) — the desk_baselines sidecar preference.
# Fresh row (< 48h) → the trigger reads its stored (expected, robust_sigma)
# and computes only the live current count; stale/absent → inline compute,
# byte-for-byte the prior behavior. The floors are shared constants (the
# sidecar module imports them from the trigger — verified, not duplicated).
# ---------------------------------------------------------------------------


def test_sidecar_constants_are_the_trigger_constants():
    """The 'same floors' consistency, asserted in the trigger's own suite as
    identity on the shared constants (the sidecar imports them — no copy)."""
    from legba.data.analysts.deterministic_handlers import desk_baseline as db

    assert db.MIN_CURRENT_SIGNALS is ats.MIN_CURRENT_SIGNALS
    assert db.MIN_CURRENT_FINDINGS is ats.MIN_CURRENT_FINDINGS
    assert db.DEFAULT_BASELINE_DAYS is ats.DEFAULT_BASELINE_DAYS
    assert db.DEFAULT_N_SIGMA is ats.DEFAULT_BASELINE_SIGMA
    assert db.METRIC_SIGNAL_VOLUME == ats.METRIC_SIGNAL_VOLUME
    assert db.METRIC_HIGH_SEV_FINDINGS == ats.METRIC_HIGH_SEV_FINDINGS


async def _insert_sidecar_baseline(
    conn: Any, desk: str, metric: str, *,
    expected: float, robust_sigma: float = 0.0, hours_ago: float = 1.0,
) -> None:
    await conn.execute(
        "INSERT INTO desk_baselines "
        "  (desk_id, metric, expected, robust_sigma, computed_at) "
        "VALUES ($1, $2, $3, $4, now() - make_interval(secs => $5)) "
        "ON CONFLICT (desk_id, metric) DO UPDATE SET "
        "  expected = EXCLUDED.expected, "
        "  robust_sigma = EXCLUDED.robust_sigma, "
        "  computed_at = EXCLUDED.computed_at",
        desk, metric, float(expected), float(robust_sigma),
        hours_ago * 3600.0,
    )


async def test_baseline_prefers_fresh_sidecar_row(pg_pool, clean_slate):
    """A fresh desk_baselines row IS the baseline: a spike the inline compute
    (empty trailing window → μ=0) would page on stays quiet under the
    sidecar's high stored expectation; lowering the stored expectation then
    fires — and the alert names the sidecar as its baseline source."""
    desk = f"desk_side_{uuid4().hex[:8]}"
    geo = [f"Z{uuid4().hex[:6].upper()}"]
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, geo)
        # Fresh sidecar row with a HUGE expectation — no trailing history at
        # all, so the inline compute would read μ=0/σ=0 and page on any spike.
        await _insert_sidecar_baseline(
            conn, desk, ats.METRIC_SIGNAL_VOLUME,
            expected=1000.0, robust_sigma=0.0, hours_ago=1.0,
        )

    r1 = await _run(pg_pool)  # seed (quiet)
    assert ats.TRIGGER_BASELINE in r1.finding.data["seeded_classes"]

    async with pg_pool.acquire() as conn:
        await _insert_signals(conn, geo, 15, days_ago=0.01)  # the spike

    # 15 >= the absolute floor but NOT > 1000 — the sidecar baseline holds.
    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 0

    # Refresh the sidecar with a low expectation → rising edge → fires, and
    # the alert records baseline_source = 'desk_baselines'.
    async with pg_pool.acquire() as conn:
        await _insert_sidecar_baseline(
            conn, desk, ats.METRIC_SIGNAL_VOLUME,
            expected=0.0, robust_sigma=0.0, hours_ago=1.0,
        )
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["metric"] == ats.METRIC_SIGNAL_VOLUME
    assert data["baseline_source"] == ats.BASELINE_SOURCE_SIDECAR
    assert data["current_24h"] == pytest.approx(15.0)
    assert data["baseline_mean"] == pytest.approx(0.0)


async def test_baseline_stale_sidecar_falls_back_inline(pg_pool, clean_slate):
    """A sidecar row older than SIDECAR_FRESH_HOURS is IGNORED: the inline
    trailing-window compute decides (behavior identical to pre-sidecar), and
    the alert records baseline_source = 'inline'."""
    desk = f"desk_stale_{uuid4().hex[:8]}"
    geo = [f"Z{uuid4().hex[:6].upper()}"]
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, geo)
        # STALE row (3 days old) with a huge expectation that would suppress
        # the spike if (wrongly) honored.
        await _insert_sidecar_baseline(
            conn, desk, ats.METRIC_SIGNAL_VOLUME,
            expected=1000.0, robust_sigma=0.0, hours_ago=72.0,
        )

    r1 = await _run(pg_pool)  # seed (quiet)
    assert ats.TRIGGER_BASELINE in r1.finding.data["seeded_classes"]

    async with pg_pool.acquire() as conn:
        await _insert_signals(conn, geo, 15, days_ago=0.01)  # the spike

    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["baseline_source"] == ats.BASELINE_SOURCE_INLINE
    assert data["current_24h"] == pytest.approx(15.0)


async def test_baseline_absent_sidecar_is_byte_identical_inline(
    pg_pool, clean_slate
):
    """No sidecar row at all (the newly-activated-analyst state): the inline
    path runs unchanged — same rising-edge-once semantics as before."""
    desk = f"desk_nosc_{uuid4().hex[:8]}"
    geo = [f"Z{uuid4().hex[:6].upper()}"]
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk, geo)
        for day in range(1, 29):
            await _insert_signals(conn, geo, 1, days_ago=day + 0.5)

    r1 = await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        await _insert_signals(conn, geo, 15, days_ago=0.01)
    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    data = _row_data(rows[0])
    assert data["baseline_source"] == ats.BASELINE_SOURCE_INLINE
    # Rising-edge-once still holds.
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 0


# ---------------------------------------------------------------------------
# DB — per-desk cap + rollup end-to-end
# ---------------------------------------------------------------------------


async def test_per_desk_cap_rolls_up_and_still_advances_watermarks(
    pg_pool, clean_slate
):
    desk = f"desk_cap_{uuid4().hex[:8]}"
    dims = [
        "leadership_transition",
        "energy_security",
        "escalation",
        "narrative_coordination",
        "internal_stability",
    ]
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(conn, desk, {d: "watch" for d in dims})
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        await _insert_scorecard(conn, desk, {d: "high" for d in dims})

    r2 = await _run(pg_pool)
    # 5 deteriorations → 3 fired + 1 rollup summarizing 2, honestly counted.
    assert r2.finding.data["fired"] == 3
    assert r2.finding.data["rollups"] == 1
    assert r2.finding.data["suppressed_into_rollups"] == 2
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 4
    rollup_rows = [
        r for r in rows if _row_data(r).get("trigger_class") == "rollup"
    ]
    assert len(rollup_rows) == 1
    roll_data = _row_data(rollup_rows[0])
    assert roll_data["suppressed_count"] == 2
    assert len(roll_data["suppressed"]) == 2
    assert rollup_rows[0]["target_id"] == desk

    # EVERY transition (kept + summarized) is watermarked — nothing refires.
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 0
    assert r3.finding.data["rollups"] == 0
    async with pg_pool.acquire() as conn:
        assert len(await _alert_rows(conn)) == 4


# ---------------------------------------------------------------------------
# X-1 — a descriptor-declared option DEMONSTRABLY reaches this handler
#
# `per_desk_cap` was the headline dead knob: the handler read
# `options.get("per_desk_cap", DEFAULT_PER_DESK_CAP)` and nothing could ever put
# a value there, so DEFAULT_PER_DESK_CAP=3 was the only reachable behavior. This
# pair runs the SAME stimuli through the SAME handler, differing only in whether
# the descriptor carries `method.options`, and traverses the runtime's OWN merge
# function (`dapr_actors._merge_descriptor_options`) rather than hand-building
# the mapping — the point is that the production channel works, not that a dict
# can hold an integer.
# ---------------------------------------------------------------------------


def _descriptor_with_options(options: dict[str, Any] | None):
    """The SHIPPED alert_trigger_scan descriptor, optionally given options.

    Rehydrated the way the registry does it (``strict=False``, per
    ``DescriptorStore.get_typed``).
    """
    from pathlib import Path

    import yaml

    from legba.data.schemas.analyst import AnalystDescriptor

    path = (
        Path(__file__).resolve().parents[2]
        / "descriptors"
        / "analyst_alert_trigger_scan.yaml"
    )
    body = yaml.safe_load(path.read_text())
    body["identity"]["version"] = "0" * 16
    if options is not None:
        body["method"]["options"] = options
    return AnalystDescriptor.model_validate(body, strict=False)


async def _run_via_descriptor(pool: Any, options: dict[str, Any] | None):
    """Fire the handler through the runtime's descriptor-options merge."""
    from legba.runtime.dapr_actors import _merge_descriptor_options

    descriptor = _descriptor_with_options(options)
    run_options: dict[str, Any] = {
        "analyst_id": "alert_trigger_scan",
        "analyst_version": descriptor.identity.version,
        "run_id": str(uuid4()),
        "sub_handler": "alert_trigger_scan",
    }
    receipt = _merge_descriptor_options(run_options, descriptor, actor_id="test")
    result = await ats.handle([], run_options, _Deps(pool, _FakeDispatcher()))
    assert isinstance(result, AnalystMethodResult)
    return result, receipt


async def _seed_five_deteriorations(pool: Any) -> str:
    """Five same-desk band deteriorations in one scan — enough to exercise any
    cap between 1 and 5."""
    desk = f"desk_x1_{uuid4().hex[:8]}"
    dims = [
        "leadership_transition",
        "energy_security",
        "escalation",
        "narrative_coordination",
        "internal_stability",
    ]
    async with pool.acquire() as conn:
        await _insert_scorecard(conn, desk, {d: "watch" for d in dims})
    await _run(pool)  # seed the class silently
    async with pool.acquire() as conn:
        await _insert_scorecard(conn, desk, {d: "high" for d in dims})
    return desk


async def test_no_descriptor_options_is_byte_identical_to_the_default_cap(
    pg_pool, clean_slate
):
    """The shipped descriptor carries no options block — the handler must
    behave EXACTLY as it did before ``method.options`` existed: cap 3."""
    await _seed_five_deteriorations(pg_pool)

    result, receipt = await _run_via_descriptor(pg_pool, None)

    assert receipt is None, "an options-less descriptor must not touch the run"
    assert result.finding.data["per_desk_cap"] == ats.DEFAULT_PER_DESK_CAP == 3
    assert result.finding.data["fired"] == 3
    assert result.finding.data["rollups"] == 1
    assert result.finding.data["suppressed_into_rollups"] == 2
    # No option receipt step on the trace either.
    assert [
        s
        for s in (result.intermediate_steps or [])
        if s.get("phase") == "handler_options"
    ] == []


async def test_descriptor_options_reach_the_handler_and_change_the_cap(
    pg_pool, clean_slate
):
    """THE X-1 proof: a NON-STANDARD ``per_desk_cap`` declared on the
    descriptor changes what this scan actually emits."""
    desk = await _seed_five_deteriorations(pg_pool)

    result, receipt = await _run_via_descriptor(pg_pool, {"per_desk_cap": 1})

    # The handler honored the DESCRIPTOR's cap, not DEFAULT_PER_DESK_CAP.
    assert result.finding.data["per_desk_cap"] == 1 != ats.DEFAULT_PER_DESK_CAP
    assert result.finding.data["fired"] == 1
    assert result.finding.data["rollups"] == 1
    assert result.finding.data["suppressed_into_rollups"] == 4

    # ...and the real product — the side-written alert rows — followed.
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 2  # 1 kept + 1 rollup
    rollups = [r for r in rows if _row_data(r).get("trigger_class") == "rollup"]
    assert len(rollups) == 1
    assert _row_data(rollups[0])["suppressed_count"] == 4
    assert rollups[0]["target_id"] == desk

    # The run receipt states what took effect.
    assert receipt["status"] == "applied"
    assert receipt["applied"] == {"per_desk_cap": 1}

    # Fire-once still holds under a descriptor-set cap: every transition the
    # rollup summarized is watermarked, so nothing refires.
    r2, _ = await _run_via_descriptor(pg_pool, {"per_desk_cap": 1})
    assert r2.finding.data["fired"] == 0
    assert r2.finding.data["rollups"] == 0


async def test_a_bad_descriptor_option_falls_back_to_the_handler_default(
    pg_pool, clean_slate
):
    """Loud degrade, end to end: an out-of-range cap is DROPPED and the scan
    runs on the in-source default rather than on nonsense."""
    await _seed_five_deteriorations(pg_pool)

    result, receipt = await _run_via_descriptor(
        pg_pool, {"per_desk_cap": 0, "per_desk_kap": 9}
    )

    assert result.finding.data["per_desk_cap"] == ats.DEFAULT_PER_DESK_CAP
    assert result.finding.data["fired"] == 3
    assert receipt["status"] == "degraded"
    assert receipt["applied"] == {}
    assert {r["key"] for r in receipt["rejected"]} == {
        "per_desk_cap",
        "per_desk_kap",
    }


# ---------------------------------------------------------------------------
# DB — trigger 6: geo convergence (folded 2026-07-29 from the former
# standalone geo_convergence_scan analyst — see geo_convergence_scan.py's
# module docstring). Ported from that module's own former DB-lifecycle suite;
# same stimuli, now through alert_trigger_scan.handle().
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def geo_clean_slate(pg_pool, clean_slate):
    """Extra cleanup for the geo tests: their own signals/source rows. The
    shared ``clean_slate`` above only handles alert_trigger_watermarks +
    analyst_outputs — geo signals/sources need their own pattern-scoped wipe
    (mirrors the pre-fold geo_convergence_scan test file's own clean_slate),
    since alert_trigger_scan's OTHER five classes never touch those tables."""
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM signals WHERE source_id LIKE 'geo6test.%'")
        await conn.execute(
            "DELETE FROM source_descriptors WHERE descriptor_id LIKE 'geo6test.%'"
        )
    yield


async def _insert_geo_source(conn: Any, source_id: str, family: str) -> None:
    await conn.execute(
        "INSERT INTO source_descriptors (descriptor_id, version, schema_uri, "
        "is_head, kind, state, owner, name, body) "
        "VALUES ($1, 'v0', 'legba/source/1.0.0', TRUE, 'rss', 'active', "
        "'test', $1, $2::jsonb) "
        "ON CONFLICT DO NOTHING",
        source_id,
        json.dumps({"scope": {"tags": [family]}}),
    )


async def _insert_geo_signal(
    conn: Any,
    source_id: str,
    *,
    geo_tags: list[str] | None = None,
    lat: float | None = None,
    lon: float | None = None,
    precision: str | None = None,
    geo_source: str | None = None,
    hours_ago: float = 1.0,
) -> str:
    from datetime import datetime, timedelta, timezone

    sid = uuid4()
    payload: dict[str, Any] = {"title": "t"}
    geo_block: dict[str, Any] = {}
    if lat is not None:
        geo_block["lat"] = lat
        geo_block["lon"] = lon
    if precision is not None:
        geo_block["precision"] = precision
    if geo_source is not None:
        geo_block["source"] = geo_source
    if geo_block:
        payload["geo"] = geo_block
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    await conn.execute(
        "INSERT INTO signals (id, source_id, payload, geo, fetched_at) "
        "VALUES ($1, $2, $3::jsonb, $4::text[], $5)",
        sid,
        source_id,
        json.dumps(payload),
        geo_tags or [],
        ts,
    )
    return str(sid)


async def _seed_three_geo_family_sources(conn: Any) -> None:
    await _insert_geo_source(conn, "geo6test.quake", "gis")
    await _insert_geo_source(conn, "geo6test.news", "news")
    await _insert_geo_source(conn, "geo6test.tg", "social")
    await _insert_geo_source(conn, "geo6test.news2", "news")
    await _insert_geo_source(conn, "geo6test.health", "health")


def _geo_rows(rows: list[Any]) -> list[Any]:
    """Just this scan's geo_convergence rows (there should be no other class
    activity in these tests, but filter defensively). Unlike the other five
    classes, a geo candidate's own ``data`` dict carries NO explicit
    ``trigger_class`` key (byte-identical to the pre-fold standalone
    analyst's payload — see geo_convergence_scan._formation_candidate /
    _dissolution_candidate); its ``event`` key ('formed'/'dissolved') is
    unique to this class among the six, so it is the reliable filter here.
    The trigger class IS discoverable on every row regardless, via the
    universal ``tags`` column every class gets from _write_alert_row
    (``f"trigger:{cand.trigger_class}"``) — just not selected by
    ``_alert_rows`` above."""
    return [r for r in rows if _row_data(r).get("event") in ("formed", "dissolved")]


async def test_geo_convergence_seeds_silently_then_steady_state_is_quiet(
    pg_pool, geo_clean_slate
):
    async with pg_pool.acquire() as conn:
        await _seed_three_geo_family_sources(conn)
        for src in ("geo6test.quake", "geo6test.news", "geo6test.tg"):
            await _insert_geo_signal(conn, src, geo_tags=["IQ"])

    r1 = await _run(pg_pool)
    assert ats.TRIGGER_GEO_CONVERGENCE in r1.finding.data["seeded_classes"]
    async with pg_pool.acquire() as conn:
        assert _geo_rows(await _alert_rows(conn)) == []  # history never pages
        wm = await conn.fetchrow(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = 'country:IQ'",
            ats.TRIGGER_GEO_CONVERGENCE,
        )
    assert wm is not None
    state = json.loads(wm["state"]) if isinstance(wm["state"], str) else wm["state"]
    assert state["active"] is True
    assert state["families"] == ["gis", "news", "social"]

    # …and the unchanged second scan fires nothing (no refire).
    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 0
    async with pg_pool.acquire() as conn:
        assert _geo_rows(await _alert_rows(conn)) == []


async def test_geo_convergence_formation_fires_once_with_contributors_then_never_refires(
    pg_pool, geo_clean_slate
):
    async with pg_pool.acquire() as conn:
        await _seed_three_geo_family_sources(conn)
        # Below the bar at seed time: two families only.
        await _insert_geo_signal(conn, "geo6test.quake", geo_tags=["SY"])
        await _insert_geo_signal(conn, "geo6test.news", geo_tags=["SY"])
    await _run(pg_pool)  # seeds (nothing formed)

    async with pg_pool.acquire() as conn:
        # Third DISTINCT family arrives → formation edge.
        await _insert_geo_signal(conn, "geo6test.tg", geo_tags=["SY"])
    dispatcher = _FakeDispatcher()
    r2 = await _run(pg_pool, dispatcher)
    assert r2.finding.data["fired"] == 1

    async with pg_pool.acquire() as conn:
        rows = _geo_rows(await _alert_rows(conn))
    assert len(rows) == 1
    row = rows[0]
    assert row["severity"] == "medium"
    data = _row_data(row)
    assert data["event"] == "formed"
    assert data["bin_key"] == "country:SY"
    assert data["distinct_family_count"] == 3
    assert sorted(data["families"]) == ["gis", "news", "social"]
    # Payload lists the contributing signals: ids + sources + families —
    # byte-identical field names/shape to the pre-fold standalone analyst.
    contribs = data["contributing_signals"]
    assert len(contribs) == 3
    assert {c["source_id"] for c in contribs} == {
        "geo6test.quake", "geo6test.news", "geo6test.tg",
    }
    assert all(c["id"] and c["family"] for c in contribs)
    # Outward fan-out carried the converged payload — channel_name PRESERVED
    # from the pre-fold standalone analyst's own CHANNEL_NAME (the dedup/
    # cooldown semantics are unaffected either way — see _CHANNEL_BY_CLASS).
    assert len(dispatcher.payloads) == 1
    p = dispatcher.payloads[0]
    assert p.severity == "medium"
    assert p.channel_name == "geo_convergence"
    assert p.verify_state.startswith("unverified")
    assert p.receipt_path == f"/api/v1/lineage/alert/{row['id']}"

    # Persisting convergence: the next scan fires NOTHING.
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 0
    async with pg_pool.acquire() as conn:
        assert len(_geo_rows(await _alert_rows(conn))) == 1


async def test_geo_convergence_same_family_pileon_never_fires(
    pg_pool, geo_clean_slate
):
    await _run(pg_pool)  # seed on an empty window
    async with pg_pool.acquire() as conn:
        await _seed_three_geo_family_sources(conn)
        # 12 signals, TWO sources, ONE family (news) → diversity bar unmet.
        for _ in range(6):
            await _insert_geo_signal(conn, "geo6test.news", geo_tags=["LY"])
            await _insert_geo_signal(conn, "geo6test.news2", geo_tags=["LY"])
    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 0
    async with pg_pool.acquire() as conn:
        assert _geo_rows(await _alert_rows(conn)) == []

    # Two more DISTINCT families flip it.
    async with pg_pool.acquire() as conn:
        await _insert_geo_signal(conn, "geo6test.quake", geo_tags=["LY"])
        await _insert_geo_signal(conn, "geo6test.health", geo_tags=["LY"])
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = _geo_rows(await _alert_rows(conn))
    assert len(rows) == 1
    assert _row_data(rows[0])["distinct_family_count"] == 3


async def test_geo_convergence_cell_tier_excludes_country_centroid_geocodes(
    pg_pool, geo_clean_slate
):
    await _run(pg_pool)  # seed empty
    async with pg_pool.acquire() as conn:
        await _seed_three_geo_family_sources(conn)
        # Three families with point-trustworthy coordinates in one 1° cell
        # (no geo country tags → the country tier stays out of the picture).
        await _insert_geo_signal(
            conn, "geo6test.quake",
            lat=33.2, lon=44.1, geo_source="geometry", precision="country",
        )
        await _insert_geo_signal(
            conn, "geo6test.news",
            lat=33.8, lon=44.9, precision="municipality",
        )
        await _insert_geo_signal(
            conn, "geo6test.tg",
            lat=33.5, lon=44.5, precision="region",
        )
        # A country-precision nominatim point (country CENTROID) in the same
        # cell MUST NOT enter the cell tier.
        await _insert_geo_signal(
            conn, "geo6test.health",
            lat=33.5, lon=44.2, precision="country", geo_source="nominatim",
        )
    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = _geo_rows(await _alert_rows(conn))
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["bin_kind"] == "cell"
    assert data["bin_key"] == "cell:33:44"
    # The centroid signal is excluded: 3 contributors, no 'health' family.
    assert data["signal_count"] == 3
    assert sorted(data["families"]) == ["gis", "news", "social"]


async def test_geo_convergence_dissolution_fires_once_then_quiet(
    pg_pool, geo_clean_slate
):
    async with pg_pool.acquire() as conn:
        await _seed_three_geo_family_sources(conn)
        for src in ("geo6test.quake", "geo6test.news", "geo6test.tg"):
            await _insert_geo_signal(conn, src, geo_tags=["SO"])
    await _run(pg_pool)  # seeds country:SO as active

    async with pg_pool.acquire() as conn:
        # The window empties out (signals age past 24h).
        await conn.execute(
            "UPDATE signals SET fetched_at = now() - interval '3 days' "
            "WHERE source_id LIKE 'geo6test.%'"
        )
    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = _geo_rows(await _alert_rows(conn))
    assert len(rows) == 1
    row = rows[0]
    assert row["severity"] == "info"
    data = _row_data(row)
    assert data["event"] == "dissolved"
    assert data["bin_key"] == "country:SO"
    assert sorted(data["previous_families"]) == ["gis", "news", "social"]

    # Dissolution fired ONCE — the next scan is quiet.
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 0
    async with pg_pool.acquire() as conn:
        assert len(_geo_rows(await _alert_rows(conn))) == 1
