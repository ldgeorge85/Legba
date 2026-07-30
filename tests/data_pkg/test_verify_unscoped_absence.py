# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W31 (2026-07-28) — the UNSCOPED-ABSENCE backstop + the prompt-side rule.

The measured 2026-W31 gold-set class: 5 of 8 sampled downgrades were an ABSENCE
claim stated as a WORLD claim on a thin-collection desk — e.g. "no weaponized
commodity embargoes" during an extensively-reported embargo, "large-scale
exercise: not observed" days before an announced exercise, "no outages were
reported" while outages were reported, and unhedged "no signals report X" /
"no coordinated narrative is evident" negatives. Faithful to inputs, wrong about the world —
the prose claimed world scope the collection cannot support.

Three surfaces:

  1. DETECTOR (``unscoped_absence_spans``) — deterministic + conservative:
     the five W31 shapes flag (paraphrased below); scoped, hedged, cited,
     survey-shaped, guarded-idiom, and presence claims do NOT.
  2. INTEGRATION — a flagged unscoped absence is ONE soft checkable-but-
     unsupported claim in the pooled deterministic score (the
     ``_fold_guard_spans`` path), with a ledger row + ``fail_class`` label;
     on the judge path the #116c text-dedup keeps the judge authoritative.
  3. PROMPTS — all 10 inline-unit descriptors carry the collection-scoped
     ABSENCE-CLAIM rule (the paired prompt-discipline commit).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from legba.data.provenance.verify import (
    FAIL_CLASS_SOFT,
    _UNSCOPED_ABSENCE,
    unscoped_absence_spans,
    build_faithfulness_critique_payload,
    verify_finding_faithfulness,
)

_DESCRIPTORS_DIR = Path(__file__).resolve().parents[2] / "descriptors"

# The 11 inline-unit descriptors the prompt rule landed on.
_UNIT_YAMLS = (
    "analyst_disruption_status.yaml",
    "analyst_escalation.yaml",
    "analyst_proliferation_watch.yaml",
    "analyst_economic_coercion.yaml",
    "analyst_military_posture.yaml",
    "analyst_internal_stability.yaml",
    "analyst_narrative_coordination.yaml",
    "analyst_energy_security.yaml",
    "analyst_leadership_transition.yaml",
    "analyst_cross_doc_corroborator.yaml",
    "analyst_corpus_researcher.yaml",
)


# ---------------------------------------------------------------------------
# 1. Detector POSITIVES — the five real W31 shapes, paraphrased
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        # world-scoped economic-coercion negative (strong opener).
        "No observable coercive economic pressure on the government — no "
        "weaponized commodity embargoes.",
        # the bold label-colon "not observed" verdict signpost.
        "**Large-scale exercise:** not observed.",
        # "no X were reported" as a world-fact.
        "No power outages were reported this week.",
        # bare "no signals report ..." (no bounded referent).
        "No signals report widespread public-health issues.",
        # "no coordinated X is evident" as a world-fact.
        "No coordinated narrative is evident across the information space.",
    ],
)
def test_w31_shapes_flag(body: str) -> None:
    spans = unscoped_absence_spans(body)
    assert len(spans) == 1
    assert spans[0].reason == _UNSCOPED_ABSENCE
    assert spans[0].as_dict()["fail_class"] == FAIL_CLASS_SOFT


def test_there_is_no_shape_flags_when_floor_exempt() -> None:
    # "There is no evidence of ..." is absence-EXEMPT on the floor (the
    # "no evidence" marker) — the backstop must catch its unscoped form.
    spans = unscoped_absence_spans("There is no evidence of an embargo.")
    assert [s.reason for s in spans] == [_UNSCOPED_ABSENCE]


def test_bluf_restating_an_unscoped_absence_flags() -> None:
    # The BLUF label is stripped before anchoring — the visible verdict line
    # is exactly where the W31 downgrades read the world-claim.
    spans = unscoped_absence_spans("**BLUF:** No coordinated narrative is evident.")
    assert [s.reason for s in spans] == [_UNSCOPED_ABSENCE]


# ---------------------------------------------------------------------------
# 2. Detector NEGATIVES — scoped / hedged / cited / survey / presence pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        # Collection-scoped forms — the exact phrasings the prompts recommend.
        "No power outages are reported in the collected signals.",
        "No signals in this desk's sources indicate health issues.",
        "**Large-scale exercise:** not observed in collected reporting.",
        "No evidence of an embargo appears in the sources reviewed.",
        "No strikes were reported among monitored outlets.",
        "No independent coverage was found in the corpus searched.",
        "Not observed in available reporting.",
        # Hedged forms (with or without scoping) pass — assessments, not
        # bald world-facts.
        "We assess no coercive embargo is underway.",
        "Likely no mobilization occurred.",
        "Assessed: no coordinated campaign is evident.",
        "It appears no embargo is underway.",
        # Cited absence is evidence-anchored — never flagged.
        "No new sanctions were announced [3].",
        # The M14 survey shape is corpus-scoped by construction.
        "None of the 78 signals reference outages.",
        # Guarded positive idioms are not absence at all.
        "No fewer than three units mobilized [1].",
        # A verbless "No <noun phrase>." fragment — doubt → no flag.
        "No confirmed movement of armor near the border.",
        # Presence claims are untouched.
        "Protesters massed in the capital on Tuesday [1].",
        # Headings are structure, never claims.
        "## No developments",
        "**No change**",
        # Forward-looking signposts stay exempt.
        "Official announcements of fuel rationing would confirm a supply crisis.",
    ],
)
def test_negatives_do_not_flag(body: str) -> None:
    assert unscoped_absence_spans(body) == []


def test_preceding_span_scoping_counts_as_nearby() -> None:
    # Scoping language in the immediately preceding sentence bounds the
    # negative ("nearby" scoping).
    body = (
        "Collection on this desk is thin this window.\n"
        "No embargo activity is reported."
    )
    assert unscoped_absence_spans(body) == []
    # ... but a distant lead does NOT rescue a later unscoped negative.
    body_far = (
        "Collection on this desk is thin this window.\n"
        "The ministry announced a new budget on Monday [1].\n"
        "No embargo activity is reported."
    )
    assert [s.reason for s in unscoped_absence_spans(body_far)] == [_UNSCOPED_ABSENCE]


def test_floor_counted_spans_never_double_flag() -> None:
    # "There is no functioning embargo." is NOT absence-exempt (no lexicon
    # marker) — the floor already counts it no_citation; the backstop must
    # skip it so one claim can never fail twice.
    assert unscoped_absence_spans("There is no functioning embargo.") == []


# ---------------------------------------------------------------------------
# 3. INTEGRATION — one soft failure in the pooled deterministic score
# ---------------------------------------------------------------------------


def _cited(sid: str) -> list[dict]:
    return [{"marker": "[1]", "signal_id": sid, "title": "Alpha strikes Bravo base"}]


async def test_unscoped_absence_demotes_deterministic_score(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    body = (
        "Alpha struck Bravo base on Monday [1].\n"
        "No power outages were reported this week.\n"
    )
    rep = await verify_finding_faithfulness(body=body, citations=_cited(str(uuid4())))
    assert rep.judge_status == "deterministic"
    # 1 supported cited claim + 1 unscoped absence = 2 checkable, 1 supported.
    assert rep.checkable_claims == 2
    assert rep.supported_claims == 1
    assert rep.faithfulness_score == pytest.approx(0.5)
    flagged = [s for s in rep.unsupported_spans if s.reason == _UNSCOPED_ABSENCE]
    assert len(flagged) == 1
    assert flagged[0].as_dict()["fail_class"] == FAIL_CLASS_SOFT
    # Ledger row mirrors the span (soft_fail, same text).
    failed_rows = [cv for cv in rep.claim_verdicts if cv.reason == _UNSCOPED_ABSENCE]
    assert len(failed_rows) == 1
    assert failed_rows[0].verdict == FAIL_CLASS_SOFT


async def test_scoped_absence_scores_clean(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    body = (
        "Alpha struck Bravo base on Monday [1].\n"
        "No power outages are reported in the collected signals.\n"
    )
    rep = await verify_finding_faithfulness(body=body, citations=_cited(str(uuid4())))
    assert rep.checkable_claims == 1
    assert rep.faithfulness_score == pytest.approx(1.0)
    assert not any(s.reason == _UNSCOPED_ABSENCE for s in rep.unsupported_spans)


async def test_payload_counts_it_as_one_soft_unsupported(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    body = (
        "Alpha struck Bravo base on Monday [1].\n"
        "No signals report widespread public-health issues.\n"
    )
    rep = await verify_finding_faithfulness(body=body, citations=_cited(str(uuid4())))
    payload = build_faithfulness_critique_payload(rep, analyzed_output_id=uuid4())
    assert payload["overall_score"] == pytest.approx(0.5)
    # NOT advisory: it is counted in the unsupported tally.
    assert "unsupported=1" in payload["body"]
    verification = payload["data"]["verification"]
    spans = [
        s for s in verification["unsupported_spans"] if s["reason"] == _UNSCOPED_ABSENCE
    ]
    assert len(spans) == 1
    assert spans[0]["fail_class"] == FAIL_CLASS_SOFT


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _PartitionJudge:
    """Canned judge: absence rubric → one verdict; shared prompt → two."""

    subprovider = "stub-judge"

    def __init__(self) -> None:
        self.absence_calls = 0

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        if system and "ABSENCE / NEGATIVE claims" in system:
            self.absence_calls += 1
            return _Response('{"verdicts": ["supported"]}')
        return _Response('{"verdicts": ["supported", "supported"]}')


async def test_judge_path_stays_authoritative_no_double_count(monkeypatch) -> None:
    """On the judge path the V3 absence rubric grades the SAME prose — the
    #116c text-dedup drops the floor's backstop span so one claim is never
    counted twice (the judge already treats unbounded/unscoped absence as
    unsupported there). Fact-rich body keeps M14 off."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body = (
        "Alpha struck Bravo base on Monday [1].\n"
        "Charlie reinforced the perimeter overnight [1].\n"
        "No power outages were reported this week.\n"
    )
    citations = [{"marker": "[1]", "signal_id": sid, "title": "Alpha strikes Bravo base"}]
    judge = _PartitionJudge()
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    assert rep.judge_status == "llm"
    assert judge.absence_calls == 1
    # 3 judged claims, all supported; the backstop span deduped away.
    assert rep.faithfulness_score == pytest.approx(1.0)
    assert not any(s.reason == _UNSCOPED_ABSENCE for s in rep.unsupported_spans)


# ---------------------------------------------------------------------------
# 4. PROMPTS — all 10 inline units carry the collection-scoped absence rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _UNIT_YAMLS)
def test_unit_prompt_carries_absence_scoping_rule(name: str) -> None:
    prompt = yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())["method"][
        "system_prompt"
    ]
    # The rule's two invariant anchors: never a world-fact, scope to the
    # collection (each prompt phrases the rest in its own voice).
    assert "world-fact" in prompt
    low = prompt.lower()
    assert "collect" in low or "corpus" in low
    # Every recommended scoped phrasing must PASS the verify-side detector —
    # a prompt-compliant unit is never flagged (the two sides share one
    # scoping vocabulary).
    assert "absence" in low
