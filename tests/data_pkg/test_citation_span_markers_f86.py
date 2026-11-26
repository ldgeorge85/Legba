# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The markers a span carries are the markers its sentence carries.

THE DEFECT the tracker row named (7 d to 2026-10-14, two units): on the judge
path ``unsupported_spans[].markers`` was ``[]`` on 492 of 771
``judge_unsupported`` spans although the span text carried ``[N]`` - while the
claim ledger's rows for the SAME sentences had the right markers. Cause: the
judge-path span was built without ``markers=`` at all; the ledger row two loops
later read them with ``_markers_in_claim``. Every judge failure therefore looked
markerless to any reader of the spans (the UI's "it cited [9]" note, an audit
asking which signal a failed claim leaned on).

Three extraction misses ride with it, each one a sentence whose marker reached
the wrong claim, or no claim, at segmentation - the stage the floor, the judge,
the ledger and the X4 uncited-share instrument all share:

  * MARKER AFTER THE TERMINATOR WITH NO SPACE - ``...13 October.[11] Kharkiv
    was struck ...``. The split needs whitespace after the terminator, so the
    two sentences were ONE claim, and the second (uncited) sentence borrowed
    the first one's marker: an uncited fact counted as cited.
  * MARKER BEHIND A CLOSING QUOTE - ``...said Kyiv "will not yield." [11]
    Kharkiv ...``. Same merge, same borrowed marker.
  * A BRACKETED COMMA LIST - ``[11, 47]`` / ``【11，47】``. The bare-marker
    regex wants ``]`` right after the digits, so the sentence read as carrying
    NO marker: a cited fact counted as uncited.

And one in the instrument itself: a BLUF LABEL alone on its line (``**BLUF:**``
then the verdict on the next line, or ``## BLUF``) was read as the BLUF, so a
read whose BLUF sentence carried its markers was stamped ``bluf_marker ==
"uncited"``.

What was already right and is pinned so it stays right: full-width ``【N】``
markers (normalised before the split), a marker after the terminator WITH a
space, a marker on the next line, bullets each their own claim, ranges.

GOLDENS. Every finding-body-like string literal and fixture under tests/ (408
bodies) segments byte-identically before and after this change; the only
instrument reading that moved is the BLUF label on its own line (two composition
bodies in test_assessment_channel_d6.py, ``uncited`` -> ``cited``; compositions
are not stamped, so no persisted number moves). The five real unit bodies below
are pinned at their unchanged counts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

from legba.data.analysts.output_contract import repair_confidence_word_token
from legba.data.provenance.citation_markers import (
    _normalize_verify_markers,
    _pull_trailing_markers,
)
from legba.data.provenance.uncited_share import (
    BLUF_CITED,
    BLUF_NONE,
    BLUF_UNCITED,
    uncited_share_fields,
)
from legba.data.provenance.verify import (
    _deterministic_floor,
    _markers_in_claim,
    _segment_claims,
    verify_finding_faithfulness,
)

FIXTURES = Path(__file__).parent / "fixtures" / "confidence_word_token"


def _claims(body: str) -> list[tuple[str, list[int]]]:
    return [(c, _markers_in_claim(c, subclaim=False)) for c in _segment_claims(body)]


def _no_citation(body: str) -> int:
    report = _deterministic_floor(body, [])
    return sum(1 for s in report.unsupported_spans if s.reason == "no_citation")


# ---------------------------------------------------------------------------
# 1. The judge-path span carries the markers the ledger row carries
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _AllUnsupportedJudge:
    subprovider = "stub-judge"

    def __init__(self, n: int) -> None:
        self._json = json.dumps({"verdicts": ["unsupported"] * n})

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None, system=None, **kw):
        return _Response(self._json)


#: Three sentences of the internal_stability read
#: (fixtures/confidence_word_token/int_rep_0807__new__s1.txt), one re-spelled in
#: full-width brackets and one with a range - the three marker shapes a unit
#: span carries.
_JUDGED_BODY = (
    "Police use of tear gas and water cannons demonstrates "
    "a hard-line state response [1][2].\n"
    "The surge of several thousand demonstrators on 6-7 August was reported "
    "by the Example Port Gazette and a wire service 【4】.\n"
    "The ruling bloc withdrew the harbour-ownership chapter on 5 August [1-2].\n"
)


async def test_a_judge_unsupported_span_carries_its_markers(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    cites = [
        {"marker": f"[{n}]", "signal_id": str(uuid4()), "title": f"t{n}", "source_text": "x"}
        for n in (1, 2, 4)
    ]
    report = await verify_finding_faithfulness(
        body=_JUDGED_BODY, citations=cites, judge_llm=_AllUnsupportedJudge(3)
    )
    assert report.judge_status == "llm"
    spans = [s for s in report.unsupported_spans if s.reason == "judge_unsupported"]
    # Before the fix every one of these was [].
    assert [s.markers for s in spans] == [[1, 2], [4], [1, 2]]
    # ... and they are the ledger's own reading of the same sentences.
    ledger = {cv.text: cv.markers for cv in report.claim_verdicts}
    assert all(ledger[s.text] == s.markers for s in spans)
    # The persisted dict carries them too.
    assert [s.as_dict()["markers"] for s in spans] == [[1, 2], [4], [1, 2]]


async def test_a_composition_judge_span_carries_its_ref_ordinals(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = (
        "Ukraine's raids on Russian refineries intensified this week [[ref:1]].\n"
        "Saudi fuel exports fell after the Ras Tanura outage [[ref:2]][[ref:3]].\n"
    )
    cites = [
        {"marker": f"[[ref:{n}]]", "ref_kind": "finding", "finding_id": str(uuid4()),
         "source_text": f"sub-claim {n}"}
        for n in (1, 2, 3)
    ]
    report = await verify_finding_faithfulness(
        body=body, citations=cites, judge_llm=_AllUnsupportedJudge(2)
    )
    judged = [s for s in report.unsupported_spans if s.reason.startswith("judge_")]
    assert [s.markers for s in judged] == [[1], [2, 3]]


# ---------------------------------------------------------------------------
# 2. Segmentation: each marker stays on the sentence it ends
# ---------------------------------------------------------------------------


def test_a_marker_glued_to_the_terminator_no_longer_cites_the_next_sentence() -> None:
    body = "Kyiv was hit by missiles on 13 October.[11] Kharkiv was struck by drones the same day.\n"
    assert _claims(body) == [
        ("Kyiv was hit by missiles on 13 October [11].", [11]),
        ("Kharkiv was struck by drones the same day.", []),
    ]
    f = uncited_share_fields(body)
    assert (f["fact_sentences"], f["uncited_fact_sentences"]) == (2, 1)  # was (1, 0)
    assert f["uncited_fact_sentences"] == _no_citation(body)


def test_a_marker_behind_a_closing_quote_stays_on_its_sentence() -> None:
    for q in ('"', "”"):
        body = f"Zelensky said Kyiv {q}will not yield.{q} [11] Kharkiv was struck by drones the same day.\n"
        claims = _claims(body)
        assert [m for _, m in claims] == [[11], []], q
        assert claims[0][0] == f"Zelensky said Kyiv {q}will not yield{q} [11]."
        assert uncited_share_fields(body)["uncited_fact_sentences"] == 1 == _no_citation(body)


#: The earlier trailing-marker rule, verbatim.
_P7_PULL_RE = re.compile(
    r"([.!?])(\s+)((?:\[\d+\]|\[\[ref:\d+\]\])(?:\s*(?:\[\d+\]|\[\[ref:\d+\]\]))*)"
)


def _p7_pull(text: str) -> str:
    return _P7_PULL_RE.sub(lambda m: f" {m.group(3)}{m.group(1)}{m.group(2)}", text)


@pytest.mark.parametrize(
    "text",
    [
        "…fighters. [[ref:1]] Next",
        "…zones.\n[21]\nNext",
        "…zones. [21][26] Next",
        "…zones. [21] [26] Next",
        "Is it? [3]\n- bullet [4]",
        "no marker here. Next",
    ],
)
def test_the_original_trailing_marker_shape_is_rewritten_byte_identically(text) -> None:
    """The earlier shape - terminator, whitespace, markers - comes out exactly as
    the rule this one replaces wrote it."""
    assert _pull_trailing_markers(text) == _p7_pull(text)
    assert _P7_PULL_RE.search(text) or "marker" in text


def test_a_bracketed_comma_list_is_one_marker_per_member() -> None:
    for spelled in ("[11, 47]", "[11,47]", "【11，47】", "［11, 47］", "【11、47】"):
        body = f"Kyiv was hit by missiles on 13 October {spelled}.\n"
        assert _claims(body) == [("Kyiv was hit by missiles on 13 October [11][47].", [11, 47])], spelled
        assert uncited_share_fields(body)["uncited_fact_sentences"] == 0  # was 1
    # Not a citation list: a single bracketed number is already a marker, a
    # composition compound is the ref rule's, and a four-digit member is a year.
    assert _normalize_verify_markers("[[ref:2,6]]") == "[[ref:2]][[ref:6]]"
    assert _normalize_verify_markers("in [2023, 2024]") == "in [2023, 2024]"


@pytest.mark.parametrize(
    "body,want",
    [
        # Full-width markers are normalised before the split.
        ("**BLUF:** Russian strikes on Kyiv intensified 【11】【47】.\n",
         [("**BLUF:** Russian strikes on Kyiv intensified [11][47].", [11, 47])]),
        ("**BLUF:** Russian strikes on Kyiv intensified. 【11】\n",
         [("**BLUF:** Russian strikes on Kyiv intensified [11].", [11])]),
        # A marker on the next line glues to the sentence it ends.
        ("Kyiv was hit by missiles on 13 October.\n[11]\nKharkiv was struck.\n",
         [("Kyiv was hit by missiles on 13 October [11].", [11]), ("Kharkiv was struck.", [])]),
        # Bullets are each their own claim, terminal punctuation or not.
        ("- Kyiv was hit by missiles on 13 October [11]\n- Kharkiv was struck by drones\n",
         [("- Kyiv was hit by missiles on 13 October [11]", [11]),
          ("- Kharkiv was struck by drones", [])]),
        # A range cites its members.
        ("Kyiv was hit by missiles on 13 October [11-13].\n",
         [("Kyiv was hit by missiles on 13 October [11-13].", [11, 12, 13])]),
    ],
)
def test_the_shapes_that_were_already_right_stay_right(body, want) -> None:
    assert _claims(body) == want


# ---------------------------------------------------------------------------
# 3. The BLUF is the verdict sentence, not its label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["**BLUF:**", "**BLUF**", "## BLUF", "BLUF:"])
def test_a_bluf_label_on_its_own_line_reads_the_sentence_under_it(label: str) -> None:
    cited = f"{label}\nRussian strikes on Kyiv intensified on 13 October [11].\n"
    uncited = f"{label}\nRussian strikes on Kyiv intensified on 13 October.\n"
    assert uncited_share_fields(cited)["bluf_marker"] == BLUF_CITED  # was uncited
    f = uncited_share_fields(uncited)
    assert f["bluf_marker"] == BLUF_UNCITED
    assert f["uncited_fact_sentences"] == _no_citation(uncited)


def test_the_bluf_label_edges() -> None:
    # A label with nothing after it cites nothing.
    assert uncited_share_fields("**BLUF:**\n")["bluf_marker"] == BLUF_UNCITED
    # The same-line form is unchanged.
    assert uncited_share_fields("**BLUF:** Tehran resumed enrichment [2].")["bluf_marker"] == BLUF_CITED
    assert uncited_share_fields("**BLUF:** Tehran resumed enrichment.")["bluf_marker"] == BLUF_UNCITED
    # A body with no BLUF still says so.
    assert uncited_share_fields("Tehran resumed enrichment [2].")["bluf_marker"] == BLUF_NONE


# ---------------------------------------------------------------------------
# 4. Goldens - five unit bodies, counts unchanged by the fix
# ---------------------------------------------------------------------------

#: The bodies behind the confidence-token fixtures (invented subjects):
#: a disruption_status, an internal_stability and three narrative_coordination
#: reads - all written without any cited-sentence rule, all with an uncited
#: BLUF, which is the shape the newer rule extends to.
_GOLDEN = {
    "dis_port_0818__new__s2.txt": (5, 2, BLUF_UNCITED),
    "int_rep_0807__new__s1.txt": (5, 3, BLUF_UNCITED),
    "nar_rep_0817__v2r4__s8.txt": (4, 3, BLUF_UNCITED),
    "nar_rep_0818__old__s2.txt": (5, 4, BLUF_UNCITED),
    "nar_rep_0818__v2r2__s5.txt": (5, 3, BLUF_UNCITED),
}


@pytest.mark.parametrize("name", sorted(_GOLDEN))
def test_the_fixture_bodies_keep_their_counts(name: str) -> None:
    body = json.loads(repair_confidence_word_token((FIXTURES / name).read_text()))["body"]
    f = uncited_share_fields(body)
    assert (f["fact_sentences"], f["uncited_fact_sentences"], f["bluf_marker"]) == _GOLDEN[name]
    assert f["uncited_fact_sentences"] == _no_citation(body)
