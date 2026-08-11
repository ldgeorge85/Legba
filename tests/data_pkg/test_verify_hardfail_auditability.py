# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W2 (2026-08-02) — HARD-FAIL AUDITABILITY, from the adjudicated readout.

Three defects, all in the surviving-hard-fail path:

  1. NO PERSISTED PROOF — 25 of 54 surviving ``judge_contradicted`` verdicts
     carried no quote anywhere. The span WAS computed (V-D uses it to decide the
     severity) and then discarded: not in the ledger detail, not in the traces.
     A hard fail nobody can audit is a hard fail nobody can trust.
  2. LEDGER LABEL COLLAPSE — ``judge_contradicted_unquoted`` survived in
     ``unsupported_spans`` but reverted to ``judge_unsupported`` in
     ``claim_verdicts``. The calibration loop reads the LEDGER, so it could not
     split the very class V-D created to be measured.
  3. RESOLVES ≠ REFUTES — 3 unearned hard fails in the panel satisfied D1
     mechanically: the judge supplied a real, verbatim evidence span that
     RESOLVES the claim's subject without opposing it. The South Africa case is
     the sharpest: a register-metadata claim that is verbatim-correct, "refuted"
     with content sitting in the PRIOR READ block the claim contrasts against.
     Australia's was the prior read entailing the claim word-for-word (the same
     judge passed the byte-identical claim shape elsewhere in the same sample).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import legba.data.provenance.verify as V
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    FAIL_CLASS_SOFT,
    verify_finding_faithfulness,
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubJudge:
    subprovider = "stub-judge"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._json = json.dumps(payload)
        self.prompts: list[str] = []

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        self.prompts.append(messages[0]["content"])
        return _Response(self._json)


_REFUTING_EVIDENCE = (
    "Reserves fell for a third consecutive month, the central bank said, "
    "reversing the rise reported in June"
)
_BODY = (
    "The central bank raised rates by fifty basis points [1].\n"
    "Reserves rose sharply on the month [1].\n"
)


def _citations(title: str = _REFUTING_EVIDENCE) -> list[dict[str, Any]]:
    return [{"marker": "[1]", "signal_id": str(uuid4()), "title": title}]


def _ledger_row(report, needle: str):
    return next(cv for cv in report.claim_verdicts if needle in cv.text)


def _span(report, needle: str):
    return next(s for s in report.unsupported_spans if needle in s.text)


# ---------------------------------------------------------------------------
# 1. The earned quote is PERSISTED
# ---------------------------------------------------------------------------


async def test_the_earned_quote_lands_in_the_claim_ledger(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        {
            "verdicts": ["supported", "contradicted"],
            "quotes": ["", "Reserves fell for a third consecutive month"],
        }
    )
    report = await verify_finding_faithfulness(
        body=_BODY, citations=_citations(), judge_llm=judge
    )
    cv = _ledger_row(report, "Reserves rose")
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "judge_contradicted"
    assert "Reserves fell for a third consecutive month" in (cv.detail or "")
    # …and it survives the persist boundary the critique payload writes.
    assert "Reserves fell" in (cv.as_dict()["detail"] or "")


async def test_the_earned_quote_lands_on_the_span_too(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        {
            "verdicts": ["supported", "contradicted"],
            "quotes": ["", "Reserves fell for a third consecutive month"],
        }
    )
    report = await verify_finding_faithfulness(
        body=_BODY, citations=_citations(), judge_llm=judge
    )
    span = _span(report, "Reserves rose")
    assert span.reason == "judge_contradicted"
    assert "Reserves fell for a third consecutive month" in (span.detail or "")


async def test_verdicts_without_an_earned_quote_carry_no_detail(monkeypatch) -> None:
    """Byte-identical for every claim that has no quote to persist."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge({"verdicts": ["supported", "unsupported"]})
    report = await verify_finding_faithfulness(
        body=_BODY, citations=_citations(), judge_llm=judge
    )
    assert _ledger_row(report, "Reserves rose").detail is None
    assert _ledger_row(report, "raised rates").detail is None


# ---------------------------------------------------------------------------
# 2. The ledger label no longer collapses
# ---------------------------------------------------------------------------


async def test_unquoted_demotion_survives_into_claim_verdicts(monkeypatch) -> None:
    """The measured collapse: the label lived in the spans and reverted to
    ``judge_unsupported`` in the ledger the calibration loop actually reads."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        {
            "verdicts": ["supported", "contradicted"],
            "quotes": ["", "a paraphrase the evidence never contains"],
        }
    )
    report = await verify_finding_faithfulness(
        body=_BODY, citations=_citations(), judge_llm=judge
    )
    assert _span(report, "Reserves rose").reason == "judge_contradicted_unquoted"
    cv = _ledger_row(report, "Reserves rose")
    assert cv.reason == "judge_contradicted_unquoted"
    assert cv.verdict == FAIL_CLASS_SOFT
    assert report.counters["hardfail_demoted_no_quote"] == 1


def test_one_mapping_serves_both_arms() -> None:
    """The span arm and the ledger arm read the SAME function, so they cannot
    disagree about a claim's class again."""
    assert V._judge_reason("contradicted") == "judge_contradicted"
    assert (
        V._judge_reason(V._VERDICT_CONTRADICTED_UNQUOTED)
        == "judge_contradicted_unquoted"
    )
    assert (
        V._judge_reason(V._VERDICT_CONTRADICTED_UNREFUTED)
        == "judge_contradicted_unrefuted"
    )
    assert V._judge_reason("unsupported") == "judge_unsupported"


# ---------------------------------------------------------------------------
# 3. REFUTES vs RESOLVES — the three unearned hard fails
# ---------------------------------------------------------------------------

# The AUSTRALIA case: the prior read entailed the claim word-for-word, so the
# "refutation" the judge quoted is the claim itself, sitting in the evidence.
_AUSTRALIA_CLAIM = (
    "Australia's standing military posture remains steady with no new "
    "deployments or capability shifts observed [1]."
)


def test_a_quote_that_restates_the_claim_cannot_refute_it() -> None:
    """R1 — the Australia shape. Evidence that says what the claim says is
    confirmation, whatever verdict label the judge attached to it."""
    corpus = V._normalize_quote_text(
        "PRIOR READ - Australia's standing military posture remains steady with "
        "no new deployments or capability shifts observed."
    )
    quote = "no new deployments or capability shifts observed"
    assert V._quote_resolves(quote, corpus) is True  # D1 is satisfied…
    assert V._quote_refutes(quote, _AUSTRALIA_CLAIM, corpus, "") is False  # …W2 is not
    # A genuine opposing span still earns it.
    opposing = "two new squadrons were deployed to Darwin this week"
    real_corpus = V._normalize_quote_text(opposing)
    assert V._quote_refutes(opposing, _AUSTRALIA_CLAIM, real_corpus, "") is True


# The SOUTH AFRICA case: a register-metadata claim that is verbatim-correct,
# "refuted" with content out of the PRIOR READ block it contrasts against.
_SA_CLAIM = (
    "The open-situation register still lists two active frames, unchanged from "
    "the prior read, and no new frame has opened [1]."
)
_SA_PRIOR_BLOCK = (
    "PRIOR READ - this unit's previous verified read of this target: two active "
    "frames on the register, load-shedding and the coalition dispute"
)


def test_a_span_from_the_prior_read_a_claim_diffs_against_cannot_refute_it() -> None:
    """R2 — the South Africa shape. The block a claim is diffing against is its
    SUBJECT; quoting it back is not opposition."""
    prior = V._normalize_quote_text(_SA_PRIOR_BLOCK)
    corpus = V._normalize_quote_text(f"{_SA_PRIOR_BLOCK}\nA separate wire report.")
    quote = "two active frames on the register, load-shedding and the coalition"
    assert V._quote_resolves(quote, corpus) is True
    assert V._quote_refutes(quote, _SA_CLAIM, corpus, prior) is False
    # A claim that does NOT frame itself against the prior read is unaffected —
    # R2 is about the diff relationship, not about prior-read text as such.
    assert (
        V._quote_refutes(
            quote, "The register lists no active frames at all [1].", corpus, prior
        )
        is True
    )


async def test_an_unrefuting_quote_demotes_to_its_own_soft_class(monkeypatch) -> None:
    """END-TO-END: the hard class is not earned, the class is DISTINCT from the
    unquoted one, and the quote is still persisted so the demotion is auditable."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = f"{_AUSTRALIA_CLAIM}\n"
    judge = _StubJudge(
        {
            "verdicts": ["contradicted"],
            "quotes": ["no new deployments or capability shifts observed"],
        }
    )
    report = await verify_finding_faithfulness(
        body=body,
        citations=_citations(
            "PRIOR READ - Australia's standing military posture remains steady "
            "with no new deployments or capability shifts observed"
        ),
        judge_llm=judge,
    )
    cv = _ledger_row(report, "standing military posture")
    assert cv.reason == "judge_contradicted_unrefuted"
    assert cv.verdict == FAIL_CLASS_SOFT
    assert "RESOLVES" in (cv.detail or "")
    assert "no new deployments" in (cv.detail or "")
    assert report.counters["hardfail_demoted_not_refuting"] == 1
    # The two demotion classes are counted separately.
    assert "hardfail_demoted_no_quote" not in report.counters


async def test_a_genuine_refutation_is_still_a_hard_fail(monkeypatch) -> None:
    """THE KEEP-TEST: V-D's correct demotions were adjudicated 2/2 and the class
    stays live — a quote that really does oppose the claim keeps the hard class."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        {
            "verdicts": ["supported", "contradicted"],
            "quotes": ["", "Reserves fell for a third consecutive month"],
        }
    )
    report = await verify_finding_faithfulness(
        body=_BODY, citations=_citations(), judge_llm=judge
    )
    span = _span(report, "Reserves rose")
    assert span.reason == "judge_contradicted"
    assert span.as_dict()["fail_class"] == FAIL_CLASS_HARD
    assert "hardfail_demoted_not_refuting" not in report.counters
    assert "hardfail_demoted_no_quote" not in report.counters


async def test_the_demotion_never_moves_the_score(monkeypatch) -> None:
    """As with V-D: the claim FAILS either way — only the severity label moves,
    so no score can be laundered by a refutes-vs-resolves reclassification."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")

    async def _run(quote: str) -> Any:
        judge = _StubJudge(
            {"verdicts": ["supported", "contradicted"], "quotes": ["", quote]}
        )
        return await verify_finding_faithfulness(
            body=_BODY, citations=_citations(), judge_llm=judge
        )

    refuting = await _run("Reserves fell for a third consecutive month")
    restating = await _run("Reserves rose sharply on the month")
    assert refuting.faithfulness_score == restating.faithfulness_score
    assert refuting.checkable_claims == restating.checkable_claims
    assert refuting.supported_claims == restating.supported_claims
