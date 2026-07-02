# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2-T2 — the 4 bounded-reasoning UNIT descriptors validate + are wired.

A glass-tower "unit" (T1 unit-factory pattern) is JUST an inline_target
DESCRIPTOR — its OWN scope predicate (subscription.targets.predicate) + its OWN
``method.system_prompt`` + its OWN ``eval.rubric``, with NO new Python kind
module and NO ``_NEW_ANALYST_KINDS`` entry. These tests gate exactly that
contract for the four P2-T2 units:

  * analyst_leadership_transition.yaml
  * analyst_energy_security.yaml
  * analyst_escalation.yaml
  * analyst_narrative_coordination.yaml

Each must:
  1. validate against the REAL AnalystDescriptor pydantic schema (via the exact
     bringup ``_load`` path — the gate the registry runs);
  2. be present in scripts/bringup_register_analysts.ANALYST_FILES so it
     registers on bringup (without this it registers against 0 rows);
  3. honour the unit contract: kind inline_target; a non-empty inline
     system_prompt; method.llm.primary → llm.primary.openai_compat; an
     eval.rubric; a g20-scoped subscription predicate; and a set (non-null)
     fallback_schedule whose cooldown_seconds is BELOW the cron interval (the
     cooldown==interval cadence-halving trap).
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from legba.data.schemas.analyst import AnalystDescriptor

_DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parents[2] / "descriptors"

# The 4 P2-T2 units: file -> (expected identity.id).
_UNIT_FILES = {
    "analyst_leadership_transition.yaml": "leadership_transition",
    "analyst_energy_security.yaml": "energy_security",
    "analyst_escalation.yaml": "escalation",
    "analyst_narrative_coordination.yaml": "narrative_coordination",
}


def _load(name: str) -> AnalystDescriptor:
    """Exact mirror of scripts/bringup_register_analysts._load."""
    body = yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return AnalystDescriptor.model_validate(body, strict=False)


# ---------------------------------------------------------------------------
# Cron interval helper — derive the smallest gap between fires (seconds).
# ---------------------------------------------------------------------------


def _expand_cron_field(field: str, lo: int, hi: int) -> list[int]:
    """Expand one cron field to its sorted set of integer values.

    Handles ``*``, comma lists, ``a-b`` ranges, ``*/n`` and ``a-b/n`` steps —
    enough to cover every schedule shape used by the analyst descriptors.
    """
    values: set[int] = set()
    for part in field.split(","):
        if part == "*":
            values.update(range(lo, hi + 1))
        elif part.startswith("*/"):
            values.update(range(lo, hi + 1, int(part[2:])))
        elif "/" in part:
            rng, step_s = part.split("/")
            step = int(step_s)
            if "-" in rng:
                a, b = rng.split("-")
                values.update(range(int(a), int(b) + 1, step))
            else:
                values.update(range(int(rng), hi + 1, step))
        elif "-" in part:
            a, b = part.split("-")
            values.update(range(int(a), int(b) + 1))
        else:
            values.add(int(part))
    return sorted(v for v in values if lo <= v <= hi)


def _cron_min_interval_seconds(schedule: str) -> int:
    """Smallest interval (seconds) between consecutive fires of a 5-field cron.

    Considers the minute + hour fields and wraps day boundaries, so a staggered
    ``0 1,13 * * *`` yields 12h and ``0 */6 * * *`` yields 6h. A schedule that
    fires at most once/day returns a full day.
    """
    minute_f, hour_f = schedule.split()[:2]
    minutes = _expand_cron_field(minute_f, 0, 59)
    hours = _expand_cron_field(hour_f, 0, 23)
    fires = sorted(h * 60 + m for h in hours for m in minutes)  # minutes-of-day
    if len(fires) < 2:
        return 24 * 3600
    diffs = [fires[i + 1] - fires[i] for i in range(len(fires) - 1)]
    diffs.append(fires[0] + 24 * 60 - fires[-1])  # wrap to next day's first fire
    return min(diffs) * 60


def test_cron_helper_sanity():
    """Guard the helper itself so a unit assertion can't pass on a broken parse."""
    assert _cron_min_interval_seconds("0 1,13 * * *") == 12 * 3600
    assert _cron_min_interval_seconds("0 */6 * * *") == 6 * 3600
    assert _cron_min_interval_seconds("0 7,19 * * *") == 12 * 3600


@pytest.mark.parametrize("name,exp_id", sorted(_UNIT_FILES.items()))
def test_unit_descriptor_contract(name: str, exp_id: str):
    """Each P2-T2 unit validates and honours the bounded-unit contract."""
    desc = _load(name)

    # Identity: the right unit, and a built-in inline_target kind (no new kind).
    assert desc.identity.id == exp_id
    assert desc.identity.kind == "inline_target"

    # The unit carries its OWN inline prompt (NOT a prompt_module).
    assert isinstance(desc.method.system_prompt, str)
    assert desc.method.system_prompt.strip(), "system_prompt must be non-empty"

    # method.llm.primary resolves to the core OpenAI-compat plane.
    primary = desc.method.llm.get("primary")
    assert isinstance(primary, dict)
    assert primary.get("raw") == "llm.primary.openai_compat"

    # eval.rubric present (the critic hard-fails without it).
    assert desc.eval is not None
    assert desc.eval.rubric and desc.eval.rubric.strip()

    # The subscription predicate scopes to the g20 fan-out set.
    assert desc.subscription.targets is not None
    predicate = desc.subscription.targets.predicate or ""
    assert "g20" in predicate
    assert "has_tag" in predicate

    # Cadence is set (NOT null) and the cooldown sits BELOW the cron interval
    # (a cooldown == interval silently halves each target's cadence).
    assert desc.cadence.fallback_schedule, "fallback_schedule must be set"
    interval = _cron_min_interval_seconds(desc.cadence.fallback_schedule)
    assert desc.cadence.cooldown_seconds < interval, (
        f"{name}: cooldown {desc.cadence.cooldown_seconds}s must be < the "
        f"{interval}s cron interval (cadence-halving trap)"
    )


def test_units_in_bringup_set():
    """The 4 units must be in ANALYST_FILES so bringup registers them (the
    predictor precedent — without this they register against 0 rows)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_bringup_register_analysts",
        pathlib.Path(__file__).resolve().parents[2]
        / "scripts"
        / "bringup_register_analysts.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in _UNIT_FILES:
        assert name in mod.ANALYST_FILES, f"{name} missing from bringup ANALYST_FILES"
