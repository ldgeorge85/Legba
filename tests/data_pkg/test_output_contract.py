# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-N1 — the inline_target OUTPUT CONTRACT, pinned on the REAL write path.

THE DEFECT THESE TESTS ENCODE. Finding
``16f6a460-541a-4deb-b536-407433411173`` (``cross_doc_corroborator``, live
2026-08-04) was persisted with the sentence ``We will use search_corpus.`` as
its TITLE and a raw JSON blob as its BODY. Its own contract — a real title, a
BLUF body, ``confidence``, ``evidence``, ``tags`` — sat one line below the plan
sentence, fully parseable, and was discarded because every recovery path in
``_coerce_finding`` was anchored on offset 0. Measured over the analyst's last
10 findings: **4 of 10** carried a tool-plan preamble plus an unparsed JSON
blob, each collapsed to the 0.3 ``unstructured`` fallback.

The fixtures below are the SHAPES those four take, and the assertions are
written against ``_coerce_finding`` — the function ``run_method`` actually
calls at REFLECT — rather than against the helpers in isolation, so a
regression that re-anchors recovery at offset 0 is a red test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legba.data.analysts.inline_target import _coerce_finding, _coerce_indicators
from legba.data.analysts.output_contract import (
    OutputContractError,
    extract_json_object,
    is_unusable_output,
    repair_confidence_word_token,
    salvage_json_envelope,
    strip_tool_plan_preamble,
)

FALLBACK = "Assessment for target"

#: Raw completion bytes for the five cells that evidenced the "0. nine"
#: confidence token — see the V-P1 section below.
_CONFIDENCE_WORD_TOKEN_FIXTURES = Path(__file__).parent / "fixtures" / "confidence_word_token"

#: The contract the corroborator emits, verbatim in shape (abridged body).
_CONTRACT = {
    "title": "Gaza mass funeral for 112 victims — corroborated by 2 outlets",
    "body": (
        "**BLUF:** A mass funeral in Gaza for 112 people killed in an Israeli "
        "strike on 23 Nov 2023 is reported by multiple outlets [65].\n\n"
        "### Corroboration\n- **Dawn (Pakistan)** reports the funeral [65].\n"
        "- **ABC News** reports the same [27].\n"
    ),
    "confidence": 0.88,
    "evidence": ["0cd1dc42-8f11-482c-911a-eb75dbdc8faa"],
    "tags": ["severity:moderate", "topic:cross_doc_corroboration"],
}


def _blob(preamble: str) -> str:
    """The live failure shape: a plan sentence, then the JSON contract."""
    return f"{preamble}\n{json.dumps(_CONTRACT, indent=2)}"


# ---------------------------------------------------------------------------
# The headline case — a JSON contract behind a tool-plan preamble
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "preamble",
    [
        # All four live titles, verbatim.
        "We will use search_corpus.",
        "Attempting to call search_corpus...",
        "We will call search_corpus.",
        "We need to use search_corpus etc.",
        # Neighbouring forms the same prompt can produce.
        "Let me search the corpus for independent coverage.",
        "I'll start by running a vector_search.",
        "First, I will gather more documents.",
        "Okay, let's find corroborating sources.",
    ],
)
def test_a_contract_behind_a_plan_sentence_lands_a_clean_finding(preamble: str) -> None:
    """The whole point: the model's OWN contract survives its preamble.

    Before V-N1 every one of these produced ``title == preamble``,
    ``confidence == 0.3``, ``tags == ['unstructured']`` and a body holding the
    raw JSON text.
    """
    finding = _coerce_finding(_blob(preamble), fallback_title=FALLBACK)

    # The model's real fields, not the fallback's.
    assert finding.title == _CONTRACT["title"]
    assert finding.confidence == pytest.approx(0.88)
    assert finding.evidence == _CONTRACT["evidence"]
    assert "severity:moderate" in finding.tags
    assert "unstructured" not in finding.tags

    # The body is rendered markdown, and carries no JSON scaffolding.
    assert finding.body.startswith("**BLUF:**")
    assert '"title":' not in finding.body
    assert '"confidence":' not in finding.body

    # The plan sentence is gone from BOTH reader-facing fields.
    assert preamble not in finding.title
    assert preamble not in finding.body


def test_the_exact_live_defect_no_longer_titles_itself_with_its_plan() -> None:
    """The precise 16f6a460 shape, asserted field by field."""
    finding = _coerce_finding(
        _blob("We will use search_corpus."), fallback_title=FALLBACK,
    )
    assert finding.title != "We will use search_corpus."
    assert finding.title == _CONTRACT["title"]
    assert finding.confidence > 0.3
    assert finding.tags != ["unstructured"]


def test_a_plan_sentence_inside_the_contract_body_is_stripped_too() -> None:
    """The model narrates, THEN serializes what it narrated.

    Order is load-bearing: the title falls back to the body's first line, so a
    strip that ran after title derivation would still surface the plan sentence
    as the headline.
    """
    payload = dict(_CONTRACT)
    payload["title"] = ""
    payload["body"] = "We will use search_corpus.\n" + str(_CONTRACT["body"])
    finding = _coerce_finding(json.dumps(payload), fallback_title=FALLBACK)
    assert not finding.body.startswith("We will use")
    assert finding.title != "We will use search_corpus."
    assert finding.body.startswith("**BLUF:**")


def test_a_plan_sentence_in_front_of_a_TRUNCATED_envelope_still_salvages() -> None:
    """The one shape neither recovery half handles alone.

    ``_salvage_envelope_body`` tests ``startswith("{")``, so a preamble in front
    of a stream that cut off mid-``body`` would defeat it exactly the way it
    defeated the primary parse. Stripping must therefore run BEFORE salvage.
    """
    truncated = (
        'We will use search_corpus.\n'
        '{"title": "Gaza funeral", "body": "**BLUF:** A mass funeral was held'
    )
    finding = _coerce_finding(truncated, fallback_title=FALLBACK)
    assert finding.body.startswith("**BLUF:**")
    assert "A mass funeral was held" in finding.body
    assert '"title":' not in finding.body
    assert finding.title != "We will use search_corpus."


def test_a_title_that_is_only_a_plan_sentence_falls_through() -> None:
    """An emptied title takes the next source, never the plan sentence."""
    payload = dict(_CONTRACT)
    payload["title"] = "Let me check the corpus."
    finding = _coerce_finding(json.dumps(payload), fallback_title=FALLBACK)
    assert finding.title != "Let me check the corpus."
    assert finding.title.startswith("**BLUF:**") or "Gaza" in finding.title


# ---------------------------------------------------------------------------
# Loud failure — garbage must not become a row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   \n\t  ",
        "{}",
        "{\n}\n",
        "```json\n{\n}\n```",
        "[ ] , : \" '",
        # The live vacuous-pass shape: an empty completion that scored
        # faithfulness 1.00 on 0 claims (finding dd916255).
        "\n\n",
        # Pure plan, no content behind it at all.
        "We will use search_corpus.",
        "Let me search the corpus.\nI will then read the documents.",
    ],
)
def test_unusable_output_raises_instead_of_writing_a_row(raw: str) -> None:
    """Degrade-not-FABRICATE: no readable content means no finding.

    A garbage row is worse than a failed run — it is indistinguishable from
    analysis at every layer above ``_coerce_finding``, and the only thing that
    caught the live one was a human reading it.
    """
    with pytest.raises(OutputContractError):
        _coerce_finding(raw, fallback_title=FALLBACK)


# ---------------------------------------------------------------------------
# The D27 prose path must survive untouched — prose IS content
# ---------------------------------------------------------------------------


def test_plain_markdown_prose_still_lands_a_finding() -> None:
    """A model that answers in markdown instead of JSON is a formatting miss
    over real analysis. It must still land, exactly as before V-N1."""
    prose = (
        "## Strait of Hormuz transits collapse\n\n"
        "**BLUF:** Transits fell from ~120/day to under 11/day [4].\n"
    )
    finding = _coerce_finding(prose, fallback_title=FALLBACK)
    assert finding.tags == ["unstructured"]
    assert finding.confidence == pytest.approx(0.3)
    assert finding.title == "Strait of Hormuz transits collapse"
    assert "Transits fell" in finding.body


def test_a_well_formed_contract_is_untouched() -> None:
    """The overwhelmingly common case is byte-for-byte what it always was."""
    finding = _coerce_finding(json.dumps(_CONTRACT), fallback_title=FALLBACK)
    assert finding.title == _CONTRACT["title"]
    assert finding.body == _CONTRACT["body"]
    assert finding.confidence == pytest.approx(0.88)


def test_estimative_first_person_is_not_a_plan_sentence() -> None:
    """"We assess…" is house estimative language (ANALYTIC_PREAMBLE rule 4).

    The strip targets PROCESS narration — which tool is about to be called —
    never a statement about the world. Getting this wrong would truncate real
    analysis, so it is pinned.
    """
    for opener in (
        "We assess that the closure is likely to hold [3].",
        "We judge the corroboration sufficient [1][2].",
        "We cannot confirm the casualty figure from the cited documents.",
        "I assess this as a single-sourced claim.",
    ):
        content, preamble = strip_tool_plan_preamble(opener)
        assert preamble == "", opener
        assert content == opener


# ---------------------------------------------------------------------------
# Confidence coercion — an out-of-range number must not destroy the finding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_conf", "expected"),
    [(1.5, 1.0), (-0.2, 0.0), (0.88, 0.88), ("0.7", 0.7), ("high", 0.5), (None, 0.5)],
)
def test_confidence_is_clamped_not_fatal(raw_conf: object, expected: float) -> None:
    """``float(parsed['confidence'])`` used to raise inside the field-extraction
    try, and the broad handler then threw away an OTHERWISE-GOOD finding — its
    real title, body, evidence and tags replaced by a raw-text blob. A bad
    confidence is a miscalibration, not a corrupt finding."""
    payload = dict(_CONTRACT)
    payload["confidence"] = raw_conf
    finding = _coerce_finding(json.dumps(payload), fallback_title=FALLBACK)
    assert finding.confidence == pytest.approx(expected)
    # The analysis survived.
    assert finding.title == _CONTRACT["title"]
    assert "coerce_failed" not in finding.tags


# ---------------------------------------------------------------------------
# The extractor's own properties
# ---------------------------------------------------------------------------


def test_extract_json_object_is_string_aware() -> None:
    """A naive depth counter closes on the first ``}`` it sees — including one
    inside a JSON string value, which an analytic body legitimately contains."""
    text = 'PLAN.\n{"body": "a brace } and another {", "title": "t"}'
    blob = extract_json_object(text)
    assert blob is not None
    assert json.loads(blob) == {"body": "a brace } and another {", "title": "t"}


def test_extract_json_object_finds_an_object_at_any_offset() -> None:
    assert extract_json_object("no object here") is None
    assert extract_json_object('prefix {"a": 1} suffix') == '{"a": 1}'
    assert extract_json_object('{"a": {"b": 2}}') == '{"a": {"b": 2}}'


def test_extract_json_object_refuses_a_truncated_object() -> None:
    """An unterminated object is ``_salvage_envelope_body``'s job, not this
    one's — returning a half object here would hand ``json.loads`` a guess."""
    assert extract_json_object('{"title": "t", "body": "cut off mid') is None


def test_is_unusable_output_keeps_one_line_of_prose() -> None:
    """The predicate that turns a degrade into a RAISE must never fire on a
    real body. One line of prose is enough to keep the row."""
    assert is_unusable_output("") is True
    assert is_unusable_output("{\n}\n") is True
    assert is_unusable_output("```json\n{\n}\n```") is True
    assert is_unusable_output("{\nsomething happened\n}") is False
    assert is_unusable_output("A single sentence of analysis.") is False


# ---------------------------------------------------------------------------
# V-P1 — the confidence digit-then-number-word token ("0. nine")
# ---------------------------------------------------------------------------
#
# THE DEFECT THESE TESTS ENCODE. The core plane occasionally spells out the
# confidence value's fractional digit ("0. nine" instead of "0.9"). "0." is
# not a valid JSON number on its own, so BOTH ``_coerce_finding``'s primary
# parse and ``parse_finding_envelope``'s "find it anywhere" recovery raised
# on the same token and fell through to ``_unstructured_finding`` — landing
# a 0.9-confidence coordination call at a flat 0.30 with an EMPTY
# ``indicators`` array. Measured in ``planning/VOICE_REPLAY_2026-08-20/
# runs/REVISION_RESULT_2026-08-21.md`` §5: five cells, all five spelling out
# "nine", all five a confident coordination-positive call — three in the
# 2026-08-21 narrative replay (``narrative_coordination``), two more in the
# frozen 2026-08-20 corpus (``disruption_status``, ``internal_stability``).
# The fixtures below are those five cells' raw completion bytes,
# byte-for-byte.


def test_repair_confidence_word_token_maps_zero_through_nine() -> None:
    """The bounded word list the fractional digit can spell out — not a
    general English-number parser, just this contract's own value shape."""
    words = (
        "zero", "one", "two", "three", "four",
        "five", "six", "seven", "eight", "nine",
    )
    for digit, word in enumerate(words):
        text = f'"confidence": 0. {word},'
        assert repair_confidence_word_token(text) == f'"confidence": 0.{digit},'


def test_repair_confidence_word_token_is_a_noop_on_well_formed_input() -> None:
    """``0.9`` has no whitespace after the dot — the pattern never matches
    it, so the overwhelmingly common case is byte-for-byte untouched."""
    text = '{"confidence": 0.9, "title": "x"}'
    assert repair_confidence_word_token(text) == text


def test_repair_confidence_word_token_leaves_body_prose_alone() -> None:
    """Anchored on the ``"confidence":`` key — a body sentence that happens
    to spell out a number in the same shape is never touched."""
    text = '{"body": "risk fell to 0. nine on the index", "confidence": 0. nine}'
    repaired = repair_confidence_word_token(text)
    assert repaired == (
        '{"body": "risk fell to 0. nine on the index", "confidence": 0.9}'
    )


# The evidenced token shape is IDENTICAL across all five cells: a digit, a
# literal ".", exactly one space, then the word — see the fixture bytes
# under fixtures/confidence_word_token/. No other whitespace variant (e.g.
# a space before the dot) was found in any of the five, so none is invented
# here.
_CONFIDENCE_WORD_TOKEN_CASES = [
    # (fixture filename, label, model's own `indicators` array survives coercion whole)
    ("nar_jp_0818__old__s2.txt", "nar_jp_0818 OLD s2 (frozen arm)", True),
    ("nar_jp_0818__v2r2__s5.txt", "nar_jp_0818 v2r2 s5", True),
    ("nar_jp_0817__v2r4__s8.txt", "nar_jp_0817 v2r4 s8", True),
    ("dis_hormuz_0818__new__s2.txt", "dis_hormuz_0818 new s2", True),
    # int_ar_0807's OWN indicators carry non-ISO dates ("21 August 2026") —
    # a separate, pre-existing IndicatorEntry validation drop, unrelated to
    # this defect. The DS-1 prose-derivation fallback fires instead and
    # still yields a non-empty array: the "indicators must never come back
    # EMPTY" property this fix is about still holds, via the sibling path.
    ("int_ar_0807__new__s1.txt", "int_ar_0807 new s1", False),
]


@pytest.mark.parametrize(
    ("fixture_name", "label", "structured_indicators_survive_whole"),
    _CONFIDENCE_WORD_TOKEN_CASES,
    ids=[c[1] for c in _CONFIDENCE_WORD_TOKEN_CASES],
)
def test_confidence_word_token_cells_land_the_intended_confidence_and_indicators(
    fixture_name: str, label: str, structured_indicators_survive_whole: bool,
) -> None:
    """The exact raw bytes of the five evidenced cells, through the REAL
    parse entry point (``_coerce_finding``) — confidence 0.9 recovered AND
    the sibling ``indicators`` array survives, rather than degrading to the
    0.30/empty-indicators salvage the defect used to produce.
    """
    raw = (_CONFIDENCE_WORD_TOKEN_FIXTURES / fixture_name).read_text(encoding="utf-8")
    assert raw.count("0. nine") == 1, f"{label}: fixture lost the evidenced token"

    finding = _coerce_finding(raw, fallback_title=FALLBACK)

    assert finding.confidence == pytest.approx(0.9), label
    assert "unstructured" not in finding.tags, label
    assert "coerce_failed" not in finding.tags, label

    indicators = finding.data.get("indicators")
    assert indicators, f"{label}: indicators array is empty — the exact defect this fixes"

    if structured_indicators_survive_whole:
        # The model's OWN structured array, run through the same coercer a
        # well-formed parse would use — proof the fix landed the real
        # structured entries, not a lossy prose-derived stand-in.
        well_formed = json.loads(raw.replace("0. nine", "0.9"))
        expected = _coerce_indicators(well_formed["indicators"])
        assert expected, f"{label}: test fixture setup is broken"
        assert indicators == expected, label


# ---------------------------------------------------------------------------
# The JSON-ENVELOPE LEAK (2026-08-29) — salvage_json_envelope
#
# Live defect: the world composition of 2026-08-29 12:00Z
# (823ff9dd-89b9-47f9-9c7e-8c9cc631e31d) published its JSON WRAPPER as the body.
# The model emitted an unrequested sixth key, `body_additional_sections`, and
# emitted it MALFORMED — `"## Tension": "…": "…"`, two string values for one
# key — so json.loads raised and the fail-safe branch stored the raw envelope.
# The `title` and `body` string literals were COMPLETE two lines above the
# break. These pin the rule: unwrap when unambiguous, raise otherwise, and
# never publish the wrapper.
# ---------------------------------------------------------------------------

#: The live captures, byte-exact out of ``analyst_outputs``.
_ENVELOPE_LEAK_FIXTURES = Path(__file__).parent / "fixtures" / "json_envelope_leak"


def test_salvage_unwraps_the_live_world_composition_envelope() -> None:
    """The exact 823ff9dd raw bytes unwrap to the model's markdown body and its
    real headline — NOT to the JSON wrapper that actually shipped."""
    raw = (_ENVELOPE_LEAK_FIXTURES / "world_assessor_823ff9dd__leaked.txt").read_text(
        encoding="utf-8"
    )
    # Fixture integrity: this MUST still be the shape that broke, or the test
    # is vacuous.
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert "body_additional_sections" in raw

    salvaged = salvage_json_envelope(raw)

    expected = (
        _ENVELOPE_LEAK_FIXTURES / "world_assessor_823ff9dd__unwrapped.md"
    ).read_text(encoding="utf-8")
    assert salvaged["body"] == expected
    assert salvaged["title"] == (
        "Russia mobilization heightens European escalation risk amid global hotspots"
    )
    # The whole point: no JSON scaffolding survives into the body.
    assert not salvaged["body"].lstrip().startswith("{")
    assert '"body":' not in salvaged["body"]
    assert "\\n" not in salvaged["body"]
    # The markers the faithfulness judge segments on are intact — the shipped
    # blob scored 2 checkable claims against the healthy run's 13.
    assert salvaged["body"].count("[[ref:") >= 5


def test_salvage_accepts_literal_newlines_inside_the_body_string() -> None:
    """The SECOND malformation shape in the live census (8 of the 10 composition
    envelopes): a body written across REAL newlines instead of ``\\n`` escapes.
    ``json.loads`` rejects the envelope for it; a raw newline inside a string
    span means a newline and nothing else, so the unwrap stays unambiguous."""
    raw = (
        '{\n  "title": "T",\n  "body": "line one\nline two [[ref:1]]",\n'
        '  "confidence": 0.6\n}'
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)

    salvaged = salvage_json_envelope(raw)

    assert salvaged["body"] == "line one\nline two [[ref:1]]"
    assert salvaged["title"] == "T"


def test_salvage_reads_the_envelopes_own_body_key_not_one_inside_a_value() -> None:
    """Depth-1 and quote-aware: a ``"body":`` sequence sitting INSIDE another
    member's string value is never mistaken for the envelope's own key — the
    difference between an unambiguous unwrap and a regex guess."""
    raw = (
        '{\n  "title": "the model quoted \\"body\\": \\"decoy\\" at us",\n'
        '  "body": "the real markdown [[ref:1]]",\n  "tags": [oops]\n}'
    )
    assert salvage_json_envelope(raw)["body"] == "the real markdown [[ref:1]]"


@pytest.mark.parametrize(
    "raw,label",
    [
        ('{\n  "title": "World read",\n  "body": "*As of 29 August 2026; composed'
         ' from 6 region', "truncated mid-body string"),
        ('{\n  "title": "T",\n  "body": "complete markdown",\n  "tags": ["a',
         "object never closes"),
        ('{\n  "action": "search_corpus",\n  "query": "Trump Hormuz",\n  "size": 5\n}',
         "tool-call JSON, no body key"),
        ('{\n  "title": "T",\n  "body": {"nested": "not a string"}\n}',
         "body is not a string"),
        ('{\n  "title": "T",\n  "body": "bad \\escape here"\n}',
         "body escapes do not decode"),
        ('{\n  "title": "T",\n  "body": "   "\n}', "body decodes to nothing"),
    ],
    ids=lambda v: v if isinstance(v, str) and " " in v and "{" not in v else "",
)
def test_salvage_fails_loud_on_anything_ambiguous(raw: str, label: str) -> None:
    """JSON-shaped but not unambiguously recoverable = RAISE. There is no third
    outcome, and in particular publishing the wrapper is not one."""
    with pytest.raises(OutputContractError):
        salvage_json_envelope(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "**BLUF:** Myanmar's repression continues [[ref:1]].",
        "",
        "   ",
        "Here is my read.\n\n## The picture\nThings happened [[ref:2]].",
    ],
)
def test_salvage_leaves_prose_entirely_alone(raw: str) -> None:
    """A completion that is not JSON-shaped returns ``{}`` — the caller's own
    degrade path keeps it byte-for-byte, so D27's plain-markdown finding still
    lands and no prose completion can be turned into a failure by this fix."""
    assert salvage_json_envelope(raw) == {}


def test_salvage_is_a_noop_on_well_formed_json_callers_never_reach_it() -> None:
    """Belt and braces: even handed a PARSEABLE envelope (which no caller does —
    ``json.loads`` takes that path first), the unwrap returns that same body
    rather than inventing a different one."""
    raw = json.dumps({"title": "T", "body": "b [[ref:1]]", "confidence": 0.7})
    assert salvage_json_envelope(raw) == {"title": "T", "body": "b [[ref:1]]"}
