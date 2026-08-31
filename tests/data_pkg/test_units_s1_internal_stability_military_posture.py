# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S1-T4 + S1-T5 — the two new PMESII bounded units + their HIDDEN wiring.

internal_stability (S1-T4, unrest/repression/coup-risk, coup-vulnerability lens,
absence-honest) and military_posture (S1-T5, capability/deployment/exercise/
mobilization/procurement posture — NOT event escalation). Each is a bounded
inline_target UNIT (the validated T1 unit-factory: NO new Python kind). This
asserts:

  1. both descriptors validate against the REAL AnalystDescriptor schema (via the
     exact bringup ``_load`` path) and pass the P2-T7 unit drift guard
     (inline_target + inline system_prompt + eval.rubric + method.llm.verify);
  2. both are wired into scripts/bringup ANALYST_FILES so bringup registers them;
  3. the bounded-question contract: exactly-one-severity guidance + [N] citation
     instruction + the topic tag + grounding block + a FRESH staggered cadence
     (disjoint from the taken slots) with cooldown BELOW the fire interval;
  4. the two HIDDEN wirings without which a unit is INVISIBLE in the product —
     (a) scorecard_banding.DIMENSIONS keys the fixed dimension on analyst_id, so
         both new analyst_ids MUST be there (+ the sync-linked producer lists);
     (b) country_composition.other_analysts fuses them — the descriptor now lists
         all SIX units AND the kind fuses 6 sub-claims end-to-end;
  5. the escalation prompt carries the one-line boundary note so escalation and
     military_posture do not double-count the same wire signal.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
from uuid import UUID, uuid4

import pytest
import yaml

from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.analysts.deterministic_handlers import scorecard_banding as sb
from legba.data.analysts.deterministic_handlers import unit_correctness_scorer as ucs
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.runtime.substrate_query_port import _ASSESSMENT_PRODUCER_ANALYSTS

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DESCRIPTORS_DIR = _ROOT / "descriptors"

# (file, expected id) for the two new units.
_NEW_UNITS = {
    "analyst_internal_stability.yaml": "internal_stability",
    "analyst_military_posture.yaml": "military_posture",
}

# The fire interval between the two staggered daily slots (12h).
_FIRE_INTERVAL_S = 12 * 3600
# Hour slots already TAKEN by the existing four units + the composition compose.
_TAKEN_UNIT_HOURS = {1, 13, 4, 16, 7, 19, 10, 22}
_COMPOSE_HOURS = {11, 23}


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
    """The hour field of a 5-field cron (``m h dom mon dow``) as an int set."""
    fields = schedule.split()
    assert len(fields) == 5, schedule
    return {int(h) for h in fields[1].split(",")}


# ---------------------------------------------------------------------------
# 1. Both descriptors validate + pass the bringup drift guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,exp_id", sorted(_NEW_UNITS.items()))
def test_new_unit_descriptor_validates(name: str, exp_id: str):
    desc = _load(name)
    assert desc.identity.id == exp_id
    assert desc.identity.kind == "inline_target"
    assert desc.identity.state == "active"


@pytest.mark.parametrize("name", sorted(_NEW_UNITS))
def test_new_unit_passes_drift_guard(name: str):
    """inline_target + inline system_prompt ⇒ a bounded UNIT that MUST carry both
    eval.rubric and method.llm.verify (else the critic hard-fails / verify falls
    back to the floor). The exact live bringup guard must accept both."""
    mod = _bringup_module()
    body = mod._load_body(name)
    assert mod._is_bounded_unit(body) is True
    has_rubric, has_verify = mod._unit_eval_coverage(body)
    assert has_rubric and has_verify
    # No raise == clean.
    mod._assert_unit_eval_coverage([(name, body)])


# ---------------------------------------------------------------------------
# 2. Both wired into bringup ANALYST_FILES
# ---------------------------------------------------------------------------


def test_new_units_in_bringup_set():
    mod = _bringup_module()
    for name in _NEW_UNITS:
        assert name in mod.ANALYST_FILES, f"{name} missing from bringup ANALYST_FILES"


# ---------------------------------------------------------------------------
# 3. The bounded-question contract + grounding + cadence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,exp_id", sorted(_NEW_UNITS.items()))
def test_prompt_contract_severity_and_citation(name: str, exp_id: str):
    prompt = _raw_body(name)["method"]["system_prompt"]
    # EXACTLY ONE severity tag guidance + the five valid levels.
    assert "EXACTLY ONE severity" in prompt
    for level in ("severity:low", "severity:moderate", "severity:elevated",
                  "severity:high", "severity:critical"):
        assert level in prompt
    # The topic tag for THIS unit.
    assert f"topic:{exp_id}" in prompt
    # Cite to the signal slice index with [N].
    assert "[N]" in prompt
    # STRICT JSON, single finding.
    assert "STRICT JSON" in prompt
    assert "EXACTLY ONE FINDING" in prompt


@pytest.mark.parametrize("name", sorted(_NEW_UNITS))
def test_grounding_block_on_and_scoped(name: str):
    g = _raw_body(name)["grounding"]
    assert g["enabled"] is True
    assert g["scope"] == ["target_geo", "slice_entities"]
    # The base substrate sources are ALWAYS present. internal_stability
    # additionally carries `vector:world_context` — the opportunistic-RAG flip that
    # was KEPT (it passed the rag_watch rule where leadership_transition was rolled
    # back, 2026-07-03). So assert the base set is a SUBSET, not an exact match,
    # and never forbid a kept vector source.
    #
    # REGISTER-1g (2026-08-29) — `situations` LEFT THIS SET, deliberately. Both
    # units are `kind: inline_target`, so `actor_substrate_slice` already hands
    # them the desk's OPEN SITUATION REGISTER as a citable [N] block with the
    # full H1 render repair (evidence age, NEVER/STALE labels, the
    # self-corroboration rule). The `situations` source produced a SECOND,
    # UNGUARDED copy of the same frames in the same context window. The register
    # is still grounded here — by the kind, not by this line — which is why the
    # assertion below is a REPLACEMENT and not a deletion.
    assert {"substrate", "graph_structure"} <= set(g["sources"])
    assert "situations" not in g["sources"], (
        "REGISTER-1g: an inline_target desk already receives the guarded "
        "unit_grounding register; re-declaring `situations` here puts an "
        "unguarded second copy back into the prompt"
    )


@pytest.mark.parametrize("name", sorted(_NEW_UNITS))
def test_broad_units_run_on_blanket_predicate(name: str):
    """Both are BROAD units (every desk), so a FIXED scorecard dimension is safe."""
    targets = _raw_body(name)["subscription"]["targets"]
    assert targets["predicate"] == 'has_tag("g20") or has_tag("watch")'
    assert targets["data_types"] == ["signal"]


@pytest.mark.parametrize("name", sorted(_NEW_UNITS))
def test_verify_declared_on_the_stack_ref_core_plane(name: str):
    """method.llm.verify present + a switchable stack_ref (never a hardcoded id)."""
    llm = _raw_body(name)["method"]["llm"]
    assert llm["verify"]["factory_kind"] == "stack_ref"
    assert llm["primary"]["factory_kind"] == "stack_ref"


def test_cadence_slots_are_fresh_staggered_and_disjoint():
    """The two new units take FRESH hour slots that collide with neither the taken
    unit fires (01/13, 04/16, 07/19, 10/22), the compose (11/23), nor each other;
    each fires exactly 2×/day and holds a cooldown BELOW the 12h fire interval."""
    hours_by_unit: dict[str, set[int]] = {}
    for name in _NEW_UNITS:
        cad = _raw_body(name)["cadence"]
        hours = _cron_hours(cad["fallback_schedule"])
        assert len(hours) == 2, f"{name} must fire 2x/day"
        # Disjoint from the already-taken unit + compose slots.
        assert hours.isdisjoint(_TAKEN_UNIT_HOURS), f"{name} collides with a taken slot"
        assert hours.isdisjoint(_COMPOSE_HOURS), f"{name} collides with the compose slot"
        # Cooldown BELOW the fire interval (a cooldown == interval halves cadence).
        assert cad["cooldown_seconds"] < _FIRE_INTERVAL_S
        hours_by_unit[name] = hours
    # The two new units do not collide with EACH OTHER either.
    a, b = hours_by_unit.values()
    assert a.isdisjoint(b)


# ---------------------------------------------------------------------------
# 4a. HIDDEN wiring — scorecard banding dimensions + the sync-linked lists
# ---------------------------------------------------------------------------


def test_scorecard_dimensions_include_the_two_new_units():
    assert "internal_stability" in sb.DIMENSIONS
    assert "military_posture" in sb.DIMENSIONS
    # The original four are still there.
    for original in ("leadership_transition", "energy_security", "escalation",
                     "narrative_coordination"):
        assert original in sb.DIMENSIONS
    # >= 6: economic_coercion (S1-T7) and any future PMESII unit extend this.
    assert len(sb.DIMENSIONS) >= 6


def test_banding_reports_the_two_new_dimensions_end_to_end():
    """band_target reports EVERY dimension always — the two new ones read honest
    insufficient (no-finding) when nothing fired, with an empty explicit basis."""
    verdict = sb.band_target("target:usa", {})
    for unit in ("internal_stability", "military_posture"):
        dim = verdict["dimensions"][unit]
        assert dim["band"] == sb.INSUFFICIENT
        assert dim["basis"] == []
        assert dim["reason"] == "no-finding"


def test_a_banded_new_dimension_names_its_basis():
    fid = str(uuid4())
    claim = sb.Claim(finding_id=fid, analyst_id="military_posture",
                     confidence=0.9, faithfulness_score=0.9,
                     tags=("severity:elevated",))
    verdict = sb.band_target("target:usa", {"military_posture": claim})
    dim = verdict["dimensions"]["military_posture"]
    assert dim["band"] == "elevated"
    assert dim["basis"] == [fid]


def test_producer_and_scorer_lists_stay_in_sync_with_dimensions():
    """The codebase documents keeping the unit set in sync across the banding
    DIMENSIONS, the correctness-scorer defaults, and the assessment-producer
    surface. The two new units join all three."""
    for unit in ("internal_stability", "military_posture"):
        assert unit in ucs._DEFAULT_UNITS
        assert unit in _ASSESSMENT_PRODUCER_ANALYSTS
    # The unit half of the two lists equals DIMENSIONS.
    assert set(ucs._DEFAULT_UNITS) == set(sb.DIMENSIONS)
    assert set(sb.DIMENSIONS) <= set(_ASSESSMENT_PRODUCER_ANALYSTS)


# ---------------------------------------------------------------------------
# 4b. HIDDEN wiring — country_composition fuses SIX units
# ---------------------------------------------------------------------------


def test_country_composition_declares_all_six_units():
    desc = _load("analyst_country_composition.yaml")
    ids = [a.id for a in desc.subscription.other_analysts]
    # The six S1-era units are the first six declared, in order. Later PMESII
    # units (economic_coercion, S1-T7) append after, so assert the prefix rather
    # than exact equality.
    assert ids[:6] == [
        "leadership_transition",
        "energy_security",
        "escalation",
        "narrative_coordination",
        "internal_stability",
        "military_posture",
    ]
    # Per-country: the descriptor carries a targets block (fans out per desk).
    assert desc.subscription.targets is not None


# --- a fixture composition run that FUSES six unit sub-claims ----------------


def _subclaim_row(*, analyst_id: str, uid: UUID, title: str) -> dict:
    """A row shaped like a verify-floored ``read_other_analyst_findings`` result
    (mirrors tests/data_pkg/test_meta_findings_composition.py::_subclaim_row)."""
    return {
        "id": uid,
        "kind": "finding",
        "title": title,
        "body": f"{title} body",
        "confidence": 0.7,
        "effective_confidence": 0.6,
        "faithfulness_score": 0.9,
        "severity": None,
        "data": {"evidence": []},
        "evidence": [],
        "target_id": "country_g20_in",
        "target_version": None,
        "analyst_id": analyst_id,
        "analyst_version": "vtest",
        "produced_at": "2026-06-30T00:00:00+00:00",
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }


class _CannedLLM:
    subprovider = "s1_composition_double"

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[dict] = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kwargs):
        self.calls.append({"messages": list(messages), "system": system})

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50
            reasoning_tokens = 0

        class _Response:
            pass

        resp = _Response()
        resp.content = json.dumps(self._payload)
        resp.usage = _Usage()
        return resp


class _Deps:
    def __init__(self, llm) -> None:
        self.llm = llm


@pytest.mark.asyncio
async def test_fixture_composition_fuses_six_units():
    """The per-country composition kind fuses SIX unit sub-claims (the two new
    units alongside the original four) into ONE read whose derived_from lineage
    back-walks to all six contributing unit findings."""
    unit_ids = [
        "leadership_transition",
        "energy_security",
        "escalation",
        "narrative_coordination",
        "internal_stability",
        "military_posture",
    ]
    uids = {u: uuid4() for u in unit_ids}
    rows = [
        _subclaim_row(analyst_id=u, uid=uids[u], title=u.replace("_", " ").title())
        for u in unit_ids
    ]
    # Cite every rendered ordinal 1..6 so all six sub-claims are fused + cited.
    body = "BLUF: six units synthesized. " + " ".join(
        f"Point {i} [[ref:{i}]]." for i in range(1, 7)
    )
    llm = _CannedLLM({
        "title": "India composition (six units)",
        "body": body,
        "confidence": 0.55,
        "evidence": ["six units fused"],
        "tags": ["composition"],
    })
    result = await synth.run_method(
        list(rows),
        {"analyst_id": "country_composition", "target_id": "country_g20_in",
         "run_id": uuid4()},
        _Deps(llm),
    )

    # Prompt: the per-country composition system prompt (not the global one).
    assert llm.calls[-1]["system"] == synth._COMPOSITION_SYSTEM
    # Lineage: derived_from back-walks to ALL SIX contributing unit findings.
    assert set(result.derived_from) == set(uids.values())
    assert len(result.derived_from) == 6
    # Citations: all six ordinals resolved to their unit finding ids.
    cited_ids = {c["ref_id"] for c in result.finding.data["citations"]}
    assert cited_ids == {str(u) for u in uids.values()}


# ---------------------------------------------------------------------------
# 5. The escalation prompt carries the military_posture boundary note
# ---------------------------------------------------------------------------


def test_escalation_prompt_draws_the_line_against_military_posture():
    prompt = _raw_body("analyst_escalation.yaml")["method"]["system_prompt"]
    assert "military_posture" in prompt
    # The one-line boundary explicitly names the anti-double-count rule.
    assert re.search(r"double-count", prompt, re.IGNORECASE)
