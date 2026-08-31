# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H4 — THE SCHEDULING RACE.

``planning/PROOF_ROUND_2026-08-25/VERDICT_DRAFT.md`` measured 30 of 31 country
targets showing desk heads landing AFTER their own composition had already
frozen (scorecard 04:40Z < composition 07:27-15:39Z < heads ~17:05Z) — a
composition's cadence is deliberately staggered AFTER its units'
``fallback_schedule`` slots (see ``descriptors/analyst_country_composition.
yaml``'s cadence comment), yet compositions were observed firing hours before
some of that day's units had even run.

ROOT CAUSE: ``source_first_runtime._wire_targets_and_triggers`` registers a
coalescing trigger for EVERY per-target analyst matched onto a target,
regardless of what that analyst actually reads. A per-target COMPOSITION
(``country_composition``, ``region_composition`` — ``subscription.targets.
data_types == ["finding"]``) consumes its OWN unit analysts' HEADS via
``other_analysts`` at run time; it never reads a raw SIGNAL. But the trigger
registration's matched bindings (``sub.bindings``) are built from the
TARGET's raw ``sources:`` list — the SAME wire feed every UNIT on that target
also watches. Wiring a finding-only composition onto that feed meant it woke
reactively (``min_llm_batch`` = 2 unrelated wire signals, no bearing on unit
completion) hours before the day's later-slotted units had run, and — because
the reactive fire stamps the SAME per-(analyst, target) cadence cooldown its
own deliberately-late scheduled tick relies on — the early reactive fire then
SUPPRESSED that correctly-ordered scheduled tick outright.

THE FIX: ``_analyst_ids_for_target`` now excludes a per-target analyst whose
``subscription.targets.data_types`` does not include ``"signal"`` from the
coalescing-trigger registration entirely (mirrors the existing ``id_list``
union exclusion immediately above it). A finding-only composition therefore
runs ONLY on its own scheduled cadence — which the descriptor authors already
ordered to land after every source unit's slot, real hours pinned below.

Two things are proven here, both against the REAL shipped descriptors (never
a hand-built stand-in for the production topology):

  1. the trigger-wiring exclusion itself (this module), against
     ``analyst_country_composition.yaml`` / ``analyst_region_composition.yaml``
     compared to a sibling UNIT descriptor matched onto the identical target;
  2. the cadence-ordering invariant the exclusion now makes load-bearing: each
     composition's scheduled tick hour is strictly AFTER every one of its
     source units' fire hour, in BOTH the AM and PM cycle — the timestamps
     that make "a composition composes the newest heads of its own cycle" a
     provable fact about the shipped topology rather than a comment.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.schemas.analyst import AnalystDescriptor
from legba.runtime import source_first_runtime

_DESCRIPTORS_DIR = Path(__file__).resolve().parents[2] / "descriptors"


def _load(descriptor_id: str) -> AnalystDescriptor:
    """A SHIPPED analyst descriptor, parsed by the real schema."""
    path = _DESCRIPTORS_DIR / f"analyst_{descriptor_id}.yaml"
    body = yaml.safe_load(path.read_text())
    return AnalystDescriptor.model_validate(body, strict=False)


class _Ident:
    def __init__(self, tid: str, kind: str = "country") -> None:
        self.id = tid
        self.kind = kind
        self.abstraction_level = "L2"


class _Scope:
    def __init__(self, geo: list[str], tags: list[str]) -> None:
        self.geo = geo
        self.entity_classes: list[str] = []
        self.tags = tags


class _Target:
    def __init__(self, tid: str, *, geo: list[str], tags: list[str]) -> None:
        self.identity = _Ident(tid)
        self.scope = _Scope(geo, tags)
        self.analyst = None


_G20_TARGET = _Target("country_g20_ir", geo=["IR"], tags=["g20"])
_REGION_TARGET = _Target("region_mena", geo=[], tags=["region"])


def _analysts_by_id(*descriptor_ids: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for did in descriptor_ids:
        descriptor = _load(did)
        out[descriptor.identity.id] = {
            "body": descriptor.model_dump(mode="json"),
        }
    return out


# ---------------------------------------------------------------------------
# 1. The exclusion itself — real shipped descriptors
# ---------------------------------------------------------------------------


def test_country_composition_registers_no_coalescing_trigger_on_a_g20_target():
    """``country_composition`` matches ``country_g20_ir`` on the predicate
    (``has_tag("g20") or has_tag("watch")``) but must NOT be handed a
    per-target coalescing trigger — its content is ``other_analysts`` heads,
    never a raw signal."""
    analysts_by_id = _analysts_by_id("country_composition")
    matched = source_first_runtime._analyst_ids_for_target(
        _G20_TARGET, analysts_by_id,
    )
    assert "country_composition" not in matched, (
        "a finding-only composition has no legitimate signal-driven wake — "
        "registering one races it against its own still-in-flight units"
    )


def test_region_composition_registers_no_coalescing_trigger_on_a_region_target():
    analysts_by_id = _analysts_by_id("region_composition")
    matched = source_first_runtime._analyst_ids_for_target(
        _REGION_TARGET, analysts_by_id,
    )
    assert "region_composition" not in matched


def test_a_sibling_signal_consuming_unit_is_still_registered_on_the_same_target():
    """The exclusion is targeted, not a blanket suppression: a UNIT with the
    identical ``has_tag("g20") or has_tag("watch")`` predicate and
    ``data_types: [signal]`` still gets its coalescing trigger — proving the
    filter keys on ``data_types``, not on the predicate or the target."""
    analysts_by_id = _analysts_by_id("escalation")
    matched = source_first_runtime._analyst_ids_for_target(
        _G20_TARGET, analysts_by_id,
    )
    assert "escalation" in matched


def test_both_compositions_and_their_sibling_unit_side_by_side():
    """The exact production mix on one country target: seven signal-consuming
    units register, the composition that reads their heads does not."""
    analysts_by_id = _analysts_by_id(
        "country_composition", "escalation", "leadership_transition",
    )
    matched = set(
        source_first_runtime._analyst_ids_for_target(_G20_TARGET, analysts_by_id)
    )
    assert matched == {"escalation", "leadership_transition"}


# ---------------------------------------------------------------------------
# 2. The boundary logic — synthetic data_types cases
# ---------------------------------------------------------------------------


def _synthetic_analysts_by_id(data_types: list[str] | None) -> dict[str, dict[str, Any]]:
    targets: dict[str, Any] = {"predicate": 'has_tag("g20")'}
    if data_types is not None:
        targets["data_types"] = data_types
    return {
        "synthetic_analyst": {
            "body": {"subscription": {"targets": targets}},
        }
    }


@pytest.mark.parametrize(
    "data_types,expect_registered",
    [
        (["signal"], True),
        (["finding"], False),
        (["signal", "finding"], True),  # signal present ⇒ still a legitimate wake
        ([], True),  # undeclared — conservative default, unchanged behaviour
        (None, True),  # field omitted entirely — same conservative default
    ],
)
def test_data_types_boundary(data_types, expect_registered):
    matched = source_first_runtime._analyst_ids_for_target(
        _G20_TARGET, _synthetic_analysts_by_id(data_types),
    )
    assert ("synthetic_analyst" in matched) == expect_registered


# ---------------------------------------------------------------------------
# 3. The cadence-ordering invariant this fix makes load-bearing
# ---------------------------------------------------------------------------

#: The eight bounded units + the two per-target compositions this train
#: closes the race for. Real descriptor ids, not a hand-picked subset.
_UNIT_IDS: tuple[str, ...] = (
    "leadership_transition",
    "internal_stability",
    "proliferation_watch",
    "energy_security",
    "military_posture",
    "escalation",
    "economic_coercion",
    "narrative_coordination",
)

_CRON_RE = re.compile(r"^(\S+)\s+(\S+)\s+\*\s+\*\s+\*$")


def _cron_hour_minutes(schedule: str) -> tuple[tuple[int, int], ...]:
    """The ``(hour, minute)`` pairs of a ``"M H,H * * *"`` cron string, sorted.

    Minute-precise (not hour-only): ``region_composition`` (``45 11,23``)
    and ``country_composition`` (``30 11,23``) share an HOUR, so an
    hour-only comparison cannot see that region's tick is still ordered
    after country's within it.
    """
    m = _CRON_RE.match(schedule.strip())
    assert m, f"unexpected cron shape: {schedule!r}"
    minute = int(m.group(1))
    return tuple(sorted((int(h), minute) for h in m.group(2).split(",")))


def test_cron_hour_minutes_parses_the_house_am_pm_shape():
    assert _cron_hour_minutes("0 1,13 * * *") == ((1, 0), (13, 0))
    assert _cron_hour_minutes("30 11,23 * * *") == ((11, 30), (23, 30))


@pytest.mark.parametrize(
    "composition_id,unit_ids",
    [
        ("country_composition", _UNIT_IDS),
        # region_composition's own "unit" is country_composition's head — the
        # SAME ordering obligation one floor up the tower.
        ("region_composition", ("country_composition",)),
    ],
)
def test_composition_tick_lands_after_every_source_units_slot(
    composition_id: str, unit_ids: tuple[str, ...],
):
    """The invariant the descriptor's own cadence comment states in prose —
    now pinned as a fact about the shipped cron strings. Proven separately for
    the AM cycle (hour < 12) and the PM cycle (hour >= 12): the composition's
    AM tick must be strictly after every source's AM (hour, minute), and
    likewise for PM — the exact ordering the reactive trigger (now removed
    from this path) used to violate."""
    comp_am, comp_pm = _cron_hour_minutes(
        _load(composition_id).cadence.fallback_schedule
    )
    assert comp_am[0] < 12 <= comp_pm[0], (composition_id, comp_am, comp_pm)

    for unit_id in unit_ids:
        unit_am, unit_pm = _cron_hour_minutes(
            _load(unit_id).cadence.fallback_schedule
        )
        assert unit_am < comp_am, (
            f"{composition_id}'s AM tick ({comp_am}) does not land after "
            f"{unit_id}'s AM fire ({unit_am}) — the composition would "
            "compose a stale/partial cycle for this source"
        )
        assert unit_pm < comp_pm, (
            f"{composition_id}'s PM tick ({comp_pm}) does not land after "
            f"{unit_id}'s PM fire ({unit_pm})"
        )
