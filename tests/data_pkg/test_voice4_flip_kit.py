# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pin the flipped desk prompts: their text in the tree is checked against digests.

The eight flipped descriptors carry the desk prompts pinned in
``scripts/voice4_flip/_flip_common`` (``INTENDED_SHA256``), and the held desk
keeps its own pin. Later changes that sit on top of those prompts (shared
contract paragraphs, per-desk paragraphs, a title hint line, level indicator
lines) are peeled off by ``d6_base`` before the digest is taken, and the tests
below police that peel so it can only remove what it names.

A descriptor edited without re-pinning moves its digest and fails here, and
``apply_flip`` refuses to PUT a unit whose tree digest does not match the pin,
so this test and that refusal are the same check at two moments.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "voice4_flip"))

from _level_peel import LATER_LEVEL_SWAPS, pre_level_indicators  # noqa: E402
from _flip_common import (  # noqa: E402
    HELD_SHA256,
    HELD_UNIT,
    INTENDED_SHA256,
    LATER_CONTRACT_PARAGRAPHS,
    LATER_DESK_PARAGRAPHS,
    LATER_TITLE_HINTS,
    MA2_DATE_FORMAT_SENTENCE,
    TITLE_AMENDMENT_SENTENCE,
    UNITS,
    d6_base,
    norm,
    pre_title_frame_fix,
    sha,
    structural_diff,
)

DESCRIPTORS = REPO_ROOT / "descriptors"

ALL_DESKS: tuple[str, ...] = UNITS + (HELD_UNIT,)


def _doc(unit: str) -> dict[str, Any]:
    return yaml.safe_load((DESCRIPTORS / f"analyst_{unit}.yaml").read_text())


def _prompt(unit: str) -> str:
    return _doc(unit)["method"]["system_prompt"]


# ---------------------------------------------------------------------------
# The byte-faithfulness anchor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", UNITS)
def test_tree_prompt_matches_the_pinned_digest(unit: str) -> None:
    """The gate. Runs everywhere.

    A descriptor edited after the flip — a typo fix, a well-meant reword — moves
    this digest, and ``apply_flip`` refuses to PUT a unit whose tree digest does
    not match. So this test and that refusal are the same check at two moments:
    here at review time, there at flip time.

    The comparison is against :func:`d6_base` rather than the raw prompt because
    a LATER train (FRAME-3) has since added a paragraph to the same house
    contract. The pin still covers exactly what it was written to cover — the pinned
    prompt text — and the peel is what keeps that true instead of re-pinning a
    digest that would then prove nothing.
    """
    assert sha(d6_base(_prompt(unit))) == INTENDED_SHA256[unit]


def test_the_held_desk_keeps_its_pre_flip_digest() -> None:
    """``narrative_coordination`` is HELD: its prompt prose must not have moved.

    FRAME-3's paragraph IS on this desk — the VOICE hold is about prose, and a
    tag contract cannot be per-desk when the desk is a scorecard dimension — so
    the pin is checked against the peeled prompt here too. The hold that still
    stands is the one the fleet-sentence tests below assert: no MA2, no MA4.
    """
    assert sha(d6_base(_prompt(HELD_UNIT))) == HELD_SHA256
    assert HELD_UNIT not in UNITS


@pytest.mark.parametrize("unit", ALL_DESKS)
def test_the_peel_removes_the_later_paragraphs_and_nothing_else(unit: str) -> None:
    """The peel is only honest if it is exactly a paragraph removal.

    Asserted both ways: every later-train paragraph is IN the tree prompt and
    OUT of the peeled one, and re-inserting nothing else changes — the peeled
    text plus the removed paragraphs accounts for every byte of the original
    minus their separators. Without this, ``d6_base`` could quietly start
    removing something the pin was supposed to be checking.

    TITLE-FRAME-FIX (2026-09-01, post-flip): ``d6_base`` now peels TWO trains,
    and the second one changed a LINE rather than appending a paragraph — see
    ``LATER_TITLE_HINTS``. The paragraph accounting below is therefore taken
    against the title-hint-rolled-back prompt, exactly as the BEFORE side is
    normalized in ``test_only_the_prompt_changed_against_the_pre_flip_descriptor``
    for the three post-flip drifts it absorbs. This keeps THIS test measuring
    what it was written to measure — that the PARAGRAPH peel is a clean
    paragraph removal — instead of being weakened to ignore length. The line
    swap gets its own byte accounting in the test below, so neither peel is
    unpoliced.
    """
    # X4 LEVEL's whole-line swaps are rolled back first, like the title hint,
    # and policed by their own accounting test below.
    full = pre_title_frame_fix(pre_level_indicators(_prompt(unit)))
    base = d6_base(_prompt(unit))
    # X4 / ESC: the house-contract paragraphs ride every desk; a per-desk
    # paragraph rides only the desks LATER_DESK_PARAGRAPHS names - and a desk it
    # does NOT name must not carry it (the peel would then be hiding an edit).
    expected = LATER_CONTRACT_PARAGRAPHS + LATER_DESK_PARAGRAPHS.get(unit, ())
    later = {norm(p) for p in expected}
    for para in expected:
        assert norm(para) in norm(full), f"{unit}: later paragraph missing from tree"
        assert norm(para) not in norm(base), f"{unit}: peel left it behind"
    for other, paras in LATER_DESK_PARAGRAPHS.items():
        if other != unit:
            for para in paras:
                if para not in LATER_DESK_PARAGRAPHS.get(unit, ()):
                    assert norm(para) not in norm(full), f"{unit}: carries {other}'s paragraph"

    paragraphs = full.split("\n\n")
    dropped = [p for p in paragraphs if norm(p) in later]
    assert len(dropped) == len(expected), f"{unit}: {len(dropped)}"
    # Byte accounting: the peel removed exactly those paragraphs and exactly one
    # blank-line separator each. Anything else — a stripped space, a joined
    # paragraph — shows up as an inequality here rather than as a digest that
    # someone re-pins.
    assert len(full) == len(base) + sum(len(p) + 2 for p in dropped), unit


@pytest.mark.parametrize("unit", ALL_DESKS)
def test_the_level_peel_is_exactly_its_declared_swaps(unit: str) -> None:
    """X4 LEVEL — the third later train's peel accounts for every
    byte it moves: on a converted desk each X4 text is present once and the
    rollback changes the length by exactly the swaps' difference; every other
    desk is untouched by it."""
    prompt = _prompt(unit)
    swaps = LATER_LEVEL_SWAPS.get(unit, ())
    for new, old in swaps:
        assert prompt.count(new) == 1, f"{unit}: LEVEL text not present exactly once"
    peeled = pre_level_indicators(prompt)
    assert len(prompt) - len(peeled) == sum(len(n) - len(o) for n, o in swaps)
    if not swaps:
        assert peeled == prompt


@pytest.mark.parametrize("unit", ALL_DESKS)
def test_the_title_hint_rollback_is_exactly_one_line_per_desk(unit: str) -> None:
    """TITLE-FRAME-FIX's peel is a ONE-LINE swap, and it is on every desk.

    The counterpart to the paragraph accounting above, for the line-shaped
    train. Three claims, because a line swap has more ways to go wrong than a
    paragraph drop:

    * the TREE carries the new hint and NOT the D6 one (the train landed, and
      the response schema did not end up with two ``"title"`` examples in it);
    * the ROLLED-BACK text carries the D6 hint and not the new one (the peel
      actually reverses it);
    * the length delta is EXACTLY the one line's delta — so the rollback cannot
      quietly start restoring anything else while the digests stay green.
    """
    d6_line, new_line = LATER_TITLE_HINTS[unit]
    tree = _prompt(unit)
    rolled = pre_title_frame_fix(tree)

    assert tree.count(new_line) == 1, f"{unit}: tree lacks the TITLE-FRAME-FIX hint"
    assert d6_line not in tree, f"{unit}: tree still carries the D6 hint"
    assert rolled.count(d6_line) == 1, f"{unit}: rollback did not restore the D6 hint"
    assert new_line not in rolled, f"{unit}: rollback left the new hint behind"
    assert len(tree) - len(rolled) == len(new_line) - len(d6_line), unit


def test_the_title_hint_rollback_refuses_an_ambiguous_prompt() -> None:
    """The discriminator: a prompt carrying BOTH lines is a REFUSAL, not a swap.

    Without this the rollback's failure mode would be silent — a descriptor that
    somehow grew a second ``"title"`` example would still hash green, because
    the first swap would land and the stray line would look like draft bytes.
    """
    d6_line, new_line = LATER_TITLE_HINTS["escalation"]
    with pytest.raises(ValueError, match="BOTH"):
        pre_title_frame_fix(f"prefix {new_line} middle {d6_line} suffix")


def test_a_reworded_title_hint_breaks_the_digest() -> None:
    """The proof that the peel did not defang the pin it rides on.

    ``d6_base`` now rewrites bytes before hashing, so the obvious worry is that
    it has become a laundering channel: reword the hint again tomorrow and the
    digest stays green. It does not — the rollback keys on the EXACT new line,
    so a further edit is not recognised, is not rolled back, and moves the
    digest. Asserted against a corrupted copy rather than by trusting the
    reasoning.
    """
    _, new_line = LATER_TITLE_HINTS["escalation"]
    tree = _prompt("escalation")
    assert sha(d6_base(tree)) == INTENDED_SHA256["escalation"]

    corrupted = tree.replace(new_line, new_line.replace("who is doing what", "X"), 1)
    assert corrupted != tree
    assert sha(d6_base(corrupted)) != INTENDED_SHA256["escalation"]


# ---------------------------------------------------------------------------
# The descriptors still validate through the path the registry uses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ALL_DESKS)
def test_descriptor_validates_as_an_analyst_descriptor(unit: str) -> None:
    """The real binding path, not a re-implementation of it.

    ``Family.ANALYST.model`` is the same class ``registry.api._parse_descriptor``
    resolves and ``model_validate(..., strict=False)`` is the same call it makes
    on the PUT body, so a descriptor that passes here is one the registry will
    accept. The held desk is included: it is a descriptor this train read, and
    "we did not break the one we did not touch" is worth one assertion.
    """
    from legba.data.registry.descriptor import Family

    body = _doc(unit)
    descriptor = Family.ANALYST.model.model_validate(body, strict=False)
    assert descriptor.identity.id == unit
    assert descriptor.method.system_prompt == body["method"]["system_prompt"]


@pytest.mark.parametrize("unit", UNITS)
def test_the_prompt_survives_a_yaml_round_trip_unchanged(unit: str) -> None:
    """Re-emitting and re-loading the value changes nothing.

    Guards the transport rather than the file: the flip hands this string to
    ``httpx`` as JSON and the registry hands it back, and a value that only
    survives in one serializer's hands would be a trap for the next tool that
    touches it.
    """
    value = _prompt(unit)
    assert yaml.safe_load(yaml.safe_dump({"p": value}))["p"] == value


# ---------------------------------------------------------------------------
# The fleet properties the flip has to land ALL AT ONCE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", UNITS)
def test_the_fleet_sentences_are_on_every_flipped_desk(unit: str) -> None:
    """MA2 and MA4, on all eight.

    MA4 is the load-bearing one: the HOUSE READ CONTRACT opens by calling
    itself "identical on every desk", so an amendment that reaches some desks
    and not others makes the contract false about itself. MA2 is a correctness
    fix — dates written as prose make ``IndicatorEntry`` drop a whole entry
    silently — and a fix that lands on seven desks is not a fix.
    """
    prompt = norm(_prompt(unit))
    assert norm(MA2_DATE_FORMAT_SENTENCE) in prompt
    assert norm(TITLE_AMENDMENT_SENTENCE) in prompt


def test_the_held_desk_carries_neither_fleet_sentence() -> None:
    """The other half of the all-at-once claim: 8 of 9 is the INTENDED state
    only because the ninth is formally held. If the held desk quietly acquired
    the sentences, something flipped it."""
    prompt = norm(_prompt(HELD_UNIT))
    assert norm(MA2_DATE_FORMAT_SENTENCE) not in prompt
    assert norm(TITLE_AMENDMENT_SENTENCE) not in prompt


def test_the_kit_and_the_voice_contract_agree_on_who_flipped() -> None:
    """Two files name the flip set; a disagreement between them is a bug.

    ``test_voice_contract`` decides which HOUSE READ CONTRACT a desk owes from
    its own ``D6_FLIPPED``; the kit decides which desks to PUT from ``UNITS``.
    Same set, or a desk gets the amended contract asserted against it and never
    receives it (or the reverse). Same shape as the DS-1 pin that made
    ``disruption_status``' classification a property of the tree.
    """
    from .test_voice_contract import D6_FLIPPED, D6_HELD

    assert set(UNITS) == set(D6_FLIPPED)
    assert D6_HELD == {HELD_UNIT}


# ---------------------------------------------------------------------------
# The structural guard the apply script leans on
# ---------------------------------------------------------------------------


def _live() -> dict[str, Any]:
    """A live head as the registry serves it: pydantic defaults materialized,
    a stamped content-hash version, and a promoted lifecycle state."""
    return {
        "identity": {"id": "x", "version": "abc123def456", "state": "active"},
        "method": {
            "system_prompt": "OLD",
            "kind": "llm_planner",
            "retries": 2,  # default the YAML never writes
            "timeout_seconds": 600,  # ditto
        },
        "action_packs": [{"pack_id": "escalate_finding", "governor_override": None}],
        "outputs": {},  # ditto
    }


def _tree() -> dict[str, Any]:
    """The same descriptor as the tree file writes it."""
    return {
        "identity": {"id": "x", "version": "0" * 16, "state": "draft"},
        "method": {"system_prompt": "NEW", "kind": "llm_planner"},
        "action_packs": [{"pack_id": "escalate_finding"}],
    }


def test_structural_diff_is_clean_on_a_registry_materialized_head() -> None:
    """The comparison is TREE-DIRECTED, and this is why it has to be.

    A live body carries every pydantic default the YAML omits. A symmetric
    comparison called all of them "drift" — ~22 per unit on the real fleet —
    which is precisely the noise that teaches an operator to wave the check
    through on the run where it matters. Three differences here are expected
    and none is drift: the stamped version, the promoted lifecycle state (the
    ``disruption_status`` case: tree says draft, live runs active), and the
    prompt this train exists to change.
    """
    assert structural_diff(_live(), _tree()) == []


def test_structural_diff_reports_a_field_the_tree_declares_differently() -> None:
    tree = _tree()
    tree["method"]["kind"] = "something_else"
    assert structural_diff(_live(), tree) == [
        "method.kind: tree 'something_else' != live 'llm_planner'"
    ]


def test_structural_diff_reports_a_tree_field_missing_from_the_live_head() -> None:
    tree = _tree()
    tree["eval"] = {"rubric": "r"}
    assert structural_diff(_live(), tree) == ["eval: declared in tree, absent live"]


def test_structural_diff_reports_a_list_the_registry_and_tree_size_differently() -> None:
    """The real ``disruption_status`` finding, in miniature.

    Its descriptor declares ``action_packs: []`` deliberately — granting the
    escalate pack would deny with a visible governor BLOCK on every
    supply-chain desk — and the live head carries one grant anyway. The train
    neither ships nor removes it (the PUT base is the live head), but it must
    not be invisible either.
    """
    tree = _tree()
    tree["action_packs"] = []
    assert structural_diff(_live(), tree) == [
        "action_packs: 0 entr(ies) in tree, 1 live"
    ]


#: Desks whose synthesis call went back to thinking "off" on the first live
#: measurement (3af2e339; test_thinking_seats.MEASURED_OFF).
_THINKING_MEASURED_OFF = frozenset({"proliferation_watch"})


@pytest.mark.parametrize("unit", UNITS)
def test_only_the_prompt_changed_against_the_pre_flip_descriptor(unit: str) -> None:
    """The tree half of "no other descriptor field changed".

    Reads the pre-flip descriptor out of git rather than trusting the diff to
    have been eyeballed, and compares every field except the prompt.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        # Not an infra gate to escalate under C-1 strict mode: the release
        # gate's containerized test image (legba/legba-test, built FROM the
        # production runtime image) never ships git — that image is
        # deliberately minimal (docker/Dockerfile.runtime's final stage
        # installs only libsodium23/libpq5/curl). The host nightly suite runs
        # the same file with git on PATH and keeps enforcing this check; this
        # is the same "not present in this checkout" idiom as the SHA-missing
        # skip below, one prerequisite earlier.
        pytest.skip(
            "git not on PATH — containerized gate; the host nightly enforces "
            "this check"
        )

    rel = f"descriptors/analyst_{unit}.yaml"
    proc = subprocess.run(
        ["git", "show", f"d1bbb519:{rel}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip("pre-flip base d1bbb519 not present in this checkout")

    before = yaml.safe_load(proc.stdout)
    after = _doc(unit)
    before["method"].pop("system_prompt")
    after["method"].pop("system_prompt")
    # The owner label was renamed across every definition after the flip; it
    # is not part of what this comparison guards.
    before.get("identity", {}).pop("owner", None)
    after.get("identity", {}).pop("owner", None)
    if unit == "disruption_status":
        # 3bd55d5c (2026-08-21, post-flip): the tree DELIBERATELY caught up to
        # the live head's escalate_finding grant — the one structural drift the
        # flip train reported. That catch-up is the fix, not a regression.
        before["action_packs"] = [{"pack_id": "escalate_finding"}]
    # REGISTER-1g (2026-08-29, post-flip): every unit in this kit is
    # ``kind: inline_target``, so ``actor_substrate_slice`` already hands it the
    # desk's OPEN SITUATION REGISTER as a citable [N] block carrying the H1
    # render repair (evidence age, NEVER/STALE labels, the self-corroboration
    # rule). The ``situations`` grounding SOURCE produced a SECOND, UNGUARDED
    # copy of the same frames in the same context window — no evidence age, no
    # labels, no rule — which is the M-1 render H1 was built to close, ten
    # prompts wide. Dropping the source is a DEDUPE: the register still reaches
    # every one of these desks, by the kind rather than by the descriptor.
    #
    # Normalized on the BEFORE side, the same way the disruption_status
    # action-pack catch-up above is, so this pin keeps asserting "nothing ELSE
    # moved" instead of being weakened to ignore grounding.
    _pre_flip_sources = before.get("grounding", {}).get("sources")
    if isinstance(_pre_flip_sources, list) and "situations" in _pre_flip_sources:
        before["grounding"]["sources"] = [
            s for s in _pre_flip_sources if s != "situations"
        ]
    # D4a (2026-08-30, post-flip): judge_sample_rate 0.10 -> 1.0 fleet-wide —
    # the 0.10 was a budget-era artifact; the sampling hash was gating
    # composition input 7.6x by coin flip (R3 mech census) and the judge plane
    # measured idle. Normalized on the BEFORE side like the two carve-outs
    # above, so this pin keeps asserting "nothing ELSE moved".
    _pre_flip_opts = before.get("method", {}).get("options")
    if isinstance(_pre_flip_opts, dict) and _pre_flip_opts.get("judge_sample_rate") == 0.10:
        _pre_flip_opts["judge_sample_rate"] = 1.0
    # 2026-09-17 (post-flip, house rule): budget_tokens_per_day is 0 on EVERY
    # descriptor (no token budgets). The pre-flip base carried 30,000,000; the
    # tree is right and this pin never learned the change (it fails only on
    # the host — the container gate has no git and skips). Normalized on the
    # BEFORE side like the carve-outs above.
    if "budget_tokens_per_day" in before.get("method", {}):
        before["method"]["budget_tokens_per_day"] = 0
    # T-2 (2026-11-14, post-flip): the desks think on their synthesis call and
    # not on their gather rounds. Normalized on the BEFORE side like the
    # carve-outs above, so this pin keeps asserting "nothing ELSE moved".
    # 3af2e339 (2026-11-15): proliferation_watch's synthesis call measured
    # three to four times slower with the verifier score flat, so it went back
    # to "off" (asserted by test_thinking_seats.MEASURED_OFF).
    before["method"].setdefault("llm", {})["thinking"] = (
        "off" if unit in _THINKING_MEASURED_OFF else "on"
    )
    before["method"]["gather"] = {"thinking": "off"}
    # F-3 (2026-09-03, post-flip): method.bounded_question is a wholly NEW field
    # — the desk's bounded question, previously implicit only in system_prompt's
    # own "BOUNDED QUESTION —" section, made explicit so the assembly's block
    # header can render it (DEMOTION_D1_SPEC §7 F-3). Unlike the three
    # carve-outs above (which normalize an already-landed VALUE change), this
    # normalizes the field's very PRESENCE: the pre-flip descriptor never had
    # it, so copy the tree's landed value onto the before side to keep this
    # pin asserting "nothing ELSE moved" rather than being weakened to ignore
    # the field. The content of the value is asserted by
    # test_bounded_question_descriptor.py, not by this pin.
    _after_bq = after.get("method", {}).get("bounded_question")
    if "bounded_question" not in before.get("method", {}) and _after_bq is not None:
        before["method"]["bounded_question"] = _after_bq
    # slice_since_watermark is a post-flip slice-reader knob on seven units,
    # asserted in test_slice_coverage.py.
    after.get("method", {}).get("options", {}).pop("slice_since_watermark", None)
    # Three more post-flip option knobs this pin never learned (each has its
    # own test): level_indicators (energy_security, military_posture),
    # offer_events (escalation) and offer_history (energy_security).
    for _knob in ("level_indicators", "offer_events", "offer_history"):
        after.get("method", {}).get("options", {}).pop(_knob, None)
    # Post-flip: method.llm.max_tokens left every unit definition. It was never
    # sent and limited nothing; each handler sends the model's own maximum
    # (asserted in test_model_token_limits.py). Normalized on the BEFORE side.
    before.get("method", {}).get("llm", {}).pop("max_tokens", None)
    assert "max_tokens" not in after.get("method", {}).get("llm", {})
    assert before == after
