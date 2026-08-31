# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Situation clustering handler — materializes the `situations` table from the
`situation_signature`-stamped findings that finding_supersession produces.

Pure-logic coverage of the grouping + situation-field derivation + the
synthetic (deps=None) summary path, and (H1) of the FORGETTING CURVE: which
clock decays a frame's intensity, which one demotes its status, and what a
resolution-grounded close now reaches. The live DB upsert is verified against
the running stack."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts import deterministic
from legba.data.analysts.deterministic import SUB_HANDLERS, OUTPUT_KIND_BY_SUB_HANDLER
from legba.data.analysts.deterministic_handlers import situation_clustering as sc
from legba.data.config import PostgresConfig
from legba.data.situations import trajectory as tj
from legba.runtime.analyst_method import AnalystMethodResult


def _row(rid: str, sig: str | None, title: str, day: int) -> dict:
    r = {"id": rid, "title": title,
         "produced_at": datetime(2026, 6, day, tzinfo=timezone.utc)}
    if sig is not None:
        r["situation_signature"] = sig
    return r


def test_registered_in_dispatch_table():
    assert "situation_clustering" in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER["situation_clustering"].value == "finding"


def test_topic_from_signature():
    assert sc._topic_from_signature("sig:country_g20_ar|argentina,earthquake") == "country_g20_ar"
    assert sc._topic_from_signature("sit:explicit-42") == ""
    assert sc._topic_from_signature("garbage") == ""


def test_topic_survives_the_dimension_tail():
    """#64 — the topic is what `_target_for_category` resolves `target_id` from,
    so a dimensioned key whose topic came back as
    `country_g20_ar#dim:internal_stability` would silently strand every split
    frame outside its own desk's grounding read."""
    dimensioned = "sig:country_g20_ar#dim:internal_stability"
    assert sc._topic_from_signature(dimensioned) == "country_g20_ar"
    assert sc._target_for_category(
        sc._topic_from_signature(dimensioned), None) == "country_g20_ar"
    assert sc._dimension_from_signature(dimensioned) == "internal_stability"
    # ...and with an entity tail as well, in the order the key writes them.
    both = "sig:country_g20_ar|argentina#dim:military_posture"
    assert sc._topic_from_signature(both) == "country_g20_ar"
    assert sc._dimension_from_signature(both) == "military_posture"
    # A pre-#64 key reports no dimension rather than a fabricated one.
    assert sc._dimension_from_signature("sig:country_g20_ar") is None


def test_group_by_signature_skips_unstamped():
    rows = [
        _row("a", "sig:x|e1", "A", 1),
        _row("b", "sig:x|e1", "B", 2),
        _row("c", None, "C", 3),  # no signature → excluded
    ]
    groups = sc._group_by_signature(rows)
    # No analyst_id on these synthetic rows → the honest UNATTRIBUTED bucket.
    key = f"sig:x|e1#dim:{sc._with_dimension('sig:x', None).split('#dim:')[1]}"
    assert set(groups) == {key}
    assert len(groups[key]) == 2


def test_situation_fields_name_is_latest_and_counts():
    rows = [
        _row("a", "sig:x|e1", "older framing", 1),
        _row("b", "sig:x|e1", "newest framing", 5),
    ]
    # Evaluate "as of" the newest member so the decay is deterministic.
    f = sc._situation_fields(
        "sig:x|e1", rows, now=datetime(2026, 6, 5, tzinfo=timezone.utc),
    )
    assert f["name"] == "newest framing"  # latest produced_at wins
    assert f["category"] == "x"
    assert f["event_count"] == 2
    # Recency-weighted intensity (exp half-life): newest member ≈ 1.0, the
    # 4-day-older one decays below 1.0 → total is < the raw count of 2.
    assert 1.0 < f["intensity_score"] < 2.0
    assert f["status"] == "active"  # freshest member is "now" → active
    assert set(f["member_finding_ids"]) == {"a", "b"}
    assert f["last_event_at"] == datetime(2026, 6, 5, tzinfo=timezone.utc)
    # Temporal frame (Phase 5a): valid_from = earliest member; an active
    # situation is an OPEN frame (valid_until NULL).
    assert f["valid_from"] == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert f["valid_until"] is None


def test_situation_lifecycle_decays_active_dormant_closed():
    """A situation fades + transitions status as its newest member ages — the
    'events come and go' mechanic."""
    rows = [_row("a", "sig:x", "framing", 5)]  # newest member at 2026-06-05
    # ~1 day later → still active, intensity ≈ 1.0
    fa = sc._situation_fields("sig:x", rows, now=datetime(2026, 6, 6, tzinfo=timezone.utc))
    assert fa["status"] == "active"
    # ~4 days later → dormant, intensity decayed below the 1-day value
    fd = sc._situation_fields("sig:x", rows, now=datetime(2026, 6, 9, tzinfo=timezone.utc))
    assert fd["status"] == "dormant"
    assert fd["intensity_score"] < fa["intensity_score"]
    # ~10 days later → closed
    fc = sc._situation_fields("sig:x", rows, now=datetime(2026, 6, 15, tzinfo=timezone.utc))
    assert fc["status"] == "closed"
    assert fc["intensity_score"] < fd["intensity_score"]
    # Temporal frame: the situation stays OPEN (valid_until NULL) while active
    # AND dormant, and only stamps valid_until = last_event_at when it CLOSES.
    assert fa["valid_from"] == datetime(2026, 6, 5, tzinfo=timezone.utc)
    assert fa["valid_until"] is None
    assert fd["valid_until"] is None  # dormant is still an open frame
    assert fc["valid_until"] == datetime(2026, 6, 5, tzinfo=timezone.utc)


async def test_handle_synthetic_summarizes_clusters():
    inputs = [
        _row("a", "sig:x|e1", "A", 1),
        _row("b", "sig:x|e1", "B", 2),
        _row("c", "sig:y|e2", "C", 3),
        _row("d", "sig:y|e2", "D", 4),
    ]
    result = await sc.handle(inputs, {"analyst_id": "situation_clustering"}, None)
    assert isinstance(result, AnalystMethodResult)
    data = result.finding.data
    assert data["sub_handler"] == "situation_clustering"
    assert data["situations_created"] == 0  # synthetic path does not write
    assert len(data["clusters"]) == 2
    assert result.finding.tags == ["deterministic", "situation_clustering"]
    assert result.finding.kind_marker == "finding"
    # 0 new situations → idempotent refresh, suppressed from the feed
    # (force_trace_only) so it doesn't repeat the identical summary every tick.
    assert result.force_trace_only is True


async def test_no_new_situations_run_is_trace_only():
    """A no-new-situation run is suppressed from the feed; the summary finding
    is still BUILT (it flows into the trace, so nothing is lost)."""
    result = await sc.handle([], {"analyst_id": "situation_clustering"}, None)
    assert result.force_trace_only is True
    assert "0 new" in result.finding.title


# ---------------------------------------------------------------------------
# DQ P6 — snapshot/JSON name reject + steady_state tag + composition exclusion
# ---------------------------------------------------------------------------


def test_situation_name_rejects_dated_snapshot_and_json_titles():
    """A dated-snapshot title ('… — YYYY-MM-DD') or a leaked JSON-envelope
    fragment ('"title": …') must NOT name a frame — it falls back to the
    signature's topic label so a report receipt never mints a JSON/date name."""
    dated = [_row("a", "sig:world", "World situational assessment — 2026-06-30", 1)]
    assert sc._situation_name(dated, "sig:world") == "Situation: world"
    jsonleak = [_row("b", "sig:world",
                     '"title": "World situational assessment — 2026-06-30",', 2)]
    assert sc._situation_name(jsonleak, "sig:world") == "Situation: world"
    # a normal event title is kept verbatim
    ok = [_row("c", "sig:x", "Russia – Energy-weapon coercion", 3)]
    assert sc._situation_name(ok, "sig:x") == "Russia – Energy-weapon coercion"


def test_situation_fields_marks_steady_state():
    """A non-event / status-quo frame is authoritatively tagged steady_state at
    materialization (the same shared predicate the grounding read uses); a real
    event frame is not."""
    steady = [_row("a", "sig:country_g20_us",
                   "United States – No observable WMD proliferation activity", 5)]
    fs = sc._situation_fields("sig:country_g20_us", steady,
                              now=datetime(2026, 6, 5, tzinfo=timezone.utc))
    assert fs["steady_state"] is True
    real = [_row("b", "sig:country_watch_ua",
                 "Ukraine – Energy-weapon coercion escalates", 5)]
    fr = sc._situation_fields("sig:country_watch_ua", real,
                              now=datetime(2026, 6, 5, tzinfo=timezone.utc))
    assert fr["steady_state"] is False


def test_group_by_signature_excludes_composition_analysts():
    """A composition/meta producer's already-stamped row is excluded from
    re-materialization (defense-in-depth for the synthetic path)."""
    unit = {**_row("u", "sig:country_g20_us", "US read", 2),
            "analyst_id": "internal_stability"}
    comp = {**_row("c", "sig:country_g20_us", "US – Composite Assessment", 3),
            "analyst_id": "country_composition"}
    groups = sc._group_by_signature([unit, comp])
    assert set(groups) == {"sig:country_g20_us#dim:internal_stability"}
    members = {
        str(r["id"]) for r in groups["sig:country_g20_us#dim:internal_stability"]
    }
    assert members == {"u"}  # the country_composition row is dropped


# ---------------------------------------------------------------------------
# H1 — THE FORGETTING CURVE (CORRECTNESS-R2 M-1)
#
# Every number below is the AR frame's REAL shape at the round's T0
# (2026-08-25T18:58:39Z, read read-only off the live DB): 396 member findings,
# 34 ledger rows of which 30 are `unchanged_checkpoint`, four significant deltas
# with the newest on 2026-08-21, `intensity 59.34` and `status active` — on a
# maritime pilots' strike that had ENDED on 5 August, three weeks earlier.
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 8, 25, 18, 58, 39, tzinfo=timezone.utc)


def _corroboration(day: int, count: int) -> tj.Corroboration:
    return tj.Corroboration(
        last_corroborated_at=datetime(2026, 8, day, 14, tzinfo=timezone.utc),
        count=count,
    )


def _ar_members(n: int = 396) -> list[dict]:
    """The AR membership shape at the round's T0: 396 desk reads across the
    30-day lookback (~13 a day — seven desks writing several times each), the
    newest an hour old, mostly saying that no material change was observed.

    The density is the point. 13 findings a day at the 3-day member half-life
    sums to ~59, which IS the intensity the live frame carried and the number
    the AR escalation head printed as "an intensity of 59.4 as of 25 August".
    """
    step = 30 * 24 / n
    return [
        {
            "id": f"m{i}",
            "title": "Argentina – No observable coercive economic pressure",
            "severity": "low",
            "produced_at": _T0 - timedelta(hours=1 + i * step),
        }
        for i in range(n)
    ]


def _member_clock_intensity(members: list[dict]) -> float:
    """The PRE-H1 number: the member-recency sum with no evidence scaling. H1
    leaves ``_decayed_intensity`` untouched, so this is exactly what the frame
    used to carry."""
    return sc._decayed_intensity([m["produced_at"] for m in members], _T0)


def test_checkpoint_membership_alone_no_longer_sustains_intensity():
    """THE HEADLINE, on the live frame's real shape. 396 desk reads, the newest
    an hour old, summing to the 59.34 the register actually printed — and a
    world that last moved the frame four days ago. The member clock says
    active; the evidence clock says dormant and worth a third as much."""
    members = _ar_members()
    before = _member_clock_intensity(members)
    assert 55.0 < before < 65.0  # the live value was 59.34

    after = sc._situation_fields(
        "sig:country_g20_ar", members, now=_T0,
        corroboration=_corroboration(21, 4),
    )
    assert after["status"] == "dormant"
    assert after["intensity_score"] < before / 2
    # The bookkeeping counters are UNCHANGED — the fix decays the frame's claim
    # to currency, it does not rewrite what the system actually wrote.
    assert after["event_count"] == 396
    assert after["last_event_at"] == max(m["produced_at"] for m in members)


def test_a_frame_the_ledger_never_moved_decays_from_its_own_opening():
    """THE FLEET'S LARGEST CLASS. The 2026-08-27 DQ sweep found 24 of 50
    non-closed frames with no evidence-bearing ledger row EVER — 22 with no
    ledger rows at all — every one rendering ``active`` at intensity up to 60.9
    with ``updated_at`` refreshed within the minute. The worst, a Saudi Arabia /
    Houthi maritime-embargo frame, was 73 days old with zero events.

    Such a frame decays from its OPENING at the longest half-life the system
    offers: no measured density means the decay must be earned by age alone."""
    members = _ar_members()
    opened = _T0 - timedelta(days=77.8)  # the live Saudi frame's age
    assert _member_clock_intensity(members) > 55.0
    after = sc._situation_fields(
        "sig:country_g20_sa", members, now=_T0,
        corroboration=None, opened_at=opened,
    )
    assert after["status"] == "dormant"
    # ~2.1% retained: under the alert plane's own 2.0 paging floor.
    assert after["intensity_score"] < 2.0
    # Never DATED as corroborated — the render says NEVER in words instead.
    assert after["last_corroborated_at"] is None
    assert after["evidence_anchor_at"] == opened
    assert after["corroboration_count"] == 0


def test_a_young_unadjudicated_frame_keeps_its_currency():
    """The counterweight, and why the uncorroborated horizon is the trajectory
    module's own 14-day DORMANCY_DAYS rather than the 72h slice: the tracker
    examines the top twelve frames hourly, so a genuinely new situation can wait
    days for its first adjudication. Waiting is not evidence of quiet."""
    members = _ar_members(12)
    fields = sc._situation_fields(
        "sig:country_g20_ar", members, now=_T0,
        corroboration=None, opened_at=_T0 - timedelta(days=4),
    )
    assert fields["status"] == "active"
    assert fields["persistence"] > 0.8
    # Past the fortnight it settles, without ever being auto-closed.
    old = sc._situation_fields(
        "sig:country_g20_ar", members, now=_T0,
        corroboration=None, opened_at=_T0 - timedelta(days=20),
    )
    assert old["status"] == "dormant"


def test_half_life_is_keyed_to_evidence_density():
    """A frame the world moved ONCE fades in a day and a half; one moved nine
    separate times holds for four and a half. Clamped at both ends."""
    assert sc.corroboration_half_life_days(1) == 1.5
    assert sc.corroboration_half_life_days(4) == 3.0
    assert sc.corroboration_half_life_days(9) == 4.5
    assert sc.corroboration_half_life_days(10_000) == 14.0
    assert sc.corroboration_half_life_days(0) == 1.0


def test_density_decides_how_far_the_same_staleness_decays():
    """SAME evidence age, different corroboration counts: the singly-sourced
    frame is gone and the nine-times-corroborated one is merely faded. This is
    what keying the half-life to evidence density buys."""
    members = _ar_members(50)
    thin = sc._situation_fields(
        "sig:x", members, now=_T0, corroboration=_corroboration(11, 1),
    )
    thick = sc._situation_fields(
        "sig:x", members, now=_T0, corroboration=_corroboration(11, 9),
    )
    assert thin["intensity_score"] < thick["intensity_score"] / 20
    assert thin["status"] == thick["status"] == "dormant"


def test_the_corroboration_clock_only_ever_demotes():
    """It can never make a frame MORE active than the member clock does, and it
    never auto-closes: closing on silence is what trajectory.next_state refuses
    (D4), and this clock must not smuggle the opposite rule in."""
    # Desks silent for eleven days => the member clock closes it. Fresh
    # corroboration must NOT resurrect it to active.
    stale_members = [{"id": "a", "title": "Argentina – protest wave",
                      "severity": "high", "produced_at": _T0 - timedelta(days=11)}]
    closed = sc._situation_fields(
        "sig:x", stale_members, now=_T0, corroboration=_corroboration(25, 6),
    )
    assert closed["status"] == "closed"
    # And a fortnight-stale corroboration clock never CLOSES a frame whose desks
    # are still writing — it goes dormant and waits.
    quiet = sc._situation_fields(
        "sig:x", _ar_members(20), now=_T0, corroboration=_corroboration(1, 2),
    )
    assert quiet["status"] == "dormant"


def test_corroboration_inside_the_slice_horizon_stays_active():
    """The horizon is the desk's own 72h slice. A frame the world moved
    yesterday is exactly as active as it was before H1."""
    fields = sc._situation_fields(
        "sig:x", _ar_members(30), now=_T0, corroboration=_corroboration(25, 3),
    )
    assert fields["status"] == "active"
    assert fields["persistence"] > 0.8


def test_a_resolution_grounded_close_now_reaches_situations_status():
    """THE RESOLUTION HOP. ``next_state`` could reach STATE_CLOSED since it
    shipped, but that verdict lived only in ``situation_events.state_to`` while
    both register reads gate on ``situations.status <> 'closed'`` — so a frame
    the tower had CLOSED went on rendering at full intensity. It closes here
    now, and stamps its temporal frame shut."""
    closed_state = tj.next_state(
        tj.STATE_ESCALATING, tj.DELTA_DE_ESCALATES, resolution_grounded=True,
    )
    assert closed_state == tj.STATE_CLOSED
    members = _ar_members(396)
    fields = sc._situation_fields(
        "sig:country_g20_ar", members, now=_T0,
        corroboration=_corroboration(21, 4), trajectory_state=closed_state,
    )
    assert fields["status"] == "closed"
    assert fields["valid_until"] == fields["last_event_at"]
    # A non-terminal trajectory state decides nothing here — the member and
    # corroboration clocks still own the ladder.
    still_open = sc._situation_fields(
        "sig:country_g20_ar", members, now=_T0,
        corroboration=_corroboration(21, 4), trajectory_state=tj.STATE_ESCALATING,
    )
    assert still_open["status"] == "dormant"


def test_decay_constants_are_env_tunable_and_typo_proof():
    """The idiom next door in runtime.grounding: an env override, and a bad
    value falls back to the module default rather than crashing a prompt
    build."""
    assert sc._tunable("LEGBA_NOT_SET_ANYWHERE_H1", 4.5) == 4.5
    key = "LEGBA_SITUATION_CORROBORATION_HALF_LIFE_DAYS"
    os.environ[key] = "6"
    try:
        assert sc.corroboration_half_life_days(1) == 6.0
    finally:
        del os.environ[key]
    for bad in ("", "   ", "not-a-number", "0", "-3"):
        os.environ[key] = bad
        try:
            assert sc.corroboration_half_life_days(1) == 1.5
        finally:
            del os.environ[key]


# ---------------------------------------------------------------------------
# #64 — THE MEGA-FRAME SPLIT, through the REAL dispatch path
#
# One country desk's seven units used to materialize ONE situation, because the
# derived key was topic-only and `situations` is keyed
# `(situation_signature, analyst_id)` on the CLUSTERING handler's id — one value
# fleet-wide. At the H1 census that was 364 members on AR, 478 on US, and
# exactly one open frame per desk across all 33 desks.
# ---------------------------------------------------------------------------

_DESKS = [
    "internal_stability", "military_posture", "economic_coercion",
    "energy_security", "narrative_coordination", "leadership_transition",
    "proliferation_watch",
]


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    await p.close()


class _Deps:
    """The dep bundle the deterministic dispatcher hands a sub-handler."""

    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool


@pytest_asyncio.fixture
async def desk(pool):
    """A target no other row can carry + this run's own clustering analyst id.

    The handler upserts on `(situation_signature, analyst_id)`, so a private
    analyst_id makes this test's frames its own namespace on a substrate the
    whole suite shares. Teardown CLOSES rather than deletes — `hypotheses` and
    the append-only ledger both reference `situations`.
    """
    target = f"country_clust_{uuid4().hex[:10]}"
    analyst_id = f"situation_clustering_{uuid4().hex[:8]}"
    yield pool, target, analyst_id
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE situations SET status = 'closed' WHERE analyst_id = $1",
            analyst_id,
        )


async def _stamped_finding(
    conn: Any, *, analyst_id: str, target_id: str, title: str, signature: str,
    hours_ago: float,
) -> UUID:
    fid = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, confidence, data, analyst_id, target_id,
             produced_at, schema_uri, situation_signature, severity)
        VALUES ($1, 'finding', $2, '', 0.9, '{}'::jsonb, $3, $4, $5,
                'iglu:legba/finding/jsonschema/1-0-0', $6, 'moderate')
        """,
        fid, title, analyst_id, target_id,
        datetime.now(timezone.utc) - timedelta(hours=hours_ago), signature,
    )
    return fid


async def _materialize(pool: Any, analyst_id: str) -> AnalystMethodResult:
    """THE REAL BINDING PATH: the descriptor's declared impl is
    ``legba.data.analysts.deterministic:run_method``, which resolves
    ``options['sub_handler']`` through ``SUB_HANDLERS`` and dispatches. Calling
    the sub-handler directly would skip the resolution that actually binds it."""
    return await deterministic.run_method(
        [],
        {
            "sub_handler": "situation_clustering",
            "analyst_id": analyst_id,
            "run_id": str(uuid4()),
        },
        _Deps(pool),
    )


async def _frames(
    conn: Any, analyst_id: str, target: str,
) -> dict[str, dict[str, Any]]:
    """THIS TEST'S frames only.

    The handler is a fleet-wide sweep — it materializes every signature group in
    the substrate on every tick, which is what production wants and what makes an
    unscoped assertion here a statement about the whole suite. Scope on BOTH the
    private analyst_id (the upsert key's other half) and the private target.
    """
    rows = await conn.fetch(
        "SELECT situation_signature, id, event_count, derived_from, category, "
        "       target_id, data "
        "  FROM situations WHERE analyst_id = $1 AND target_id = $2",
        analyst_id, target,
    )
    return {r["situation_signature"]: dict(r) for r in rows}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_desk_materializes_one_frame_per_dimension(desk):
    """THE REPAIR, end to end. Seven units, one country, findings that all carry
    the pre-#64 topic-only signature in their stored column — seven frames, not
    one, and every one still resolves its country home."""
    pool, target, analyst_id = desk
    legacy_sig = f"sig:{target}"
    async with pool.acquire() as conn:
        for i, unit in enumerate(_DESKS * 3):
            await _stamped_finding(
                conn, analyst_id=unit, target_id=target,
                title=f"{unit} read {i}", signature=legacy_sig, hours_ago=1 + i,
            )

    await _materialize(pool, analyst_id)

    async with pool.acquire() as conn:
        frames = await _frames(conn, analyst_id, target)
    assert set(frames) == {f"{legacy_sig}#dim:{d}" for d in _DESKS}, (
        "a desk's seven units must not collapse into one country-absorbing frame"
    )
    for sig, frame in frames.items():
        assert frame["event_count"] == 3
        # `target_id` is resolved from the topic BEFORE the marker: a split frame
        # that lost it would vanish from its own desk's grounding read.
        assert frame["category"] == target
        assert frame["target_id"] == target
        assert json.loads(frame["data"])["dimension"] == sig.split("#dim:")[1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_split_factor_is_bounded_by_the_registry_not_by_content(desk):
    """WHY THIS IS NOT K=full COMING BACK.

    The full-entity key produced 1 situation from 11.5k findings because it
    hashed a MODEL-GENERATED set whose membership churned every cycle, so two
    reads of the same event never collided. The dimension is the opposite kind of
    value: a registered descriptor identity stamped by the runtime, from a
    vocabulary the registry closes. Here the same unit emits findings whose
    entity lists churn wildly — and they all still land in ONE frame, because
    content does not enter the key at all. The split factor is bounded by the
    analyst set (~8 on a country desk), known before the migration runs.
    """
    pool, target, analyst_id = desk
    legacy_sig = f"sig:{target}"
    async with pool.acquire() as conn:
        for i in range(25):
            await _stamped_finding(
                conn, analyst_id="energy_security", target_id=target,
                title=f"churning entities {uuid4().hex}", signature=legacy_sig,
                hours_ago=1 + i,
            )

    await _materialize(pool, analyst_id)

    async with pool.acquire() as conn:
        frames = await _frames(conn, analyst_id, target)
    assert set(frames) == {f"{legacy_sig}#dim:energy_security"}
    assert frames[f"{legacy_sig}#dim:energy_security"]["event_count"] == 25


@pytest.mark.integration
@pytest.mark.asyncio
async def test_materialization_is_idempotent_under_the_unique_index(desk):
    """The handler upserts on `(situation_signature, analyst_id)` every 20
    minutes. Re-running must UPDATE the same seven rows, never mint a second
    set — the property the re-key must not have broken."""
    pool, target, analyst_id = desk
    legacy_sig = f"sig:{target}"
    async with pool.acquire() as conn:
        for i, unit in enumerate(_DESKS):
            await _stamped_finding(
                conn, analyst_id=unit, target_id=target, title=f"{unit} read",
                signature=legacy_sig, hours_ago=1 + i,
            )

    first = await _materialize(pool, analyst_id)
    async with pool.acquire() as conn:
        before = await _frames(conn, analyst_id, target)
    second = await _materialize(pool, analyst_id)
    async with pool.acquire() as conn:
        after = await _frames(conn, analyst_id, target)

    assert set(before) == set(after) == {
        f"{legacy_sig}#dim:{d}" for d in _DESKS
    }
    assert {s: f["id"] for s, f in before.items()} == {
        s: f["id"] for s, f in after.items()
    }, "a re-run must UPDATE the same rows, not mint a second set"
    # The handler is a fleet-wide sweep, so its `created` counter is a statement
    # about the whole substrate on the FIRST pass. It is a statement about this
    # test on the second: nothing else writes between the two calls, so a
    # non-zero count there would mean the re-key minted a duplicate key.
    assert first.finding.data["situations_created"] >= len(_DESKS)
    assert second.finding.data["situations_created"] == 0
    assert second.finding.data["situations_updated"] >= len(_DESKS)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_findings_own_dimension_wins_over_a_stale_stored_key(desk):
    """The half-migrated case, which is the state the fleet is in between the
    code deploy and migration 0188. A member stamped BEFORE the re-key and one
    stamped AFTER must land in the SAME frame — otherwise the mega-frame goes on
    being fed by its own back-catalogue for the whole 30-day lookback and the
    migration becomes a correctness prerequisite racing the deploy."""
    pool, target, analyst_id = desk
    legacy_sig = f"sig:{target}"
    async with pool.acquire() as conn:
        old = await _stamped_finding(
            conn, analyst_id="military_posture", target_id=target,
            title="stamped before the re-key", signature=legacy_sig, hours_ago=30,
        )
        new = await _stamped_finding(
            conn, analyst_id="military_posture", target_id=target,
            title="stamped after the re-key",
            signature=f"{legacy_sig}#dim:military_posture", hours_ago=1,
        )

    await _materialize(pool, analyst_id)

    async with pool.acquire() as conn:
        frames = await _frames(conn, analyst_id, target)
    assert set(frames) == {f"{legacy_sig}#dim:military_posture"}
    members = set(frames[f"{legacy_sig}#dim:military_posture"]["derived_from"])
    assert members == {old, new}
