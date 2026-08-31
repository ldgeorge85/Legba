# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase-V VOICE — pin the prompt-side properties the diagnostic can measure.

VOICE_DIAGNOSTIC_2026-08-04 traced nine classes of prose defect to prompt
lines, not to the model. This file pins the repairs at the ONE layer a test can
reach: what the assembled prompt SAYS. It deliberately does NOT assert anything
about model OUTPUT — whether a finding actually opens with a dated as-of line,
whether the world read actually leads with the highest-stakes item, whether the
template echo actually fell from 42% — because a test that stubs a model and
then grades its own stub proves nothing. Those are OUTPUT properties, and they
are re-measured against the live corpus after deploy with the diagnostic's own
frequency queries (see the post-deploy measurement plan in the branch notes).

What IS pinnable, and pinned here:

  * the HOUSE READ CONTRACT is present, VERBATIM, in all nine unit
    descriptors — the anti-drift mechanism for a constant that has to be
    pasted rather than code-appended (see ``_tradecraft`` for why);
  * the banned template phrases appear in an assembled unit prompt ONLY inside
    the ban itself — i.e. no descriptor still ORDERS the sentence it bans;
  * the slice header carries the as-of coordinates, and the window comes from
    the descriptor rather than from prompt prose (the D8a drift class);
  * the composition prompts carry the new shape and keep the traceability rule
    that polices it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.analysts import inline_target as it
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts._tradecraft import (
    BANNED_PHRASE_MARKERS,
    COMPOSITION_BODY_SHAPE,
    CONSEQUENCE_RULE,
    NO_INSTRUMENT_READINGS,
    TITLE_NOT_THE_AS_OF_LINE,
    UNIT_BODY_SHAPE,
    UNIT_BODY_SHAPE_D6,
    UNIT_READ_CONTRACT,
    UNIT_READ_CONTRACT_D6,
    UNIT_VERDICT_RULE,
    with_preamble_if_absent,
)
from legba.data.analysts.unit_grounding import (
    UNIT_GROUNDING_CLAUSE,
    with_grounding_clause,
)
from legba.runtime.actor_substrate_slice import (
    DEFAULT_SLICE_WINDOW_HOURS,
    resolve_slice_window_hours,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTORS = REPO_ROOT / "descriptors"

#: The NINE bounded reasoning units Phase-V rewrites. ``disruption_status`` is
#: the ninth (DS-1): the wave filed it as a non-unit because it GATHERS, and it
#: went on ordering the banned template sentence in 14/15 findings after the
#: wave — the one desk where D2/D3/D4/D5 measurably never landed. Every
#: parametrized pin below now runs against it, which is what makes the
#: reclassification a property of the tree rather than a note in a plan.
UNITS: tuple[str, ...] = (
    "escalation",
    "energy_security",
    "economic_coercion",
    "internal_stability",
    "military_posture",
    "proliferation_watch",
    "leadership_transition",
    "narrative_coordination",
    "disruption_status",
)

#: D6 (2026-08-19 drafts, flipped 2026-08-20) — the units whose descriptors
#: carry the amended house contract, i.e. everything in :data:`UNITS` except the
#: one desk the VOICE-3 replay HELD.
#:
#: MA4 splices :data:`TITLE_NOT_THE_AS_OF_LINE` INTO the contract, so a flipped
#: descriptor no longer contains the pre-D6 contract as a substring and a held
#: one no longer contains the amended one. The contract's own first line claims
#: it is "identical on every desk" — while this split exists that claim is
#: TRUE OF EIGHT DESKS, and the split is pinned here rather than papered over so
#: the divergence cannot outlive the HOLD silently.
D6_FLIPPED: frozenset[str] = frozenset(UNITS) - {"narrative_coordination"}

#: The desk VOICE-3 held back: its replay could not catch the coordination
#: signal on the two positive windows. Its descriptor keeps the pre-D6 prompt
#: (and therefore the pre-D6 contract) until that condition is met.
D6_HELD: frozenset[str] = frozenset(UNITS) - D6_FLIPPED


def _expected_contract(unit: str) -> str:
    return UNIT_READ_CONTRACT_D6 if unit in D6_FLIPPED else UNIT_READ_CONTRACT


def _expected_body_shape(unit: str) -> str:
    return UNIT_BODY_SHAPE_D6 if unit in D6_FLIPPED else UNIT_BODY_SHAPE


#: Every composition prompt that must carry the new shape.
COMPOSITION_PROMPTS: tuple[tuple[str, str], ...] = (
    ("country", synth._COMPOSITION_SYSTEM),
    ("region", synth._REGION_COMPOSITION_SYSTEM),
    ("world", synth._WORLD_OVER_REGIONS_SYSTEM),
    ("thematic", synth._THEMATIC_COMPOSITION_SYSTEM),
)

#: Every composition prompt RANKS, and since VOICE-4 every one of them carries
#: the full shared ``CONSEQUENCE_RULE``. ``_COMPOSITION_SYSTEM`` used to be
#: excluded here: it held the rubric only in reduced form inside its own shape
#: rule, on the theory that a country desk ranks seven units rather than the
#: world. The D6 drafts ported the intact rule down — the tower inherits its
#: ordering from the layer below, so a floor that ranks by a different key is
#: exactly how the defect propagates — and the reduced rubric stays alongside it
#: (pinned separately by
#: ``test_country_composition_carries_the_reduced_consequence_rubric``).
RANKING_PROMPTS: tuple[tuple[str, str], ...] = COMPOSITION_PROMPTS


def _norm(text: str) -> str:
    """Whitespace-normalized text.

    The contract is a Python constant pasted into a YAML block scalar, so the
    LINE WRAPPING differs between the two by construction. Normalizing frees
    the wrapping and pins the words — which is the property that matters and
    the only one that can survive a re-wrap of an 80-line block.
    """
    return " ".join(text.split())


def _descriptor(unit: str) -> dict[str, Any]:
    return yaml.safe_load((DESCRIPTORS / f"analyst_{unit}.yaml").read_text())


def _system_prompt(unit: str) -> str:
    return _descriptor(unit)["method"]["system_prompt"]


def _assembled(unit: str) -> str:
    """The prompt the unit actually runs: house preamble + descriptor + clause.

    The same composition ``inline_target._effective_system_prompt`` performs at
    run time (the deps-builder applies the preamble, the kind appends the
    grounding clause), so a property asserted here is a property of the real
    prompt rather than of the descriptor fragment.
    """
    return with_grounding_clause(with_preamble_if_absent(_system_prompt(unit)) or "")


# ---------------------------------------------------------------------------
# The HOUSE READ CONTRACT — one definition, eight pasted copies, no drift
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", UNITS)
def test_unit_descriptor_carries_the_house_contract_verbatim(unit: str) -> None:
    """Drift between ``_tradecraft`` and a descriptor is a RED TEST, not a
    silent divergence.

    The contract cannot be code-appended: the one choke point that could append
    it (``_effective_system_prompt``) is shared with three non-unit
    inline_target analysts whose body shapes are legitimately different. So it
    is pasted, and this test is what makes the paste safe.

    D6: which contract a desk owes depends on whether it has flipped — see
    :data:`D6_FLIPPED`. The assertion is unchanged in kind (a descriptor
    carries its contract VERBATIM); only the expected constant is now a
    function of the desk's flip state.
    """
    assert _norm(_expected_contract(unit)) in _norm(_system_prompt(unit))


def test_the_only_d6_contract_change_is_the_title_amendment() -> None:
    """MA4 is ONE sentence, and this is what keeps it one sentence.

    The amended contract is derived from the pre-D6 one by a single splice, so
    undoing that splice here and comparing reproduces the whole delta. A second
    change smuggled into the D6 contract — a reworded body shape, a dropped
    rule — shows up as an inequality rather than as prose nobody diffed.
    """
    assert TITLE_NOT_THE_AS_OF_LINE in UNIT_READ_CONTRACT_D6
    assert TITLE_NOT_THE_AS_OF_LINE not in UNIT_READ_CONTRACT
    assert (
        _norm(UNIT_READ_CONTRACT_D6).replace(
            _norm(TITLE_NOT_THE_AS_OF_LINE) + " ", "", 1
        )
        == _norm(UNIT_READ_CONTRACT)
    )


def test_the_held_desk_is_the_only_one_still_on_the_pre_d6_contract() -> None:
    """The HOLD, as a property of the tree rather than a note in a plan.

    When narrative_coordination's replay condition is met and its descriptor
    flips, this test goes red and names the constant to edit — which is the
    point. Until then it pins that the split is exactly one desk wide, and that
    nobody flipped the held desk without lifting the hold here.
    """
    assert D6_HELD == {"narrative_coordination"}
    for unit in UNITS:
        prompt = _norm(_system_prompt(unit))
        carries = _norm(TITLE_NOT_THE_AS_OF_LINE) in prompt
        assert carries == (unit in D6_FLIPPED), (
            f"{unit}: carries the MA4 TITLE amendment={carries} but "
            f"D6_FLIPPED membership={unit in D6_FLIPPED}"
        )


@pytest.mark.parametrize("unit", UNITS)
def test_assembled_unit_prompt_states_the_as_of_rule(unit: str) -> None:
    """D1 — the as-of clause reaches every unit through the grounding clause.

    Appended in CODE (not pasted), so it needs no descriptor copy and cannot
    drift across eight files.
    """
    assembled = _norm(_assembled(unit))
    assert "AS-OF AND WINDOW" in assembled
    assert "Run date (as-of)" in assembled
    assert "ZERO NEW FACTS" in assembled
    # Horizons in days/weeks, never a bare "near-term".
    assert 'give it in DAYS or WEEKS from the as-of date' in assembled
    # A gather_only run replaces the user prompt wholesale and shows NO slice
    # header, so the clause must say what to do when its anchor is missing.
    # Without this leg the rule would be an instruction the corpus_researcher
    # cannot follow, which is how a prompt teaches a model to guess.
    assert "OMIT the as-of line rather than inventing a date" in assembled


@pytest.mark.parametrize("unit", UNITS)
def test_assembled_unit_prompt_carries_the_one_body_shape(unit: str) -> None:
    """D4 — one skeleton, every desk. Before this the eight units used five
    different house styles and one emitted its whole body as a single
    paragraph.

    D6 amends the TITLE rule inside this shape (MA4), so the expected spec is
    the flipped or the pre-D6 one per :data:`D6_FLIPPED`."""
    assembled = _norm(_assembled(unit))
    assert _norm(_expected_body_shape(unit)) in assembled
    for section in (
        "## What changed",
        "## Why it matters",
        "## What would change this read",
        "## Indicators",
    ):
        assert section in assembled, section


#: The paragraphs allowed to NAME a banned phrase, keyed by their opening
#: words, because naming one is the whole job of each:
#:
#:   * ``UNIT_VERDICT_RULE`` — the ban itself, which quotes what it forbids;
#:   * the D6 ``WHAT EACH MISTAKE COSTS`` paragraph (P2), whose mush-move slot
#:     is instantiated with a phrase from THIS desk's vocabulary — escalation
#:     fills it with 'call a real move "steady tension"', the literal example
#:     the shared preamble note gives, which is also a banned marker.
#:
#: Both are prohibitions. Anywhere ELSE in the prompt, a marker is an order.
_PROHIBITION_OPENERS: tuple[str, ...] = (
    "STATE THE VERDICT IN YOUR OWN WORDS.",
    "WHAT EACH MISTAKE COSTS.",
)


def _prohibition_paragraphs(unit: str) -> list[str]:
    """The assembled prompt's paragraphs that are allowed to name a marker."""
    return [
        p
        for p in _assembled(unit).split("\n\n")
        if _norm(p).startswith(_PROHIBITION_OPENERS)
    ]


@pytest.mark.parametrize("unit", UNITS)
def test_banned_phrases_appear_only_inside_a_prohibition(unit: str) -> None:
    """D2 — the ban ships WITH the replacement shape, and nothing still orders
    the banned sentence.

    The failure this guards against is subtle and was the diagnostic's explicit
    warning: banning a phrase while a descriptor elsewhere still instructs it
    produces synonym-swapping ("the principal vector"), not judgment. So the
    assertion is not "the phrase is absent" — it is "every occurrence sits
    inside a paragraph whose job is to FORBID it".

    D6 widened the allowance from one paragraph to two. P2 prices the refusal
    to judge by naming a concrete mush-move in the desk's own vocabulary, and
    on escalation that phrase is a banned marker. Counting against the ban
    alone would have made a correctly-written prohibition look like an order.
    The guard did not weaken: the allowance is still a fixed, named set of
    prohibition paragraphs, and a marker in the bounded question, the vectors
    or the rubric still fails.
    """
    assembled = _norm(_assembled(unit))
    zones = _prohibition_paragraphs(unit)
    for marker in BANNED_PHRASE_MARKERS:
        allowed = sum(_norm(z).count(marker) for z in zones)
        assert assembled.count(marker) == allowed, (
            f"{unit}: {marker!r} appears {assembled.count(marker)}x in the "
            f"assembled prompt but only {allowed}x inside a prohibition "
            "paragraph — some descriptor line is still asking for it"
        )


@pytest.mark.parametrize("unit", UNITS)
def test_the_prohibition_paragraphs_are_actually_prohibitions(unit: str) -> None:
    """The fence under the widened allowance above.

    ``_PROHIBITION_OPENERS`` grants two paragraphs the right to name a banned
    phrase. That right is only safe while those paragraphs still FORBID —
    reword P2 into something that orders the mush-move and the allowance would
    launder it. So each zone is pinned to the text that makes it a prohibition:
    the ban to its own constant, and P2 to the price it puts on refusing to
    judge.
    """
    zones = {
        _norm(p).split(".")[0]: _norm(p) for p in _prohibition_paragraphs(unit)
    }
    ban = zones.get("STATE THE VERDICT IN YOUR OWN WORDS")
    assert ban is not None, f"{unit}: the verdict rule paragraph is missing"
    assert _norm(UNIT_VERDICT_RULE) in ban

    cost = zones.get("WHAT EACH MISTAKE COSTS")
    if unit in D6_FLIPPED:
        assert cost is not None, f"{unit}: D6-flipped but P2 is missing"
        # P2 states BOTH prices; the shared note forbids shipping it with one.
        assert "fails the finding" in cost
        assert "is WORSE" in cost
    else:
        assert cost is None, f"{unit}: held desk carries P2"


@pytest.mark.parametrize("unit", UNITS)
def test_unit_prompt_binds_absence_scoping_to_the_bluf(unit: str) -> None:
    """D3 — 336/701 BLUFs asserted an absence and only 40 were scoped."""
    assembled = _norm(_assembled(unit))
    assert "THE BLUF IS UNDER THE ABSENCE RULE TOO" in assembled
    assert "CALIBRATE CONFIDENCE TO THE OBSERVATION, NOT TO THE WORLD" in assembled
    assert "signal count is not evidence" in assembled


@pytest.mark.parametrize("unit", UNITS)
def test_unit_prompt_bans_instrument_readings_in_prose(unit: str) -> None:
    """D5 — indicator schema fields were being pasted into the prose."""
    assembled = _norm(_assembled(unit))
    assert _norm(NO_INSTRUMENT_READINGS) in assembled


# ---------------------------------------------------------------------------
# DS-1 — the ninth unit, and the classification that hid it
# ---------------------------------------------------------------------------
#
# Everything above this line runs on ``disruption_status`` too, which is most of
# the fix. What the parametrized pins CANNOT catch is a regression of the
# CLASSIFICATION itself: drop it back into ``NON_UNITS`` and the descriptor
# still carries the contract (the paste is already in the file), so every test
# above stays green while the PUT script stops shipping it as a unit and the
# next prompt edit re-opens the same hole. These three pin the classification.


def test_the_put_script_files_disruption_status_as_a_unit() -> None:
    """The tree-side classification, read out of the script that SHIPS it.

    ``voice_prompt_puts`` is where the miss lived: the descriptor is not code,
    so a unit reaches production only when that script PUTs it, and it PUTs the
    unit half and the non-unit half with different rules.
    """
    import ast

    src = Path(__file__).resolve().parents[2] / "scripts" / "voice_prompt_puts.py"
    tuples: dict[str, list[str]] = {}
    for node in ast.walk(ast.parse(src.read_text())):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Tuple):
                tuples[node.target.id] = [
                    e.value for e in node.value.elts if isinstance(e, ast.Constant)
                ]
    assert "disruption_status" in tuples["UNITS"]
    assert "disruption_status" not in tuples["NON_UNITS"]
    # The tuple this file pins and the tuple the script ships are the same set:
    # a unit added here but not there is a prompt that never reaches production.
    assert set(UNITS) == set(tuples["UNITS"])


def test_disruption_status_no_longer_orders_the_template_sentence() -> None:
    """The measured defect, in one assertion per phrasing.

    Q-5 measured "the dominant vector is <X>, and the direction is <Y>" in
    14/15 findings AFTER the voice wave and 57/61 before — unchanged, because
    the prompt still ORDERED it: "state the direction of travel — DEGRADING,
    HOLDING, or RECOVERING — and say which, in those words". A ban alone would
    have produced "the principal vector"; the order had to go.
    """
    prompt = _norm(_system_prompt("disruption_status"))
    # NOT a bare "in those words": the contract's own COVERAGE-vs-NEGATIVE rule
    # legitimately ends "say so in those words". The ORDER is what had to go.
    assert "and say which, in those words" not in prompt
    assert "Identify the DOMINANT vector" not in prompt
    # The replacement SHAPE, in this desk's own vocabulary, is what stands in
    # its place — a driver, a direction, a pace, and the thing that caps it.
    assert "Name the ONE vector that is actually driving this desk's read" in prompt
    assert "what is holding it back" in prompt
    # The enum survives as a CATEGORY the structured fields carry, not as the
    # sentence: it is the six-vector taxonomy this desk is scored on.
    assert 'Put the direction category — DEGRADING, HOLDING, or RECOVERING — in' in prompt


def test_disruption_status_retired_its_own_body_skeleton() -> None:
    """D4 — the pre-wave skeleton was byte-identical before and after the PUT
    this desk did receive. It is gone from the prompt entirely now: two body
    specs in one prompt is how a model gets to pick."""
    prompt = _norm(_system_prompt("disruption_status"))
    for retired in ("## Key points", "## Assessment", "## Indicators to watch"):
        assert retired not in prompt, retired
    # The lane-specific method content the contract does NOT cover survives —
    # this was never a proposal to pave the desk over with the house shape.
    for kept in (
        "QUANTITATIVE CLAIMS",
        "A FIGURE IS ONLY AS CURRENT AS ITS SOURCE",
        "BOUNDARY — stay in your lane",
        "interdiction & physical risk",
        "downstream shortage",
    ):
        assert kept in prompt, kept


def test_disruption_status_keeps_the_retrieved_rules_it_already_had() -> None:
    """It is a unit that GATHERS. DS-1 moved it across one axis, not both: the
    V-N2 retrieval half it took in the earlier PUT still has to be there."""
    assembled = _norm(_assembled("disruption_status"))
    assert "RETRIEVED DOCUMENTS ARE CONTEXT, NOT A CLOCK" in assembled
    assert "SEPARATE THE REPORTING DATE FROM THE EVENT DATE" in assembled


# ---------------------------------------------------------------------------
# D8a — the window is DERIVED, and no unit prompt states one in prose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", UNITS)
def test_no_unit_prompt_hardcodes_its_own_slice_window(unit: str) -> None:
    """The D8a defect class, closed by construction.

    ``narrative_coordination`` told the model it read a "last-24h signal slice"
    while its descriptor declared 72h — for weeks, on the one unit whose whole
    method (synchrony) is window-sensitive. A prompt that names its own window
    can drift from the query that cut the slice; a prompt that reads the window
    off the rendered header cannot.
    """
    prompt = _system_prompt(unit).lower()
    for phrase in ("last-24h", "last 24h", "24-hour slice", "last-72h", "72-hour slice"):
        assert phrase not in prompt, f"{unit}: prompt hardcodes {phrase!r}"


def test_narrative_coordination_prompt_window_matches_its_descriptor() -> None:
    """The unit's stated window IS the descriptor's, because it is the same
    value rendered once — not two strings that have to be kept in sync."""
    doc = _descriptor("narrative_coordination")
    declared = doc["subscription"]["targets"]["time_window"]
    assert declared == "72h"

    class _Targets:
        time_window = declared

    class _Sub:
        targets = _Targets()

    class _Desc:
        subscription = _Sub()

    hours = resolve_slice_window_hours(_Desc())  # type: ignore[arg-type]
    assert hours == 72
    header = it._render_user_prompt([], "country_g20_mx", run_date="2026-08-03",
                                    window_hours=hours)
    assert "Slice window: trailing 72h (3 days) to the run date" in header


def test_resolve_slice_window_hours_reads_the_descriptor_or_defaults() -> None:
    """One resolver, two consumers (the slice query and the prompt header)."""

    def _desc(value: Any) -> Any:
        class _Targets:
            time_window = value

        class _Sub:
            targets = _Targets()

        class _Desc:
            subscription = _Sub()

        return _Desc()

    assert resolve_slice_window_hours(_desc("336h")) == 336
    assert resolve_slice_window_hours(_desc(48)) == 48
    # Never a fabricated window: anything unparseable falls to the documented
    # default rather than to a guess.
    assert resolve_slice_window_hours(_desc("not-a-window")) == DEFAULT_SLICE_WINDOW_HOURS
    assert resolve_slice_window_hours(_desc(None)) == DEFAULT_SLICE_WINDOW_HOURS
    assert resolve_slice_window_hours(_desc(0)) == DEFAULT_SLICE_WINDOW_HOURS
    assert resolve_slice_window_hours(_desc(True)) == DEFAULT_SLICE_WINDOW_HOURS


# ---------------------------------------------------------------------------
# The slice header — the text the as-of line is copied from
# ---------------------------------------------------------------------------


def test_slice_header_carries_run_date_window_and_count() -> None:
    prompt = it._render_user_prompt(
        [], "country_g20_de", run_date="2026-08-03", window_hours=72,
    )
    head = prompt.split("\n\n")[0].splitlines()
    assert head == [
        "Target: country_g20_de",
        "Run date (as-of): 2026-08-03",
        "Slice window: trailing 72h (3 days) to the run date",
        "Number of signals: 0",
    ]


def test_slice_header_omits_an_unknown_window_rather_than_guessing() -> None:
    """An absent window renders NO window line. Printing a default here would
    reproduce the D8a lie one layer down."""
    prompt = it._render_user_prompt([], "t", run_date="2026-08-03")
    assert "Slice window" not in prompt
    assert "Run date (as-of): 2026-08-03" in prompt


def test_slice_header_run_date_defaults_to_today_utc() -> None:
    import datetime

    prompt = it._render_user_prompt([], "t")
    today = datetime.datetime.now(tz=datetime.timezone.utc).date().isoformat()
    assert f"Run date (as-of): {today}" in prompt


@pytest.mark.asyncio
async def test_run_method_threads_the_window_from_options() -> None:
    """The descriptor's window reaches the rendered prompt through the actor's
    ``options`` stamp — the seam that makes prose and query the same value."""
    import json
    from dataclasses import dataclass
    from typing import Mapping
    from uuid import uuid4

    @dataclass
    class _Usage:
        prompt_tokens: int = 10
        completion_tokens: int = 5
        reasoning_tokens: int = 0

    @dataclass
    class _Response:
        content: str = ""
        usage: _Usage | None = None

    class _CapturingLLM:
        subprovider = "stub-test"

        def __init__(self) -> None:
            self.users: list[str] = []

        async def chat_complete(
            self,
            messages: list[Mapping[str, Any]],
            *,
            max_tokens: int | None = None,
            temperature: float | None = None,
            system: str | None = None,
            **kwargs: Any,
        ) -> Any:
            self.users.append(str(messages[-1].get("content", "")))
            return _Response(
                content=json.dumps(
                    {
                        "title": "t",
                        "body": "Body with a cited claim [1].",
                        "confidence": 0.5,
                        "evidence": ["e1"],
                        "tags": ["severity:low"],
                    }
                ),
                usage=_Usage(),
            )

    llm = _CapturingLLM()
    deps = it.InlineTargetDeps(llm=llm, system_prompt="TASK — unit.")
    row = {
        "id": uuid4(),
        "title": "A reshuffle is reported",
        "produced_at": "2026-08-02T14:00:00+00:00",
        "source_url": "https://example.com/x",
        "data": {"summary": "Cabinet-level changes signalled."},
    }
    await it.run_method(
        [row], {"analyst_id": "unit.x", "slice_window_hours": 72}, deps,
    )
    assert any("Slice window: trailing 72h (3 days)" in u for u in llm.users)


# ---------------------------------------------------------------------------
# The composition layer — D6, D7, D5, D8c
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,prompt", COMPOSITION_PROMPTS)
def test_composition_prompt_carries_the_shared_structure_spec(
    name: str, prompt: str
) -> None:
    """"One structure spec, both layers" is literal: the unit and composition
    shapes are generated from the same mechanics text and differ only in their
    section lists."""
    assert _norm(COMPOSITION_BODY_SHAPE) in _norm(prompt), name
    # The mechanics half is byte-identical across layers.
    mechanics = "Every '##' header sits ALONE on its own line with a blank line"
    assert mechanics in prompt, name
    assert mechanics in UNIT_BODY_SHAPE
    assert "at most 90 characters" in prompt.lower(), name


@pytest.mark.parametrize("name,prompt", COMPOSITION_PROMPTS)
def test_composition_prompt_requires_tension_and_coverage(
    name: str, prompt: str
) -> None:
    """D6 — judgment becomes the body, coverage becomes a footer."""
    assert "## The picture" in prompt, name
    assert "## Tension" in prompt, name
    assert "## Coverage" in prompt, name
    assert "AT MOST THREE paragraphs of connected argument" in prompt, name
    # The roll-call is named and forbidden.
    assert "OBSERVATION / JUDGMENT skeleton" in prompt, name


@pytest.mark.parametrize("name,prompt", COMPOSITION_PROMPTS)
def test_composition_hedge_rule_is_a_duty_not_a_voice(name: str, prompt: str) -> None:
    """The old rule handed the model the bureaucratic register verbatim."""
    assert "Hedging is a CALIBRATION DUTY, not a house voice" in prompt, name


@pytest.mark.parametrize("name,prompt", COMPOSITION_PROMPTS)
def test_composition_tension_covers_factual_disagreement(
    name: str, prompt: str
) -> None:
    """D8c — the Hormuz case: two units asserted incompatible states of one
    fact and the composition reported them as agreement, because the old rule
    only ever asked about DIRECTIONAL disagreement."""
    assert "DISAGREEMENT INCLUDES FACTUAL DISAGREEMENT" in prompt, name
    assert "incompatible STATES OF THE SAME FACT" in prompt, name


@pytest.mark.parametrize("name,prompt", COMPOSITION_PROMPTS)
def test_composition_bans_instrument_readings(name: str, prompt: str) -> None:
    """D5 — 56/117 compositions narrated a microsecond ISO timestamp and
    27/117 printed ``intensity NN.NN`` / event counts as prose."""
    assert _norm(NO_INSTRUMENT_READINGS) in _norm(prompt), name
    assert "USE them to decide, never PRINT them" in prompt, name


@pytest.mark.parametrize("name,prompt", RANKING_PROMPTS)
def test_ranking_prompts_rank_by_consequence(name: str, prompt: str) -> None:
    """D7 — the flagship defect: the tower ranked the world by how well-sourced
    a finding was rather than by how much it mattered."""
    assert _norm(CONSEQUENCE_RULE) in _norm(prompt), name
    assert "MAGNITUDE x PROXIMITY" in prompt, name
    assert "NEVER the ordering key" in prompt, name
    assert "STANDING PICTURE BEFORE DELTA" in prompt, name
    assert "Novelty is not consequence" in prompt, name


def test_country_composition_carries_the_reduced_consequence_rubric() -> None:
    """A country desk ranks seven units rather than the world, so it takes the
    stakes ladder in reduced form inside its own shape rule."""
    prompt = synth._COMPOSITION_SYSTEM
    assert "ranked on the STAKES the cited blocks describe" in prompt
    assert "NEVER on which block scored the highest effective_confidence" in prompt


@pytest.mark.parametrize("name,prompt", COMPOSITION_PROMPTS)
def test_traceability_rule_survives_the_rewrite(name: str, prompt: str) -> None:
    """The one line in D6 that could invite synthesis beyond the evidence is
    "say what these blocks TOGETHER show that none shows alone". TRACEABILITY is
    what polices it, so the shape rule must never ship without it."""
    assert "TRACEABILITY" in prompt, name
    assert (
        "an in-range [[ref:N]] does NOT license a claim its block does not make"
        in prompt
    ), name
    assert "is a PROMISE that" in prompt, name


@pytest.mark.parametrize("name,prompt", COMPOSITION_PROMPTS)
def test_composition_prompt_states_the_as_of_rule(name: str, prompt: str) -> None:
    """D1 at the composition layer — the as-of date is copied from the newest
    ``produced_at`` printed on a shown block, never read off the clock."""
    assert "AS-OF AND WINDOW" in prompt, name
    assert "MOST RECENT produced_at printed on a shown block" in prompt, name
    assert "ZERO NEW FACTS" in prompt, name


@pytest.mark.parametrize("name,prompt", COMPOSITION_PROMPTS)
def test_composition_prompt_prefers_the_evidence_window_directive(
    name: str, prompt: str,
) -> None:
    """H4 — the as-of line's date/time is COPIED from the deterministic
    EVIDENCE WINDOW block when one is shown, never re-derived by scanning
    every rendered block (the arithmetic step that produced a self-
    inconsistent stamp — a claimed "latest 01:40 UTC" against heads the render
    actually showed produced at 16-17 UTC). The old scan-the-blocks wording
    survives ONLY as the fallback for an unmeasured window."""
    assert "EVIDENCE WINDOW" in prompt, name
    assert "copy them verbatim, never recompute them" in prompt, name
    assert "When NO EVIDENCE WINDOW block is shown" in prompt, name


# ---------------------------------------------------------------------------
# The verify seam — two sections the new shape adds on EVERY finding
# ---------------------------------------------------------------------------
#
# Both of these are regressions the prompt change would have caused SILENTLY:
# the shape is descriptor-side, the grading is code-side, and nothing connects
# them. Shipping D1/D4 without these two exemptions would have subtracted from
# the faithfulness score of every finding in the tower on deploy day, for
# following the prompt.


@pytest.mark.parametrize(
    "line",
    [
        "*As of 3 August 2026; slice covers the trailing 72h to that date; 26 signals.*",
        "*As of 4 August 2026 00:00 UTC; composed from 5 region reads, latest 3 August 12:00.*",
        "*As of 3 August 2026; composed from 7 unit reads, latest 19:50 UTC.*",
    ],
)
def test_as_of_line_is_structure_at_both_verify_gates(line: str) -> None:
    """No signal states the run date, so the as-of line can carry no [N] and the
    judge has nothing to check it against. Exempt at both gates or every finding
    gains an uncited span on deploy day."""
    from legba.data.provenance import verify as vf

    assert vf._is_as_of_line(line)
    assert not vf._is_fact_asserting(line)
    assert not vf._is_judgeable_claim(line)
    assert vf._claim_kind(line) == "structure"


@pytest.mark.parametrize(
    "line",
    [
        # A dated claim, unwrapped — the ordinary case.
        "As of 2 August, transits fell from ~120/day to under 11 [74].",
        # Italicised AND cited: the citation is positive evidence of a claim.
        "*As of 2 August, Iran resumed enrichment at Fordow [12].*",
        # Italicised, uncited, but carrying a present-fact verb.
        "*As of 2 August, Iran resumed enrichment at Fordow.*",
        # Italicised and uncited, but says something about the WORLD rather than
        # naming a run coordinate.
        "*As of 2 August, Brazil recalled its ambassador.*",
    ],
)
def test_a_real_dated_claim_is_never_exempted_as_an_as_of_line(line: str) -> None:
    """H1 — an exemption must never hide a present fact from the judge. Four
    independent narrowing conditions, and a claim needs to fail only one."""
    from legba.data.provenance import verify as vf

    assert not vf._is_as_of_line(line)
    assert vf._is_fact_asserting(line)
    assert vf._is_judgeable_claim(line)


def test_what_would_change_this_read_is_section_skipped() -> None:
    """D4 puts ``## What would change this read`` on EVERY desk. It is
    forward-looking by construction — an observation that has not happened, plus
    a statement about our own collection — so its bullets cannot cite. The
    adjacent ``## What changed`` is the CITED section and must survive."""
    from legba.data.provenance import verify as vf

    body = (
        "*As of 3 August 2026; slice covers the trailing 72h; 26 signals.*\n\n"
        "**BLUF:** Brazil pulled its ambassador on 2 August [14].\n\n"
        "## What changed\n"
        "- Brazil recalled its ambassador on 2 August [14].\n\n"
        "## Why it matters\n"
        "Recall is the most reversible step in the toolkit [18].\n\n"
        "## What would change this read\n"
        "Brazil going past recall would move the verdict.\n"
        "This desk does not collect Itamaraty procedural filings.\n\n"
        "## Indicators\n"
        "- Ambassador returns to Buenos Aires.\n"
    )
    spans = vf._segment_claims(body)
    joined = "\n".join(spans)
    assert "Brazil recalled its ambassador on 2 August [14]." in joined
    assert "most reversible step" in joined
    assert "Itamaraty" not in joined
    assert "Ambassador returns to Buenos Aires" not in joined


def test_legacy_global_meta_prompt_takes_no_voice_rules() -> None:
    """``_SYSTEM_PROMPT`` composes findings with no continuity slice, no block
    handles, and no shown ``produced_at`` — every Phase-V rule would be a rule
    about something it will never see."""
    assert "AS-OF AND WINDOW" not in synth._SYSTEM_PROMPT
    assert "## The picture" not in synth._SYSTEM_PROMPT
    assert "CONSEQUENCE, NOT CONFIDENCE" not in synth._SYSTEM_PROMPT


def test_grounding_clause_no_longer_orders_instrument_readings_printed() -> None:
    """D5 at the unit layer: the register cap named four fields, two of which
    are instrument readings, and the model read the list as a print order."""
    assert "its own name and status" in UNIT_GROUNDING_CLAUSE
    assert (
        "its own name, status, intensity and event count" not in UNIT_GROUNDING_CLAUSE
    )
    assert "USE them to decide, never PRINT them" in UNIT_GROUNDING_CLAUSE
