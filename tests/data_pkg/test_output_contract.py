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

import pytest

from legba.data.analysts.inline_target import _coerce_finding
from legba.data.analysts.output_contract import (
    OutputContractError,
    extract_json_object,
    is_unusable_output,
    strip_tool_plan_preamble,
)

FALLBACK = "Assessment for target"

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
