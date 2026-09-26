# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""o4 — THE APERTURE GUARD: the mark, and the authority that may not grade it.

THE LIVE SPECIMEN, and every fixture here is taken from it. ``world_assessment``
finding ``eefce385`` (2026-09-25 12:35Z) graded 7 of 8 claims supported and lost
the eighth — the last sentence of its mandated ``## What this reading misses``
section — to an ``absence_slice_contradicted`` HARD fail, which put the read on
the demotion floor at 0.50. Two of the fourteen world reads in seven days.

The sentence generalised: it denied *"gaps in coverage of possible proliferation
developments, additional economic-coercion or escalation dynamics, and broader
G20-level political or security shifts"* on a record that had just handed the
voice twenty-six uncarried roster units BY NAME (the sentence before it names
them). That is the guess the declared aperture exists to retire, and the prompt
already forbids it in terms.

TWO HALVES, DELIBERATELY INDEPENDENT, and this file proves each against the
other's absence:

  * THE MARK — ``assessment_unsupported.aperture_guess``. Deterministic, inside
    the aperture section only, on an absence assertion that names none of the
    record's declared units. Published inline with UTF-16-exact offsets like
    every sibling class. It says WHAT is wrong with the sentence.
  * THE ROUTING — ``assessment_aperture`` + V-B's router. The whole section
    leaves the slice route, because the retained input slice is what the surface
    WAS SHOWN and a row in it can never refute a claim about what went
    UNCARRIED. It says WHO is not allowed to decide it — and V-I5's binding rule
    carries that to the judge path as well, so the hard class is unavailable on
    both. The defect still fails; only the unearned severity goes.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from legba.data.analysts import assessment_unsupported as au
from legba.data.provenance import absence_slice as asl
from legba.data.provenance import assessment_aperture as aa
from legba.data.provenance.judge_quote_rules import claim_is_routed_out
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    FAIL_CLASS_SOFT,
    fail_class_for_reason,
    verify_finding_faithfulness,
)

# ---------------------------------------------------------------------------
# The record, the prose. A spine shaped like the live one: two carried blocks,
# a roster with units it did not carry, a drop ledger with one ranked-out read.
# ---------------------------------------------------------------------------

SPINE: dict[str, Any] = {
    "blocks": [
        {
            "ordinal": 1,
            "desk": "escalation",
            "target_id": "country_watch_pk",
            "target_name": "Watch — Pakistan",
            "question": "escalation",
            "spans": [{"text": "Pakistan’s precision airstrikes into Afghanistan"}],
        },
        {
            "ordinal": 2,
            "desk": "economic_coercion",
            "target_id": "country_watch_ua",
            "target_name": "Watch — Ukraine",
            "spans": [{"text": "Ukraine’s drone campaign against Russian refineries"}],
        },
    ],
    "coverage_roster": ["country_g20_ar", "country_g20_au", "country_watch_sd"],
    "coverage": [{"unit": "country_g20_ar", "status": "in_basis"}],
    "unit_names": {
        "country_g20_ar": "G20 — Argentina",
        "country_g20_au": "G20 — Australia",
        "country_watch_sd": "Watch — Sudan",
        "country_watch_pk": "Watch — Pakistan",
        "country_watch_ua": "Watch — Ukraine",
    },
    "drops": {
        "not_selected": [
            {
                "desk": "escalation",
                "target_id": "country_watch_sd",
                "target_name": "Watch — Sudan",
                "title": "Sudan: RSF shelling resumes over El Fasher",
            }
        ],
        "counts": {"invisible_heads": 3},
    },
    "lead": {"kind": "co_leads", "test": {"n_candidates": 33}},
}

#: THE SENTENCE, verbatim from the live body (the U+2011 non-breaking hyphens
#: the core plane emits included — they are what ``text_fold`` exists for, and a
#: fixture that quietly ASCII-fies them would test a sentence nobody wrote).
GUESS_SENTENCE = (
    "The absence of these reads leaves gaps in coverage of possible "
    "proliferation developments, additional economic‑coercion or escalation "
    "dynamics, and broader G20‑level political or security shifts that could "
    "materially alter the cross‑country picture."
)

#: The sentence BEFORE it in the same live body — the honest one, which names
#: the units. It must cost nothing.
NAMED_SENTENCE = (
    "The record’s declared aperture lists the roster units that were not "
    "carried, including G20 — Argentina, Australia and Watch — Sudan [[ref:1]]."
)


def _body(*aperture_lines: str, section: str = "What this reading misses") -> str:
    return (
        "**Two threads carry this window**\n"
        "\n"
        "**BLUF:** Pakistan’s cross‑border escalation [[ref:1]] and Ukraine’s "
        "economic coercion [[ref:2]] co‑lead the window.\n"
        "\n"
        f"## {section}\n"
        "\n" + "\n".join(aperture_lines) + "\n"
    )


def _marks(body: str, spine: dict[str, Any] | None = None):
    return au.find_unsupported(body, spine if spine is not None else SPINE)


def _guesses(marks) -> list[dict[str, Any]]:
    return [m for m in marks if m["class"] == au.UNSUPPORTED_APERTURE_GUESS]


# ===========================================================================
# 1 — THE MARK
# ===========================================================================


def test_the_guess_class_fires_on_the_live_sentence() -> None:
    """THE SPECIMEN. The mark lands on the absence CLAUSE, not the sentence."""
    body = _body(GUESS_SENTENCE)
    marks, _ = _marks(body)
    hit = _guesses(marks)
    assert len(hit) == 1
    assert hit[0]["detector"] == au.DETECTOR_DETERMINISTIC
    assert hit[0]["text"].startswith("absence of these reads")
    assert "naming none of the" in hit[0]["note"]
    assert "aperture" in hit[0]["note"]


def test_the_offsets_are_utf16_exact_like_every_sibling() -> None:
    """The reader slices ``body.slice(char_start, char_end)`` in JavaScript, so
    the offsets are UTF-16 code units. An ASTRAL character before the mark moves
    the Python code-point index and the published offset by different amounts;
    ``mark_text`` slices the way the reader does, so it is the proof."""
    for lead in ("", "🛰 "):
        body = _body(f"{lead}{GUESS_SENTENCE}")
        marks, _ = _marks(body)
        hit = _guesses(marks)
        assert len(hit) == 1
        assert au.mark_text(body, hit[0]) == hit[0]["text"], (
            "the published offsets do not index the words the mark names"
        )
    # And the astral character actually moved the offset — otherwise the loop
    # above proves nothing about the unit.
    plain = _guesses(_marks(_body(GUESS_SENTENCE))[0])[0]
    astral = _guesses(_marks(_body(f"🛰 {GUESS_SENTENCE}"))[0])[0]
    assert astral["char_start"] == plain["char_start"] + 3, (
        "an astral character is TWO UTF-16 units plus the space"
    )


def test_a_sentence_that_names_a_declared_unit_passes() -> None:
    """The direction that matters. Naming what the record could not carry is the
    behaviour the whole train exists to reward, and it must cost nothing."""
    body = _body(NAMED_SENTENCE)
    marks, _ = _marks(body)
    assert _guesses(marks) == []


def test_a_named_unit_rescues_the_same_absence_idiom() -> None:
    """The class is decided by what the sentence NAMES, not by its grammar: the
    identical absence assertion with one declared unit in it is not a guess."""
    named = (
        "No read on Watch — Sudan (country_watch_sd) reached this record, so "
        "nothing here speaks to the escalation there [[ref:1]]."
    )
    assert au._aperture_absence_at(named) >= 0, "the absence idiom is present"
    assert _guesses(_marks(_body(named))[0]) == []


def test_the_guess_class_runs_in_the_aperture_section_only() -> None:
    """Elsewhere in the body a generalised negative is a claim about the world,
    and the scope-widening and uncited classes already own it."""
    body = _body(GUESS_SENTENCE, section="The reading")
    marks, _ = _marks(body)
    assert _guesses(marks) == []


def test_the_unrostered_diagnosis_keeps_the_label() -> None:
    """ORDERING IS A CONTRACT (the severity chain's own rule): the most specific
    available diagnosis wins. A sentence NAMING a place the record never
    declared is ``aperture_unrostered``; this class does not also fire on it."""
    body = _body(
        "This record provides no coverage of Central Asia or broader South "
        "America [[ref:1]]."
    )
    marks, _ = _marks(body)
    assert [m["class"] for m in marks if m["class"].startswith("aperture")] == [
        au.UNSUPPORTED_APERTURE_UNROSTERED
    ]


def test_the_guess_replaces_the_uncited_mark_and_says_so() -> None:
    """H8 yields. ``uncited`` marks the WHOLE sentence, so a guess mark inside
    one would be dropped by the dedupe and the row would publish a checked zero
    over the class that fired. The generic reason rides in the note instead, so
    nothing the row knew is lost."""
    body = _body(GUESS_SENTENCE)  # the live sentence carries no ordinal
    marks, checked = _marks(body)
    assert checked["by_class"][au.UNSUPPORTED_APERTURE_GUESS] == 1
    assert checked["by_class"][au.UNSUPPORTED_UNCITED] == 0
    assert checked["overlaps_suppressed"] == 0
    assert "names no ordinal" in _guesses(marks)[0]["note"]


def test_an_ordinal_bearing_guess_leaves_the_uncited_class_alone() -> None:
    """The yield is scoped to the sentence that fired, not to the class: a
    second aperture sentence uncited for the ordinary reason is still marked."""
    body = _body(
        GUESS_SENTENCE[:-1] + " [[ref:1]].",
        "Watch — Sudan (country_watch_sd) sat below the cut this cycle.",
    )
    _, checked = _marks(body)
    assert checked["by_class"][au.UNSUPPORTED_APERTURE_GUESS] == 1
    assert checked["by_class"][au.UNSUPPORTED_UNCITED] == 1


def test_the_unit_set_is_targets_and_not_desks_or_frames() -> None:
    """THE EXCLUSION IS THE CLASS. The live guess generalised into the DESK
    vocabulary ("economic-coercion or escalation dynamics"), so admitting a bare
    desk as "a named unit" would exempt the one sentence this class exists for.
    A frame ("G20", "Watch") names no place either."""
    units = au.aperture_declared_units(SPINE)
    assert "country_watch_sd" in units and "sudan" in units
    assert "watch - sudan" in units
    assert "escalation" not in units and "economic_coercion" not in units
    assert "watch" not in units and "g20" not in units
    # Narrower than the identifier set, which DOES carry the desk (its own
    # ledger-grounding exemption depends on that) — the two are not the same
    # fence and must not drift into each other.
    assert "escalation" in au.aperture_unit_identifiers(SPINE)


def test_the_checked_negative_publishes_the_class_and_its_denominator() -> None:
    """M-11, one tier up: a nothing-flagged line that does not say what it
    checked means nothing, and that holds for this class as for the others."""
    _, checked = _marks(_body(NAMED_SENTENCE))
    assert checked["version"] == "unsupported.v5"
    assert au.UNSUPPORTED_APERTURE_GUESS in checked["deterministic_classes"]
    assert checked["by_class"][au.UNSUPPORTED_APERTURE_GUESS] == 0
    assert checked["aperture_units_declared"] == len(
        au.aperture_declared_units(SPINE)
    )
    assert checked["aperture_units_declared"] > 0


def test_a_read_with_no_such_sentence_is_byte_identical_to_v4() -> None:
    """THE FLIP-KIT CLAIM at this tier: the class is ADDITIVE. Every body that
    produces no guess mark must publish exactly the marks, the offsets and the
    counters it published before — the class only ever appears beside them."""
    bodies = [
        _body(NAMED_SENTENCE),
        _body("This record provides no coverage of Central Asia [[ref:1]]."),
        "**Headline**\n\n**BLUF:** One thread carries this record [[ref:1]].\n"
        "\n## The reading\n"
        "The export chain is under pressure in the desk's own words [[ref:1]].\n",
        "",
    ]
    for body in bodies:
        marks, checked = _marks(body)
        assert _guesses(marks) == []
        assert checked["by_class"][au.UNSUPPORTED_APERTURE_GUESS] == 0
        assert checked["overlaps_suppressed"] == 0
        # v4's own keys, unchanged and in place.
        assert set(checked) >= {
            "marks", "sentences_examined", "judge_state", "spine_candidates",
            "aperture_vocabulary_words",
        }


# ===========================================================================
# 2 — THE ROUTING
# ===========================================================================


def test_the_restated_heading_is_pinned_against_its_owner() -> None:
    """``data.provenance`` may not import ``data.analysts`` — the edge runs the
    other way — so the heading is RESTATED here and held against the module that
    owns it, the ``assembly_arms`` / ``assessment_weighting`` discipline. A
    rename on either side fails loudly rather than silently unrouting the
    section."""
    assert aa.APERTURE_SECTION_HEADING == au._APERTURE_HEADING


def test_the_section_is_found_inline_as_well_as_on_its_own_line() -> None:
    """``verify._segment_claims_raw`` carries a repair (W4) for the glued
    heading the assessors emit, so a section boundary this pass could not see
    would silently route the whole aperture back onto the slice."""
    glued = (
        "**BLUF:** One thread carries this record [[ref:1]].## What this "
        f"reading misses\n{GUESS_SENTENCE}\n"
    )
    assert GUESS_SENTENCE[:40] in aa.aperture_section(glued)
    assert aa.claim_in_aperture_section(GUESS_SENTENCE, glued)


def test_a_body_with_no_aperture_section_answers_no() -> None:
    """Every unit finding, every composition, every desk head — i.e. the whole
    fleet bar two voices — is inert here."""
    plain = "No new infrastructure incidents appear in this desk's collection [1]."
    assert aa.aperture_section(plain) == ""
    assert not aa.claim_in_aperture_section(plain, plain)
    assert asl._absence_route_exclusion(plain, body=plain) is None


def test_the_router_takes_the_aperture_sentence_off_the_slice() -> None:
    """The class, on both gates, and only with the body: the pre-o4 signature
    (no body) is byte-identical, which is what keeps every other caller inert."""
    body = _body(NAMED_SENTENCE, GUESS_SENTENCE)
    assert asl.absence_scope_qualifier(GUESS_SENTENCE) is not None, (
        "the sentence must still be a V-B candidate, or the test proves nothing"
    )
    assert asl._absence_route_exclusion(GUESS_SENTENCE) is None
    assert (
        asl._absence_route_exclusion(GUESS_SENTENCE, body=body)
        == aa.ROUTE_EXCLUSION_APERTURE
    )
    # V-I5: the routing decision is BINDING, so the judge path answers the same.
    assert (
        claim_is_routed_out(GUESS_SENTENCE, body=body)
        == aa.ROUTE_EXCLUSION_APERTURE
    )
    assert claim_is_routed_out(GUESS_SENTENCE) is None


def test_the_route_exclusion_demotes_a_hard_judge_verdict_to_soft() -> None:
    """The contract this lane keeps: a marked guess is a SOFT finding. The
    reason V-I5 substitutes is already classified, and it is not the hard one."""
    assert fail_class_for_reason("absence_slice_contradicted") == FAIL_CLASS_HARD
    assert (
        fail_class_for_reason("judge_contradicted_route_excluded")
        == FAIL_CLASS_SOFT
    )


# --- the real binding path -------------------------------------------------


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


#: The V-B stage-2 system prompt's own opening words. The discriminator is the
#: EXACT phrase rather than the word "slice", because the V3 ABSENCE RUBRIC —
#: which a body like this one routes its negatives to — also says "slice", and a
#: double that could not tell the two judge calls apart would report the rubric
#: partition as a slice consultation and pass this file for the wrong reason.
_STAGE2_MARKER = "ACTUAL INPUT SLICE"

_NUMBERED_CLAIM_RE = re.compile(r"^\s*\d+\.\s", re.MULTILINE)


class _SliceJudge:
    """Answers the V-B stage-2 slice call; supports everything else. Modelled on
    ``test_verify_absence_slice_body._SliceJudge`` so the two paths are exercised
    by the same double."""

    subprovider = "stub"

    def __init__(self, slice_json: dict | None = None) -> None:
        self._slice = slice_json
        self.slice_calls = 0

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kw):
        prompt = messages[0]["content"]
        if _STAGE2_MARKER in (system or ""):
            self.slice_calls += 1
            return _Response(json.dumps(self._slice or {}))
        n = len(_NUMBERED_CLAIM_RE.findall(prompt))
        return _Response(json.dumps({"verdicts": ["supported"] * max(n, 1)}))


class _FakeConn:
    """asyncpg-shaped double returning the projection's own column names."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchrow(self, sql: str, *args):
        if "analyst_traces" in sql:
            return {"input_row_refs": [uuid4() for _ in self._rows]}
        return None

    async def fetch(self, sql: str, *args):
        return self._rows


#: The slice row that produced the live hard fail: an escalation report. It is
#: in the slice BECAUSE the record carried an escalation read — which is the
#: premise of the aperture sentence, not a refutation of it.
_ESCALATION_ROW = {
    "title": "Cross-border escalation: airstrikes reported overnight",
    "body": (
        "Officials described a sharp escalation in cross-border strikes and "
        "renewed economic-coercion measures against energy exporters."
    ),
    "source_id": "src.wire",
    "provenance_kind": "",
    "row_kind": "signal",
}

_CITATIONS = [
    {
        "marker": "[[ref:1]]",
        "ref_kind": "finding",
        "signal_id": str(uuid4()),
        "title": "Pakistan escalation",
        "evidence_text": "Pakistan’s precision airstrikes into Afghanistan",
    },
    {
        "marker": "[[ref:2]]",
        "ref_kind": "finding",
        "signal_id": str(uuid4()),
        "title": "Ukraine economic coercion",
        "evidence_text": "Ukraine’s drone campaign against Russian refineries",
    },
]


async def test_the_aperture_sentence_never_reaches_the_slice_judge(
    monkeypatch,
) -> None:
    """REAL BINDING PATH — ``verify_finding_faithfulness`` with a slice
    connection, the entry ``actor_critic`` calls. The sentence is a V-B
    candidate and the slice carries the rows that hard-failed it live; it must
    leave on the router and be counted saying so."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _SliceJudge({"violating_title": _ESCALATION_ROW["title"],
                         "verdict": "contradicted"})
    report = await verify_finding_faithfulness(
        body=_body(NAMED_SENTENCE, GUESS_SENTENCE),
        citations=_CITATIONS,
        judge_llm=judge,
        slice_conn=_FakeConn([_ESCALATION_ROW]),
        run_id=uuid4(),
    )
    assert judge.slice_calls == 0, "stage 2 was consulted about the aperture"
    assert report.counters.get("absence_slice_route_excluded") == 1
    assert report.counters.get("absence_slice_route_excluded_aperture") == 1
    assert "absence_slice_contradicted" not in report.counters
    assert not [
        cv for cv in report.claim_verdicts
        if cv.reason == "absence_slice_contradicted"
    ]
    assert not [
        s for s in report.unsupported_spans
        if s.reason == "absence_slice_contradicted"
    ]


async def test_the_same_sentence_outside_the_section_still_reaches_the_slice(
    monkeypatch,
) -> None:
    """THE COUNTERFACTUAL that makes the test above mean something: the routing
    is positional, so the identical sentence under a different heading is
    screened exactly as it was before this lane."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _SliceJudge({"violating_title": _ESCALATION_ROW["title"],
                         "verdict": "contradicted"})
    report = await verify_finding_faithfulness(
        body=_body(GUESS_SENTENCE, section="The reading"),
        citations=_CITATIONS,
        judge_llm=judge,
        slice_conn=_FakeConn([_ESCALATION_ROW]),
        run_id=uuid4(),
    )
    assert "absence_slice_route_excluded_aperture" not in report.counters
    assert judge.slice_calls == 1, (
        "the sentence stopped being a slice candidate for some other reason — "
        "the counterfactual is no longer the counterfactual"
    )


async def test_a_finding_with_no_aperture_section_is_byte_identical(
    monkeypatch,
) -> None:
    """Every row in the fleet that is not one of these two voices: the fold must
    be indistinguishable from the pre-o4 pass, counter map included."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = (
        "**BLUF:** The corridor remains open [1].\n"
        "No new infrastructure incidents appear in this desk's collection for "
        "the trailing window [1].\n"
    )
    citations = [{"marker": "[1]", "signal_id": str(uuid4()), "title": "wire"}]
    judge = _SliceJudge({"verdict": "no_violation"})
    report = await verify_finding_faithfulness(
        body=body,
        citations=citations,
        judge_llm=judge,
        slice_conn=_FakeConn([_ESCALATION_ROW]),
        run_id=uuid4(),
    )
    assert not [c for c in report.counters if c.endswith("_aperture")]
