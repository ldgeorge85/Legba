# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2-3 — the ``band_calibration_tracker`` scorecard calibration harness.

Pure tests (no DB): registry wiring + verify-exempt drift, the horizon-outcome
truth table, honest-None rates on empty denominators, and the summary
finding's honesty (zero state + no Brier key of any kind). Ephemeral-DB tests
(the ``migrated_pg`` fixture, migration 0093 applied): claim logging with the
no-dup-per-transition unique-index floor, the watermark no-refire property
(including a lost-watermark rescan), the non-directional skip, the horizon
resolution truth table against live SQL (both horizons, held / worsened /
improved / reverted / insufficient / unresolvable), the never-overwrite +
void contracts, and the run finding's aggregate shape.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    OutputKind,
)
from legba.data.analysts.deterministic_handlers import (
    band_calibration_tracker as bct,
)
from legba.data.config import PostgresConfig
from legba.data.provenance.kinds import STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_registered_as_finding_sub_handler():
    """The summary IS the measurement product the /eval/calibration band
    section reads — a genuine FINDING (the calibration_tracking precedent),
    which requires membership in the verify-exempt structural registry (the
    trace-only drift guard asserts set equality)."""
    assert SUB_HANDLERS["band_calibration_tracker"] is bct.handle
    assert (
        OUTPUT_KIND_BY_SUB_HANDLER["band_calibration_tracker"]
        is OutputKind.FINDING
    )
    assert "band_calibration_tracker" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


async def test_refuses_loud_without_pool():
    with pytest.raises(RuntimeError, match="pg_pool"):
        await bct.handle([], {"sub_handler": "band_calibration_tracker"}, None)


def test_descriptor_validates_and_ships_draft():
    """The shipped descriptor round-trips the real AnalystDescriptor schema
    (the same validation the registrar runs at bringup) and ships DRAFT —
    registration + the activate flip are deploy steps."""
    import pathlib

    import yaml

    from legba.data.schemas.analyst import AnalystDescriptor

    root = pathlib.Path(__file__).resolve().parents[2]
    body = yaml.safe_load(
        (root / "descriptors" / "analyst_band_calibration_tracker.yaml").read_text()
    )
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    assert desc.identity.id == "band_calibration_tracker"
    assert desc.identity.kind == "deterministic"
    assert desc.identity.state == "draft"
    assert desc.method.sub_handler == "band_calibration_tracker"
    # META — no per-target fan-out.
    assert desc.subscription.targets is None


# ---------------------------------------------------------------------------
# Pure — the horizon-outcome truth table (hard_band_at_horizon_v1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "direction,to_band,at_horizon,outcome",
    [
        # Deterioration claim (watch→elevated shape): held / worsened / reverted.
        ("deterioration", "elevated", "elevated", "held"),
        ("deterioration", "elevated", "high", "worsened"),
        ("deterioration", "elevated", "critical", "worsened"),
        ("deterioration", "elevated", "watch", "reverted"),
        ("deterioration", "elevated", "low", "reverted"),
        # Improvement claim (high→watch shape): held / improved / reverted.
        ("improvement", "watch", "watch", "held"),
        ("improvement", "watch", "low", "improved"),
        ("improvement", "watch", "elevated", "reverted"),
        ("improvement", "watch", "critical", "reverted"),
        # Coverage lost at horizon — neither confirms nor reverts.
        ("deterioration", "elevated", "insufficient-evidence", "insufficient"),
        ("improvement", "watch", "insufficient-evidence", "insufficient"),
        # No later row / unreadable band — honest abstain, never a grade.
        ("deterioration", "elevated", None, "unresolvable"),
        ("deterioration", "elevated", "garbage", "unresolvable"),
        ("improvement", "not-a-band", "watch", "unresolvable"),
    ],
)
def test_classify_horizon_outcome(direction, to_band, at_horizon, outcome):
    assert bct.classify_horizon_outcome(direction, to_band, at_horizon) == outcome


def test_confirmed_outcomes_are_the_persistence_numerator():
    assert bct.CONFIRMED_OUTCOMES == {"held", "worsened", "improved"}


# ---------------------------------------------------------------------------
# Pure — aggregate honesty
# ---------------------------------------------------------------------------


def _claim(dim: str, direction: str, o14=None, o28=None) -> dict[str, Any]:
    return {
        "dimension": dim,
        "direction": direction,
        "outcome_14": o14,
        "outcome_28": o28,
    }


def test_summarize_rates_honest_none_on_zero_denominator():
    """insufficient/unresolvable are EXCLUDED from both denominators; a zero
    scored denominator yields None rates, never a fabricated 0.0/1.0."""
    rows = [
        _claim("escalation", "deterioration", o14="insufficient"),
        _claim("escalation", "deterioration", o14="unresolvable"),
    ]
    s = bct.summarize_claims(rows, lookback_days=365)
    h14 = s["horizons"]["14d"]
    assert h14["resolved"] == 2
    assert h14["scored"] == 0
    assert h14["persistence_rate"] is None
    assert h14["reversal_rate"] is None
    assert h14["excluded_insufficient"] == 1
    assert h14["excluded_unresolvable"] == 1


def test_summarize_splits_by_direction_and_dimension():
    rows = [
        _claim("escalation", "deterioration", o14="held", o28="reverted"),
        _claim("escalation", "deterioration", o14="worsened"),
        _claim("escalation", "deterioration", o14="reverted"),
        _claim("energy_security", "improvement", o14="improved"),
    ]
    s = bct.summarize_claims(rows, lookback_days=365)
    assert s["claims_total"] == 4
    h14 = s["horizons"]["14d"]
    assert h14["confirmed"] == 3 and h14["reverted"] == 1
    assert h14["persistence_rate"] == pytest.approx(0.75)
    assert h14["reversal_rate"] == pytest.approx(0.25)
    # 28d: only one resolved (reverted) — persistence honestly 0.0 with n=1.
    h28 = s["horizons"]["28d"]
    assert h28["resolved"] == 1 and h28["scored"] == 1
    assert h28["persistence_rate"] == pytest.approx(0.0)
    # Direction split: deteriorations vs improvements, separately.
    det = s["by_direction"]["deterioration"]
    assert det["claims"] == 3
    assert det["14d"]["persistence_rate"] == pytest.approx(2 / 3)
    imp = s["by_direction"]["improvement"]
    assert imp["14d"]["persistence_rate"] == pytest.approx(1.0)
    # Dimension split carries its own sample sizes.
    assert s["by_dimension"]["escalation"]["claims"] == 3
    assert s["by_dimension"]["energy_security"]["14d"]["scored"] == 1
    # The honesty contract rides the aggregate itself.
    assert s["no_brier"] is True
    assert "not probabilities" in s["honesty_note"]


def test_build_finding_zero_state_honesty_and_no_brier_key():
    """Zero state: an explicit no-transitions / no-claim-made statement, the
    calibration tag, the honesty note — and NO brier* key anywhere (bands are
    not probabilities; the harness must never mint one)."""
    summary = bct.summarize_claims([], lookback_days=365)
    finding = bct.build_finding(
        summary=summary,
        logged=0,
        resolved_by_horizon={"14d": 0, "28d": 0},
        skipped_non_directional=0,
        scanned_rows=0,
        warnings=[],
    )
    assert "0 claims on record" in finding.title
    assert "no persistence claim made" in finding.title
    assert "calibration" in finding.tags
    bc = finding.data["band_calibration"]
    assert bc["claims_total"] == 0
    assert bc["no_brier"] is True
    assert bc["honesty_note"] == bct.HONESTY_NOTE
    assert bct.HONESTY_NOTE in finding.body
    # NEVER a skill boast and NEVER a Brier: no brier-named metric key exists
    # (no_brier is the explicit negative flag, not a metric).
    assert not any(k.startswith("brier") for k in bc)
    assert bc["horizons"]["14d"]["persistence_rate"] is None


def test_build_finding_nonzero_title_counts_only():
    summary = bct.summarize_claims(
        [_claim("escalation", "deterioration", o14="held")], lookback_days=365
    )
    finding = bct.build_finding(
        summary=summary,
        logged=2,
        resolved_by_horizon={"14d": 1, "28d": 0},
        skipped_non_directional=1,
        scanned_rows=5,
        warnings=[],
    )
    assert "logged=2" in finding.title and "resolved=1" in finding.title
    assert finding.data["band_calibration"]["resolved_this_run"] == 1
    assert finding.data["band_calibration"]["logged_this_run"] == 2


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
    """Fresh claims + scan-state tables and NO leftover scorecard rows, so
    every test gets its own deterministic scan window (other suites in this
    session DB insert their own scorecard rows per-test too)."""
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE band_calibration_claims")
        await conn.execute("TRUNCATE band_calibration_scan_state")
        await conn.execute("DELETE FROM analyst_outputs WHERE kind = 'scorecard'")
        await conn.execute(
            "DELETE FROM analyst_outputs "
            "WHERE analyst_id = 'band_calibration_tracker'"
        )
    yield


class _Deps:
    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool


async def _run(pool: Any, **opts: Any):
    options = {
        "sub_handler": "band_calibration_tracker",
        "analyst_id": "band_calibration_tracker",
        "run_id": str(uuid4()),
        **opts,
    }
    result = await bct.handle([], options, _Deps(pool))
    assert isinstance(result, AnalystMethodResult)
    return result


def _bc(result) -> dict[str, Any]:
    return result.finding.data["band_calibration"]


async def _insert_scorecard(
    conn: Any,
    desk: str,
    bands: dict[str, str],
    *,
    produced_at: datetime | None = None,
):
    """One kind='scorecard' row in the LIVE column shape (payload `data` with
    `bands` NESTED under the data column — the scorecard_producer dump)."""
    row_id = uuid4()
    dims = {
        dim: {
            "band": band,
            "basis": [],
            "reason": "qualified",
            "effective_confidence": 0.8,
        }
        for dim, band in bands.items()
    }
    data = {
        "kind_marker": "scorecard",
        "tags": ["deterministic", "scorecard"],
        "data": {
            "sub_handler": "scorecard_producer",
            "bands": {"target_id": desk, "dimensions": dims},
        },
    }
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, data, target_id, analyst_id, "
        "   schema_uri, produced_at) "
        "VALUES ($1, 'scorecard', $2, '', 1.0, $3::jsonb, $4, "
        "        'scorecard_producer', 'iglu:legba/scorecard/jsonschema/1-0-0', "
        "        COALESCE($5::timestamptz, now()))",
        row_id,
        f"Scorecard {desk}",
        json.dumps(data),
        desk,
        produced_at,
    )
    return row_id


async def _insert_claim(
    conn: Any,
    *,
    desk: str,
    dimension: str,
    from_band: str,
    to_band: str,
    direction: str,
    transition_at: datetime,
    outcome_14: str | None = None,
    resolved_by_14: str | None = None,
):
    claim_id = uuid4()
    await conn.execute(
        "INSERT INTO band_calibration_claims "
        "  (id, desk, dimension, from_band, to_band, direction, transition_at, "
        "   scorecard_row_id, resolution_spec, horizon_14_at, horizon_28_at, "
        "   outcome_14, resolved_by_14) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7::timestamptz, $8, $9, "
        "        $7::timestamptz + interval '14 days', "
        "        $7::timestamptz + interval '28 days', $10, $11)",
        claim_id,
        desk,
        dimension,
        from_band,
        to_band,
        direction,
        transition_at,
        uuid4(),
        bct.RESOLUTION_SPEC,
        outcome_14,
        resolved_by_14,
    )
    return claim_id


async def _claims_for(conn: Any, desk: str) -> list[Any]:
    return await conn.fetch(
        "SELECT * FROM band_calibration_claims WHERE desk = $1 "
        "ORDER BY dimension, transition_at",
        desk,
    )


# ---------------------------------------------------------------------------
# DB — claim logging: no dup per transition, watermark no-refire
# ---------------------------------------------------------------------------


async def test_logs_claim_once_and_never_refires(pg_pool, clean_slate):
    desk = f"desk_bc_{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(
            conn, desk, {"escalation": "watch"},
            produced_at=now - timedelta(minutes=3),
        )

    # Run 1 — a single row per desk has nothing to transition FROM.
    r1 = await _run(pg_pool)
    assert _bc(r1)["logged_this_run"] == 0
    assert _bc(r1)["scanned_scorecard_rows"] == 1

    async with pg_pool.acquire() as conn:
        row_b = await _insert_scorecard(
            conn, desk, {"escalation": "elevated"},
            produced_at=now - timedelta(minutes=1),
        )

    # Run 2 — the watch→elevated transition logs exactly one claim.
    r2 = await _run(pg_pool)
    assert _bc(r2)["logged_this_run"] == 1
    async with pg_pool.acquire() as conn:
        claims = await _claims_for(conn, desk)
    assert len(claims) == 1
    c = claims[0]
    assert c["dimension"] == "escalation"
    assert c["from_band"] == "watch" and c["to_band"] == "elevated"
    assert c["direction"] == "deterioration"
    assert str(c["scorecard_row_id"]) == str(row_b)
    assert c["resolution_spec"] == bct.RESOLUTION_SPEC
    assert c["horizon_14_at"] == c["transition_at"] + timedelta(days=14)
    assert c["horizon_28_at"] == c["transition_at"] + timedelta(days=28)
    # Unresolved until the horizon passes — honesty fields stay NULL.
    assert c["outcome_14"] is None and c["outcome_28"] is None

    # Run 3 — watermark: nothing new is scanned, nothing refires.
    r3 = await _run(pg_pool)
    assert _bc(r3)["logged_this_run"] == 0
    assert _bc(r3)["scanned_scorecard_rows"] == 0

    # Lost watermark: the rescan re-compares history but the unique
    # (desk, dimension, scorecard_row_id) index blocks any duplicate claim.
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE band_calibration_scan_state")
    r4 = await _run(pg_pool)
    assert _bc(r4)["logged_this_run"] == 0
    async with pg_pool.acquire() as conn:
        claims = await _claims_for(conn, desk)
    assert len(claims) == 1


async def test_non_directional_transitions_are_skipped(pg_pool, clean_slate):
    """Evidence transitions carry no directional risk statement — no claim is
    logged; the receipt counts them honestly."""
    desk = f"desk_bc_{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(
            conn, desk, {"escalation": "elevated"},
            produced_at=now - timedelta(minutes=3),
        )
        await _insert_scorecard(
            conn, desk, {"escalation": "insufficient-evidence"},
            produced_at=now - timedelta(minutes=1),
        )
    r = await _run(pg_pool)
    assert _bc(r)["logged_this_run"] == 0
    assert _bc(r)["skipped_non_directional"] == 1
    async with pg_pool.acquire() as conn:
        assert await _claims_for(conn, desk) == []


# ---------------------------------------------------------------------------
# DB — horizon resolution truth table
# ---------------------------------------------------------------------------


async def test_resolution_truth_table_14d_only(pg_pool, clean_slate):
    """T0 = now-15d: the 14d horizon has passed (graded), the 28d has not
    (stays NULL). One later scorecard row inside (T0, T0+14d] carries the
    then-current bands the spec reads."""
    desk = f"desk_bc_{uuid4().hex[:8]}"
    desk_bare = f"desk_bc_{uuid4().hex[:8]}"  # no later scorecard row at all
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(days=15)
    async with pg_pool.acquire() as conn:
        # The frozen later read: one row 5 days after T0 (within the horizon).
        await _insert_scorecard(
            conn,
            desk,
            {
                "escalation": "elevated",              # == to_band → held
                "military_posture": "critical",        # beyond → worsened
                "energy_security": "low",              # further down → improved
                "narrative_coordination": "low",       # back down → reverted
                "internal_stability": "insufficient-evidence",  # coverage lost
            },
            produced_at=t0 + timedelta(days=5),
        )
        await _insert_claim(
            conn, desk=desk, dimension="escalation", from_band="watch",
            to_band="elevated", direction="deterioration", transition_at=t0,
        )
        await _insert_claim(
            conn, desk=desk, dimension="military_posture", from_band="watch",
            to_band="elevated", direction="deterioration", transition_at=t0,
        )
        await _insert_claim(
            conn, desk=desk, dimension="energy_security", from_band="elevated",
            to_band="watch", direction="improvement", transition_at=t0,
        )
        await _insert_claim(
            conn, desk=desk, dimension="narrative_coordination",
            from_band="watch", to_band="elevated", direction="deterioration",
            transition_at=t0,
        )
        await _insert_claim(
            conn, desk=desk, dimension="internal_stability", from_band="watch",
            to_band="elevated", direction="deterioration", transition_at=t0,
        )
        # A desk with NO later scorecard row: the frozen window is empty →
        # unresolvable (an honest abstain, never a fabricated outcome).
        await _insert_claim(
            conn, desk=desk_bare, dimension="escalation", from_band="watch",
            to_band="elevated", direction="deterioration", transition_at=t0,
        )

    r = await _run(pg_pool)
    assert _bc(r)["resolved_this_run_by_horizon"] == {"14d": 6, "28d": 0}

    async with pg_pool.acquire() as conn:
        by_dim = {c["dimension"]: c for c in await _claims_for(conn, desk)}
        bare = (await _claims_for(conn, desk_bare))[0]
    assert by_dim["escalation"]["outcome_14"] == "held"
    assert by_dim["escalation"]["resolved_band_14"] == "elevated"
    assert by_dim["escalation"]["resolved_by_14"] == bct.RESOLVED_BY
    assert by_dim["escalation"]["resolved_at_14"] is not None
    assert by_dim["military_posture"]["outcome_14"] == "worsened"
    assert by_dim["energy_security"]["outcome_14"] == "improved"
    assert by_dim["narrative_coordination"]["outcome_14"] == "reverted"
    assert by_dim["internal_stability"]["outcome_14"] == "insufficient"
    assert bare["outcome_14"] == "unresolvable"
    assert bare["resolved_band_14"] is None
    # 28d horizon has NOT passed — graded nothing, stays NULL for all.
    assert all(c["outcome_28"] is None for c in by_dim.values())
    assert bare["outcome_28"] is None

    # The run finding's aggregate reflects the truth table honestly:
    # confirmed=3 (held+worsened+improved), reverted=1 → persistence 0.75,
    # insufficient + unresolvable excluded from the denominator.
    h14 = _bc(r)["horizons"]["14d"]
    assert h14["confirmed"] == 3 and h14["reverted"] == 1
    assert h14["persistence_rate"] == pytest.approx(0.75)
    assert h14["reversal_rate"] == pytest.approx(0.25)
    assert h14["excluded_insufficient"] == 1
    assert h14["excluded_unresolvable"] == 1
    # Direction split reports deteriorations and improvements separately.
    assert _bc(r)["by_direction"]["improvement"]["14d"]["confirmed"] == 1
    # And still: no brier-named key anywhere in the section.
    assert not any(k.startswith("brier") for k in _bc(r))


async def test_resolution_both_horizons_and_latest_row_wins(pg_pool, clean_slate):
    """T0 = now-30d: both horizons have passed and grade in ONE run. Each
    horizon reads the LATEST row inside its own (T0, T0+H] window — the 14d
    grade and the 28d grade can differ."""
    desk = f"desk_bc_{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(days=30)
    async with pg_pool.acquire() as conn:
        # Inside (T0, T0+14d]: still elevated → 14d held.
        await _insert_scorecard(
            conn, desk, {"escalation": "elevated"},
            produced_at=t0 + timedelta(days=10),
        )
        # Inside (T0+14d, T0+28d]: dropped back → 28d reverted.
        await _insert_scorecard(
            conn, desk, {"escalation": "watch"},
            produced_at=t0 + timedelta(days=20),
        )
        await _insert_claim(
            conn, desk=desk, dimension="escalation", from_band="watch",
            to_band="elevated", direction="deterioration", transition_at=t0,
        )
    r = await _run(pg_pool)
    assert _bc(r)["resolved_this_run_by_horizon"] == {"14d": 1, "28d": 1}
    async with pg_pool.acquire() as conn:
        c = (await _claims_for(conn, desk))[0]
    assert c["outcome_14"] == "held" and c["resolved_band_14"] == "elevated"
    assert c["outcome_28"] == "reverted" and c["resolved_band_28"] == "watch"


async def test_never_overwrites_and_skips_voided(pg_pool, clean_slate):
    """An already-resolved horizon is NEVER overwritten (operator labels win);
    a 'voided:' horizon is withdrawn from grading and stays NULL."""
    desk = f"desk_bc_{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(days=15)
    async with pg_pool.acquire() as conn:
        # A later row that would grade 'reverted' if the resolver ran.
        await _insert_scorecard(
            conn, desk, {"escalation": "low", "military_posture": "low"},
            produced_at=t0 + timedelta(days=5),
        )
        operator_claim = await _insert_claim(
            conn, desk=desk, dimension="escalation", from_band="watch",
            to_band="elevated", direction="deterioration", transition_at=t0,
            outcome_14="held", resolved_by_14="operator:test",
        )
        voided_claim = await _insert_claim(
            conn, desk=desk, dimension="military_posture", from_band="watch",
            to_band="elevated", direction="deterioration", transition_at=t0,
            resolved_by_14=f"{bct.VOID_PREFIX}test",
        )
    r = await _run(pg_pool)
    assert _bc(r)["resolved_this_run_by_horizon"]["14d"] == 0
    async with pg_pool.acquire() as conn:
        rows = {c["dimension"]: c for c in await _claims_for(conn, desk)}
    assert rows["escalation"]["id"] == operator_claim
    assert rows["escalation"]["outcome_14"] == "held"
    assert rows["escalation"]["resolved_by_14"] == "operator:test"
    assert rows["military_posture"]["id"] == voided_claim
    assert rows["military_posture"]["outcome_14"] is None
    assert rows["military_posture"]["resolved_by_14"] == f"{bct.VOID_PREFIX}test"


# ---------------------------------------------------------------------------
# DB — summary finding honesty (zero state on a live substrate)
# ---------------------------------------------------------------------------


async def test_zero_state_finding_on_empty_substrate(pg_pool, clean_slate):
    """No scorecard rows at all: a finding still returns (the every-run
    contract) and it states the zero honestly — no claim made, no rate."""
    r = await _run(pg_pool)
    assert "0 claims on record" in r.finding.title
    assert "no persistence claim made" in r.finding.title
    assert "calibration" in r.finding.tags
    bc = _bc(r)
    assert bc["claims_total"] == 0
    assert bc["scanned_scorecard_rows"] == 0
    assert bc["horizons"]["14d"]["persistence_rate"] is None
    assert bc["no_brier"] is True
    assert bc["honesty_note"] == bct.HONESTY_NOTE
    assert r.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }
