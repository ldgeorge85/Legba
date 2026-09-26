# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""F-3 — the desk's BOUNDED QUESTION as an explicit descriptor field.

``planning/DEMOTION_D1_SPEC_2026-09-04.md`` §7 F-3: the assembly's block
header is ruled to be "the desk's bounded question"
(``data.data.assembly.blocks[].question``, §1.2) — and no descriptor field
held one. The question lived only, implicitly, inside each bounded unit's own
``method.system_prompt``, under its own "BOUNDED QUESTION —" heading. This
train adds ``method.bounded_question``: one interrogative sentence,
quote-derived from that prose, authored on each of the nine bounded units so
a future assembler can render the block header without re-parsing prose.

The three composition tiers named by the assembly's own tier enum
(§1.2, ``data.data.assembly.tier: country | world | thematic`` —
``country_composition``, ``world_assessor``, ``escalation_composition``)
deliberately declare NO value: each synthesizes across several bounded
questions and does not answer one itself. That is not a gap this train left
behind — the spec's own ruling for F-3 is that the (separate, later) D-2
assembler falls back to the desk display name (``identity.name``) and stamps
``question_source: "fallback_desk_name"`` when the field is absent. D-2
itself is out of scope here (sequenced with, not inside, D-2); this file
proves only the descriptor-side contract.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from legba.data.registry.descriptor import Family
from legba.data.schemas.analyst import AnalystDescriptor, MethodBlock

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTORS = REPO_ROOT / "descriptors"

#: The nine bounded units — the VOICE fleet. Listed as a literal tuple rather
#: than imported from test_voice4_flip_kit's ALL_DESKS: this file's claim
#: (the field exists, fleet-wide) is independent of the VOICE-4 flip's own
#: claim (which eight got new prose), and should not break if that set ever
#: changes shape.
UNITS: tuple[str, ...] = (
    "escalation",
    "energy_security",
    "economic_coercion",
    "internal_stability",
    "leadership_transition",
    "military_posture",
    "narrative_coordination",
    "proliferation_watch",
    "disruption_status",
)

#: The three composition tiers — the assembly's own tier enum, §1.2.
#: NOT ``region_composition``: that analyst is not one of the three named
#: tiers and is slated for retirement under D-5/F-8, outside this train.
COMPOSITION_TIERS: tuple[str, ...] = (
    "country_composition",
    "world_assessor",
    "escalation_composition",
)

#: A short topic anchor per unit: lowercase substrings that must appear, case-
#: insensitively, in BOTH the extracted ``bounded_question`` and the unit's
#: own ``system_prompt`` — the cheap, deterministic half of "quote-derive, do
#: not invent scope". Not a full word-coverage check (hyphenation/casing
#: makes that fragile); a topic-anchor floor that a fabricated question could
#: not accidentally pass.
UNIT_TOPIC_ANCHORS: dict[str, tuple[str, ...]] = {
    "escalation": ("escalation",),
    "energy_security": ("energy-security",),
    "economic_coercion": ("coercive economic",),
    "internal_stability": ("internal political stability",),
    "leadership_transition": ("top leadership",),
    "military_posture": ("military posture",),
    "narrative_coordination": ("coordinated narrative",),
    "proliferation_watch": ("nuclear", "wmd"),
    "disruption_status": ("physical flow", "degrading, holding, or recovering"),
}


def _doc(desk: str) -> dict:
    return yaml.safe_load((DESCRIPTORS / f"analyst_{desk}.yaml").read_text())


def _descriptor(desk: str) -> AnalystDescriptor:
    """The real binding path: ``Family.ANALYST.model`` is the class
    ``registry.api._parse_descriptor`` resolves and
    ``model_validate(..., strict=False)`` is the call it makes on a PUT body
    (same idiom as ``test_voice4_flip_kit.test_descriptor_validates_as_an_analyst_descriptor``).
    """
    return Family.ANALYST.model.model_validate(_doc(desk), strict=False)


# ---------------------------------------------------------------------------
# The schema: optional, so an undecorated descriptor stays valid
# ---------------------------------------------------------------------------


def test_method_block_defaults_bounded_question_to_none() -> None:
    m = MethodBlock(kind="llm_single_turn", prompt_module="path:to:module")
    assert m.bounded_question is None


def test_method_block_accepts_an_explicit_bounded_question() -> None:
    m = MethodBlock(
        kind="llm_single_turn",
        prompt_module="path:to:module",
        bounded_question="Is this a real question?",
    )
    assert m.bounded_question == "Is this a real question?"


# ---------------------------------------------------------------------------
# Present, one interrogative sentence, faithful to the prompt — all nine units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", UNITS)
def test_bounded_question_present_on_every_unit(unit: str) -> None:
    descriptor = _descriptor(unit)
    q = descriptor.method.bounded_question
    assert isinstance(q, str) and q.strip(), f"{unit}: bounded_question missing/empty"


@pytest.mark.parametrize("unit", UNITS)
def test_bounded_question_is_declared_at_method_path_in_the_tree(unit: str) -> None:
    """The spec's defined path (§1.2's ``blocks[].question`` is populated FROM
    this): ``method.bounded_question`` in the tree YAML itself, not merely a
    pydantic default."""
    body = _doc(unit)
    assert "bounded_question" in body["method"], f"{unit}: not declared in tree"
    assert isinstance(body["method"]["bounded_question"], str)


@pytest.mark.parametrize("unit", UNITS)
def test_bounded_question_is_one_interrogative_sentence(unit: str) -> None:
    """Interrogative (ends '?') and ONE sentence (no earlier sentence-ending
    punctuation) — the multi-sentence "BOUNDED QUESTION —" prose is condensed
    to a single question for the block header, per the task's own contract."""
    q = _descriptor(unit).method.bounded_question
    assert q.endswith("?"), f"{unit}: not interrogative: {q!r}"
    body_text = q[:-1]
    for terminator in (". ", "? ", "! "):
        assert terminator not in body_text, f"{unit}: more than one sentence: {q!r}"


@pytest.mark.parametrize("unit", UNITS)
def test_bounded_question_is_quote_derived_not_invented(unit: str) -> None:
    """Faithfulness floor: the unit's topic anchor(s) must appear, case-
    insensitively, in BOTH the descriptor field and the unit's own
    system_prompt — the field cannot name a topic the prompt never granted."""
    descriptor = _descriptor(unit)
    q = descriptor.method.bounded_question.lower()
    prompt = descriptor.method.system_prompt.lower()
    for anchor in UNIT_TOPIC_ANCHORS[unit]:
        assert anchor in prompt, f"{unit}: anchor {anchor!r} not even in system_prompt (fixture bug)"
        assert anchor in q, f"{unit}: bounded_question lacks its own topic anchor {anchor!r}: {q!r}"


@pytest.mark.parametrize("unit", UNITS)
def test_bounded_question_names_the_unit_scope_not_another_desks(unit: str) -> None:
    """Cross-check: no OTHER unit's topic anchor leaks into this one's
    question — the boundary the prompts themselves police ("Do NOT assess
    ... those belong to other units")."""
    q = _descriptor(unit).method.bounded_question.lower()
    for other, anchors in UNIT_TOPIC_ANCHORS.items():
        if other == unit:
            continue
        for anchor in anchors:
            # Some anchors are generic enough to legitimately co-occur
            # (none currently do); this stays a real cross-desk guard because
            # every anchor above is unit-specific vocabulary.
            assert anchor not in q, (
                f"{unit}: question carries {other}'s anchor {anchor!r}: {q!r}"
            )


# ---------------------------------------------------------------------------
# Honestly absent on the three composition tiers, with a workable fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", COMPOSITION_TIERS)
def test_bounded_question_not_declared_on_composition_tiers(tier: str) -> None:
    """The tree itself never sets the key — not even to an explicit null —
    for a composition tier: the absence is a deliberate non-declaration, not
    a value that happens to be None."""
    body = _doc(tier)
    assert "bounded_question" not in body["method"], (
        f"{tier}: composition tiers answer many bounded questions, not one"
    )


@pytest.mark.parametrize("tier", COMPOSITION_TIERS)
def test_bounded_question_validates_as_none_on_composition_tiers(tier: str) -> None:
    """Same real binding path as the unit test above: an undecorated
    descriptor still validates — the optional-with-None contract (task item
    3) — through the exact class the registry PUT uses."""
    descriptor = _descriptor(tier)
    assert descriptor.method.bounded_question is None


@pytest.mark.parametrize("tier", COMPOSITION_TIERS)
def test_the_honest_fallback_per_d2s_contract(tier: str) -> None:
    """D-2's ruled fallback (§7 F-3): when ``bounded_question`` is absent,
    render the desk display name instead. D-2 (the renderer) is a separate,
    later train; this proves the descriptor-side half of the contract is
    satisfiable — the exact expression a renderer would use resolves to a
    real, non-empty string rather than blanking the block header."""
    descriptor = _descriptor(tier)
    question_source = "descriptor" if descriptor.method.bounded_question else "fallback_desk_name"
    rendered = descriptor.method.bounded_question or descriptor.identity.name
    assert question_source == "fallback_desk_name"
    assert isinstance(rendered, str) and rendered.strip()
    assert rendered == descriptor.identity.name


@pytest.mark.parametrize("unit", UNITS)
def test_the_honest_fallback_prefers_the_declared_question(unit: str) -> None:
    """The other half: when present, the fallback expression must pick the
    declared question, never silently prefer the display name."""
    descriptor = _descriptor(unit)
    question_source = "descriptor" if descriptor.method.bounded_question else "fallback_desk_name"
    rendered = descriptor.method.bounded_question or descriptor.identity.name
    assert question_source == "descriptor"
    assert rendered == descriptor.method.bounded_question
    assert rendered != descriptor.identity.name


# ---------------------------------------------------------------------------
# Round-trip: YAML and the pydantic model both preserve the value unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("desk", UNITS + COMPOSITION_TIERS)
def test_bounded_question_survives_a_yaml_round_trip(desk: str) -> None:
    value = _doc(desk)["method"].get("bounded_question")
    assert yaml.safe_load(yaml.safe_dump({"q": value}))["q"] == value


@pytest.mark.parametrize("desk", UNITS + COMPOSITION_TIERS)
def test_bounded_question_survives_a_pydantic_dump_and_reload(desk: str) -> None:
    """``model_dump(mode="json")`` is what the registry writes back to the DB
    (``registry/descriptor.py``); reloading it must reproduce the same value,
    present or None, byte for byte."""
    descriptor = _descriptor(desk)
    dumped = descriptor.model_dump(mode="json", by_alias=True)
    assert dumped["method"]["bounded_question"] == descriptor.method.bounded_question
    reloaded = Family.ANALYST.model.model_validate(dumped, strict=False)
    assert reloaded.method.bounded_question == descriptor.method.bounded_question


def test_units_and_composition_tiers_are_disjoint_and_match_the_spec_counts() -> None:
    """Fixture sanity: nine units (VOICE fleet), three composition tiers
    (the assembly's tier enum), zero overlap."""
    assert len(UNITS) == 9
    assert len(COMPOSITION_TIERS) == 3
    assert set(UNITS).isdisjoint(COMPOSITION_TIERS)
    assert set(UNIT_TOPIC_ANCHORS) == set(UNITS)
