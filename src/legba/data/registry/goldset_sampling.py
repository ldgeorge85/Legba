# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic weekly gold-set sampling (P2-5) — pure, DB-free.

The correctness gold set grows only if labeling is cheap: the operator agreed
to label a HANDFUL of findings per week if a lightweight surface exists. This
module is that surface's selection brain — each ISO week it picks a small,
stratified, STABLE sample of verified head findings for labeling:

  * STRATIFIED — round 1 walks the bounded units in sorted order and takes one
    finding per unit (per-unit coverage), alternating which faithfulness band
    (high vs low) leads per (week, unit) so the sample mixes solid and shaky
    reads instead of clustering on either; round 2 fills the remaining slots
    from the leftover pool by hash priority.
  * STABLE — selection priority is a rendezvous hash ``sha256(week:finding_id)``
    (highest wins), so the same week always ranks the same finding identically,
    and candidate churn only perturbs the picks it actually touches (an
    index-shuffle would reshuffle everything on any insertion). The route layer
    additionally PINS the week's membership on first read
    (``goldset_week_samples``) so mid-week churn cannot shift it at all.
  * EXCLUDING already-labeled — the caller passes the finding ids labeled
    BEFORE the week started; labels created DURING the week do not exclude, so
    the worksheet keeps showing the item (with its saved state) instead of
    resampling mid-week.

REGISTRY-IMAGE SAFE: stdlib only — importable by ``goldset_api`` without
pulling the deterministic-handler package (pycountry / networkx) into the slim
registry image. ``DEFAULT_UNITS`` therefore MIRRORS
``unit_correctness_scorer._DEFAULT_UNITS`` (the since_api mirrored-constants
precedent); a drift-guard test asserts the two stay identical.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

#: The bounded reasoning units the correctness plane measures individually.
#: MIRROR of ``legba.data.analysts.deterministic_handlers.unit_correctness_scorer
#: ._DEFAULT_UNITS`` — keep in sync (drift guard: tests/data_pkg/
#: test_goldset_sampling.py::test_default_units_mirror_scorer).
DEFAULT_UNITS: tuple[str, ...] = (
    "leadership_transition",
    "energy_security",
    "escalation",
    "narrative_coordination",
    "internal_stability",
    "military_posture",
    "economic_coercion",
)

#: Weekly sample size — "a handful": one per unit (7) + one hash-priority fill.
DEFAULT_SAMPLE_SIZE = 8

#: Faithfulness band split. The verify floor is 0.50 (below it a finding is not
#: "verified" product); 0.75 splits the verified range into shaky [0.50, 0.75)
#: vs solid [0.75, 1.0] so the mix covers both failure-hunting and spot-checks.
HIGH_FAITHFULNESS_BAND = 0.75


@dataclass(frozen=True)
class Candidate:
    """One verified, non-superseded, recent head finding eligible for labeling."""

    finding_id: str
    unit: str
    target_id: str | None
    faithfulness: float | None


@dataclass(frozen=True)
class SampledFinding:
    """One picked worksheet slot: the finding + its stratum bookkeeping."""

    finding_id: str
    unit: str
    rank: int


# ---------------------------------------------------------------------------
# Week arithmetic (ISO week = the sampling epoch; Monday 00:00 UTC boundaries)
# ---------------------------------------------------------------------------


def iso_week_key(d: date) -> str:
    """The ISO week key, e.g. ``2026-W30`` — the sample's seed + pin key."""
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_start_utc(d: date) -> datetime:
    """Monday 00:00 UTC of ``d``'s ISO week — the already-labeled exclusion
    boundary (labels created before this never re-enter a sample)."""
    monday = d - timedelta(days=d.isoweekday() - 1)
    return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)


def next_week_start_utc(d: date) -> datetime:
    """Next Monday 00:00 UTC — when the next sample opens (the UI's honest
    "all labeled — next sample Monday" empty state)."""
    return week_start_utc(d) + timedelta(days=7)


# ---------------------------------------------------------------------------
# Deterministic priorities
# ---------------------------------------------------------------------------


def _priority(week: str, finding_id: str) -> int:
    """Rendezvous priority for one (week, finding): highest wins. Pure hash —
    no RNG state, so ranking is order-independent and insertion-stable."""
    digest = hashlib.sha256(f"{week}:{finding_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _prefers_low_band(week: str, unit: str) -> bool:
    """Which faithfulness band leads for this (week, unit) — a deterministic
    parity bit that rotates weekly, so over weeks each unit's picks mix
    high- and low-faithfulness reads."""
    digest = hashlib.sha256(f"{week}:{unit}:band".encode("utf-8")).digest()
    return bool(digest[0] & 1)


def _band(c: Candidate) -> str:
    """``high`` / ``low`` faithfulness band (an unscored faithfulness reads as
    low — it is the shakier object, exactly what a spot-check should see)."""
    if c.faithfulness is not None and c.faithfulness >= HIGH_FAITHFULNESS_BAND:
        return "high"
    return "low"


# ---------------------------------------------------------------------------
# The sampler
# ---------------------------------------------------------------------------


def select_weekly_sample(
    candidates: Sequence[Candidate],
    *,
    week: str,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    exclude: Iterable[str] = (),
) -> list[SampledFinding]:
    """Pick this week's labeling sample — deterministic in (candidates, week,
    exclude); see the module docstring for the strata.

    Round 1 (unit coverage): for each unit present, in sorted unit order, take
    its best candidate — "best" = highest priority within the (week, unit)'s
    preferred faithfulness band, falling back to the other band when the
    preferred one is empty. Round 2 (fill): remaining slots go to the leftover
    pool by global priority. Duplicate finding_ids are collapsed (first kept);
    excluded ids never appear.
    """
    excluded = set(exclude)
    seen: set[str] = set()
    pool: list[Candidate] = []
    for c in candidates:
        if c.finding_id in excluded or c.finding_id in seen:
            continue
        seen.add(c.finding_id)
        pool.append(c)

    by_unit: dict[str, list[Candidate]] = {}
    for c in pool:
        by_unit.setdefault(c.unit, []).append(c)

    picked: list[Candidate] = []
    picked_ids: set[str] = set()

    # Round 1 — one per unit, preferred-band first.
    for unit in sorted(by_unit):
        if len(picked) >= sample_size:
            break
        unit_pool = by_unit[unit]
        preferred = "low" if _prefers_low_band(week, unit) else "high"
        in_band = [c for c in unit_pool if _band(c) == preferred]
        bucket = in_band if in_band else unit_pool
        best = max(bucket, key=lambda c: (_priority(week, c.finding_id), c.finding_id))
        picked.append(best)
        picked_ids.add(best.finding_id)

    # Round 2 — fill remaining slots by global priority.
    if len(picked) < sample_size:
        rest = [c for c in pool if c.finding_id not in picked_ids]
        rest.sort(key=lambda c: (_priority(week, c.finding_id), c.finding_id), reverse=True)
        for c in rest[: sample_size - len(picked)]:
            picked.append(c)
            picked_ids.add(c.finding_id)

    return [
        SampledFinding(finding_id=c.finding_id, unit=c.unit, rank=i)
        for i, c in enumerate(picked)
    ]


__all__ = [
    "Candidate",
    "SampledFinding",
    "DEFAULT_UNITS",
    "DEFAULT_SAMPLE_SIZE",
    "HIGH_FAITHFULNESS_BAND",
    "iso_week_key",
    "week_start_utc",
    "next_week_start_utc",
    "select_weekly_sample",
]
