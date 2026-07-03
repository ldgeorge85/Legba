# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S1-T6 — the proliferation_watch bounded unit + its NARROW-unit discipline.

proliferation_watch (WMD-capability trajectory: enrichment/fissile material,
weaponization, delivery systems, testing, safeguards, proliferation networks) is
the first NARROW bounded unit — it fans out on ``has_tag("nuclear_watch")`` (the
8 nuclear-relevant desks) instead of the blanket ``g20+watch`` predicate the
broad units use. This is the concrete demonstration of the S1-T3(a) tag-scoped
fan-out pattern (the predicate engine already existed; this is the first
consumer that narrows it).

The distinguishing discipline vs the broad units (test_units_s1_internal_
stability_military_posture.py) is INVERTED here:

  * the predicate is the NARROW ``has_tag("nuclear_watch")`` (not the blanket);
  * the unit is deliberately KEPT OFF the FIXED scorecard_banding.DIMENSIONS
    tuple (+ the sync-linked unit_correctness_scorer._DEFAULT_UNITS) — a fixed
    dimension would render a misleading ``insufficient-evidence`` band on the 17
    non-nuclear desks (it does not APPLY there) and would break the
    ``DIMENSIONS == _DEFAULT_UNITS`` sync invariant;
  * it is surfaced in the product via country_composition.other_analysts instead
    (verify-floored INNER JOIN → naturally absent on non-nuclear desks);
  * the military_posture prompt now draws the boundary line against it.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
import yaml

from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.analysts.deterministic_handlers import scorecard_banding as sb
from legba.data.analysts.deterministic_handlers import unit_correctness_scorer as ucs

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DESCRIPTORS_DIR = _ROOT / "descriptors"

_UNIT_FILE = "analyst_proliferation_watch.yaml"
_UNIT_ID = "proliferation_watch"

# The fire interval between the two staggered daily slots (12h).
_FIRE_INTERVAL_S = 12 * 3600
# Hour slots TAKEN by the seven broad units (01/13 leadership, 02/14 internal,
# 04/16 energy, 05/17 military, 07/19 escalation, 09/21 economic, 10/22 narrative).
_TAKEN_UNIT_HOURS = {1, 13, 2, 14, 4, 16, 5, 17, 7, 19, 9, 21, 10, 22}
# The compose slots (:30 past 08/20 escalation-comp + 11/23 country-comp) and the
# world/00-12 run — proliferation_watch (03/15) must avoid all of them too.
_COMPOSE_HOURS = {8, 11, 20, 23, 0, 12}


def _load(name: str) -> AnalystDescriptor:
    """Exact mirror of scripts/bringup_register_analysts._load."""
    body = yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return AnalystDescriptor.model_validate(body, strict=False)


def _raw_body(name: str) -> dict:
    return yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())


def _bringup_module():
    spec = importlib.util.spec_from_file_location(
        "_bringup_register_analysts",
        _ROOT / "scripts" / "bringup_register_analysts.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cron_hours(schedule: str) -> set[int]:
    fields = schedule.split()
    assert len(fields) == 5, schedule
    return {int(h) for h in fields[1].split(",")}


# ---------------------------------------------------------------------------
# 1. Descriptor validates + passes the bringup unit drift guard
# ---------------------------------------------------------------------------


def test_descriptor_validates():
    desc = _load(_UNIT_FILE)
    assert desc.identity.id == _UNIT_ID
    assert desc.identity.kind == "inline_target"
    assert desc.identity.state == "active"


def test_passes_unit_drift_guard():
    """inline_target + inline system_prompt ⇒ a bounded UNIT that MUST carry both
    eval.rubric and method.llm.verify. The live bringup guard must accept both."""
    mod = _bringup_module()
    body = mod._load_body(_UNIT_FILE)
    assert mod._is_bounded_unit(body) is True
    has_rubric, has_verify = mod._unit_eval_coverage(body)
    assert has_rubric and has_verify
    mod._assert_unit_eval_coverage([(_UNIT_FILE, body)])


def test_in_bringup_set():
    mod = _bringup_module()
    assert _UNIT_FILE in mod.ANALYST_FILES


# ---------------------------------------------------------------------------
# 2. The NARROW predicate — the S1-T3(a) distinguishing assertion
# ---------------------------------------------------------------------------


def test_narrow_nuclear_watch_predicate():
    """The DISTINGUISHING test vs the broad units: proliferation_watch narrows its
    fan-out to the nuclear desks via has_tag("nuclear_watch"), NOT the blanket
    g20+watch predicate. This is the S1-T3(a) tag-scoped fan-out demonstrated."""
    targets = _raw_body(_UNIT_FILE)["subscription"]["targets"]
    assert targets["predicate"] == 'has_tag("nuclear_watch")'
    assert targets["data_types"] == ["signal"]


# ---------------------------------------------------------------------------
# 3. The bounded-question contract + grounding + cadence
# ---------------------------------------------------------------------------


def test_prompt_contract_severity_and_citation():
    prompt = _raw_body(_UNIT_FILE)["method"]["system_prompt"]
    assert "EXACTLY ONE severity" in prompt
    for level in ("severity:low", "severity:moderate", "severity:elevated",
                  "severity:high", "severity:critical"):
        assert level in prompt
    assert f"topic:{_UNIT_ID}" in prompt
    assert "[N]" in prompt
    assert "STRICT JSON" in prompt
    assert "EXACTLY ONE FINDING" in prompt


def test_grounding_block_on_scoped_and_rag_off():
    """Grounding on + target-scoped, and — RAG rolled back platform-wide (#176) —
    the sources carry NO vector:world_context (ships RAG-off like the others)."""
    g = _raw_body(_UNIT_FILE)["grounding"]
    assert g["enabled"] is True
    assert g["scope"] == ["target_geo", "slice_entities"]
    assert set(g["sources"]) == {"substrate", "situations", "graph_structure"}
    assert not any(str(s).startswith("vector:") for s in g["sources"])


def test_verify_declared_on_the_stack_ref_core_plane():
    llm = _raw_body(_UNIT_FILE)["method"]["llm"]
    assert llm["verify"]["factory_kind"] == "stack_ref"
    assert llm["primary"]["factory_kind"] == "stack_ref"


def test_cadence_is_fresh_staggered_slot():
    """proliferation_watch takes a FRESH hour slot (03/15) disjoint from every
    taken unit fire + compose slot, fires 2×/day, cooldown BELOW the 12h interval."""
    cad = _raw_body(_UNIT_FILE)["cadence"]
    hours = _cron_hours(cad["fallback_schedule"])
    assert len(hours) == 2, "must fire 2x/day"
    assert hours.isdisjoint(_TAKEN_UNIT_HOURS), "collides with a taken unit slot"
    assert hours.isdisjoint(_COMPOSE_HOURS), "collides with a compose/world slot"
    assert cad["cooldown_seconds"] < _FIRE_INTERVAL_S


# ---------------------------------------------------------------------------
# 4. NARROW-unit discipline — OFF the fixed scorecard, ON country_composition
# ---------------------------------------------------------------------------


def test_kept_off_the_fixed_scorecard_dimensions():
    """A NARROW unit must NOT join the FIXED scorecard tuple: it would mis-render
    insufficient-evidence on the 17 non-nuclear desks and break the sync invariant.
    The fixed set stays the 7 broad units."""
    assert _UNIT_ID not in sb.DIMENSIONS
    assert _UNIT_ID not in ucs._DEFAULT_UNITS
    # The fixed tuple is exactly the seven BROAD units — proliferation is excluded.
    assert len(sb.DIMENSIONS) == 7
    assert set(ucs._DEFAULT_UNITS) == set(sb.DIMENSIONS)


def test_surfaced_via_country_composition():
    """It IS surfaced in the product — as an other_analysts source on
    country_composition (safe: the READ_SLICE INNER JOIN is verify-floored, so a
    non-nuclear desk simply contributes zero rows)."""
    desc = _load("analyst_country_composition.yaml")
    ids = [a.id for a in desc.subscription.other_analysts]
    assert _UNIT_ID in ids


# ---------------------------------------------------------------------------
# 5. Boundary — military_posture draws the line against proliferation_watch
# ---------------------------------------------------------------------------


def test_military_posture_prompt_draws_the_line_against_proliferation():
    prompt = _raw_body("analyst_military_posture.yaml")["method"]["system_prompt"]
    assert "proliferation_watch" in prompt
    # The boundary hands WMD-capability specifics (enrichment/weaponization/
    # delivery/testing) to this unit while keeping declared doctrine as posture.
    assert "WMD-CAPABILITY" in prompt or "WMD capability" in prompt
