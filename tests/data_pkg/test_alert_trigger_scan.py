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
from datetime import datetime, timedelta, timezone
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
# Pure — H3-GUARD: semantics_changed pre-empts every other classification.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frm,to",
    [
        ("watch", "high"),        # would be deterioration/high
        ("high", "watch"),        # would be improvement/medium
        ("insufficient-evidence", "high"),  # would be evidence-gained/medium
        ("garbage", "high"),      # would be indeterminate/medium
    ],
)
def test_classify_band_transition_semantics_changed_preempts_everything(frm, to):
    from legba.data.analysts.deterministic_handlers import scorecard_banding

    assert ats.classify_band_transition(frm, to, semantics_changed=True) == (
        scorecard_banding.SEMANTICS_MIGRATION,
        scorecard_banding.SEMANTICS_MIGRATION_SEVERITY,
    )


def test_classify_band_transition_default_is_byte_identical_to_no_semantics_arg():
    """Default `semantics_changed=False` reproduces every existing outcome —
    a caller that never checks semantics gets the same answer it always has."""
    cases = [
        ("watch", "high"), ("low", "watch"), ("high", "critical"),
        ("high", "watch"), ("critical", "low"),
        ("insufficient-evidence", "high"), ("elevated", "insufficient-evidence"),
        ("garbage", "high"),
    ]
    for frm, to in cases:
        assert ats.classify_band_transition(frm, to) == (
            ats.classify_band_transition(frm, to, semantics_changed=False)
        )


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
# Pure — FRAME-3 steady-state suppression guard (2026-08-29)
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def test_classify_finding_suppression_no_prior_state_never_suppresses():
    suppress, reason = ats.classify_finding_suppression(
        prev_state=None,
        severity="high",
        delta_tag="steady",
        now=_NOW,
        cooldown_hours=24,
    )
    assert (suppress, reason) == (False, "first_page_for_desk")


def test_classify_finding_suppression_steady_within_cooldown_suppresses():
    prev = {
        "severity": "high",
        "paged_at": (_NOW - timedelta(hours=2)).isoformat(),
    }
    assert ats.classify_finding_suppression(
        prev_state=prev,
        severity="high",
        delta_tag="steady",
        now=_NOW,
        cooldown_hours=24,
    ) == (True, "steady_state_within_cooldown")
    # An ABSENT tag ("nobody said") is treated the same as steady, never
    # invented as a claim of movement.
    assert ats.classify_finding_suppression(
        prev_state=prev,
        severity="high",
        delta_tag=None,
        now=_NOW,
        cooldown_hours=24,
    ) == (True, "steady_state_within_cooldown")


@pytest.mark.parametrize("delta_tag", ["rose", "fell", "new"])
def test_classify_finding_suppression_delta_tag_vetoes_even_same_band(delta_tag):
    """The model tag is read only as a VETO — it can force a page, but (per
    the original 2026-08-21 deferral) is never trusted ALONE to suppress."""
    prev = {
        "severity": "high",
        "paged_at": (_NOW - timedelta(hours=2)).isoformat(),
    }
    suppress, reason = ats.classify_finding_suppression(
        prev_state=prev,
        severity="high",
        delta_tag=delta_tag,
        now=_NOW,
        cooldown_hours=24,
    )
    assert (suppress, reason) == (False, f"delta_tag_{delta_tag}")


def test_classify_finding_suppression_band_changed_always_pages():
    prev = {
        "severity": "high",
        "paged_at": (_NOW - timedelta(minutes=1)).isoformat(),
    }
    assert ats.classify_finding_suppression(
        prev_state=prev,
        severity="critical",
        delta_tag="steady",
        now=_NOW,
        cooldown_hours=24,
    ) == (False, "band_changed")


def test_classify_finding_suppression_cooldown_elapsed_pages_as_heartbeat():
    prev = {
        "severity": "high",
        "paged_at": (_NOW - timedelta(hours=25)).isoformat(),
    }
    assert ats.classify_finding_suppression(
        prev_state=prev,
        severity="high",
        delta_tag="steady",
        now=_NOW,
        cooldown_hours=24,
    ) == (False, "cooldown_elapsed")
    # Exactly AT the boundary also pages (>=, never a fencepost trap).
    prev_at_boundary = {
        "severity": "high",
        "paged_at": (_NOW - timedelta(hours=24)).isoformat(),
    }
    assert ats.classify_finding_suppression(
        prev_state=prev_at_boundary,
        severity="high",
        delta_tag="steady",
        now=_NOW,
        cooldown_hours=24,
    ) == (False, "cooldown_elapsed")


def test_classify_finding_suppression_unparseable_timestamp_never_suppresses():
    prev = {"severity": "high", "paged_at": "not-a-timestamp"}
    assert ats.classify_finding_suppression(
        prev_state=prev,
        severity="high",
        delta_tag="steady",
        now=_NOW,
        cooldown_hours=24,
    ) == (False, "no_prior_page_timestamp")


# ---------------------------------------------------------------------------
# Pure — D2 90-day wager: daily page budget ranking + allocation
# ---------------------------------------------------------------------------


def _budget_cand(trigger_class: str, sev: str, *, delta_tag=None, title="t"):
    data = {}
    if delta_tag is not None or trigger_class == ats.TRIGGER_FINDING:
        data["severity_delta_tag"] = delta_tag
    return ats.AlertCandidate(
        trigger_class=trigger_class,
        severity=sev,
        title=title,
        body="",
        target_id="d1",
        data=data,
    )


def test_budget_magnitude_tier_band_crossing_beats_within_band():
    band = _budget_cand(ats.TRIGGER_BAND, "high")
    rose = _budget_cand(ats.TRIGGER_FINDING, "high", delta_tag="rose")
    steady = _budget_cand(ats.TRIGGER_FINDING, "high", delta_tag="steady")
    assert ats.budget_magnitude_tier(band) > ats.budget_magnitude_tier(rose)
    assert ats.budget_magnitude_tier(rose) > ats.budget_magnitude_tier(steady)


def test_budget_magnitude_tier_delta_rise_fall_beats_steady():
    for delta in ("rose", "fell", "new"):
        moved = _budget_cand(ats.TRIGGER_FINDING, "high", delta_tag=delta)
        steady = _budget_cand(ats.TRIGGER_FINDING, "high", delta_tag="steady")
        none_tag = _budget_cand(ats.TRIGGER_FINDING, "high", delta_tag=None)
        assert ats.budget_magnitude_tier(moved) > ats.budget_magnitude_tier(steady)
        assert ats.budget_magnitude_tier(moved) > ats.budget_magnitude_tier(none_tag)


def test_apply_daily_page_budget_severity_first_then_magnitude_tier():
    """Severity outranks magnitude: a medium band-crossing still loses to a
    high steady-tag heartbeat — severity is the PRIMARY key."""
    low_crossing = _budget_cand(ats.TRIGGER_BAND, "medium", title="crossing")
    high_heartbeat = _budget_cand(
        ats.TRIGGER_FINDING, "high", delta_tag="steady", title="heartbeat"
    )
    deferred = ats.apply_daily_page_budget(
        [low_crossing, high_heartbeat], already_paged_today=0, budget=1
    )
    assert deferred == 1
    assert high_heartbeat.data["budget_deferred"] is False
    assert low_crossing.data["budget_deferred"] is True


def test_apply_daily_page_budget_respects_already_paged_today():
    cands = [_budget_cand(ats.TRIGGER_BAND, "high", title=f"c{i}") for i in range(3)]
    deferred = ats.apply_daily_page_budget(
        cands, already_paged_today=5, budget=5
    )
    assert deferred == 3
    assert all(c.data["budget_deferred"] is True for c in cands)


def test_apply_daily_page_budget_zero_or_negative_remaining_never_negative_slots():
    cands = [_budget_cand(ats.TRIGGER_BAND, "high", title="only")]
    deferred = ats.apply_daily_page_budget(
        cands, already_paged_today=99, budget=5
    )
    assert deferred == 1
    assert cands[0].data["budget_deferred"] is True


def test_apply_daily_page_budget_marks_every_candidate_explicitly():
    """Even survivors get an explicit False — never just absent."""
    cands = [_budget_cand(ats.TRIGGER_BAND, "high", title="c1")]
    ats.apply_daily_page_budget(cands, already_paged_today=0, budget=5)
    assert cands[0].data["budget_deferred"] is False


# ---------------------------------------------------------------------------
# Pure — D2 addendum: the kind-diversity cap
# ---------------------------------------------------------------------------


def test_kind_cap_next_ranked_other_kind_wins_the_freed_slot():
    """4 same-kind 'high' candidates + 1 lower-ranked other-kind 'medium'
    candidate, cap=3, budget=5: the 4th same-kind candidate is capped OUT,
    and the NEXT-ranked candidate — the other kind — wins the slot it freed,
    even though it individually ranks below the capped-out one."""
    band = [
        _budget_cand(ats.TRIGGER_BAND, "high", title=f"band{i}") for i in range(4)
    ]
    contention = _budget_cand(ats.TRIGGER_CONTENTION, "medium", title="contention0")
    cands = band + [contention]
    deferred = ats.apply_daily_page_budget(
        cands, already_paged_today=0, budget=5, per_kind_cap=3
    )
    assert deferred == 1
    assert sum(1 for c in band if c.data["budget_deferred"] is False) == 3
    assert sum(1 for c in band if c.data["budget_deferred"] is True) == 1
    assert contention.data["budget_deferred"] is False, (
        "the other kind must win the slot the capped-out band candidate freed"
    )


def test_kind_cap_all_one_kind_day_leaves_slots_unused_not_backfilled():
    """6 same-kind candidates, cap=3, budget=5: only 3 page (the cap), the
    other 3 are deferred, and the 2 UNUSED budget slots are never backfilled
    with more of the capped kind — that would defeat the cap's purpose."""
    cands = [
        _budget_cand(ats.TRIGGER_BAND, "high", title=f"band{i}") for i in range(6)
    ]
    deferred = ats.apply_daily_page_budget(
        cands, already_paged_today=0, budget=5, per_kind_cap=3
    )
    paged = [c for c in cands if c.data["budget_deferred"] is False]
    assert len(paged) == 3, "the kind cap binds before the budget does"
    assert deferred == 3


def test_kind_cap_is_day_cumulative_via_already_paged_today_by_kind():
    """A kind already at its cap from an EARLIER scan today must stay capped
    in a LATER scan's candidate batch, even though this batch alone never
    exceeds the cap on its own."""
    cands = [_budget_cand(ats.TRIGGER_SITUATION_ESCALATION, "critical", title="se-new")]
    deferred = ats.apply_daily_page_budget(
        cands,
        already_paged_today=0,
        budget=5,
        per_kind_cap=3,
        already_paged_today_by_kind={ats.TRIGGER_SITUATION_ESCALATION: 3},
    )
    assert deferred == 1
    assert cands[0].data["budget_deferred"] is True


def test_kind_cap_default_does_not_disturb_prior_behavior_below_the_cap():
    """Fewer candidates of one kind than the default cap (3): every existing
    assertion in this file that never passed per_kind_cap explicitly still
    holds — the default only bites when a SINGLE kind would otherwise take
    more than 3 of the day's slots."""
    cands = [
        _budget_cand(ats.TRIGGER_BAND, "high", title=f"band{i}") for i in range(2)
    ]
    deferred = ats.apply_daily_page_budget(cands, already_paged_today=0, budget=5)
    assert deferred == 0
    assert all(c.data["budget_deferred"] is False for c in cands)


def test_budget_per_kind_cap_env_default_and_override(monkeypatch):
    monkeypatch.delenv(ats._BUDGET_PER_KIND_CAP_ENV, raising=False)
    assert ats._budget_per_kind_cap_from_env() == ats.DEFAULT_BUDGET_PER_KIND_CAP == 3
    monkeypatch.setenv(ats._BUDGET_PER_KIND_CAP_ENV, "1")
    assert ats._budget_per_kind_cap_from_env() == 1
    monkeypatch.setenv(ats._BUDGET_PER_KIND_CAP_ENV, "not-a-number")
    assert ats._budget_per_kind_cap_from_env() == ats.DEFAULT_BUDGET_PER_KIND_CAP


def test_daily_page_budget_env_default_and_override(monkeypatch):
    monkeypatch.delenv(ats._DAILY_PAGE_BUDGET_ENV, raising=False)
    assert ats._daily_page_budget_from_env() == ats.DEFAULT_DAILY_PAGE_BUDGET
    monkeypatch.setenv(ats._DAILY_PAGE_BUDGET_ENV, "12")
    assert ats._daily_page_budget_from_env() == 12
    # Garbage falls back to the default rather than raising.
    monkeypatch.setenv(ats._DAILY_PAGE_BUDGET_ENV, "not-a-number")
    assert ats._daily_page_budget_from_env() == ats.DEFAULT_DAILY_PAGE_BUDGET


def test_bool_env_tolerant_parsing(monkeypatch):
    monkeypatch.delenv("LEGBA_TEST_BOOL_ENV_PROBE", raising=False)
    assert ats._bool_env("LEGBA_TEST_BOOL_ENV_PROBE", False) is False
    assert ats._bool_env("LEGBA_TEST_BOOL_ENV_PROBE", True) is True
    for val in ("1", "true", "True", "yes", "on"):
        monkeypatch.setenv("LEGBA_TEST_BOOL_ENV_PROBE", val)
        assert ats._bool_env("LEGBA_TEST_BOOL_ENV_PROBE", False) is True
    for val in ("0", "false", "no", "off", "garbage"):
        monkeypatch.setenv("LEGBA_TEST_BOOL_ENV_PROBE", val)
        assert ats._bool_env("LEGBA_TEST_BOOL_ENV_PROBE", True) is False


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
        # D2 90-day wager (2026-08-29) — the CODE default for both is
        # different (kill list OFF, budget 5; see
        # test_handle_production_defaults_kill_list_off_and_budget_five for
        # a pin on the REAL default with no options override at all). This
        # test HARNESS default keeps every pre-D2 test exercising
        # contention_flip/geo_convergence firing mechanics and multi-alert
        # scans passing unmodified; the D2-specific tests below override
        # these explicitly to exercise the real defaults.
        "contention_flip_enabled": True,
        "geo_convergence_enabled": True,
        "daily_page_budget": 10_000,
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
    conn: Any,
    desk: str,
    bands: dict[str, str],
    basis: dict[str, list] | None = None,
    *,
    semantics: tuple[str | None, str | None] | None = None,
) -> UUID:
    """``semantics`` (H3-GUARD) is ``(banding_semantics, damping_semantics)``;
    omitted (the default) reproduces a PRE-H3 card exactly — no
    ``banding_semantics`` / ``damping_semantics`` keys at all, matching every
    existing caller of this fixture and today's behavior byte-for-byte.
    """
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
    bands_block: dict[str, Any] = {"target_id": desk, "dimensions": dims}
    if semantics is not None:
        bands_block["banding_semantics"], bands_block["damping_semantics"] = semantics
    # Mirror the LIVE column shape: the data COLUMN carries the full payload
    # dump, so the producer's payload `data` (with `bands`) is NESTED.
    data = {
        "kind_marker": "scorecard",
        "tags": ["deterministic", "scorecard"],
        "data": {
            "sub_handler": "scorecard_producer",
            "bands": bands_block,
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
    delta_tag: str | None = None,
    derived: list[UUID] | None = None,
) -> UUID:
    fid = uuid4()
    tags = [f"severity:{sev_tag}"] if sev_tag else []
    if delta_tag:
        tags.append(f"severity_delta:{delta_tag}")
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
        # BOTH of this desk's dimensions were watermarked. Scoped to the desk
        # because the band scan seeds every head scorecard in the table, whoever
        # owns it: `clean_slate` resets the watermarks but cannot reset
        # `analyst_outputs`, so a sibling file's scorecard is a third genuine
        # key and the global count failed at 3 == 2 in the shuffled nightly on a
        # scan that had watermarked this desk exactly right. The band watermark
        # key is `f"{desk}|{dim}"` — see `_scan_band_crossings`.
        seeded = await conn.fetchval(
            "SELECT count(*) FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key LIKE $2 || '|%'",
            ats.TRIGGER_BAND,
            desk,
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
# H3-GUARD — semantics-mismatch classification (band_crossing). The H3
# banding train (damper retired + basis alignment) legitimately moves bands
# fleet-wide on its first post-deploy sweep, and every one of those moves
# straddles a banding_semantics/damping_semantics stamp change — this must
# read as `semantics-migration`/`low`, never deterioration/evidence-gained.
# ---------------------------------------------------------------------------


async def test_semantics_mismatch_classifies_as_migration_not_deterioration(
    pg_pool, clean_slate
):
    """A synthetic prior/new card pair with DIFFERING stamps (the real H3
    shape: the prior card predates `damping_semantics` entirely, the new one
    carries both) must classify as `semantics-migration`/`low` — never
    `deterioration`/`high` — even though the band moved straight up the
    ladder (watch -> critical, which would ordinarily be the loudest possible
    alert)."""
    desk = f"desk_h3_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        # PRE-H3 card: no semantics stamps at all.
        await _insert_scorecard(conn, desk, {"escalation": "watch"})
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        # POST-H3 card: both stamps present. Band moves watch -> critical —
        # a real-looking deterioration if the guard were absent.
        await _insert_scorecard(
            conn, desk, {"escalation": "critical"},
            semantics=("standing", "off"),
        )
    dispatcher = _FakeDispatcher()
    r2 = await _run(pg_pool, dispatcher)
    assert r2.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    data = _row_data(row)
    assert data["trigger_class"] == ats.TRIGGER_BAND
    assert data["direction"] == "semantics-migration"
    assert row["severity"] == "low"
    assert "escalation" in str(data["transitions"])
    assert data["transitions"] == [
        {"dimension": "escalation", "from_band": "watch", "to_band": "critical"}
    ]
    assert "re-derived under new semantics" in row["title"]
    # The outward payload carries the same informational severity.
    assert len(dispatcher.payloads) == 1
    assert dispatcher.payloads[0].severity == "low"

    # Never refires (the watermark advanced, now carrying the new stamps).
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 0


async def test_semantics_mismatch_folds_multi_dimension_moves_into_one_alert(
    pg_pool, clean_slate
):
    """H3's real shape: ALL FOUR-ish dimensions on a desk move at once on the
    first post-deploy sweep (every dimension shares the same card-level
    stamps). This must fold into AT MOST ONE informational alert per desk —
    never one per dimension."""
    desk = f"desk_h3fold_{uuid4().hex[:8]}"
    dims = ["escalation", "energy_security", "military_posture"]
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(conn, desk, {d: "watch" for d in dims})
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        await _insert_scorecard(
            conn, desk, {d: "elevated" for d in dims},
            semantics=("standing", "off"),
        )
    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 1  # ONE alert, not three
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["direction"] == "semantics-migration"
    assert rows[0]["severity"] == "low"
    assert {t["dimension"] for t in data["transitions"]} == set(dims)


async def test_identical_semantics_stamps_are_byte_identical_to_no_stamps(
    pg_pool, clean_slate
):
    """THE NO-OP PROOF: once both cards carry IDENTICAL semantics stamps,
    behavior is byte-identical to today (no stamps at all) — same direction,
    same severity, same title shape. Two desks, two scenarios, compared
    field-for-field."""
    desk_stamped = f"desk_h3id_{uuid4().hex[:8]}"
    desk_unstamped = f"desk_h3un_{uuid4().hex[:8]}"

    async with pg_pool.acquire() as conn:
        await _insert_scorecard(
            conn, desk_stamped, {"escalation": "watch"},
            semantics=("standing", "off"),
        )
        await _insert_scorecard(conn, desk_unstamped, {"escalation": "watch"})
    await _run(pg_pool)  # seed both

    async with pg_pool.acquire() as conn:
        # BOTH cards carry the SAME stamps this time (no migration boundary).
        await _insert_scorecard(
            conn, desk_stamped, {"escalation": "high"},
            semantics=("standing", "off"),
        )
        # The unstamped desk never carries any stamps — today's behavior.
        await _insert_scorecard(conn, desk_unstamped, {"escalation": "high"})
    await _run(pg_pool)

    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    by_desk = {r["target_id"]: r for r in rows}
    stamped, unstamped = by_desk[desk_stamped], by_desk[desk_unstamped]
    stamped_data, unstamped_data = _row_data(stamped), _row_data(unstamped)

    assert stamped["severity"] == unstamped["severity"] == "high"
    assert stamped_data["direction"] == unstamped_data["direction"] == "deterioration"
    assert stamped_data["from_band"] == unstamped_data["from_band"] == "watch"
    assert stamped_data["to_band"] == unstamped_data["to_band"] == "high"
    # Titles are identical modulo the desk name — same template either way.
    assert stamped["title"].replace(desk_stamped, "DESK") == (
        unstamped["title"].replace(desk_unstamped, "DESK")
    )


# ---------------------------------------------------------------------------
# DB — trigger 2: new verified high-severity finding
# ---------------------------------------------------------------------------


async def test_verified_finding_bar_and_no_refire(pg_pool, clean_slate):
    """Exactly one of four findings clears the verified bar, and it fires once.

    ORDER DEPENDENCE. `clean_slate` resets the watermarks and this analyst's
    own alert rows, but it cannot reset `findings` — the whole suite shares
    that table. The seeding `_run` below therefore only watermarks the findings
    that exist AT THAT MOMENT; a sibling file that inserts a qualifying
    finding+critique later in the same session hands this scan a second genuine
    candidate. Under `--randomly-seed` that is what happened, and the global
    `fired == 1` failed at 2 while every one of this test's own four findings
    had been judged correctly.

    So the assertions are scoped to the four findings this test created. That
    is also the sharper statement: `fired == 1` was only ever a proxy for "the
    good one fired and the other three did not", and it could be satisfied by a
    scan that fired somebody else's finding and none of ours.
    """
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
        unverified = await _insert_finding(conn, sev_tag="high", confidence=0.95)

    mine = {str(good), str(low), str(exempt), str(unverified)}

    def _my_alerts(rows: list[Any]) -> list[Any]:
        return [r for r in rows if _row_data(r).get("finding_id") in mine]

    dispatcher = _FakeDispatcher()
    r2 = await _run(pg_pool, dispatcher)
    assert r2.finding.data["fired"] >= 1
    async with pg_pool.acquire() as conn:
        rows = _my_alerts(await _alert_rows(conn))
    assert len(rows) == 1, (
        "of the four findings, only the verified non-exempt one may fire"
    )
    data = _row_data(rows[0])
    assert data["trigger_class"] == ats.TRIGGER_FINDING
    assert data["finding_id"] == str(good)
    assert data["effective_confidence"] == pytest.approx(0.80)
    assert rows[0]["severity"] == "high"
    assert good in list(rows[0]["derived_from"])
    # The outward payload states the REAL faithfulness verdict. Correlated by
    # alert_row_id rather than taken as payloads[0] — a co-firing candidate
    # from another file would otherwise be read as ours.
    mine_out = [
        p for p in dispatcher.payloads if p.alert_row_id == str(rows[0]["id"])
    ]
    assert len(mine_out) == 1, "the fired alert must be dispatched exactly once"
    assert mine_out[0].verify_state == "faithfulness=0.80"
    assert mine_out[0].effective_confidence == pytest.approx(0.80)

    # No refire on the next scan.
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        again = _my_alerts(await _alert_rows(conn))
    assert [r["id"] for r in again] == [rows[0]["id"]], (
        "a second scan must not re-fire an already-alerted finding"
    )


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
# DB — FRAME-3 steady-state suppression guard (2026-08-29)
# ---------------------------------------------------------------------------


async def _find_alert_by_finding_id(conn: Any, finding_id: UUID) -> Any:
    rows = _my_finding_alerts(await _alert_rows(conn), {str(finding_id)})
    assert len(rows) == 1, f"expected exactly one alert row for {finding_id}"
    return rows[0]


def _my_finding_alerts(rows: list[Any], finding_ids: set[str]) -> list[Any]:
    return [r for r in rows if _row_data(r).get("finding_id") in finding_ids]


async def _set_desk_state_watermark(
    conn: Any, desk: str, *, severity: str, hours_ago: float
) -> None:
    """Back-date a desk's FRAME-3 steady-state watermark (TRIGGER_FINDING_STATE)
    to simulate an elapsed cooldown without sleeping in a test."""
    paged_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    await conn.execute(
        "INSERT INTO alert_trigger_watermarks "
        "  (trigger_class, watermark_key, state, fired_at) "
        "VALUES ($1, $2, $3::jsonb, now()) "
        "ON CONFLICT (trigger_class, watermark_key) DO UPDATE "
        "  SET state = EXCLUDED.state, updated_at = now(), fired_at = now()",
        ats.TRIGGER_FINDING_STATE,
        desk,
        json.dumps({"severity": severity, "paged_at": paged_at}),
    )


async def test_steady_state_guard_suppresses_second_unchanged_page(
    pg_pool, clean_slate
):
    """The Niger example from the 2026-08-29 sweep: a second 'nothing changed'
    verified_finding on a desk whose severity state already paged recently is
    suppressed — but the row still lands, tagged, never fanned out."""
    await _run(pg_pool)  # seed all classes

    dispatcher = _FakeDispatcher()
    async with pg_pool.acquire() as conn:
        f1 = await _insert_finding(
            conn, target="country_watch_ne", sev_tag="high", confidence=0.9
        )
        await _insert_faith_critique(conn, f1, 0.80)
    r1 = await _run(pg_pool, dispatcher)
    assert r1.finding.data["fired"] == 1
    assert r1.finding.data["guard_suppressed"] == 0

    async with pg_pool.acquire() as conn:
        row1 = await _find_alert_by_finding_id(conn, f1)
    assert _row_data(row1)["guard_suppressed"] is False
    assert len([p for p in dispatcher.payloads if p.alert_row_id == str(row1["id"])]) == 1

    # "Niger maintains high alert posture, no new shift observed" — same
    # severity band, an explicit steady tag, well within the cooldown.
    async with pg_pool.acquire() as conn:
        f2 = await _insert_finding(
            conn, target="country_watch_ne", sev_tag="high", confidence=0.9,
            delta_tag="steady",
        )
        await _insert_faith_critique(conn, f2, 0.80)
    dispatcher2 = _FakeDispatcher()
    r2 = await _run(pg_pool, dispatcher2)
    assert r2.finding.data["fired"] == 0, "the suppressed candidate must not fire"
    assert r2.finding.data["guard_suppressed"] == 1

    async with pg_pool.acquire() as conn:
        row2 = await _find_alert_by_finding_id(conn, f2)
        tags_row = await conn.fetchrow(
            "SELECT data FROM analyst_outputs WHERE id = $1", row2["id"]
        )
    data2 = _row_data(row2)
    assert data2["guard_suppressed"] is True
    assert data2["guard_suppression_reason"] == "steady_state_within_cooldown"
    assert data2["severity_delta_tag"] == "steady"
    full = (
        json.loads(tags_row["data"])
        if isinstance(tags_row["data"], str)
        else dict(tags_row["data"])
    )
    assert "suppressed:true" in full["tags"]
    # SUPPRESS != DROP: the row is durable even though it never paged.
    assert dispatcher2.payloads == []

    # No refire of f2 on a third scan — the per-finding watermark still holds.
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 0
    assert r3.finding.data["guard_suppressed"] == 0
    async with pg_pool.acquire() as conn:
        assert len(_my_finding_alerts(await _alert_rows(conn), {str(f2)})) == 1


async def test_steady_state_guard_delta_tag_rose_still_pages(pg_pool, clean_slate):
    """The other live example: a first-time escalation must always page — the
    model's OWN rose/fell/new tag vetoes suppression even at an unchanged
    band and inside the cooldown."""
    await _run(pg_pool)

    async with pg_pool.acquire() as conn:
        f1 = await _insert_finding(
            conn, target="country_g20_sa", sev_tag="high", confidence=0.9
        )
        await _insert_faith_critique(conn, f1, 0.80)
    r1 = await _run(pg_pool)
    assert r1.finding.data["fired"] == 1

    async with pg_pool.acquire() as conn:
        f2 = await _insert_finding(
            conn, target="country_g20_sa", sev_tag="high", confidence=0.9,
            delta_tag="rose",
        )
        await _insert_faith_critique(conn, f2, 0.80)
    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 1, "rose vetoes suppression"
    assert r2.finding.data["guard_suppressed"] == 0
    async with pg_pool.acquire() as conn:
        row2 = await _find_alert_by_finding_id(conn, f2)
    assert _row_data(row2)["guard_suppression_reason"] == "delta_tag_rose"


async def test_steady_state_guard_band_change_always_pages(pg_pool, clean_slate):
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        f1 = await _insert_finding(
            conn, target="country_g20_us", sev_tag="high", confidence=0.9
        )
        await _insert_faith_critique(conn, f1, 0.80)
    await _run(pg_pool)

    async with pg_pool.acquire() as conn:
        f2 = await _insert_finding(
            conn, target="country_g20_us", sev_tag="critical", confidence=0.9,
            delta_tag="steady",
        )
        await _insert_faith_critique(conn, f2, 0.80)
    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 1
    assert r2.finding.data["guard_suppressed"] == 0
    async with pg_pool.acquire() as conn:
        row2 = await _find_alert_by_finding_id(conn, f2)
    assert _row_data(row2)["guard_suppression_reason"] == "band_changed"


async def test_steady_state_guard_cooldown_elapsed_pages_as_heartbeat(
    pg_pool, clean_slate
):
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        f1 = await _insert_finding(
            conn, target="country_g20_ir", sev_tag="high", confidence=0.9
        )
        await _insert_faith_critique(conn, f1, 0.80)
    await _run(pg_pool)

    # Back-date the desk's own steady-state watermark past the cooldown.
    async with pg_pool.acquire() as conn:
        await _set_desk_state_watermark(
            conn, "country_g20_ir", severity="high", hours_ago=25
        )
        f2 = await _insert_finding(
            conn, target="country_g20_ir", sev_tag="high", confidence=0.9,
            delta_tag="steady",
        )
        await _insert_faith_critique(conn, f2, 0.80)
    r2 = await _run(pg_pool)  # default cooldown is 24h
    assert r2.finding.data["fired"] == 1, "an elapsed cooldown pages as a heartbeat"
    assert r2.finding.data["guard_suppressed"] == 0
    async with pg_pool.acquire() as conn:
        row2 = await _find_alert_by_finding_id(conn, f2)
    assert _row_data(row2)["guard_suppression_reason"] == "cooldown_elapsed"


async def test_steady_state_guard_cooldown_hours_option_is_honored(
    pg_pool, clean_slate
):
    """steady_cooldown_hours=0 — even an immediate repeat pages; proves the
    knob reaches the handler with no deploy, S-1's precedent."""
    await _run(pg_pool, steady_cooldown_hours=0)
    async with pg_pool.acquire() as conn:
        f1 = await _insert_finding(
            conn, target="country_watch_iq", sev_tag="high", confidence=0.9
        )
        await _insert_faith_critique(conn, f1, 0.80)
    await _run(pg_pool, steady_cooldown_hours=0)

    async with pg_pool.acquire() as conn:
        f2 = await _insert_finding(
            conn, target="country_watch_iq", sev_tag="high", confidence=0.9,
            delta_tag="steady",
        )
        await _insert_faith_critique(conn, f2, 0.80)
    r2 = await _run(pg_pool, steady_cooldown_hours=0)
    assert r2.finding.data["fired"] == 1
    assert r2.finding.data["guard_suppressed"] == 0


async def test_steady_state_guard_can_be_disabled_via_option(pg_pool, clean_slate):
    await _run(pg_pool, suppress_steady_state=False)
    async with pg_pool.acquire() as conn:
        f1 = await _insert_finding(
            conn, target="country_watch_ye", sev_tag="high", confidence=0.9
        )
        await _insert_faith_critique(conn, f1, 0.80)
    await _run(pg_pool, suppress_steady_state=False)

    async with pg_pool.acquire() as conn:
        f2 = await _insert_finding(
            conn, target="country_watch_ye", sev_tag="high", confidence=0.9,
            delta_tag="steady",
        )
        await _insert_faith_critique(conn, f2, 0.80)
    r2 = await _run(pg_pool, suppress_steady_state=False)
    assert r2.finding.data["fired"] == 1
    assert r2.finding.data["guard_suppressed"] == 0
    async with pg_pool.acquire() as conn:
        row2 = await _find_alert_by_finding_id(conn, f2)
    assert _row_data(row2)["guard_suppression_reason"] == "suppression_disabled"


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


async def _insert_fact_from_signal(conn: Any, *, subject: str, value: str) -> tuple[UUID, UUID]:
    """A fact in the PRODUCTION shape: a real ``facts`` row whose ``derived_from``
    carries a real ``signals`` id. Returns ``(fact_id, signal_id)``.

    The sibling contention tests above use a bare ``uuid4()`` as their "fact" and
    have a finding cite it directly. Production never looks like that: findings
    cite SIGNALS. Live 2026-08-03, of 229,768 ``derived_from`` refs carried by
    findings in 7 days, 221,268 resolved to signals, 7,874 to other outputs, and
    ZERO to facts (34 of 757,436 all-time). Those tests could only pass because
    the rig invented a citation shape the engine does not produce — which is how
    a trigger class that CANNOT fire stayed green for its whole life.
    """
    sid, fid = uuid4(), uuid4()
    # Stamped WELL INTO THE PAST on purpose. The session DB is shared across
    # every file in tests/data_pkg and nothing truncates `signals`, so a
    # now()-stamped row here lands at the head of the newest-N window that
    # `test_claim_watch`'s `_attributed_and_window` slices (`ORDER BY
    # fetched_at DESC LIMIT window`, across ALL sources) and silently shrinks
    # that file's global-DF sample — verified: it flipped
    # `global_specificity_downweighted` from 3 to 6 two files later. Backdating
    # keeps these rows out of every newest-N slice while leaving this test's own
    # `derived_from` bridge exactly as real. (claim_watch has since hardened its
    # own side too — its df window now rides the file's future-stamped stream —
    # but staying out of the shared window's head remains this file's half of
    # the bargain: this file's SPIKE stream cannot take it, spikes are recent by
    # definition, which is exactly why the window had to stop being shared.)
    await conn.execute(
        "INSERT INTO signals (id, source_id, geo, fetched_at, content_hash) "
        "VALUES ($1, 'w1c3_source', '{}'::text[], now() - interval '400 days', $2)",
        sid, uuid4().hex,
    )
    await conn.execute(
        "INSERT INTO facts (id, subject, predicate, value, confidence, "
        "                   source_type, valid_from, analyst_id, derived_from, "
        "                   schema_uri) "
        "VALUES ($1, $2, 'located in', $3, 0.8, 'derived', now(), 'w1c3', "
        "        $4::uuid[], 'iglu:legba/fact/jsonschema/1-0-0')",
        fid, subject, value, [sid],
    )
    return fid, sid


async def test_contention_flip_fires_when_the_finding_cites_the_facts_EVIDENCE(
    pg_pool, clean_slate
):
    """W1-C3 — the bridge, in the shape production actually produces.

    Before this fix the verified-tie LATERAL matched `f.derived_from &&
    fact_ids` alone, so the class had NEVER fired an alert despite 1,606
    watermark rows: contention groups track FACT ids, findings cite SIGNAL ids,
    and the two populations never meet (exactly 1 of 2,152 live groups had ANY
    finding citing its facts, verified or not). Here the finding cites the
    SIGNAL its contested fact was derived from — the join the substrate's own
    lineage supports — and the flip must page.
    """
    async with pg_pool.acquire() as conn:
        subj = f"w1c3_{uuid4().hex[:8]}"
        fact_a, sig_a = await _insert_fact_from_signal(conn, subject=subj, value="a")
        fact_b, _ = await _insert_fact_from_signal(conn, subject=subj, value="b")
        cid = await _insert_contention(conn, subject=subj)
        await _insert_contention_value(conn, cid, [fact_a], "value-a")
        await _insert_contention_value(conn, cid, [fact_b], "value-b")
        # THE PRODUCTION CITATION SHAPE: the finding cites the SIGNAL, never
        # the fact id. Under the pre-fix query this finding is invisible.
        tied = await _insert_finding(
            conn, sev_tag=None, confidence=0.9, derived=[sig_a]
        )
        await _insert_faith_critique(conn, tied, 0.85)

    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE fact_contention SET status = 'surfaced', "
            "surfaced_value = 'a', surfaced_fact_id = $2, updated_at = now() "
            "WHERE id = $1",
            cid, fact_a,
        )

    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 1, (
        "a contention flip with a verified finding resting on the disputed "
        "evidence fired nothing — the fact/signal id populations still do not "
        "meet, and the class remains a structural false negative"
    )
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["trigger_class"] == ats.TRIGGER_CONTENTION
    assert data["change"] == "contested->surfaced"
    assert data["verified_finding_id"] == str(tied)


async def test_contention_flip_still_needs_a_shared_evidence_ancestor(
    pg_pool, clean_slate
):
    """The bridge widens the join along real lineage; it must not make every
    verified finding count. A finding citing an UNRELATED signal — one no
    contested fact was derived from — leaves the flip silent (watermarked, not
    paged), exactly as before."""
    async with pg_pool.acquire() as conn:
        subj = f"w1c3u_{uuid4().hex[:8]}"
        fact_a, _ = await _insert_fact_from_signal(conn, subject=subj, value="a")
        cid = await _insert_contention(conn, subject=subj)
        await _insert_contention_value(conn, cid, [fact_a], "value-a")
        # A verified finding, but resting on evidence with no lineage tie.
        _, unrelated_sig = await _insert_fact_from_signal(
            conn, subject=f"other_{uuid4().hex[:8]}", value="z"
        )
        stray = await _insert_finding(
            conn, sev_tag=None, confidence=0.9, derived=[unrelated_sig]
        )
        await _insert_faith_critique(conn, stray, 0.9)

    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE fact_contention SET status = 'surfaced', "
            "surfaced_value = 'a', surfaced_fact_id = $2, updated_at = now() "
            "WHERE id = $1",
            cid, fact_a,
        )
    assert (await _run(pg_pool)).finding.data["fired"] == 0


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


# The geo BINS these tests converge in, and why they look like nonsense.
#
# ORDER DEPENDENCE, and the fix. `geo_clean_slate` deletes this file's own
# `geo6test.*` signals, and cannot delete anybody else's. But a geo bin is not
# owned by a source — it is a COUNTRY TAG or a 1° CELL, and every geo-tagged
# signal in the shared `signals` table joins the bin it falls in, whoever wrote
# it. So these tests' exact bin assertions ("the families here are exactly gis,
# news and social"; "3 contributors") were statements about the whole suite's
# signal table.
#
# `tests/data_pkg/test_watchlist.py` is what proved it, and the pair reproduces
# in file order with no shuffle at all:
#     pytest tests/data_pkg/test_watchlist.py tests/data_pkg/test_alert_trigger_scan.py
# Its `_insert_signal` writes `source_id='test_p5_6_source'` rows that it never
# cleans up: one tagged `geo=['IR','IQ']` (joining `country:IQ`) and one at
# 33.31/44.36 for its Baghdad "far event" (joining `cell:33:44`). Both bins were
# in use here. The source has no descriptor, so it contributes the honest
# `src:<source_id>` family fallback, and the assertions failed at
# `['gis','news','social','src:test_p5_6_source']` and `4 == 3` — on a scan that
# had binned every row exactly right.
#
# The bins therefore move somewhere nothing real can follow:
#
#   * COUNTRY tier — the ISO 3166-1 USER-ASSIGNED range (AA, QM-QZ, XA-XZ, ZZ),
#     which is permanently unassigned and so can never appear in a `signals.geo`
#     tag that came from anywhere but this file. `country_key` normalizes any
#     two-letter alpha tag, so these are ordinary bins to the scan under test.
#   * CELL tier — 77°N 168°E, Arctic Ocean north of Wrangel Island. Not a real
#     place anything reports from, and 44 degrees from the nearest coordinate
#     any other test in the tree uses.
#
# Keeping the assertions EXACT is the point: "these three families and no
# others" is what a convergence bin means, and weakening it to a subset check
# would have passed just as happily on the polluted bin above.
_GEO_CC_SEED = "XA"          # the seeds-silently test
_GEO_CC_FORMATION = "XB"     # the formation test
_GEO_CC_PILEON = "XC"        # the same-family pile-on test
_GEO_CC_DISSOLUTION = "XD"   # the dissolution test
_GEO_CELL_LAT, _GEO_CELL_LON = 77, 168


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
            await _insert_geo_signal(conn, src, geo_tags=[_GEO_CC_SEED])

    r1 = await _run(pg_pool)
    assert ats.TRIGGER_GEO_CONVERGENCE in r1.finding.data["seeded_classes"]
    async with pg_pool.acquire() as conn:
        assert _geo_rows(await _alert_rows(conn)) == []  # history never pages
        wm = await conn.fetchrow(
            "SELECT state FROM alert_trigger_watermarks "
            "WHERE trigger_class = $1 AND watermark_key = $2",
            ats.TRIGGER_GEO_CONVERGENCE, f"country:{_GEO_CC_SEED}",
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
        await _insert_geo_signal(conn, "geo6test.quake",
                                 geo_tags=[_GEO_CC_FORMATION])
        await _insert_geo_signal(conn, "geo6test.news",
                                 geo_tags=[_GEO_CC_FORMATION])
    await _run(pg_pool)  # seeds (nothing formed)

    async with pg_pool.acquire() as conn:
        # Third DISTINCT family arrives → formation edge.
        await _insert_geo_signal(conn, "geo6test.tg",
                                 geo_tags=[_GEO_CC_FORMATION])
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
    assert data["bin_key"] == f"country:{_GEO_CC_FORMATION}"
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
            await _insert_geo_signal(conn, "geo6test.news",
                                     geo_tags=[_GEO_CC_PILEON])
            await _insert_geo_signal(conn, "geo6test.news2",
                                     geo_tags=[_GEO_CC_PILEON])
    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 0
    async with pg_pool.acquire() as conn:
        assert _geo_rows(await _alert_rows(conn)) == []

    # Two more DISTINCT families flip it.
    async with pg_pool.acquire() as conn:
        await _insert_geo_signal(conn, "geo6test.quake",
                                 geo_tags=[_GEO_CC_PILEON])
        await _insert_geo_signal(conn, "geo6test.health",
                                 geo_tags=[_GEO_CC_PILEON])
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
            lat=_GEO_CELL_LAT + 0.2, lon=_GEO_CELL_LON + 0.1,
            geo_source="geometry", precision="country",
        )
        await _insert_geo_signal(
            conn, "geo6test.news",
            lat=_GEO_CELL_LAT + 0.8, lon=_GEO_CELL_LON + 0.9,
            precision="municipality",
        )
        await _insert_geo_signal(
            conn, "geo6test.tg",
            lat=_GEO_CELL_LAT + 0.5, lon=_GEO_CELL_LON + 0.5, precision="region",
        )
        # A country-precision nominatim point (country CENTROID) in the same
        # cell MUST NOT enter the cell tier.
        await _insert_geo_signal(
            conn, "geo6test.health",
            lat=_GEO_CELL_LAT + 0.5, lon=_GEO_CELL_LON + 0.2,
            precision="country", geo_source="nominatim",
        )
    r2 = await _run(pg_pool)
    assert r2.finding.data["fired"] == 1
    async with pg_pool.acquire() as conn:
        rows = _geo_rows(await _alert_rows(conn))
    assert len(rows) == 1
    data = _row_data(rows[0])
    assert data["bin_kind"] == "cell"
    assert data["bin_key"] == f"cell:{_GEO_CELL_LAT}:{_GEO_CELL_LON}"
    # The centroid signal is excluded: 3 contributors, no 'health' family.
    assert data["signal_count"] == 3
    assert sorted(data["families"]) == ["gis", "news", "social"]


async def test_geo_convergence_dissolution_fires_once_then_quiet(
    pg_pool, geo_clean_slate
):
    async with pg_pool.acquire() as conn:
        await _seed_three_geo_family_sources(conn)
        for src in ("geo6test.quake", "geo6test.news", "geo6test.tg"):
            await _insert_geo_signal(conn, src, geo_tags=[_GEO_CC_DISSOLUTION])
    await _run(pg_pool)  # seeds the dissolution bin as active

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
    assert data["bin_key"] == f"country:{_GEO_CC_DISSOLUTION}"
    assert sorted(data["previous_families"]) == ["gis", "news", "social"]

    # Dissolution fired ONCE — the next scan is quiet.
    r3 = await _run(pg_pool)
    assert r3.finding.data["fired"] == 0
    async with pg_pool.acquire() as conn:
        assert len(_geo_rows(await _alert_rows(conn))) == 1


# ---------------------------------------------------------------------------
# Trigger 7 — production_deficit (S-1, the expected-vs-actual production gauge)
# ---------------------------------------------------------------------------
#
# The seventh class differs from the other six in one way worth testing
# explicitly: it alerts on a CONDITION that persists rather than an event that
# happens. A frozen feed is still frozen ten minutes later, so a plain
# fire-once contract would either page forever or go quiet the moment the
# condition worsened. The paging policy is therefore escalation-only, and
# these tests pin every rung of it.

_PD_SOURCE = "source.pdtest.frozen"


@pytest_asyncio.fixture
async def pd_clean_slate(pg_pool):
    """Watermarks + the gauge's own inputs cleared, so a production_deficit
    test measures ONLY the loop it seeded."""

    async def _wipe(conn):
        await conn.execute("TRUNCATE alert_trigger_watermarks")
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = 'alert_trigger_scan'"
        )
        await conn.execute(
            "DELETE FROM source_descriptors WHERE descriptor_id LIKE 'source.pdtest.%'"
        )
        await conn.execute(
            "DELETE FROM signals WHERE source_id LIKE 'source.pdtest.%'"
        )
        await conn.execute(
            "DELETE FROM source_poll_outcomes WHERE source_id LIKE 'source.pdtest.%'"
        )

    async with pg_pool.acquire() as conn:
        await _wipe(conn)
    yield
    async with pg_pool.acquire() as conn:
        await _wipe(conn)


async def _seed_frozen_source(conn: Any, *, hours_silent: int, polls: int) -> None:
    """The AP shape: an active hourly feed that produced a burst then froze,
    with every poll since recording success / healthy / 0-written."""
    body = {
        "identity": {"id": _PD_SOURCE, "kind": "rss", "state": "active"},
        "acquisition": "poll",
        "cadence": {"schedule": {"raw": "13 * * * *", "ui_hint": {}}},
    }
    await conn.execute(
        """
        INSERT INTO source_descriptors
            (descriptor_id, version, schema_uri, is_head, kind, state, owner,
             name, body, created_at)
        VALUES ($1, 'v1', 'legba/source/1.0.0', TRUE, 'rss', 'active', 'test',
                $1, $2::jsonb, now() - interval '60 days')
        ON CONFLICT (descriptor_id, version) DO NOTHING
        """,
        _PD_SOURCE,
        json.dumps(body),
    )
    for i in range(20):
        await conn.execute(
            """
            INSERT INTO signals
                (id, source_id, source_version, payload, content_hash,
                 fetched_at, created_at, updated_at, schema_uri)
            VALUES ($1, $2, 'v1', '{}'::jsonb, $3,
                    now(),
                    now() - make_interval(hours => $4),
                    now(), 'iglu:legba/signal/jsonschema/1-0-0')
            """,
            uuid4(),
            _PD_SOURCE,
            uuid4().hex,
            hours_silent + i,
        )
    for h in range(polls):
        await conn.execute(
            """
            INSERT INTO source_poll_outcomes
                (source_id, source_version, outcome, health_state,
                 signals_written, occurred_at)
            VALUES ($1, 'v1', 'success', 'healthy', 0,
                    now() - make_interval(hours => $2))
            """,
            _PD_SOURCE,
            h,
        )


def _pd_rows(rows: list[Any]) -> list[Any]:
    return [r for r in rows if _row_data(r).get("loop_id") == _PD_SOURCE]


def _fanned_body(dispatcher: Any) -> str:
    return "\n".join(getattr(p, "detail", "") for p in dispatcher.payloads)


def test_production_deficit_is_registered_in_every_per_class_registry():
    """The registries a new trigger class silently half-lands in.

    verified_finding is the ONE documented exception to the unverified-reason
    registry — it carries the finding's real faithfulness score instead (see
    ``_sink_payload``) — so it is named here rather than left to a loose check.
    """
    assert ats.TRIGGER_PRODUCTION_DEFICIT == "production_deficit"
    assert ats.TRIGGER_PRODUCTION_DEFICIT in ats.TRIGGER_CLASSES
    for cls in ats.TRIGGER_CLASSES:
        assert cls in ats._CLASS_PRIORITY, cls
        if cls != ats.TRIGGER_FINDING:
            assert cls in ats._UNVERIFIED_REASONS, cls
    assert len(set(ats._CLASS_PRIORITY.values())) == len(ats._CLASS_PRIORITY)


def test_gauge_thresholds_are_declared_handler_options():
    """A ``gauge_*`` knob not declared in HANDLER_OPTIONS is DROPPED whole at
    descriptor-resolution time, so an operator retune would silently no-op."""
    from legba.data.analysts.handler_options import HANDLER_OPTIONS
    from legba.data.registry.production_gauge import GaugeConfig

    declared = {
        o.name[len("gauge_"):]
        for o in HANDLER_OPTIONS["alert_trigger_scan"]
        if o.name.startswith("gauge_")
    }
    assert declared == set(GaugeConfig().__dataclass_fields__)


def test_gauge_options_reach_the_config():
    from legba.data.analysts.deterministic_handlers import _production_deficit_scan

    cfg = _production_deficit_scan.config_from_options(
        {"gauge_window_days": 5, "per_desk_cap": 3, "gauge_source_gap_multiple": 9.0}
    )
    assert cfg.window_days == 5
    assert cfg.source_gap_multiple == 9.0


async def test_production_deficit_seeds_standing_deficits_without_paging(
    pg_pool, pd_clean_slate
):
    """The 0091 seed contract on a condition rather than an event: bringing the
    class up on a live substrate adopts the standing backlog silently. But the
    RECEIPT says so — a gauge whose first run swallowed seven real deficits
    and reported nothing would read as all-clear, which is the exact failure
    it exists to prevent."""
    async with pg_pool.acquire() as conn:
        await _seed_frozen_source(conn, hours_silent=24 * 8, polls=120)

    r1 = await _run(pg_pool, per_desk_cap=20)
    counts = r1.finding.data["counts_by_class"][ats.TRIGGER_PRODUCTION_DEFICIT]
    assert ats.TRIGGER_PRODUCTION_DEFICIT in r1.finding.data["seeded_classes"]
    assert counts["candidates"] == 0
    assert counts["seeded_deficits"] >= 1
    assert counts["gauged"] >= 1
    async with pg_pool.acquire() as conn:
        assert _pd_rows(await _alert_rows(conn)) == []

    # Adopted, so the steady state stays quiet.
    r2 = await _run(pg_pool, per_desk_cap=20)
    async with pg_pool.acquire() as conn:
        assert _pd_rows(await _alert_rows(conn)) == []
    assert (
        r2.finding.data["counts_by_class"][ats.TRIGGER_PRODUCTION_DEFICIT]["deficits"]
        >= 1
    )


async def test_production_deficit_fires_when_a_new_deficit_appears(
    pg_pool, pd_clean_slate
):
    """The real firing path: the class seeds on a healthy engine, a loop goes
    quiet, and the next scan pages with the expectation stated."""
    await _run(pg_pool, per_desk_cap=20)  # seed with no deficit present

    async with pg_pool.acquire() as conn:
        await _seed_frozen_source(conn, hours_silent=24 * 8, polls=120)

    dispatcher = _FakeDispatcher()
    r = await _run(pg_pool, dispatcher, per_desk_cap=20)
    async with pg_pool.acquire() as conn:
        rows = _pd_rows(await _alert_rows(conn))
    assert len(rows) == 1
    row = rows[0]
    assert row["severity"] == "critical"
    data = _row_data(row)
    assert data["trigger_class"] == ats.TRIGGER_PRODUCTION_DEFICIT
    assert data["loop_class"] == "source_production"
    assert data["loop_id"] == _PD_SOURCE
    assert data["evidence"]["sub_state"] == "drought"
    # The page states the EXPECTATION, not just the symptom — an operator must
    # be able to act on the notification without opening the route.
    assert "EXPECTED:" in _fanned_body(dispatcher)
    assert (
        r.finding.data["counts_by_class"][ats.TRIGGER_PRODUCTION_DEFICIT]["paging"] >= 1
    )

    # Persisting at the same severity NEVER refires.
    await _run(pg_pool, per_desk_cap=20)
    async with pg_pool.acquire() as conn:
        assert len(_pd_rows(await _alert_rows(conn))) == 1


async def test_production_deficit_refires_only_on_escalation(
    pg_pool, pd_clean_slate
):
    """A condition that gets WORSE is news again; one that merely persists is
    not. The watermark fingerprints the severity rank, so the alert ledger
    reads as a story rather than a heartbeat."""
    await _run(pg_pool, per_desk_cap=20)  # seed clean

    async with pg_pool.acquire() as conn:
        # ~2 days silent on an hourly feed: over the 1-day bar, `high`.
        await _seed_frozen_source(conn, hours_silent=50, polls=50)
    await _run(pg_pool, per_desk_cap=20)
    async with pg_pool.acquire() as conn:
        first = _pd_rows(await _alert_rows(conn))
    assert len(first) == 1
    assert first[0]["severity"] == "high"

    # Same severity a scan later — silence.
    await _run(pg_pool, per_desk_cap=20)
    async with pg_pool.acquire() as conn:
        assert len(_pd_rows(await _alert_rows(conn))) == 1

    # It gets worse: push the newest signal past the critical rung.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE signals SET created_at = created_at - interval '10 days' "
            "WHERE source_id = $1",
            _PD_SOURCE,
        )
    await _run(pg_pool, per_desk_cap=20)
    async with pg_pool.acquire() as conn:
        rows = _pd_rows(await _alert_rows(conn))
    assert len(rows) == 2
    assert rows[1]["severity"] == "critical"
    assert _row_data(rows[1])["previous_severity"] == "high"
    assert "escalated" in rows[1]["title"]


async def test_production_deficit_recovery_is_silent_and_re_arms(
    pg_pool, pd_clean_slate
):
    """Recovery clears the watermark WITHOUT an all-clear page (the phone is
    for bad news; the route shows the recovery), and the next deficit fires
    cleanly rather than being swallowed as 'ongoing'."""
    await _run(pg_pool, per_desk_cap=20)

    async with pg_pool.acquire() as conn:
        await _seed_frozen_source(conn, hours_silent=24 * 8, polls=120)
    await _run(pg_pool, per_desk_cap=20)
    async with pg_pool.acquire() as conn:
        assert len(_pd_rows(await _alert_rows(conn))) == 1

    # The feed comes back.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE signals SET created_at = now() - interval '10 minutes' "
            "WHERE source_id = $1",
            _PD_SOURCE,
        )
    r = await _run(pg_pool, per_desk_cap=20)
    assert (
        r.finding.data["counts_by_class"][ats.TRIGGER_PRODUCTION_DEFICIT]["recoveries"]
        >= 1
    )
    async with pg_pool.acquire() as conn:
        assert len(_pd_rows(await _alert_rows(conn))) == 1  # no all-clear alert

    # It freezes again — and pages again, not swallowed as an ongoing state.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE signals SET created_at = now() - interval '9 days' "
            "WHERE source_id = $1",
            _PD_SOURCE,
        )
    await _run(pg_pool, per_desk_cap=20)
    async with pg_pool.acquire() as conn:
        assert len(_pd_rows(await _alert_rows(conn))) == 2


async def test_production_deficit_reports_its_measurement_even_when_quiet(
    pg_pool, pd_clean_slate
):
    """A scan that pages nothing still records WHAT IT MEASURED. "The engine
    checked N loops and they were producing" is the fact that distinguishes a
    working gauge from a dead one — precisely the distinction the twelve
    silently-dead components never had."""
    r = await _run(pg_pool, per_desk_cap=20)
    counts = r.finding.data["counts_by_class"][ats.TRIGGER_PRODUCTION_DEFICIT]
    for field in (
        "loops", "gauged", "deficits", "paging", "escalations", "recoveries",
        "seeded_deficits", "candidate_bound_hit", "unavailable",
    ):
        assert field in counts, field


async def test_production_deficit_unverified_posture_is_honest(
    pg_pool, pd_clean_slate
):
    """The gauge reads receipts and row-birth timestamps — engine telemetry,
    with no prose and no claim about the world — so its outward verify state
    must say exactly that rather than borrow a faithfulness score."""
    await _run(pg_pool, per_desk_cap=20)
    async with pg_pool.acquire() as conn:
        await _seed_frozen_source(conn, hours_silent=24 * 8, polls=120)
    dispatcher = _FakeDispatcher()
    await _run(pg_pool, dispatcher, per_desk_cap=20)

    payloads = [
        p
        for p in dispatcher.payloads
        if "Production deficit" in getattr(p, "summary", "")
    ]
    assert payloads
    assert "engine telemetry" in payloads[0].verify_state
    assert payloads[0].target_id is None
    assert payloads[0].channel_name == ats.CHANNEL_NAME


# ---------------------------------------------------------------------------
# DB — D2, the 90-day product wager (2026-08-29): daily page budget
# ---------------------------------------------------------------------------


async def test_daily_page_budget_caps_fleet_wide_pages_and_marks_the_rest(
    pg_pool, clean_slate
):
    """Five real band-crossing candidates on one desk, budget=2 — every row
    still WRITES (SUPPRESS/DEFER never means DROP), only 2 actually page."""
    desk = await _seed_five_deteriorations(pg_pool)
    dispatcher = _FakeDispatcher()
    r = await _run(pg_pool, dispatcher, per_desk_cap=20, daily_page_budget=2)
    assert r.finding.data["fired"] == 5
    assert r.finding.data["budget_deferred"] == 3
    assert len(dispatcher.payloads) == 2, "only the budget's slots actually page"

    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    my_rows = [row for row in rows if row["target_id"] == desk]
    assert len(my_rows) == 5
    deferred = [row for row in my_rows if _row_data(row)["budget_deferred"] is True]
    paged = [row for row in my_rows if _row_data(row)["budget_deferred"] is False]
    assert len(deferred) == 3
    assert len(paged) == 2

    async with pg_pool.acquire() as conn:
        tag_rows = await conn.fetch(
            "SELECT data FROM analyst_outputs WHERE id = ANY($1::uuid[])",
            [row["id"] for row in deferred],
        )
    for tr in tag_rows:
        full = (
            json.loads(tr["data"])
            if isinstance(tr["data"], str)
            else dict(tr["data"])
        )
        assert "budget_deferred:true" in full["tags"]


async def test_daily_page_budget_persists_already_paged_across_scans(
    pg_pool, clean_slate
):
    """``already_paged_today`` is read LIVE from the alert ledger every scan,
    not held in memory — a second scan the same UTC day still respects what
    an earlier scan already spent."""
    await _seed_five_deteriorations(pg_pool)
    r1 = await _run(pg_pool, per_desk_cap=20, daily_page_budget=3)
    assert r1.finding.data["already_paged_today"] == 0
    assert r1.finding.data["budget_deferred"] == 2

    desk2 = f"desk_x1_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(conn, desk2, {"escalation": "watch"})
    await _run(pg_pool, per_desk_cap=20, daily_page_budget=3)  # seed desk2's class
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(conn, desk2, {"escalation": "high"})
    r2 = await _run(pg_pool, per_desk_cap=20, daily_page_budget=3)
    assert r2.finding.data["already_paged_today"] == 3, (
        "the first scan's 3 real pages are still on the ledger"
    )
    assert r2.finding.data["budget_deferred"] == 1  # the one new candidate


async def test_daily_page_budget_env_var_reaches_the_handler(pg_pool, clean_slate, monkeypatch):
    """No ``daily_page_budget`` option at all — falls back to the env var."""
    monkeypatch.setenv(ats._DAILY_PAGE_BUDGET_ENV, "1")
    await _seed_five_deteriorations(pg_pool)
    deps = _Deps(pg_pool, _FakeDispatcher())
    options = {
        "sub_handler": "alert_trigger_scan",
        "analyst_id": "alert_trigger_scan",
        "run_id": str(uuid4()),
        "per_desk_cap": 20,
        # Kill list stays ON here — this test is only about the budget knob.
        "contention_flip_enabled": True,
        "geo_convergence_enabled": True,
    }
    r = await ats.handle([], options, deps)
    assert r.finding.data["daily_page_budget"] == 1
    assert r.finding.data["budget_deferred"] == 4


async def test_kind_cap_is_day_cumulative_across_scans_db(pg_pool, clean_slate):
    """The DB-level proof that the kind cap is a DAY total, not a per-scan
    total: 5 band_crossing candidates in one scan hit the default cap of 3
    (2 deferred); a SECOND scan later that day, for a DIFFERENT desk, offers
    only ONE new band_crossing candidate — nowhere near 3 on its own — but it
    is still deferred, because band_crossing already spent its 3 slots in
    the FIRST scan (``already_paged_today_by_kind``, read live from the
    ledger, not held in memory across scans)."""
    await _seed_five_deteriorations(pg_pool)
    r1 = await _run(
        pg_pool, per_desk_cap=20, daily_page_budget=10, budget_per_kind_cap=3
    )
    assert r1.finding.data["fired"] == 5
    assert r1.finding.data["budget_deferred"] == 2, "the kind cap bites first"

    desk2 = f"desk_x1_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(conn, desk2, {"escalation": "watch"})
    await _run(
        pg_pool, per_desk_cap=20, daily_page_budget=10, budget_per_kind_cap=3
    )  # seed desk2's class
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(conn, desk2, {"escalation": "high"})
    r2 = await _run(
        pg_pool, per_desk_cap=20, daily_page_budget=10, budget_per_kind_cap=3
    )
    assert r2.finding.data["fired"] == 1
    assert r2.finding.data["budget_deferred"] == 1, (
        "band_crossing already spent its per-kind cap earlier TODAY, even "
        "though this scan's own batch never approached the cap by itself"
    )
    assert r2.finding.data["already_paged_today_by_kind"].get(ats.TRIGGER_BAND) == 3


# ---------------------------------------------------------------------------
# DB — D2, the 90-day product wager (2026-08-29): kill list
# ---------------------------------------------------------------------------


async def test_contention_flip_killed_by_default_writes_no_row_but_counts(
    pg_pool, clean_slate
):
    fact_a = uuid4()
    async with pg_pool.acquire() as conn:
        cid = await _insert_contention(conn, subject=f"subj_{uuid4().hex[:8]}")
        await _insert_contention_value(conn, cid, [fact_a], "value-a")
        tied = await _insert_finding(
            conn, sev_tag=None, confidence=0.9, derived=[fact_a]
        )
        await _insert_faith_critique(conn, tied, 0.85)
    await _run(pg_pool)  # seed — the existing contention fires nothing regardless

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE fact_contention SET status = 'surfaced', "
            "surfaced_value = 'value-a', surfaced_fact_id = $2, "
            "updated_at = now() WHERE id = $1",
            cid,
            fact_a,
        )
    # Explicit OFF — the real production default; overrides the test
    # harness's convenience ON.
    r = await _run(pg_pool, contention_flip_enabled=False)
    assert r.finding.data["fired"] == 0
    counts = r.finding.data["counts_by_class"][ats.TRIGGER_CONTENTION]
    assert counts["killed"] is True
    assert counts["killed_would_have_fired"] == 1
    assert counts["candidates"] == 0
    async with pg_pool.acquire() as conn:
        rows = await _alert_rows(conn)
    assert [
        row for row in rows
        if _row_data(row).get("trigger_class") == ats.TRIGGER_CONTENTION
    ] == []

    # No backlog on resurrection — the watermark already advanced while
    # killed, so the SAME transition does not replay as a fresh candidate.
    r2 = await _run(pg_pool, contention_flip_enabled=True)
    assert r2.finding.data["fired"] == 0
    counts2 = r2.finding.data["counts_by_class"][ats.TRIGGER_CONTENTION]
    assert counts2["killed"] is False
    assert counts2["candidates"] == 0


async def test_contention_flip_kill_switch_env_var(pg_pool, clean_slate, monkeypatch):
    monkeypatch.setenv(ats._CONTENTION_FLIP_ENABLED_ENV, "true")
    fact_a = uuid4()
    async with pg_pool.acquire() as conn:
        cid = await _insert_contention(conn, subject=f"subj_{uuid4().hex[:8]}")
        await _insert_contention_value(conn, cid, [fact_a], "value-a")
        tied = await _insert_finding(
            conn, sev_tag=None, confidence=0.9, derived=[fact_a]
        )
        await _insert_faith_critique(conn, tied, 0.85)
    deps = _Deps(pg_pool, _FakeDispatcher())
    seed_options = {
        "sub_handler": "alert_trigger_scan",
        "analyst_id": "alert_trigger_scan",
        "run_id": str(uuid4()),
    }
    await ats.handle([], seed_options, deps)  # seed, no options override

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE fact_contention SET status = 'surfaced', "
            "surfaced_value = 'value-a', surfaced_fact_id = $2, "
            "updated_at = now() WHERE id = $1",
            cid,
            fact_a,
        )
    r = await ats.handle(
        [], {**seed_options, "run_id": str(uuid4())}, deps
    )
    assert r.finding.data["fired"] == 1, "the env var alone re-enables the class"


async def test_geo_convergence_killed_by_default_writes_no_row_but_counts(
    pg_pool, geo_clean_slate
):
    async with pg_pool.acquire() as conn:
        await _seed_three_geo_family_sources(conn)
        await _insert_geo_signal(conn, "geo6test.quake", geo_tags=[_GEO_CC_FORMATION])
        await _insert_geo_signal(conn, "geo6test.news", geo_tags=[_GEO_CC_FORMATION])
    await _run(pg_pool)  # seeds (below the 3-family bar)

    async with pg_pool.acquire() as conn:
        await _insert_geo_signal(conn, "geo6test.tg", geo_tags=[_GEO_CC_FORMATION])
    r = await _run(pg_pool, geo_convergence_enabled=False)
    assert r.finding.data["fired"] == 0
    counts = r.finding.data["counts_by_class"][ats.TRIGGER_GEO_CONVERGENCE]
    assert counts["killed"] is True
    assert counts["killed_would_have_fired"] == 1
    async with pg_pool.acquire() as conn:
        assert _geo_rows(await _alert_rows(conn)) == []

    # No backlog on resurrection.
    r2 = await _run(pg_pool, geo_convergence_enabled=True)
    assert r2.finding.data["fired"] == 0
    counts2 = r2.finding.data["counts_by_class"][ats.TRIGGER_GEO_CONVERGENCE]
    assert counts2["killed"] is False
    assert counts2["candidates"] == 0


async def test_handle_production_defaults_kill_list_off_and_budget_five(
    pg_pool, clean_slate, monkeypatch
):
    """Pins the REAL production default with NO test-harness convenience
    overrides at all: contention_flip and geo_convergence off, budget 5."""
    monkeypatch.delenv(ats._CONTENTION_FLIP_ENABLED_ENV, raising=False)
    monkeypatch.delenv(ats._GEO_CONVERGENCE_ENABLED_ENV, raising=False)
    monkeypatch.delenv(ats._DAILY_PAGE_BUDGET_ENV, raising=False)
    deps = _Deps(pg_pool, _FakeDispatcher())
    options = {
        "sub_handler": "alert_trigger_scan",
        "analyst_id": "alert_trigger_scan",
        "run_id": str(uuid4()),
    }
    r = await ats.handle([], options, deps)
    assert r.finding.data["daily_page_budget"] == ats.DEFAULT_DAILY_PAGE_BUDGET == 5
    assert r.finding.data["counts_by_class"][ats.TRIGGER_CONTENTION]["killed"] is True
    assert (
        r.finding.data["counts_by_class"][ats.TRIGGER_GEO_CONVERGENCE]["killed"]
        is True
    )
