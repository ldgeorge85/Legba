# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G1 — the correctness grader, from its pure pieces to the REAL binding path.

THE FIXTURES ARE RECORDED, NOT INVENTED. Every claim text, every excluded span,
every reference development and every graded verdict below was taken from the
Program 1 step-2 run of 2026-09-16 over Israel (``planning/PROGRAM1_2026-09-16/
step2/``: ``segmentation_IL.json``, ``claims_IL.json``, ``ref_IL_A.json``,
``scoring/step2_cal_F{0,2,3}.jsonl``). That matters because the whole claim this
port makes is that a claim the hand run kept is a claim the job keeps, and a
span the hand run excluded is excluded here under the SAME NAMED REASON. A
fixture somebody made up could not test that. ``planning/`` is gitignored, so
the rows are inlined here rather than read from it — this file is the committed
record of them.

The live half (``test_the_real_binding_path_*``) drives
``deterministic.run_method`` with ``options.sub_handler='correctness_grader'``,
never the handler function directly: the dispatcher, the option merge and the
deps bundle are part of what has to work.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts import deterministic
from legba.data.analysts.deterministic_handlers import (
    _correctness_adjudicate as ADJ,
)
from legba.data.analysts.deterministic_handlers import (
    _correctness_calibration as CAL,
)
from legba.data.analysts.deterministic_handlers import _correctness_grade as GRADE
from legba.data.analysts.deterministic_handlers import _correctness_packet as PACKET
from legba.data.analysts.deterministic_handlers import _correctness_segment as SEG
from legba.data.analysts.deterministic_handlers import correctness_grader as CG
from legba.data.analysts.deterministic_handlers._correctness_rubric import (
    LABELS,
    RUBRIC_SHA256,
    RUBRIC_TEXT,
)
from legba.data.config import PostgresConfig
from legba.runtime.analyst_method import AnalystMethodResult

# ---------------------------------------------------------------------------
# RECORDED FIXTURES — Israel, as-of 2026-09-16T19:30:00+00:00
# ---------------------------------------------------------------------------

#: Two of the 33 developments from ``step2/ref_IL_A.json``, reduced to the five
#: fields the packet carries.
REF_DEVELOPMENTS = [
    {
        "item_id": "RD-IL-A-1",
        "summary": (
            "Israel's 27 October general election took formal shape inside the "
            "window: 38 candidate lists were filed with the Central Elections "
            "Committee by the 10 p.m. deadline on Tuesday 8 September, the last "
            "of them Likud, Bezalel Smotrich's Religious Zionist Party and the "
            "Arab-majority Joint List."
        ),
        "decisive_span": (
            "Israeli political parties have submitted their slates for the "
            "Knesset elections on October 27."
        ),
        "outlet": "Al Jazeera",
        "publish_date": "2026-09-09",
    },
    {
        "item_id": "RD-IL-A-2",
        "summary": (
            "Five polls published in the days after the candidate lists closed "
            "showed a fragmented race with no clear bloc advantage."
        ),
        "decisive_span": (
            "Five polls published after the Knesset candidate lists closed show "
            "a highly fragmented political landscape, with no clear advantage "
            "for either major bloc, Maariv reported on Sunday."
        ),
        "outlet": "The Jerusalem Post",
        "publish_date": "2026-09-13",
    },
]

#: The reference's own band table, verbatim.
REF_BANDS = {
    "leadership_transition": "high", "energy_security": "elevated",
    "escalation": "high", "narrative_coordination": "high",
    "internal_stability": "elevated", "military_posture": "high",
    "economic_coercion": "elevated", "proliferation_watch": "high",
}

REFERENCE = {
    "header": {
        "round": "PROGRAM1_STEP2", "country": "IL",
        "t0": "2026-09-16T19:30:00+00:00",
        "window": "2026-09-02T19:30:00+00:00 -> 2026-09-16T19:30:00+00:00",
        "built_at": "2026-09-16T21:40:00+00:00", "builder": "opus-web-lane",
        "grader_model": "claude-opus-5[1m]",
    },
    "ref_bands": REF_BANDS,
    "ref_developments": [
        dict(dev, source_url=f"https://example.test/{dev['item_id']}",
             source_tier=3, significance="major", hindsight=False,
             dimension="leadership_transition")
        for dev in REF_DEVELOPMENTS
    ],
    "gaps": ["Haredi conscription: no dated in-window event surfaced."],
}

#: Excluded spans, one per reason the live IL run actually produced, each with
#: the reason the hand run recorded for it (``step2/segmentation_IL.json``).
RECORDED_EXCLUSIONS: list[tuple[str, str]] = [
    (
        "machine_line_registered_shape",
        "· military_posture · country_watch_il · 2026-09-16T04:59:19 · "
        "severity high · 2 sources",
    ),
    (
        "read_bookkeeping_not_world:pairs examined",
        "- no conflicting pair was detected among the 8 shown blocks "
        "(28 pairs examined; BLUF-grain; ambivalent pairs declined).",
    ),
    (
        "round_notice",
        "[SECTION WITHHELD BY THE ROUND — this read's own bookkeeping about "
        "which inputs it did and did not use is withheld from grading by "
        "design, exactly as its evidence trail is.",
    ),
]

#: A recorded composition claim: its raw span and the text the grader saw.
RECORDED_COMPOSITION_RAW = (
    "> A change in Israel’s premiership is unlikely within the next two "
    "weeks, as coalition fractures test Prime Minister Benjamin Netanyahu but "
    "no snap election or no‑confidence vote is evident [[ref:4]]."
)
RECORDED_COMPOSITION_TEXT = (
    "A change in Israel’s premiership is unlikely within the next two "
    "weeks, as coalition fractures test Prime Minister Benjamin Netanyahu but "
    "no snap election or no‑confidence vote is evident."
)
RECORDED_COMPOSITION_RAW_2 = (
    "> Israel’s ruling coalition is still fracturing, keeping coup risk "
    "elevated[14] [[ref:3]]."
)
RECORDED_COMPOSITION_TEXT_2 = (
    "Israel’s ruling coalition is still fracturing, keeping coup risk "
    "elevated."
)

#: A recorded desk claim (internal_stability), unmarked.
RECORDED_DESK_TEXT = (
    "These intra‑elite disputes raise the base probability of a "
    "coup‑type seizure despite Israel’s traditionally low "
    "coup‑vulnerability, because they erode regime cohesion and create "
    "openings for rival factions."
)

#: The three families' recorded verdicts on CR-078760a8 — the live split.
RECORDED_SPLIT = {"F0": "silent", "F2": "contradicts", "F3": "contains"}
#: CR-82212f8b — a real 2-of-3 majority carrying one UNPARSEABLE.
RECORDED_MAJORITY = {"F0": "contains", "F2": "UNPARSEABLE", "F3": "contains"}

DIMENSIONS = frozenset({
    "leadership_transition", "energy_security", "escalation",
    "narrative_coordination", "internal_stability", "military_posture",
    "economic_coercion", "proliferation_watch",
})


# ---------------------------------------------------------------------------
# The rubric is the instrument's identity
# ---------------------------------------------------------------------------


def test_the_rubric_in_the_tree_is_the_one_the_gate_passed():
    """The digest pinned in code is the digest VERDICT_P1v4 names.

    If this fails the rubric file moved, and every ``unit_correctness`` row
    already written under ``1b51d7f5…`` describes a different instrument from
    the one that would run next.
    """
    assert RUBRIC_SHA256 == (
        "1b51d7f5187c7f93af2e2cccc0775e21ab7efc41678bc28c4a2dcbe287fe7d8c"
    )
    assert "# ANNEX C v4" in RUBRIC_TEXT


def test_the_labels_are_read_out_of_the_rubric_not_hardcoded():
    assert LABELS == ("contains", "contradicts", "silent")


def test_the_span_policy_is_read_out_of_the_rubric():
    """Which label needs a span is the RUBRIC's statement, not this code's."""
    policy = GRADE.span_policy()
    assert policy["require"] == frozenset({"contains", "contradicts"})
    assert policy["forbid"] == frozenset({"silent"})


def test_a_rubric_whose_two_statements_disagree_is_refused():
    from legba.data.analysts.deterministic_handlers._correctness_rubric import (
        RubricDigestError,
        extract_labels,
    )

    with pytest.raises(RubricDigestError):
        extract_labels(
            "## The three labels\n- **a** — x\n- **b** — y\n- **c** — z\n"
            '\n## Output contract\n{"verdict": "a|b|DIFFERENT"}\n'
        )


# ---------------------------------------------------------------------------
# Segmentation + exclusions, against the recorded spans
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason,text", RECORDED_EXCLUSIONS)
def test_recorded_exclusions_keep_their_recorded_reason(reason, text):
    """Each span the live IL run excluded is excluded here, under ITS OWN name.

    Not merely "excluded": the reason is the audit. A rule that dropped a
    machine line by calling it short would pass a count check and lose the only
    thing an operator can review.
    """
    assert SEG.exclusion_reason(text, DIMENSIONS) == reason


def test_an_ordinary_desk_claim_survives_every_rule():
    assert SEG.exclusion_reason(RECORDED_DESK_TEXT, DIMENSIONS) is None


def test_a_quoted_composition_claim_survives_every_rule():
    assert SEG.exclusion_reason(RECORDED_COMPOSITION_RAW, DIMENSIONS) is None


def test_a_roster_line_is_excluded_but_an_absence_claim_is_kept():
    """VERDICT_P1v4's observation, and the line that must survive it.

    A family graded the tail of a "Verified reads: …" roster — scaffolding, not
    a claim. But an absence claim that NAMES dimensions is a CLAIM under ANNEX C
    v4 rule 3, and it is exactly the class the rubric was revised to grade. A
    roster rule that swallowed it would delete the claims this job exists to
    measure.
    """
    assert SEG.exclusion_reason(
        "Verified reads: leadership_transition [[ref:2]], energy_security "
        "[[ref:1]], escalation [[ref:3]].", DIMENSIONS,
    ) == "roster_enumeration_prefix"
    assert SEG.exclusion_reason(
        "All seven verified reads are listed above with their markers.",
        DIMENSIONS,
    ) == "roster_enumeration_all_verified_reads"
    assert SEG.exclusion_reason(
        "leadership_transition [[ref:4]] and energy_security [[ref:1]] and "
        "escalation [[ref:3]]", DIMENSIONS,
    ) == "roster_enumeration_dimension_list"
    # THE LINE THAT MUST SURVIVE.
    assert SEG.exclusion_reason(
        "leadership_transition [[ref:4]] and energy_security [[ref:1]] showed "
        "no material change; proliferation_watch [[ref:12]] is below "
        "verification floor.", DIMENSIONS,
    ) is None


def test_markers_and_decoration_are_stripped_exactly_as_recorded():
    text, markers = SEG.strip_markers(RECORDED_COMPOSITION_RAW)
    assert text == RECORDED_COMPOSITION_TEXT
    assert markers == ["[[ref:4]]"]
    text2, markers2 = SEG.strip_markers(RECORDED_COMPOSITION_RAW_2)
    assert text2 == RECORDED_COMPOSITION_TEXT_2
    assert markers2 == ["[14]", "[[ref:3]]"]


def test_a_lone_narrow_no_break_space_inside_a_word_survives():
    """The renders use U+202F as a digit separator. Rewriting it would EDIT the
    claim, not clean it."""
    raw = "**BLUF:** Israel integrated the SPICE 1000 munition onto F-35s."
    assert SEG.strip_markers(raw)[0] == raw


def test_the_two_grains_can_never_collide_on_identical_text():
    desk = SEG.atom_id(SEG.GRAIN_DESK, "escalation", "t", "x" * 60)
    comp = SEG.atom_id(SEG.GRAIN_COMPOSITION, "escalation", "t", "x" * 60)
    assert desk.startswith("DR-") and comp.startswith("CR-")
    assert desk[3:] == comp[3:] and desk != comp


def test_the_atom_id_is_stable_under_recasing_and_rewrapping():
    """A re-render that only re-cases or re-wraps a span must not mint a new
    atom, or the per-claim ledger stops joining to itself across runs."""
    assert SEG.atom_id(SEG.GRAIN_DESK, "a", "t", "  The  Ministry approved. ") \
        == SEG.atom_id(SEG.GRAIN_DESK, "a", "t", "the ministry approved.")


def test_every_span_is_accounted_for_exactly_once():
    def seg(body):
        return [s.strip() for s in (body or "").split("\n") if s.strip()]

    def judge(span):
        return not span.strip().startswith("#")

    head = {
        "analyst_id": "internal_stability", "grain": SEG.GRAIN_DESK,
        "output_id": "h-1", "created_at": "2026-09-16T16:05:50+00:00",
        "grader_body": "\n".join([
            "## What changed",
            RECORDED_DESK_TEXT,
            RECORDED_EXCLUSIONS[0][1],
            "Short.",
        ]),
    }
    out = SEG.segment_head(head, seg, judge, DIMENSIONS)
    assert out["n_kept"] + out["n_excluded"] == out["n_spans"]
    assert out["n_kept"] == 1
    assert {e["reason"] for e in out["excluded"]} >= {
        "not_judgeable_span", "machine_line_registered_shape",
    }
    assert all(e.get("text") is not None for e in out["excluded"])


def test_an_atom_id_collision_stops_the_run():
    def seg(body):
        return [body]

    head = {
        "analyst_id": "escalation", "grain": SEG.GRAIN_DESK,
        "output_id": "h-1", "created_at": "t", "grader_body": RECORDED_DESK_TEXT,
    }
    with pytest.raises(SEG.SegmentationError):
        SEG.segment_all(
            [head, dict(head, output_id="h-2")], seg, lambda s: True, DIMENSIONS
        )


def test_the_redactor_cuts_the_telemetry_and_says_so():
    body = "\n".join([
        "## The record",
        "> A thing happened [[ref:1]] · desk · Israel · severity high · "
        "verify 0.36 · cited mass 3.85 · 6 sources",
        "## Not carried",
        "- three blocks were dropped",
        "## Next",
        "- something else",
    ])
    out, cuts = SEG.redact_render(body)
    assert "verify 0.36" not in out and "cited mass" not in out
    assert cuts["attribution_fields"] == 2
    assert cuts["sections"] == ["## Not carried"]
    assert SEG.REDACTION_NOTICE in out
    assert "three blocks were dropped" not in out


# ---------------------------------------------------------------------------
# The packet
# ---------------------------------------------------------------------------


def _claims() -> list[dict[str, Any]]:
    return [
        {"id": "CR-078760a8", "grain": "composition",
         "analyst_id": "country_composition", "text": RECORDED_COMPOSITION_TEXT},
        {"id": "DR-002e675d", "grain": "desk",
         "analyst_id": "internal_stability", "text": RECORDED_DESK_TEXT},
        {"id": "CR-2ef7e057", "grain": "composition",
         "analyst_id": "country_composition",
         "text": RECORDED_COMPOSITION_TEXT_2},
    ]


def test_the_reference_reduction_is_a_whitelist():
    """Exactly five fields per development survive, and the round apparatus does
    not. A blacklist would let a field nobody anticipated ride in on a reference
    written by another lane."""
    reduced = PACKET.reduce_reference(REFERENCE, "IL")
    for dev in reduced["ref_developments"]:
        assert sorted(dev) == sorted(PACKET.REF_DEV_FIELDS)
    blob = json.dumps(reduced, ensure_ascii=False)
    for dropped in ("source_url", "source_tier", "significance", "hindsight",
                    "dimension", "grader_model", "PROGRAM1_STEP2", "gaps"):
        assert dropped not in blob
    assert reduced["ref_bands"] == REF_BANDS


def test_a_missing_item_id_is_derived_by_the_recorded_convention():
    reduced = PACKET.reduce_reference(
        {"ref_developments": [{"summary": "x"}]}, "IL"
    )
    assert reduced["ref_developments"][0]["item_id"] == "RD-IL-A-1"
    assert PACKET.reduce_reference(
        {"ref_developments": [{"summary": "x"}]}, "IL", "B"
    )["ref_developments"][0]["item_id"] == "RD-IL-B-1"


def test_the_packet_is_byte_identical_and_ordered_by_id():
    """ONE packet, rendered once. The families are handed the SAME bytes, which
    is what makes the only remaining variable the family."""
    reduced = PACKET.reduce_reference(REFERENCE, "IL")
    packet = PACKET.build_packet(_claims(), reduced, packet_kind="cg_IL")
    assert [i["p1_id"] for i in packet["items"]] == [
        "CR-078760a8", "CR-2ef7e057", "DR-002e675d",
    ]
    assert all(i["reference"] is reduced for i in packet["items"])
    assert all(sorted(i) == ["assertion", "p1_id", "reference"]
               for i in packet["items"])
    # Rendering it twice is byte-identical; that IS the guarantee the sha means.
    assert PACKET.packet_text(packet) == PACKET.packet_text(packet)
    rebuilt = PACKET.build_packet(
        list(reversed(_claims())), reduced, packet_kind="cg_IL"
    )
    assert PACKET.packet_sha256(rebuilt) == PACKET.packet_sha256(packet)


def test_a_different_reference_moves_the_packet_sha():
    """The sha is the join between a published share and the evidence it rests
    on: a reference swap MUST move it."""
    a = PACKET.build_packet(
        _claims(), PACKET.reduce_reference(REFERENCE, "IL"), packet_kind="cg_IL"
    )
    other = dict(REFERENCE, ref_developments=REFERENCE["ref_developments"][:1])
    b = PACKET.build_packet(
        _claims(), PACKET.reduce_reference(other, "IL"), packet_kind="cg_IL"
    )
    assert PACKET.packet_sha256(a) != PACKET.packet_sha256(b)


def test_the_packet_carries_no_grain_no_analyst_and_no_head_id():
    packet = PACKET.build_packet(
        _claims(), PACKET.reduce_reference(REFERENCE, "IL"), packet_kind="cg_IL"
    )
    blob = json.dumps(packet, ensure_ascii=False)
    assert '"grain"' not in blob
    assert '"analyst_id"' not in blob
    assert '"head_id"' not in blob


def test_the_recorded_packet_is_leak_clean():
    packet = PACKET.build_packet(
        _claims(), PACKET.reduce_reference(REFERENCE, "IL"), packet_kind="cg_IL"
    )
    assert PACKET.scan_packet(packet) == []


def test_the_leak_scan_catches_a_payload_key_and_a_machine_identifier():
    """And does NOT convict an analyst's ordinary nouns — the R4 lesson: a bare
    substring test on 'regime' fired on all ten live packets, every hit a
    political noun."""
    assert PACKET.leak_scan('{"regime": "assembly"}')
    assert PACKET.leak_scan("cited_mass 3.85")
    assert PACKET.leak_scan("the tariff regime eroded elite-regime cohesion") == []


def test_an_id_outside_the_two_namespaces_is_refused():
    with pytest.raises(PACKET.PacketError):
        PACKET.build_packet(
            [{"id": "P1-deadbeef", "text": "x" * 60}],
            PACKET.reduce_reference(REFERENCE, "IL"), packet_kind="cg_IL",
        )


def test_an_empty_assertion_and_a_duplicate_id_are_refused():
    reduced = PACKET.reduce_reference(REFERENCE, "IL")
    with pytest.raises(PACKET.PacketError):
        PACKET.build_packet(
            [{"id": "DR-00000001", "text": "   "}], reduced, packet_kind="cg_IL"
        )
    claim = _claims()[0]
    with pytest.raises(PACKET.PacketError):
        PACKET.build_packet([claim, dict(claim)], reduced, packet_kind="cg_IL")


# ---------------------------------------------------------------------------
# The span check
# ---------------------------------------------------------------------------


def _reduced_reference() -> dict[str, Any]:
    return PACKET.reduce_reference(REFERENCE, "IL")


def test_a_verbatim_span_from_a_recorded_development_is_accepted():
    policy = GRADE.span_policy()
    ref = _reduced_reference()
    assert GRADE.span_check(
        "contains",
        "38 candidate lists were filed with the Central Elections Committee",
        ref, policy,
    )[0]
    assert GRADE.span_check(
        "contradicts",
        "no clear advantage for either major bloc", ref, policy,
    )[0]


def test_a_paraphrase_is_rejected_as_not_verbatim():
    ok, code, msg = GRADE.span_check(
        "contains", "the parties handed in their lists", _reduced_reference(),
        GRADE.span_policy(),
    )
    assert not ok and code == "not_verbatim"
    assert "not verbatim from any development" in msg


def test_a_band_table_span_is_named_as_the_band_table():
    policy = GRADE.span_policy()
    ref = _reduced_reference()
    for span in ("escalation: 'high'", '"escalation": "high"',
                 "escalation high", "internal_stability: elevated"):
        ok, code, msg = GRADE.span_check("contradicts", span, ref, policy)
        assert not ok and code == "band_table", span
        assert "band table" in msg


def test_span_emptiness_is_policed_in_both_directions():
    policy = GRADE.span_policy()
    ref = _reduced_reference()
    ok, code, _ = GRADE.span_check("contains", "", ref, policy)
    assert not ok and code == "empty"
    assert GRADE.span_check("silent", "", ref, policy)[0]
    assert GRADE.span_check("silent", None, ref, policy)[0]
    ok, code, msg = GRADE.span_check(
        "silent", "38 candidate lists were filed", ref, policy
    )
    assert not ok and code == "nonempty_on_empty_label" and "EMPTY" in msg


def test_normalisation_straightens_quotes_but_keeps_case():
    assert GRADE.normalize_span("Mali’s “deal”") == "Mali's \"deal\""
    assert GRADE.normalize_span("  a   b\n\n c ") == "a b c"
    assert GRADE.normalize_span("Deal") != GRADE.normalize_span("deal")
    assert GRADE.normalize_span(None) == "" and GRADE.normalize_span(7) == ""


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------


_GOOD_REPLY = json.dumps({
    "verdict": "contains", "core_claim": "c", "decisive_span": "s", "reason": "r",
})


@pytest.mark.parametrize("raw", [
    _GOOD_REPLY,
    "\n\n  " + _GOOD_REPLY + "  \n",
    "```json\n" + _GOOD_REPLY + "\n```",
    "Here you go:\n" + _GOOD_REPLY + "\nHope that helps.",
    _GOOD_REPLY.replace("{", "｛").replace("}", "｝"),
    _GOOD_REPLY + "【3】【4】",
])
def test_a_reply_parses_through_the_house_artefacts(raw):
    """Full-width braces and 【N】 citation markers are a known core-plane
    artefact; failing on them would measure the font, not the grader."""
    obj = GRADE.extract_json_object(raw)
    assert obj and obj["verdict"] == "contains"


def test_garbage_parses_to_none_and_is_never_guessed():
    assert GRADE.extract_json_object("I cannot comply with that request.") is None
    assert GRADE.extract_json_object("") is None
    _, why = GRADE.validate({"verdict": "accurate"}, LABELS)
    assert "not one of the rubric's three labels" in why[0]
    _, why = GRADE.validate({"verdict": "Contains"}, LABELS)
    assert why, "the label check is exact, never case-folded"


# ---------------------------------------------------------------------------
# grade_one — the two-attempt protocol
# ---------------------------------------------------------------------------


class _Usage:
    def __init__(self, cost=0.0, tin=10, tout=5, model="m"):
        self.cost_estimate_usd = cost
        self.prompt_tokens = tin
        self.completion_tokens = tout
        self.model = model


class _Response:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage or _Usage()


class _ScriptedLLM:
    """Replies in order; records every call. No network, no key."""

    def __init__(self, *replies, cost=0.0):
        self._replies = list(replies)
        self._cost = cost
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        reply = self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return _Response(reply, _Usage(cost=self._cost))


def _reply(verdict, span):
    return json.dumps({
        "verdict": verdict, "core_claim": "c", "decisive_span": span,
        "reason": "r",
    })


def _item():
    return {
        "p1_id": "CR-078760a8",
        "assertion": RECORDED_COMPOSITION_TEXT,
        "reference": _reduced_reference(),
    }


_VERBATIM = "38 candidate lists were filed with the Central Elections Committee"


@pytest.mark.asyncio
async def test_a_good_reply_is_one_call_with_no_retry():
    llm = _ScriptedLLM(_reply("contains", _VERBATIM))
    row = await GRADE.grade_one(_item(), "F0", llm)
    assert len(llm.calls) == 1
    assert row["verdict"] == "contains"
    assert row["retried"] is False
    assert row["span_unverified"] is False


@pytest.mark.asyncio
async def test_max_tokens_is_never_sent_to_any_family():
    """HARD house rule for the core plane, and the calibration sent none to
    OpenRouter either — sending one here would change the instrument."""
    for family in GRADE.FAMILY_ORDER:
        llm = _ScriptedLLM(_reply("contains", _VERBATIM))
        await GRADE.grade_one(_item(), family, llm)
        assert "max_tokens" not in llm.calls[0]["kwargs"]
        assert llm.calls[0]["kwargs"]["temperature"] == 1.0  # fleet rule: 1.0 everywhere (operator)


@pytest.mark.asyncio
async def test_a_band_table_span_is_retried_once_and_the_fix_is_verified():
    llm = _ScriptedLLM(
        _reply("contradicts", "escalation: 'high'"),
        _reply("contradicts", _VERBATIM),
    )
    row = await GRADE.grade_one(_item(), "F0", llm)
    assert len(llm.calls) == 2
    assert row["verdict"] == "contradicts"
    assert row["retried"] is True and row["span_unverified"] is False
    # The retry names the SPAN failure, not the label.
    assert "FIX THE SPAN" in llm.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_a_span_that_fails_twice_leaves_the_label_standing_flagged():
    """Amendment 2. A span quibble must never cost a label the grader actually
    gave: the scorer gates on labels and reports unverified spans beside them."""
    llm = _ScriptedLLM(
        _reply("contradicts", "escalation: 'high'"),
        _reply("contradicts", "internal_stability: elevated"),
    )
    row = await GRADE.grade_one(_item(), "F0", llm)
    assert row["verdict"] == "contradicts"
    assert row["span_unverified"] is True
    assert row["span_failure"] == "band_table"


@pytest.mark.asyncio
async def test_a_verdict_outside_the_label_set_is_unparseable_after_one_retry():
    llm = _ScriptedLLM(_reply("accurate", "x"), _reply("also-wrong", "x"))
    row = await GRADE.grade_one(_item(), "F0", llm)
    assert len(llm.calls) == 2
    assert row["verdict"] == GRADE.UNPARSEABLE
    assert "must be exactly one of" in llm.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_a_valid_label_is_never_thrown_away_by_a_garbage_retry():
    llm = _ScriptedLLM(
        _reply("contains", "a paraphrase"), "I changed my mind."
    )
    row = await GRADE.grade_one(_item(), "F0", llm)
    assert row["verdict"] == "contains" and row["span_unverified"] is True


@pytest.mark.asyncio
async def test_a_transport_failure_is_retried_not_scolded():
    llm = _ScriptedLLM(RuntimeError("connection reset"),
                       _reply("contains", _VERBATIM))
    row = await GRADE.grade_one(_item(), "F0", llm)
    assert row["verdict"] == "contains"
    # The second call is a plain retry of the same request: no assistant turn,
    # no corrective user turn was appended.
    assert len(llm.calls[1]["messages"]) == len(llm.calls[0]["messages"])


@pytest.mark.asyncio
async def test_a_dead_family_is_recorded_not_raised():
    llm = _ScriptedLLM(RuntimeError("502"), RuntimeError("502"))
    row = await GRADE.grade_one(_item(), "F2", llm)
    assert row["verdict"] == GRADE.UNPARSEABLE
    assert "502" in (row["reason"] or "")


# ---------------------------------------------------------------------------
# Cost + the ceiling
# ---------------------------------------------------------------------------


def test_the_ceiling_defaults_to_zero_and_a_typo_reads_as_zero(monkeypatch):
    monkeypatch.delenv(GRADE.CEILING_ENV, raising=False)
    assert GRADE.daily_ceiling_usd() == 0.0
    monkeypatch.setenv(GRADE.CEILING_ENV, "not-a-number")
    assert GRADE.daily_ceiling_usd() == 0.0, "a typo must fail toward not spending"
    monkeypatch.setenv(GRADE.CEILING_ENV, "-5")
    assert GRADE.daily_ceiling_usd() == 0.0
    monkeypatch.setenv(GRADE.CEILING_ENV, "0.50")
    assert GRADE.daily_ceiling_usd() == 0.5


def test_the_guard_stops_before_the_call_that_would_breach():
    assert not GRADE.would_breach(0.10, 0.25, 0.01)
    assert GRADE.would_breach(0.245, 0.25, 0.01)
    assert not GRADE.would_breach(0.24, 0.25, 0.01), "exactly hitting is allowed"
    assert not GRADE.would_breach(0.0, 0.25, 0.0)
    assert GRADE.would_breach(0.0, 0.0, 0.0015), "a $0 ceiling admits no paid call"


def test_a_paid_call_that_reports_no_cost_is_priced_from_the_registry():
    """An unpriced component would otherwise make a paid run look free, and a
    ceiling that cannot see spend is not a ceiling."""
    cost, source = GRADE.call_cost_usd("F2", _Usage(cost=0.0, tin=7000, tout=300))
    assert cost > 0 and source == "registered_price_fallback"
    cost, source = GRADE.call_cost_usd("F2", _Usage(cost=0.004))
    assert cost == 0.004 and source == "handler_usage"
    cost, source = GRADE.call_cost_usd("F0", _Usage(cost=9.99, tin=7000))
    assert cost == 0.0 and source == "core_plane_zero"


@pytest.mark.asyncio
async def test_at_a_zero_ceiling_the_paid_families_are_never_called():
    """The operator's DEFAULT path. F2/F3 must not be called, attempted, or
    estimated — a $0 ceiling that still made one call is not $0."""
    f0 = _ScriptedLLM(_reply("contains", _VERBATIM))
    f2 = _ScriptedLLM(_reply("contains", _VERBATIM), cost=0.01)
    f3 = _ScriptedLLM(_reply("contains", _VERBATIM), cost=0.01)
    items = [_item()]
    by_family, ledger = await CG._grade_claims(
        items, {"F0": f0, "F2": f2, "F3": f3},
        ceiling_usd=0.0, spent_usd=0.0,
        system_message=GRADE.build_system_message(),
        policy=GRADE.span_policy(),
    )
    assert f2.calls == [] and f3.calls == []
    assert set(by_family) == {"F0"}
    assert ledger["run_cost_usd"] == 0.0
    assert "F2" in ledger["skipped_families"] and "F3" in ledger["skipped_families"]


@pytest.mark.asyncio
async def test_the_paid_families_are_never_called_past_the_ceiling():
    """Two claims, a ceiling that affords one paid call. The guard fires BEFORE
    the second, not after it."""
    items = [
        dict(_item(), p1_id="CR-078760a8"),
        dict(_item(), p1_id="CR-2ef7e057"),
    ]
    f0 = _ScriptedLLM(_reply("contains", _VERBATIM))
    f2 = _ScriptedLLM(_reply("contains", _VERBATIM), cost=0.02)
    by_family, ledger = await CG._grade_claims(
        items, {"F0": f0, "F2": f2},
        ceiling_usd=0.03, spent_usd=0.0,
        system_message=GRADE.build_system_message(),
        policy=GRADE.span_policy(),
    )
    assert len(f0.calls) == 2, "the $0 family grades every claim"
    assert len(f2.calls) == 1, "the paid family stopped before the breach"
    assert ledger["capped_out"] is True
    assert ledger["capped_before"] == "F2:CR-2ef7e057"
    assert ledger["run_cost_usd"] == 0.02


@pytest.mark.asyncio
async def test_the_triage_sends_only_non_silent_claims_to_the_paid_families():
    """F2/F3 grade only what F0 did not call `silent`. A silent claim cannot
    move the correctness share, so a second opinion on it spends money to
    confirm an absence."""
    items = [
        dict(_item(), p1_id="CR-078760a8"),
        dict(_item(), p1_id="CR-2ef7e057"),
    ]
    f0 = _ScriptedLLM(_reply("silent", ""), _reply("contains", _VERBATIM))
    f2 = _ScriptedLLM(_reply("contains", _VERBATIM), cost=0.001)
    by_family, ledger = await CG._grade_claims(
        items, {"F0": f0, "F2": f2},
        ceiling_usd=1.0, spent_usd=0.0,
        system_message=GRADE.build_system_message(),
        policy=GRADE.span_policy(),
    )
    assert len(f0.calls) == 2
    assert len(f2.calls) == 1
    assert ledger["triage"]["n_sent_to_paid_families"] == 1
    assert set(by_family["F2"]) == {"CR-2ef7e057"}


# ---------------------------------------------------------------------------
# Adjudication and the two shares
# ---------------------------------------------------------------------------


def test_the_recorded_split_is_published_as_split_never_tie_broken():
    label, n_agree, single = ADJ.adjudicate(RECORDED_SPLIT)
    assert label == ADJ.SPLIT and n_agree == 1 and single is False


def test_one_unparseable_cannot_stop_a_real_majority():
    label, n_agree, single = ADJ.adjudicate(RECORDED_MAJORITY)
    assert label == "contains" and n_agree == 2 and single is False


def test_an_unparseable_majority_is_never_promoted_to_a_label():
    label, n_agree, _ = ADJ.adjudicate(
        {"F0": GRADE.UNPARSEABLE, "F2": GRADE.UNPARSEABLE, "F3": "contains"}
    )
    assert label == ADJ.UNPARSEABLE_ADJ and n_agree == 2


def test_two_families_agreeing_is_a_majority_and_two_disagreeing_is_a_split():
    assert ADJ.adjudicate({"F0": "silent", "F2": "silent"})[:2] == ("silent", 2)
    assert ADJ.adjudicate({"F0": "contains", "F2": "silent"})[0] == ADJ.SPLIT


def test_a_lone_family_publishes_its_label_and_is_flagged_single_family():
    """The one deliberate divergence from the hand run: at a $0 ceiling every
    claim has one family, and calling them all `split` would publish nothing at
    all. The FLAG is the honesty."""
    label, n_agree, single = ADJ.adjudicate({"F0": "contains"})
    assert label == "contains" and n_agree == 1 and single is True
    label, _, single = ADJ.adjudicate({"F0": GRADE.UNPARSEABLE})
    assert label == ADJ.UNPARSEABLE_ADJ and single is True


def test_nothing_at_all_is_unparseable_not_a_crash():
    assert ADJ.adjudicate({})[0] == ADJ.UNPARSEABLE_ADJ


def test_the_two_shares_carry_the_denominators_they_rest_on():
    from collections import Counter

    s = ADJ.shares(Counter({"contains": 3, "contradicts": 1, "silent": 5,
                            ADJ.SPLIT: 1}), 10)
    assert s["correctness_share"] == 0.75 and s["correctness_n"] == 4
    assert s["coverage_share"] == 0.4 and s["coverage_n"] == 10
    assert s["split"] == 1


def test_a_unit_the_reference_never_bears_on_has_no_share_not_a_zero():
    from collections import Counter

    s = ADJ.shares(Counter({"silent": 7, ADJ.SPLIT: 3}), 10)
    assert s["correctness_share"] is None, "None, never 0.0"
    assert s["correctness_n"] == 0 and s["coverage_share"] == 0.0
    assert ADJ.shares(Counter(), 0)["coverage_share"] is None


def test_one_confirmed_claim_in_twenty_is_100_percent_at_5_percent_coverage():
    from collections import Counter

    s = ADJ.shares(Counter({"contains": 1, "silent": 19}), 20)
    assert s["correctness_share"] == 1.0 and s["coverage_share"] == 0.05


def test_score_unit_keeps_the_per_family_labels_unpooled():
    rows = [
        {"adjudicated": "contains",
         "label_by_family": {"F0": "contains", "F3": "contains"},
         "single_family": False},
        {"adjudicated": ADJ.SPLIT,
         "label_by_family": dict(RECORDED_SPLIT), "single_family": False},
        {"adjudicated": "silent", "label_by_family": {"F0": "silent"},
         "single_family": True},
    ]
    unit = ADJ.score_unit(rows, analyst_id="country_composition",
                          grain="composition")
    assert unit["n"] == 3
    assert unit["per_family_labels"] == {
        "F0": {"contains": 1, "silent": 2},
        "F2": {"contradicts": 1},
        "F3": {"contains": 2},
    }, "a family that read the unit differently is visible, never averaged away"
    assert unit["n_single_family"] == 1
    assert unit["correctness_share"] == 1.0 and unit["correctness_n"] == 1
    assert unit["coverage_n"] == 3


def test_unparseable_never_agrees_with_itself_in_the_agreement_math():
    """PREREG_P1 §6's rule, enforced in the DATA rather than by special-casing
    the shared function."""
    both_bad = ADJ.family_agreement({
        "F0": {"a": GRADE.UNPARSEABLE}, "F2": {"a": GRADE.UNPARSEABLE},
    })
    assert both_bad["pooled_rate"] == 0.0


def test_the_pairwise_floor_stops_a_rogue_family_being_averaged_away():
    labels = {f"a{i}": None for i in range(10)}
    f0 = {k: "contains" for k in labels}
    f2 = {k: "contains" for k in labels}
    f3 = {k: ("contains" if i < 3 else "silent")
          for i, k in enumerate(labels)}
    result = ADJ.family_agreement({"F0": f0, "F2": f2, "F3": f3})
    assert result["pooled_rate"] >= 0.5
    assert result["pass"] is False
    assert result["pairs_below_floor"]
    assert "PAIRWISE FLOOR BREACHED" in result["verdict"]


# ---------------------------------------------------------------------------
# The calibration interlock (G3)
# ---------------------------------------------------------------------------


_MODELS = {
    "F0": "core-120b",
    "F2": "meta-llama/llama-3.3-70b-instruct",
    "F3": "mistralai/mistral-large-2512",
}


def test_no_passing_row_refuses_to_grade():
    with pytest.raises(CAL.CalibrationRefusal) as exc:
        CAL.select_calibration([], list(_MODELS.values()))
    assert exc.value.reason == CAL.NO_CALIBRATION


def test_a_subset_of_the_calibrated_families_is_inside_what_was_measured():
    """F0 alone at a $0 ceiling must still be able to publish: refusing it would
    mean a $0 deployment could never publish anything."""
    row = {"id": "x", "model_ids": json.dumps(_MODELS), "pooled": 0.8444}
    assert CAL.select_calibration([row], [_MODELS["F0"]])["id"] == "x"


def test_a_model_the_gate_never_saw_stops_the_grader():
    row = {"id": "x", "model_ids": json.dumps(_MODELS)}
    with pytest.raises(CAL.CalibrationRefusal) as exc:
        CAL.select_calibration([row], ["mistralai/mistral-small-2601"])
    assert exc.value.reason == CAL.MODELS_NOT_COVERED
    assert "re-gate" in exc.value.detail


def test_model_ids_accepts_the_shapes_asyncpg_hands_back():
    row_str = {"id": "a", "model_ids": json.dumps(_MODELS)}
    row_dict = {"id": "b", "model_ids": dict(_MODELS)}
    row_list = {"id": "c", "model_ids": list(_MODELS.values())}
    for row in (row_str, row_dict, row_list):
        assert CAL.select_calibration([row], [_MODELS["F0"]])["id"] == row["id"]


def test_the_draw_is_seeded_derived_and_reproducible():
    claims = [{"id": f"DR-{i:08x}", "analyst_id": "escalation"}
              for i in range(120)]
    seed = CAL.draw_seed(RUBRIC_SHA256, list(_MODELS.values()), "2026-09-16")
    first = CAL.draw_calibration_sample(claims, 30, seed)
    assert len(first) == 30
    assert [c["id"] for c in first] == sorted(c["id"] for c in first)
    assert first == CAL.draw_calibration_sample(claims, 30, seed)
    # A different rubric, or a repointed model, draws a DIFFERENT sample.
    other = CAL.draw_seed("b" * 64, list(_MODELS.values()), "2026-09-16")
    assert [c["id"] for c in CAL.draw_calibration_sample(claims, 30, other)] \
        != [c["id"] for c in first]


def test_a_pool_smaller_than_n_draws_all_of_it():
    claims = [{"id": f"DR-{i:08x}"} for i in range(5)]
    assert len(CAL.draw_calibration_sample(claims, 30, "seed")) == 5


# ---------------------------------------------------------------------------
# The loader's pure halves (scripts/load_unit_reference.py)
# ---------------------------------------------------------------------------


def _loader():
    """Import the loader script as a module — it is a deploy step and its
    derivations are as load-bearing as the handler's."""
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "load_unit_reference.py"
    )
    spec = importlib.util.spec_from_file_location("_load_unit_reference", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_window_is_read_from_the_header_and_never_invented():
    loader = _loader()
    start, end = loader.parse_window(REFERENCE, None, None)
    assert start.isoformat() == "2026-09-02T19:30:00+00:00"
    assert end.isoformat() == "2026-09-16T19:30:00+00:00"
    with pytest.raises(SystemExit):
        loader.parse_window({"header": {}}, None, None)


def test_a_development_bearing_on_two_dimensions_counts_for_both():
    """R3's references carry `dimension` as a LIST — a strike on a disputed
    border bears on escalation AND military_posture. Reading it as a string
    counts neither, and every dimension then reads THIN."""
    loader = _loader()
    assert loader._dev_dimensions({"dimension": ["escalation", "military_posture"]}) \
        == ["escalation", "military_posture"]
    assert loader._dev_dimensions({"dimension": "escalation"}) == ["escalation"]
    assert loader._dev_dimensions({}) == []

    reference = {
        "ref_bands": {"escalation": "high", "military_posture": "high",
                      "energy_security": "elevated"},
        "ref_developments": [
            {"dimension": ["escalation", "military_posture"]},
            {"dimension": ["escalation", "military_posture"]},
            {"dimension": ["energy_security"]},
        ],
    }
    assert loader.thin_dimensions(reference, 2) == ["energy_security"]


def test_the_span_verified_rate_is_the_builders_number_never_re_derived():
    loader = _loader()
    assert loader.span_verified_rate(REFERENCE, None) is None
    assert loader.span_verified_rate(REFERENCE, 0.94) == 0.94
    assert loader.span_verified_rate(
        {"header": {"span_verified_rate": 0.8}}, None
    ) == 0.8


def test_the_seeded_calibration_names_the_v4_result_verbatim():
    """The seed is VERDICT_P1v4's numbers, and its model ids come from the
    grader's own registry — so it can never claim to have calibrated a model the
    code would not use."""
    loader = _loader()
    assert loader.V4_POOLED == 0.8444
    assert loader.V4_N_ATOMS == 30
    assert {p["rate"] for p in loader.V4_PAIRWISE} == {0.833, 0.900, 0.800}
    assert all(
        p["rate"] >= ADJ.PAIRWISE_BAR for p in loader.V4_PAIRWISE
    )
    assert loader.V4_POOLED >= ADJ.POOLED_BAR


# ---------------------------------------------------------------------------
# THE REAL BINDING PATH — deterministic.run_method, a live substrate
# ---------------------------------------------------------------------------

_TARGET = "country_watch_zz"
_COMPOSITION_BODY = "\n".join([
    "## The record",
    "> " + RECORDED_COMPOSITION_TEXT + " [[ref:4]]",
    "> " + RECORDED_COMPOSITION_TEXT_2.replace(".", "[14] [[ref:3]].", 1),
    "## Not carried",
    "- three blocks were dropped",
])
_DESK_BODY = "\n".join([
    "## What changed",
    RECORDED_DESK_TEXT,
    RECORDED_EXCLUSIONS[0][1],
])


class _Deps:
    def __init__(self, pool, extras=None):
        self.pg_pool = pool
        self.extras = dict(extras or {})


class _AlwaysLLM:
    """One canned reply for every claim. Counts its calls."""

    def __init__(self, verdict="contains", span=_VERBATIM, cost=0.0):
        self._reply = _reply(verdict, span)
        self._cost = cost
        self.calls = 0

    async def chat_complete(self, messages, **kwargs):
        self.calls += 1
        return _Response(self._reply, _Usage(cost=self._cost))


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    async def _wipe(conn):
        await conn.execute(
            "DELETE FROM unit_correctness WHERE target_id = $1", _TARGET
        )
        await conn.execute(
            "DELETE FROM unit_references WHERE target_id = $1", _TARGET
        )
        await conn.execute(
            "DELETE FROM grader_calibrations WHERE notes LIKE 'pytest%'"
        )
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE target_id = $1", _TARGET
        )

    async with pg_pool.acquire() as conn:
        await _wipe(conn)
    yield
    async with pg_pool.acquire() as conn:
        await _wipe(conn)


async def _seed_reference(
    conn,
    as_of: datetime,
    *,
    window_end: datetime | None = None,
    built_at: datetime | None = None,
    sha: str = "c" * 64,
) -> UUID:
    """A reference row. ``window_end`` defaults a day AHEAD of the stamp; pass
    it explicitly to seed the LIVE shape — a window that closed BEFORE the read
    that grades against it."""
    ref_id = uuid4()
    end = as_of + timedelta(days=1) if window_end is None else window_end
    await conn.execute(
        """
        INSERT INTO unit_references (
            id, target_id, window_start, window_end, built_at, builder,
            ref_json, span_verified_rate, thin_dimensions, sha256
        ) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,NULL,$8::text[],$9)
        """,
        ref_id, _TARGET, end - timedelta(days=14), end,
        built_at or end, "pytest-lane", json.dumps(REFERENCE),
        ["proliferation_watch"], sha,
    )
    return ref_id


async def _seed_calibration(conn, models=None) -> None:
    await conn.execute(
        """
        INSERT INTO grader_calibrations (
            id, rubric_sha, model_ids, pooled, pairwise, gate_pass, packet_sha,
            n_atoms, notes
        ) VALUES ($1,$2,$3::jsonb,$4,$5::jsonb,TRUE,$6,$7,'pytest seed')
        ON CONFLICT DO NOTHING
        """,
        uuid4(), RUBRIC_SHA256,
        json.dumps(models if models is not None else GRADE.model_ids()),
        0.8444, json.dumps([{"a": "F0", "b": "F3", "rate": 0.9}]),
        "a" * 64, 30,
    )


async def _seed_head(conn, analyst_id: str, body: str, as_of: datetime) -> UUID:
    head_id = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, analyst_id, analyst_version, run_id, target_id, kind, title,
             body, confidence, data, produced_at, created_at, schema_uri)
        VALUES ($1,$2,$3,$4,$5,'finding',$6,$7,1.0,$8::jsonb,$9,$9,$10)
        """,
        head_id, analyst_id, "0" * 16, uuid4(), _TARGET,
        f"{analyst_id} read", body,
        json.dumps({"data": {"assembly": {"regime": "assembly"}}}),
        as_of - timedelta(hours=2),
        "iglu:legba/finding/jsonschema/1-0-0",
    )
    return head_id


async def _run(pool, extras=None, **opts) -> AnalystMethodResult:
    """Drive the handler through ``deterministic.run_method`` — the REAL binding
    path the runtime uses, never a direct module call."""
    options = {
        "sub_handler": "correctness_grader",
        "analyst_id": "correctness_grader",
        "run_id": str(uuid4()),
        "grader_targets": [_TARGET],
        **opts,
    }
    result = await deterministic.run_method([], options, _Deps(pool, extras))
    assert isinstance(result, AnalystMethodResult)
    return result


def test_the_sub_handler_is_registered_in_the_dispatch_table():
    assert "correctness_grader" in deterministic.SUB_HANDLERS
    assert deterministic.SUB_HANDLERS["correctness_grader"] is CG.handle


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_real_binding_path_flag_off_writes_absolutely_nothing(
    pg_pool, clean_slate, monkeypatch
):
    """The shipped state. No row, no LLM call, no spend — only a receipt saying
    the flag is off."""
    monkeypatch.delenv(CG.ENABLED_ENV, raising=False)
    as_of = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _seed_reference(conn, as_of)
        await _seed_calibration(conn)
        await _seed_head(conn, "escalation", _DESK_BODY, as_of)
    llm = _AlwaysLLM()
    result = await _run(pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: llm})
    assert llm.calls == 0
    data = result.finding.data
    assert data["enabled"] is False
    assert data["n_units_written"] == 0
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM unit_correctness WHERE target_id = $1", _TARGET
        ) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_real_binding_path_refuses_without_a_passing_calibration(
    pg_pool, clean_slate, monkeypatch
):
    """G3's interlock, at the top of the sweep: before a head is read and long
    before a call is made."""
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    as_of = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _seed_reference(conn, as_of)
        await _seed_head(conn, "escalation", _DESK_BODY, as_of)
    llm = _AlwaysLLM()
    result = await _run(pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: llm})
    assert llm.calls == 0
    refusal = result.finding.data["refusal"]
    assert refusal["reason"] == CAL.NO_CALIBRATION
    assert "REFUSED" in result.finding.title


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_real_binding_path_records_no_reference_and_grades_nothing(
    pg_pool, clean_slate, monkeypatch
):
    """A correctness number computed against no reference is not one. The unit
    is NAMED as unmeasured rather than scored as zero."""
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    as_of = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _seed_calibration(conn)
        await _seed_head(conn, "escalation", _DESK_BODY, as_of)
    llm = _AlwaysLLM()
    result = await _run(pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: llm})
    assert llm.calls == 0
    per_target = result.finding.data["per_target"]
    assert [t["status"] for t in per_target] == ["no_reference"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_real_binding_path_writes_the_unit_row_and_its_claim_ledger(
    pg_pool, clean_slate, monkeypatch
):
    """END TO END through the dispatcher: two heads, two grains, real rows.

    Asserts the things a published number has to be able to say: which head,
    which reference, which rubric, how many claims, both shares, the per-claim
    ledger under it, and that the composition is its own grain rather than
    pooled into the desks'.
    """
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.delenv(GRADE.CEILING_ENV, raising=False)
    as_of = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        ref_id = await _seed_reference(conn, as_of)
        await _seed_calibration(conn)
        desk_head = await _seed_head(
            conn, "internal_stability", _DESK_BODY, as_of
        )
        comp_head = await _seed_head(
            conn, "country_composition", _COMPOSITION_BODY, as_of
        )
    llm = _AlwaysLLM("contains", _VERBATIM)
    result = await _run(
        pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: llm},
        as_of=as_of.isoformat(),
    )
    data = result.finding.data
    assert data["enabled"] is True and data["refusal"] is None
    assert data["n_units_written"] == 2, data["per_target"]
    assert llm.calls >= 3

    async with pg_pool.acquire() as conn:
        rows = {
            r["analyst_id"]: r for r in await conn.fetch(
                "SELECT * FROM unit_correctness WHERE target_id = $1", _TARGET
            )
        }
        assert set(rows) == {"internal_stability", "country_composition"}
        desk = rows["internal_stability"]
        assert desk["head_id"] == desk_head
        assert desk["reference_id"] == ref_id
        assert desk["rubric_sha"] == RUBRIC_SHA256
        assert desk["grain"] == "desk"
        assert desk["n_claims"] == 1 and desk["n_contains"] == 1
        assert float(desk["correctness_share"]) == 1.0
        assert float(desk["coverage_share"]) == 1.0
        # ONE family ran, so every claim is flagged.
        assert desk["n_single_family"] == desk["n_claims"]
        assert float(desk["cost_usd"]) == 0.0, "the core plane is $0"
        families = json.loads(desk["families"])
        assert families["F0"]["model"]
        assert families["single_family"] is True
        assert "F2" not in families and "F3" not in families

        comp = rows["country_composition"]
        assert comp["grain"] == "composition" and comp["head_id"] == comp_head
        assert comp["n_claims"] == 2, "both quoted blocks, the ledger neither"

        claims = await conn.fetch(
            """
            SELECT c.* FROM unit_correctness_claims c
              JOIN unit_correctness u ON u.id = c.correctness_id
             WHERE u.target_id = $1 ORDER BY c.claim_id
            """,
            _TARGET,
        )
        assert len(claims) == 3
        assert {c["claim_id"][:3] for c in claims} == {"DR-", "CR-"}
        for claim in claims:
            assert claim["adjudicated"] == "contains"
            assert claim["single_family"] is True
            assert claim["n_families"] == 1
            assert json.loads(claim["label_by_family"]) == {"F0": "contains"}
            spans = json.loads(claim["spans"])
            assert spans["F0"]["decisive_span"] == _VERBATIM
            assert spans["F0"]["span_unverified"] is False
            assert claim["claim_text"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_real_binding_path_is_idempotent_and_does_not_respend(
    pg_pool, clean_slate, monkeypatch
):
    """Same head, same reference, same rubric = the same measurement.

    And the skip happens BEFORE the calls, so a re-run does not burn the paid
    calls the unique index would then discard.
    """
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    as_of = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _seed_reference(conn, as_of)
        await _seed_calibration(conn)
        await _seed_head(conn, "internal_stability", _DESK_BODY, as_of)

    first = _AlwaysLLM()
    await _run(pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: first},
               as_of=as_of.isoformat())
    assert first.calls >= 1

    second = _AlwaysLLM()
    result = await _run(pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: second},
                        as_of=as_of.isoformat())
    assert second.calls == 0, "a re-run must not re-spend"
    assert [t["status"] for t in result.finding.data["per_target"]] == [
        "already_graded"
    ]
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM unit_correctness WHERE target_id = $1", _TARGET
        ) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_real_binding_path_refuses_when_a_model_is_uncalibrated(
    pg_pool, clean_slate, monkeypatch
):
    """Repointing the core plane at a model the gate never saw stops the job —
    the G3 trigger, enforced rather than remembered."""
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.setenv(GRADE.CORE_MODEL_ENV, "some-other-model-v9")
    as_of = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _seed_reference(conn, as_of)
        await _seed_calibration(conn, models={"F0": "core-120b"})
        await _seed_head(conn, "internal_stability", _DESK_BODY, as_of)
    llm = _AlwaysLLM()
    result = await _run(pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: llm})
    assert llm.calls == 0
    assert result.finding.data["refusal"]["reason"] == CAL.MODELS_NOT_COVERED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_real_binding_path_requires_a_live_pool(monkeypatch):
    """An instrument that cannot read the substrate must not emit a
    clean-looking zero."""
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    with pytest.raises(RuntimeError, match="deps.pg_pool"):
        await deterministic.run_method(
            [], {"sub_handler": "correctness_grader",
                 "analyst_id": "correctness_grader"}, _Deps(None)
        )


# ---------------------------------------------------------------------------
# D2 — REFERENCE CURRENCY (G1-FIX)
#
# 0196 shipped `window_start <= as_of AND window_end >= as_of`. A reference is
# built for a window that ENDS at its own T0, and every read it grades lands
# AFTER that instant, so the predicate made the nightly sweep ungradable by
# construction: on 2026-09-16 a reference for 09-02T19:30Z -> 09-16T19:30Z
# matched nothing at 22:32Z the same day. These tests pin the fix and — just as
# important — the two gaps it must keep APART.
# ---------------------------------------------------------------------------


def test_the_grace_env_beats_the_descriptor_option(monkeypatch):
    """Env over descriptor, the ceiling's discipline.

    How long a published number may rest on an ageing reference is a
    measurement-integrity decision; a descriptor PUT is an API call any holder
    of the registry token can make, and it must not be able to widen it.
    """
    monkeypatch.delenv(CG.REFERENCE_GRACE_ENV, raising=False)
    assert CG.reference_grace_days({}) == CG.DEFAULT_REFERENCE_GRACE_DAYS
    assert CG.reference_grace_days({"reference_grace_days": 3}) == 3
    monkeypatch.setenv(CG.REFERENCE_GRACE_ENV, "1")
    assert CG.reference_grace_days({"reference_grace_days": 30}) == 1
    # Zero is a REAL setting — strict containment, the pre-fix semantics.
    monkeypatch.setenv(CG.REFERENCE_GRACE_ENV, "0")
    assert CG.reference_grace_days({"reference_grace_days": 30}) == 0


def test_an_unparseable_grace_falls_back_and_never_reads_as_forever(monkeypatch):
    monkeypatch.setenv(CG.REFERENCE_GRACE_ENV, "a fortnight")
    assert CG.reference_grace_days({}) == CG.DEFAULT_REFERENCE_GRACE_DAYS
    assert CG.reference_grace_days({"reference_grace_days": 2}) == 2
    monkeypatch.setenv(CG.REFERENCE_GRACE_ENV, "-5")
    assert CG.reference_grace_days({}) == CG.DEFAULT_REFERENCE_GRACE_DAYS


def test_the_age_is_fractional_and_floored_at_zero():
    """A sweep 3h02m past a window's close is 0.13 days stale, not 0 and not 1.

    And a read INSIDE the window is aged zero, never negative: the column is
    the age of the reference the number rests on, not a signed offset.
    """
    end = datetime(2026, 9, 16, 19, 30, tzinfo=timezone.utc)
    late = datetime(2026, 9, 16, 22, 32, 27, tzinfo=timezone.utc)
    assert CG.reference_age_days(late, end) == pytest.approx(0.1267, abs=1e-4)
    assert CG.reference_age_days(end - timedelta(hours=5), end) == 0.0
    assert CG.reference_age_days(end + timedelta(days=6), end) == 6.0
    assert CG.reference_age_days(late, None) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_real_binding_path_grades_after_the_window_closes(
    pg_pool, clean_slate, monkeypatch
):
    """THE LIVE DEFECT. A reference whose window ended hours ago still grades.

    This is the exact shape of the 2026-09-16 forced run: window_end 19:30Z,
    read at 22:32Z. Before the fix the sweep reported "no unit_references row
    covers this stamp" and wrote nothing.
    """
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.delenv(CG.REFERENCE_GRACE_ENV, raising=False)
    window_end = datetime.now(timezone.utc) - timedelta(hours=3, minutes=2)
    as_of = window_end + timedelta(hours=3, minutes=2)
    async with pg_pool.acquire() as conn:
        ref_id = await _seed_reference(conn, as_of, window_end=window_end)
        await _seed_calibration(conn)
        await _seed_head(conn, "internal_stability", _DESK_BODY, as_of)

    result = await _run(
        pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: _AlwaysLLM()},
        as_of=as_of.isoformat(),
    )
    data = result.finding.data
    assert data["n_units_written"] == 1, data["per_target"]
    assert [t["status"] for t in data["per_target"]] == ["ok"]

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM unit_correctness WHERE target_id = $1", _TARGET
        )
    assert row["reference_id"] == ref_id
    assert float(row["reference_age_days"]) == pytest.approx(0.1264, abs=1e-3)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_receipt_prints_the_reference_age(
    pg_pool, clean_slate, monkeypatch
):
    """The caveat travels WITH the number. "88% correct" and "88% correct
    against a reference that closed four days ago" are different statements."""
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.delenv(CG.REFERENCE_GRACE_ENV, raising=False)
    as_of = datetime.now(timezone.utc)
    window_end = as_of - timedelta(days=4)
    async with pg_pool.acquire() as conn:
        await _seed_reference(conn, as_of, window_end=window_end)
        await _seed_calibration(conn)
        await _seed_head(conn, "internal_stability", _DESK_BODY, as_of)

    result = await _run(
        pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: _AlwaysLLM()},
        as_of=as_of.isoformat(),
    )
    assert "reference_age=4.00d" in result.finding.body
    assert f"grace={CG.DEFAULT_REFERENCE_GRACE_DAYS}d" in result.finding.body
    target = result.finding.data["per_target"][0]
    assert float(target["reference"]["age_days"]) == pytest.approx(4.0, abs=1e-3)
    assert target["reference"]["grace_days"] == CG.DEFAULT_REFERENCE_GRACE_DAYS
    assert result.finding.data["reference_grace_env"] == CG.REFERENCE_GRACE_ENV


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_reference_past_the_grace_is_stale_not_absent(
    pg_pool, clean_slate, monkeypatch
):
    """``reference_stale`` is NOT a softer ``no_reference``.

    One says track R2 never reached this target; the other says it did and then
    STOPPED. Different operator actions, so different statuses — and the second
    is invisible if the receipt only ever knows the first.
    """
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.delenv(CG.REFERENCE_GRACE_ENV, raising=False)
    as_of = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _seed_reference(
            conn, as_of, window_end=as_of - timedelta(days=9)
        )
        await _seed_calibration(conn)
        await _seed_head(conn, "internal_stability", _DESK_BODY, as_of)

    llm = _AlwaysLLM()
    result = await _run(
        pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: llm}, as_of=as_of.isoformat(),
    )
    assert llm.calls == 0, "a stale reference must not spend a call"
    target = result.finding.data["per_target"][0]
    assert target["status"] == CG.STATUS_REFERENCE_STALE
    assert float(target["reference_age_days"]) == pytest.approx(9.0, abs=1e-3)
    assert target["reference"]["current"] is False
    assert "REFERENCE STALE" in " ".join(result.finding.data["warnings"])
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM unit_correctness WHERE target_id = $1", _TARGET
        ) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_target_that_never_had_a_reference_is_still_no_reference(
    pg_pool, clean_slate, monkeypatch
):
    """The other gap keeps its own name, and carries no age."""
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    as_of = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _seed_calibration(conn)
        await _seed_head(conn, "internal_stability", _DESK_BODY, as_of)
    result = await _run(
        pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: _AlwaysLLM()},
        as_of=as_of.isoformat(),
    )
    target = result.finding.data["per_target"][0]
    assert target["status"] == CG.STATUS_NO_REFERENCE
    assert target["reference_age_days"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_newer_window_supersedes_an_older_one_inside_the_grace(
    pg_pool, clean_slate, monkeypatch
):
    """Two references both current at the stamp: the one reaching CLOSEST to it
    wins. A grace that let an older window keep grading past a fresh one would
    be worse than no grace at all."""
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.delenv(CG.REFERENCE_GRACE_ENV, raising=False)
    as_of = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        old_id = await _seed_reference(
            conn, as_of, window_end=as_of - timedelta(days=5),
            built_at=as_of, sha="a" * 64,
        )
        new_id = await _seed_reference(
            conn, as_of, window_end=as_of - timedelta(days=1),
            built_at=as_of - timedelta(days=1), sha="b" * 64,
        )
        await _seed_calibration(conn)
        await _seed_head(conn, "internal_stability", _DESK_BODY, as_of)
    assert old_id != new_id

    result = await _run(
        pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: _AlwaysLLM()},
        as_of=as_of.isoformat(),
    )
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM unit_correctness WHERE target_id = $1", _TARGET
        )
    assert row["reference_id"] == new_id, (
        "the reference whose window reaches closest to the stamp must win, "
        "even though the other was BUILT later"
    )
    assert float(row["reference_age_days"]) == pytest.approx(1.0, abs=1e-3)
    assert result.finding.data["n_units_written"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_zero_grace_restores_strict_containment(
    pg_pool, clean_slate, monkeypatch
):
    """The pre-fix semantics stay REACHABLE, by env, without a code edit."""
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.setenv(CG.REFERENCE_GRACE_ENV, "0")
    as_of = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _seed_reference(
            conn, as_of, window_end=as_of - timedelta(hours=3)
        )
        await _seed_calibration(conn)
        await _seed_head(conn, "internal_stability", _DESK_BODY, as_of)
    result = await _run(
        pg_pool, extras={CG.F0_DEPS_EXTRA_KEY: _AlwaysLLM()},
        as_of=as_of.isoformat(),
    )
    assert result.finding.data["per_target"][0]["status"] == (
        CG.STATUS_REFERENCE_STALE
    )
    assert result.finding.data["reference_grace_days"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_population_sweep_uses_the_same_grace_as_the_lookup(
    pg_pool, clean_slate, monkeypatch
):
    """No explicit targets: the sweep discovers its population from the
    reference table. A population predicate looser or tighter than the lookup's
    would grade nothing and say nothing about why."""
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.delenv(CG.REFERENCE_GRACE_ENV, raising=False)
    as_of = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await _seed_reference(
            conn, as_of, window_end=as_of - timedelta(days=2)
        )
        await _seed_calibration(conn)
        await _seed_head(conn, "internal_stability", _DESK_BODY, as_of)

    options = {
        "sub_handler": "correctness_grader",
        "analyst_id": "correctness_grader",
        "run_id": str(uuid4()),
        "as_of": as_of.isoformat(),
        # Other tests in this session leave their own targets behind; the cap
        # is about cost, not about which target this assertion is looking for.
        "max_targets_per_run": 50,
    }
    result = await deterministic.run_method(
        [], options, _Deps(pg_pool, {CG.F0_DEPS_EXTRA_KEY: _AlwaysLLM()})
    )
    assert _TARGET in result.finding.data["targets"]
    graded = [
        t for t in result.finding.data["per_target"]
        if t["target_id"] == _TARGET
    ]
    assert graded and graded[0]["status"] == "ok"


# ---------------------------------------------------------------------------
# THE RUBRIC FILE'S BYTES — pinned against a LITERAL, from disk
# ---------------------------------------------------------------------------


def test_the_rubric_file_on_disk_still_hashes_to_the_gated_digest():
    """The strongest form of the pin, and the one the 2026-09-20 work needs.

    ``_correctness_rubric`` already refuses to import if the file moves, but it
    compares against its OWN constant — a commit that edited both would pass
    that check. This test hashes the FILE and compares it to the literal
    VERDICT_P1v4 names, so the two can never move together silently.

    It matters here because the ``prior_relative`` exclusion class changes which
    spans are HANDED to the rubric and not one byte of the rubric itself: no
    re-gate is owed, and this is the test that says so.
    """
    import hashlib as _hashlib

    from legba.data.analysts.deterministic_handlers import (
        _correctness_rubric as RUB,
    )

    with open(RUB.RUBRIC_PATH, "rb") as handle:
        raw = handle.read()
    assert _hashlib.sha256(raw).hexdigest() == (
        "1b51d7f5187c7f93af2e2cccc0775e21ab7efc41678bc28c4a2dcbe287fe7d8c"
    )


# ---------------------------------------------------------------------------
# PRIOR-RELATIVE CLAIMS — the class no world reference can bear on
# ---------------------------------------------------------------------------

#: Live spans from ``unit_correctness_claims`` on 2026-09-20, each of which the
#: graders labelled ``contradicts`` against a reference that has no prior read.
#: 13 of the 48 live ``contradicts`` labels were of this shape.
PRIOR_RELATIVE_LIVE: list[tuple[str, str]] = [
    (
        "no_change_versus_prior",
        "No material change versus the prior read; the welfare boost and "
        "UN‑GA warning were already noted in the previous assessment and "
        "no new development alters the transition outlook.",
    ),
    (
        "no_change_versus_prior",
        "No material change since the prior read on 16 September 2026, which "
        "recorded an elevated posture.",
    ),
    (
        "no_change_versus_prior",
        "No material change in capability, deployment, exercises, readiness, "
        "procurement or doctrine compared with the prior 18 September read, "
        "which already recorded an elevated posture.",
    ),
    (
        "no_change_versus_prior",
        "Assessed: No material change versus the prior verified read, and the "
        "register carries no new leadership signal.",
    ),
    (
        "as_in_the_prior_read",
        "No material change – the pressure stays moderate as in the prior "
        "read on 19 September 2026 and the window ledger shows no new "
        "disruptions up to 19 September 2026.",
    ),
    (
        "unchanged_from_prior",
        "No fresh outages, curtailments, or fuel‑price spikes appear in "
        "this desk's collection, so the pressure level is unchanged from the "
        "prior read that already noted elevated pressure.",
    ),
    (
        "already_noted_in_the_prior_read",
        "The Plaza de Mayo occupation and the teacher strike were already "
        "noted in the previous read, and the register repeats them.",
    ),
    (
        "remains_as_previously_assessed",
        "The standing military posture remains as previously assessed, with "
        "the same force design and the same readiness stance.",
    ),
    (
        "nothing_alters_the_standing_read",
        "No new development alters the outlook for the coalition, and the "
        "register adds nothing the desk had not already weighed.",
    ),
]

#: Spans that MUST stay gradeable. The first four are world ABSENCE claims —
#: ANNEX C v4 rule 3 makes them claims and they are exactly the class the rubric
#: was revised to grade. The last two MENTION a prior read while asserting
#: something about the world, which is the false positive a looser rule makes:
#: measured over the 980 live rows, "a prior-read mention anywhere plus a
#: no-change phrase anywhere" swallowed both of them.
PRIOR_RELATIVE_NEGATIVES: list[str] = [
    "Israel's energy‑security pressure remains moderate with no new supply "
    "disruptions, price spikes, or infrastructure attacks reported in the "
    "latest three days.",
    "No new sanctions, tariffs, capital controls, or currency shocks appeared "
    "in the past three days anywhere in the country.",
    "The coalition has not deteriorated further and no new incidents were "
    "reported inside the window at all.",
    "leadership_transition [[ref:4]] and energy_security [[ref:1]] showed no "
    "material change; proliferation_watch [[ref:12]] is below verification "
    "floor.",
    "No material change – the high pressure highlighted in the prior read "
    "persists, with continued fuel‑price spikes and ongoing oil‑supply "
    "risk signals.",
    "No new tariff adjustments, sanction designations, or other economic "
    "measures were reported in the current slice; the standing 12.5 % US and "
    "55 % Chinese beef tariffs remain unchanged (prior read).",
    "Recent diplomatic friction — Australia's refusal to join UK‑led "
    "sanctions on Israeli settlements — does not alter the underlying "
    "military posture.",
]


@pytest.mark.parametrize("pattern,text", PRIOR_RELATIVE_LIVE)
def test_a_prior_relative_claim_is_excluded_under_its_own_pattern(pattern, text):
    """Excluded, and the ledger says WHICH shape excluded it.

    The reference is a window of world developments with no prior read in it, so
    a grader handed one of these can only ever mislabel it — and live it did,
    reading "developments happened" and returning ``contradicts`` under ANNEX C
    v4 rule 3.
    """
    assert SEG.prior_relative_pattern(text) == pattern
    assert SEG.exclusion_reason(text, DIMENSIONS) == (
        f"{SEG.PRIOR_RELATIVE_REASON}:{pattern}"
    )


@pytest.mark.parametrize("text", PRIOR_RELATIVE_NEGATIVES)
def test_a_world_absence_claim_is_never_taken_for_a_prior_relative_one(text):
    """The rule is anchored on the PRIOR READ, never on the negation.

    "No new disruptions", "has not deteriorated", "no new incidents" assert
    something about the WORLD; ANNEX C v4 rule 3 makes them claims and the
    rubric was revised to grade them. A span that merely MENTIONS a prior read
    while asserting something about the world keeps its claim too.
    """
    assert SEG.prior_relative_pattern(text) is None
    assert SEG.exclusion_reason(text, DIMENSIONS) is None


def test_the_prior_relative_exclusion_travels_with_its_reason_and_detail():
    """Same ledger mechanism as every other exclusion — not a second one.

    ``segment_head`` returns each excluded span with ``span_index``, ``reason``
    and ``text``, exactly as ``segmentation_IL.json`` recorded its 58; this
    class adds the ``detail`` field the ``not_judgeable_span`` rule already
    uses, and nothing else.
    """
    prior = PRIOR_RELATIVE_LIVE[0][1]
    head = {
        "grain": SEG.GRAIN_DESK, "analyst_id": "leadership_transition",
        "created_at": "2026-09-20T01:20:00+00:00", "output_id": "o",
        "grader_body": f"{RECORDED_DESK_TEXT}\n\n{prior}",
    }
    out = SEG.segment_head(
        head, lambda body: body.split("\n\n"), lambda span: True, DIMENSIONS,
    )
    assert out["n_kept"] == 1
    assert out["atoms"][0]["text"] == RECORDED_DESK_TEXT
    excluded = out["excluded"]
    assert len(excluded) == 1
    assert excluded[0]["reason"] == (
        f"{SEG.PRIOR_RELATIVE_REASON}:no_change_versus_prior"
    )
    assert excluded[0]["text"] == prior
    assert excluded[0]["detail"] == SEG.PRIOR_RELATIVE_DETAIL
    assert "span_index" in excluded[0]


def test_the_prior_relative_class_is_counted_on_the_receipt():
    """Counted, not merely dropped: a denominator that shrank silently is a
    denominator nobody can reconstruct."""
    receipt = CG.build_receipt(
        as_of=datetime(2026, 9, 20, 1, 20, tzinfo=timezone.utc),
        enabled=True,
        targets=["country_g20_br"],
        per_target=[{
            "target_id": "country_g20_br", "status": "graded",
            "n_units_written": 1, "n_claims": 9, "cost_usd": 0.0,
            "units": [],
            "exclusions": {"excluded_by_reason": {
                "heading": 3,
                f"{SEG.PRIOR_RELATIVE_REASON}:no_change_versus_prior": 5,
                f"{SEG.PRIOR_RELATIVE_REASON}:as_in_the_prior_read": 2,
            }},
        }],
        calibration=None, refusal=None, ceiling_usd=0.0, spent_before=0.0,
        warnings=[],
    )
    assert receipt.data["n_prior_relative_excluded"] == 7
    assert "prior-relative spans excluded: 7" in receipt.body


# ---------------------------------------------------------------------------
# F0 — the core plane's served model id
# ---------------------------------------------------------------------------


def test_the_core_model_default_is_the_public_model_name(monkeypatch):
    """The shipped fallback names the MODEL — ``gpt-oss-120b`` — and never a
    particular deployment's private serving alias for it.

    A core plane that serves those weights under its own deployment-local id
    declares that id in ``LEGBA_LLM_MODEL_NAME``. The calibration lookup matches
    on whatever the env resolves to, so the alias belongs in the deployment's
    own config and has no business in tracked content.
    """
    assert GRADE.CORE_MODEL_DEFAULT == "gpt-oss-120b"
    assert GRADE.CORE_MODEL_ENV == "LEGBA_LLM_MODEL_NAME"
    monkeypatch.delenv(GRADE.CORE_MODEL_ENV, raising=False)
    assert GRADE.core_model_id() == "gpt-oss-120b"
    assert GRADE.model_ids()["F0"] == "gpt-oss-120b"
    monkeypatch.setenv(GRADE.CORE_MODEL_ENV, "  some-served-id  ")
    assert GRADE.core_model_id() == "some-served-id"
    assert GRADE.model_ids()["F0"] == "some-served-id"


# ---------------------------------------------------------------------------
# F3 — the model OpenRouter actually serves (2026-09-20 repoint)
# ---------------------------------------------------------------------------


def test_the_f3_family_names_the_model_the_component_now_serves():
    """``mistralai/mistral-large-2512`` was REMOVED from OpenRouter on
    2026-09-20 and the judge component was repointed at mistral-medium-3.1. The
    component id does not move; the model id and the prices that back the
    ceiling do."""
    f3 = GRADE.FAMILIES["F3"]
    assert f3["model"] == "mistralai/mistral-medium-3.1"
    assert f3["component"] == "llm.judge.openrouter_mistral_large.openai_compat"
    assert (f3["price_in"], f3["price_out"]) == (0.40, 2.00)
    assert GRADE.model_ids()["F3"] == "mistralai/mistral-medium-3.1"


def test_the_repointed_f3_is_outside_the_v4_gate_until_a_re_gate_lands():
    """The interlock firing IS the instrument working.

    The v4 row calibrated mistral-large-2512. The run would now use
    mistral-medium-3.1, which that row never saw, so the grader must REFUSE
    rather than pool a new model's labels into a number measured on a different
    one.
    """
    live = GRADE.model_ids()
    v4_row = {
        "id": "v4",
        "model_ids": json.dumps({**live, "F3": "mistralai/mistral-large-2512"}),
        "pooled": 0.8444,
    }
    with pytest.raises(CAL.CalibrationRefusal) as exc:
        CAL.select_calibration([v4_row], list(live.values()))
    assert exc.value.reason == CAL.MODELS_NOT_COVERED
    assert "re-gate" in exc.value.detail


# ---------------------------------------------------------------------------
# ROTATION — a cap must bound the COST, never decide who is ever measured
# ---------------------------------------------------------------------------

_ROTATE = "country_rotate_"
#: never graded, and the LOWER target_id of the two — so it also pins the
#: tie-break inside the NULLS FIRST block.
_ROTATE_NEVER_A = f"{_ROTATE}ab"
_ROTATE_NEVER_B = f"{_ROTATE}cc"
_ROTATE_OLDEST = f"{_ROTATE}aa"
_ROTATE_MIDDLE = f"{_ROTATE}dd"
_ROTATE_NEWEST = f"{_ROTATE}bb"
_ROTATE_ALL = (
    _ROTATE_NEVER_A, _ROTATE_NEVER_B, _ROTATE_OLDEST, _ROTATE_MIDDLE,
    _ROTATE_NEWEST,
)


@pytest_asyncio.fixture
async def rotation_roster(pg_pool):
    """Five targets with a current reference; three of them already graded, at
    three different times. Scoped DELETEs only — the session database is shared,
    and a TRUNCATE here would poison every other file that reads these tables.
    """
    as_of = datetime(2026, 9, 20, 1, 20, tzinfo=timezone.utc)

    async def _wipe(conn):
        await conn.execute(
            "DELETE FROM unit_correctness_claims WHERE correctness_id IN ("
            " SELECT id FROM unit_correctness WHERE target_id = ANY($1::text[])"
            ")", list(_ROTATE_ALL),
        )
        await conn.execute(
            "DELETE FROM unit_correctness WHERE target_id = ANY($1::text[])",
            list(_ROTATE_ALL),
        )
        await conn.execute(
            "DELETE FROM unit_references WHERE target_id = ANY($1::text[])",
            list(_ROTATE_ALL),
        )

    async with pg_pool.acquire() as conn:
        await _wipe(conn)
        refs: dict[str, UUID] = {}
        for index, target in enumerate(_ROTATE_ALL):
            ref_id = uuid4()
            refs[target] = ref_id
            await conn.execute(
                """
                INSERT INTO unit_references (
                    id, target_id, window_start, window_end, built_at, builder,
                    ref_json, span_verified_rate, thin_dimensions, sha256
                ) VALUES ($1,$2,$3,$4,$5,'pytest-rotation',$6::jsonb,NULL,
                          '{}'::text[],$7)
                """,
                ref_id, target, as_of - timedelta(days=14), as_of,
                as_of, json.dumps(REFERENCE), f"{index:064x}",
            )
        graded = {
            _ROTATE_OLDEST: as_of - timedelta(days=10),
            _ROTATE_MIDDLE: as_of - timedelta(days=3),
            _ROTATE_NEWEST: as_of - timedelta(hours=1),
        }
        for target, when in graded.items():
            await conn.execute(
                """
                INSERT INTO unit_correctness (
                    id, analyst_id, target_id, head_id, as_of, reference_id,
                    rubric_sha, grain, n_claims, correctness_share,
                    coverage_share, created_at
                ) VALUES ($1,'escalation',$2,$3,$4,$5,$6,'desk',0,NULL,NULL,$7)
                """,
                uuid4(), target, uuid4(), as_of, refs[target], RUBRIC_SHA256,
                when,
            )
    yield as_of
    async with pg_pool.acquire() as conn:
        await _wipe(conn)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_roster_arrives_least_recently_graded_first(
    pg_pool, rotation_roster
):
    """The defect, and the fix, in one statement.

    ``ORDER BY target_id`` with ``[:max_targets]`` handed the sweep the SAME
    prefix every night: AR, AU, BR, CA, CN graded on every run and the other 27
    countries never once. Ordered by recency, any cap CYCLES the roster.

    The assertion is over THIS fixture's targets in the order the real statement
    returned them — a total order restricted to a subset keeps its relative
    order, so the test needs no control over the rest of the shared database.
    """
    as_of = rotation_roster
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            CG._TARGETS_WITH_REFERENCE_SQL, as_of, 7,
        )
    order = [str(r["target_id"]) for r in rows
             if str(r["target_id"]).startswith(_ROTATE)]
    assert order == [
        # never graded first — an inner join would have made "never graded"
        # mean "never eligible", and Postgres's default NULLS LAST would have
        # put them dead last instead
        _ROTATE_NEVER_A, _ROTATE_NEVER_B,
        # then oldest number first
        _ROTATE_OLDEST, _ROTATE_MIDDLE, _ROTATE_NEWEST,
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_cap_of_two_cycles_the_roster_instead_of_pinning_it(
    pg_pool, rotation_roster
):
    """Grade the head of the queue, and the NEXT sweep sees a different head.

    The second fetch is taken after writing a row for the two targets the first
    one returned — which is exactly what a sweep does — and they must fall to
    the back.
    """
    as_of = rotation_roster
    async with pg_pool.acquire() as conn:
        first = [
            str(r["target_id"]) for r in
            await conn.fetch(CG._TARGETS_WITH_REFERENCE_SQL, as_of, 7)
            if str(r["target_id"]).startswith(_ROTATE)
        ][:2]
        assert first == [_ROTATE_NEVER_A, _ROTATE_NEVER_B]
        for target in first:
            ref_id = await conn.fetchval(
                "SELECT id FROM unit_references WHERE target_id = $1", target
            )
            await conn.execute(
                """
                INSERT INTO unit_correctness (
                    id, analyst_id, target_id, head_id, as_of, reference_id,
                    rubric_sha, grain, n_claims, created_at
                ) VALUES ($1,'escalation',$2,$3,$4,$5,$6,'desk',0,$7)
                """,
                uuid4(), target, uuid4(), as_of, ref_id, RUBRIC_SHA256, as_of,
            )
        second = [
            str(r["target_id"]) for r in
            await conn.fetch(CG._TARGETS_WITH_REFERENCE_SQL, as_of, 7)
            if str(r["target_id"]).startswith(_ROTATE)
        ]
    assert second[:2] == [_ROTATE_OLDEST, _ROTATE_MIDDLE]
    assert second[-2:] == [_ROTATE_NEVER_A, _ROTATE_NEVER_B]


# ---------------------------------------------------------------------------
# THE TURN BUDGET (2026-09-21, H2) — the sweep ends INSIDE its actor turn
# ---------------------------------------------------------------------------
#
# The 01:20Z sweep held one actor turn for the whole roster — 32 targets at
# ~2.5 min each — so reconcile's deadline blew behind it and the 2026-09-21
# redeploy cut the run at 26/32 with NO receipt, because the receipt is the
# turn's return value. LEGBA_GRADER_PASS_BUDGET_SECONDS bounds the wall clock
# the target loop may hold; the check fires BETWEEN targets, and the deferred
# tail is named on the receipt. These drive ``handle`` with a fake pool —
# the budget, the deferral and the receipt are pure control flow.


class _FakeConn:
    """Answers the two reads ``handle`` makes before the target loop."""

    async def fetchval(self, sql, *args):  # _SPENT_TODAY_SQL
        return 0.0

    async def fetch(self, sql, *args):     # roster read (explicit targets skip it)
        return []

    async def fetchrow(self, sql, *args):
        return None


class _FakePool:
    """The one thing ``handle`` needs of ``deps.pg_pool``: an async acquire."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquire()


def _fake_calibration(**_kw):
    async def _resolve(conn, rubric_sha, model_ids):
        return {
            "id": uuid4(), "pooled": 0.8444, "packet_sha": "a" * 64,
            "n_atoms": 30, "created_at": datetime(2026, 9, 1,
                                                tzinfo=timezone.utc),
        }
    return _resolve


def _fake_grade_target(*, delay: float = 0.0):
    async def _grade(conn, **kwargs):
        if delay:
            await asyncio.sleep(delay)
        return {
            "target_id": kwargs["target_id"], "status": "ok", "units": [],
            "warnings": [], "cost_usd": 0.0, "n_claims": 0,
            "n_units_written": 1,
        }
    return _grade


async def _run_fake_pool(monkeypatch, targets, *, delay=0.0):
    """Drive the real ``handle`` with no substrate: the gate and the per-target
    work are patched, the BUDGET and the RECEIPT are not."""
    monkeypatch.setenv(CG.ENABLED_ENV, "1")
    monkeypatch.setattr(CG, "resolve_calibration", _fake_calibration())
    monkeypatch.setattr(CG, "grade_target", _fake_grade_target(delay=delay))
    return await _run(
        _FakePool(_FakeConn()),
        extras={CG.F0_DEPS_EXTRA_KEY: _AlwaysLLM()},
        grader_targets=list(targets),
    )


def test_the_pass_budget_parses_and_defaults(monkeypatch):
    monkeypatch.delenv(CG.PASS_BUDGET_ENV, raising=False)
    assert CG.pass_budget_seconds() == CG.DEFAULT_PASS_BUDGET_SECONDS
    monkeypatch.setenv(CG.PASS_BUDGET_ENV, "not-a-number")
    assert CG.pass_budget_seconds() == CG.DEFAULT_PASS_BUDGET_SECONDS
    monkeypatch.setenv(CG.PASS_BUDGET_ENV, "600")
    assert CG.pass_budget_seconds() == 600.0
    monkeypatch.setenv(CG.PASS_BUDGET_ENV, "0")
    assert CG.pass_budget_seconds() == 0.0


@pytest.mark.asyncio
async def test_the_budget_defers_the_tail_and_the_receipt_names_it(
    monkeypatch,
):
    """A 10 ms budget and a 100 ms target: the FIRST target finishes (the
    check is between targets, never inside one) and the other two are named
    on the receipt — the finding the cut sweep never got to write."""
    monkeypatch.setenv(CG.PASS_BUDGET_ENV, "0.01")
    targets = ["country_watch_aa", "country_watch_bb", "country_watch_cc"]
    result = await _run_fake_pool(monkeypatch, targets, delay=0.1)
    data = result.finding.data
    assert [t["target_id"] for t in data["per_target"]] == targets[:1]
    assert data["deferred_targets"] == targets[1:]
    assert "country_watch_bb" in result.finding.body
    assert any(
        CG.PASS_BUDGET_ENV in w for w in data["warnings"]
    ), "the receipt says WHY the tail did not run"


@pytest.mark.asyncio
async def test_a_disabled_budget_runs_every_target(monkeypatch):
    """``<= 0`` is the pre-fix escape hatch: the whole list, no deferral."""
    monkeypatch.setenv(CG.PASS_BUDGET_ENV, "0")
    targets = ["country_watch_aa", "country_watch_bb", "country_watch_cc"]
    result = await _run_fake_pool(monkeypatch, targets, delay=0.01)
    data = result.finding.data
    assert [t["target_id"] for t in data["per_target"]] == targets
    assert data["deferred_targets"] == []


@pytest.mark.asyncio
async def test_a_wide_budget_is_unchanged_behaviour(monkeypatch):
    """Room for the whole roster: nothing deferred, no budget warning."""
    monkeypatch.setenv(CG.PASS_BUDGET_ENV, "3600")
    targets = ["country_watch_aa", "country_watch_bb"]
    result = await _run_fake_pool(monkeypatch, targets)
    data = result.finding.data
    assert [t["target_id"] for t in data["per_target"]] == targets
    assert data["deferred_targets"] == []
    assert not any(CG.PASS_BUDGET_ENV in w for w in data["warnings"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_deferred_target_is_the_next_sweeps_first_pick(
    pg_pool, rotation_roster
):
    """The budget's promise: what a cut sweep did not reach leads the NEXT
    queue. Grading the two at the head (exactly what a two-target sweep
    writes) must leave the deferred tail — oldest-graded first — in front."""
    as_of = rotation_roster
    async with pg_pool.acquire() as conn:
        first = [
            str(r["target_id"]) for r in
            await conn.fetch(CG._TARGETS_WITH_REFERENCE_SQL, as_of, 7)
            if str(r["target_id"]).startswith(_ROTATE)
        ]
        graded, deferred = first[:2], first[2:]
        assert graded == [_ROTATE_NEVER_A, _ROTATE_NEVER_B]
        for target in graded:
            ref_id = await conn.fetchval(
                "SELECT id FROM unit_references WHERE target_id = $1", target
            )
            await conn.execute(
                """
                INSERT INTO unit_correctness (
                    id, analyst_id, target_id, head_id, as_of, reference_id,
                    rubric_sha, grain, n_claims, created_at
                ) VALUES ($1,'escalation',$2,$3,$4,$5,$6,'desk',0,$7)
                """,
                uuid4(), target, uuid4(), as_of, ref_id, RUBRIC_SHA256, as_of,
            )
        second = [
            str(r["target_id"]) for r in
            await conn.fetch(CG._TARGETS_WITH_REFERENCE_SQL, as_of, 7)
            if str(r["target_id"]).startswith(_ROTATE)
        ]
    assert second[:3] == deferred, (
        "the deferred tail leads the next sweep, in its own queue order"
    )


def test_loader_refuses_ref_bands_off_the_ladder():
    """2026-09-25: a top-up lane wrote ``energy_security: "severe"`` and the
    merge carried it into a new reference row unrecognised. The loader's pure
    fence refuses any band outside the five-band ladder + insufficient-basis."""
    import pytest

    mod = _loader()
    mod._refuse_off_ladder_bands({"escalation": "high", "energy_security": "insufficient-basis"})
    with pytest.raises(SystemExit) as exc:
        mod._refuse_off_ladder_bands({"energy_security": "severe"})
    assert "severe" in str(exc.value) and "off the ladder" in str(exc.value)

