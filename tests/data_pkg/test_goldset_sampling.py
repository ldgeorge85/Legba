# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2-5 — deterministic weekly gold-set sampling (pure, DB-free).

Covers the sampler's contract: determinism (same week → same sample,
candidate-order independent), stratification (per-unit coverage + a high/low
faithfulness mix), exclusion (already-labeled ids never appear), rendezvous
stability (candidate churn only perturbs the picks it touches), the size cap,
the ISO-week arithmetic, and the mirrored-units drift guard against
``unit_correctness_scorer``.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timezone
from uuid import uuid4

from legba.data.registry.goldset_sampling import (
    DEFAULT_SAMPLE_SIZE,
    DEFAULT_UNITS,
    HIGH_FAITHFULNESS_BAND,
    Candidate,
    iso_week_key,
    next_week_start_utc,
    select_weekly_sample,
    week_start_utc,
)

WEEK = "2026-W30"


def _cand(unit: str, faith: float | None = 0.9, fid: str | None = None) -> Candidate:
    return Candidate(
        finding_id=fid or str(uuid4()),
        unit=unit,
        target_id=f"country_g20_{unit[:2]}",
        faithfulness=faith,
    )


def _pool_all_units(per_unit: int = 4) -> list[Candidate]:
    """A pool with both faithfulness bands in every default unit."""
    pool: list[Candidate] = []
    for unit in DEFAULT_UNITS:
        for i in range(per_unit):
            faith = 0.9 if i % 2 == 0 else 0.55  # alternate high/low
            pool.append(_cand(unit, faith))
    return pool


# ---------------------------------------------------------------------------
# Determinism + stability
# ---------------------------------------------------------------------------


def test_same_week_same_candidates_same_sample():
    pool = _pool_all_units()
    a = select_weekly_sample(pool, week=WEEK)
    b = select_weekly_sample(pool, week=WEEK)
    assert [s.finding_id for s in a] == [s.finding_id for s in b]


def test_candidate_order_does_not_matter():
    pool = _pool_all_units()
    shuffled = pool[:]
    random.Random(7).shuffle(shuffled)
    a = select_weekly_sample(pool, week=WEEK)
    b = select_weekly_sample(shuffled, week=WEEK)
    assert [s.finding_id for s in a] == [s.finding_id for s in b]


def test_different_week_reseeds_the_sample():
    """The seed IS the ISO week: with a wide pool, week N and week N+1 pick
    observably different samples (rendezvous priorities re-hash)."""
    pool = _pool_all_units(per_unit=8)
    a = {s.finding_id for s in select_weekly_sample(pool, week="2026-W30")}
    b = {s.finding_id for s in select_weekly_sample(pool, week="2026-W31")}
    assert a != b


def test_rendezvous_stability_under_candidate_churn():
    """Adding one candidate perturbs at most its own unit slot + the fill slot —
    never a global reshuffle (the rendezvous-hash property the pin relies on
    before first read)."""
    pool = _pool_all_units()
    before = {s.finding_id for s in select_weekly_sample(pool, week=WEEK)}
    after = {
        s.finding_id
        for s in select_weekly_sample(pool + [_cand(DEFAULT_UNITS[0])], week=WEEK)
    }
    assert len(before & after) >= DEFAULT_SAMPLE_SIZE - 2


# ---------------------------------------------------------------------------
# Stratification
# ---------------------------------------------------------------------------


def test_per_unit_coverage():
    """k=8 over 7 populated units → every unit appears at least once."""
    sample = select_weekly_sample(_pool_all_units(), week=WEEK)
    assert len(sample) == DEFAULT_SAMPLE_SIZE
    assert {s.unit for s in sample} == set(DEFAULT_UNITS)


def test_faithfulness_mix_over_weeks():
    """Across several weeks the samples include BOTH high- and low-band
    findings — the per-(week, unit) band-lead bit rotates, so the loop never
    fixates on only-solid or only-shaky reads."""
    pool = _pool_all_units(per_unit=6)
    faith_by_id = {c.finding_id: c.faithfulness for c in pool}
    bands = set()
    for wk in ("2026-W28", "2026-W29", "2026-W30", "2026-W31"):
        for s in select_weekly_sample(pool, week=wk):
            f = faith_by_id[s.finding_id]
            bands.add("high" if f is not None and f >= HIGH_FAITHFULNESS_BAND else "low")
    assert bands == {"high", "low"}


def test_missing_unit_does_not_block_fill():
    """Only two units populated → round 1 yields 2, round 2 fills to k from the
    leftover pool (no invented rows for empty units)."""
    pool = [_cand(DEFAULT_UNITS[0]) for _ in range(6)] + [
        _cand(DEFAULT_UNITS[1]) for _ in range(6)
    ]
    sample = select_weekly_sample(pool, week=WEEK)
    assert len(sample) == DEFAULT_SAMPLE_SIZE
    assert {s.unit for s in sample} == {DEFAULT_UNITS[0], DEFAULT_UNITS[1]}


# ---------------------------------------------------------------------------
# Exclusion + caps
# ---------------------------------------------------------------------------


def test_already_labeled_excluded():
    pool = _pool_all_units()
    excluded = {c.finding_id for c in pool[:10]}
    sample = select_weekly_sample(pool, week=WEEK, exclude=excluded)
    assert not ({s.finding_id for s in sample} & excluded)


def test_small_pool_returns_everything_once():
    pool = [_cand(DEFAULT_UNITS[0]), _cand(DEFAULT_UNITS[1])]
    sample = select_weekly_sample(pool, week=WEEK)
    assert sorted(s.finding_id for s in sample) == sorted(c.finding_id for c in pool)
    # Ranks are the display order 0..n-1.
    assert [s.rank for s in sample] == list(range(len(sample)))


def test_empty_pool_is_an_honest_empty_sample():
    assert select_weekly_sample([], week=WEEK) == []


def test_duplicate_finding_ids_collapse():
    c = _cand(DEFAULT_UNITS[0])
    sample = select_weekly_sample([c, c, c], week=WEEK)
    assert [s.finding_id for s in sample] == [c.finding_id]


# ---------------------------------------------------------------------------
# Week arithmetic
# ---------------------------------------------------------------------------


def test_iso_week_key_format_and_year_rollover():
    assert iso_week_key(date(2026, 7, 23)) == "2026-W30"
    # 2027-01-01 is a Friday of ISO week 2026-W53.
    assert iso_week_key(date(2027, 1, 1)) == "2026-W53"


def test_week_start_is_monday_utc():
    start = week_start_utc(date(2026, 7, 23))  # a Thursday
    assert start == datetime(2026, 7, 20, tzinfo=timezone.utc)
    assert start.isoweekday() == 1
    assert next_week_start_utc(date(2026, 7, 23)) == datetime(
        2026, 7, 27, tzinfo=timezone.utc
    )
    # A Monday's week starts on itself.
    assert week_start_utc(date(2026, 7, 20)) == datetime(
        2026, 7, 20, tzinfo=timezone.utc
    )


# ---------------------------------------------------------------------------
# Drift guard — the registry-slim mirror of the scorer's unit list
# ---------------------------------------------------------------------------


def test_default_units_mirror_scorer():
    """goldset_sampling.DEFAULT_UNITS MIRRORS unit_correctness_scorer's
    _DEFAULT_UNITS (the registry image cannot import the handler package);
    this guard is where drift fails loud."""
    from legba.data.analysts.deterministic_handlers.unit_correctness_scorer import (
        _DEFAULT_UNITS,
    )

    assert DEFAULT_UNITS == _DEFAULT_UNITS
