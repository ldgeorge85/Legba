# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-C (2026-07-31) — METADATA CLAIMS verified by LOOKUP, not exempt blind.

Readout structural finding #3: a claim quoting the platform's OWN metadata is
UNJUDGEABLE from evidence text — its truthmaker is a COLUMN the judge never
sees. Both live artifacts in the readout dossier are of that shape:

  * ``soft_fail#10`` (Sudan country_composition, judge_unsupported)
    "The unit's effective confidence of 0.68 indicates a likely but not certain
    conclusion." — TRUE against the cited sub-claim's captured
    ``effective_confidence``.
  * ``hard_fail#4`` (DR Congo country_composition, judge_contradicted)
    "… but these indications are below verification thresholds …
    [[ref:6]][[ref:7]]" — TRUE against the cited sub-claims' C-TIER ``tier``
    stamps, and a MIXED clause (it also asserts a first-order absence).

Surfaces:

  1. LOOKUP — match → supported (``metadata_verified``) with the real value in
     the verdict detail; mismatch → soft ``metadata_mismatch`` naming the real
     value (a defect class previously invisible: prose misquoting its own
     numbers); columns absent → today's path (``metadata_unverifiable``).
  2. ANTI-LAUNDERING — a matching value only lifts a claim that IS about its
     metadata. A mixed clause is counted (``metadata_verified_not_dominant``)
     and keeps the grader's verdict.
  3. ARITHMETIC — the override rescores over the SAME denominator the path used,
     on both the floor and the judge path.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from legba.data.provenance.verify import (
    FAIL_CLASS_SOFT,
    VERDICT_SUPPORTED,
    build_faithfulness_critique_payload,
    verify_finding_faithfulness,
)

# ---------------------------------------------------------------------------
# Doubles + the composition citation bridge (the shape the CITE block stamps)
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubJudge:
    subprovider = "stub-judge"

    def __init__(self, verdicts_json: str) -> None:
        self._json = verdicts_json

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        return _Response(self._json)


def _subclaim_citation(
    n: int, *, eff: float | None = None, tier: str | None = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "marker": f"[[ref:{n}]]",
        "ordinal": n,
        "ref_id": str(uuid4()),
        "ref_kind": "finding",
        "evidence_text": f"sub-claim {n} body",
        "title": f"sub-claim {n}",
    }
    if eff is not None:
        entry["effective_confidence"] = eff
    if tier is not None:
        entry["tier"] = tier
    return entry


# The Sudan artifact: one cited sub-claim at effective_confidence 0.68.
_CONF_BODY = (
    "The units indicate routine coverage of the conflict [[ref:1]].\n"
    "The unit's effective confidence of 0.68 indicates a likely but not "
    "certain conclusion.\n"
)
_CONF_CITATIONS = [_subclaim_citation(1, eff=0.68)]


def _verdict_for(report, needle: str):
    return next(cv for cv in report.claim_verdicts if needle in cv.text)


def _span_for(report, needle: str):
    return next(
        (s for s in report.unsupported_spans if needle in s.text), None
    )


# ---------------------------------------------------------------------------
# 1. LOOKUP — the Sudan artifact
# ---------------------------------------------------------------------------


async def test_confidence_claim_matching_the_column_is_supported(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_CONF_BODY, citations=_CONF_CITATIONS
    )
    cv = _verdict_for(report, "effective confidence of 0.68")
    assert cv.verdict == VERDICT_SUPPORTED
    assert cv.reason is None
    assert "effective_confidence=0.68" in (cv.detail or "")
    # The stale no_citation span is gone, and the score reflects it.
    assert _span_for(report, "effective confidence of 0.68") is None
    assert report.counters["metadata_verified"] == 1
    assert report.checkable_claims == 2
    assert report.supported_claims == 2
    assert report.faithfulness_score == 1.0


async def test_confidence_claim_overrides_the_judge_too(monkeypatch) -> None:
    """The judge structurally cannot check a column — V-C is the authority on
    this class on BOTH paths, so a judge 'unsupported' is replaced."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge('{"verdicts": ["supported", "unsupported"]}')
    report = await verify_finding_faithfulness(
        body=_CONF_BODY, citations=_CONF_CITATIONS, judge_llm=judge
    )
    assert report.judge_status == "llm"
    cv = _verdict_for(report, "effective confidence of 0.68")
    assert cv.verdict == VERDICT_SUPPORTED
    assert report.counters["metadata_verified"] == 1
    assert report.supported_claims == 2
    assert report.faithfulness_score == 1.0


async def test_misquoted_confidence_is_a_soft_mismatch(monkeypatch) -> None:
    """The NEW defect class: prose stating a number its own column contradicts.
    The real value lands in the verdict detail."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_CONF_BODY, citations=[_subclaim_citation(1, eff=0.41)]
    )
    cv = _verdict_for(report, "effective confidence of 0.68")
    assert cv.verdict == FAIL_CLASS_SOFT
    assert cv.reason == "metadata_mismatch"
    assert "0.41" in (cv.detail or "") and "0.68" in (cv.detail or "")
    span = _span_for(report, "effective confidence of 0.68")
    assert span is not None and span.reason == "metadata_mismatch"
    assert span.as_dict()["fail_class"] == FAIL_CLASS_SOFT
    assert "0.41" in (span.detail or "")
    assert report.counters["metadata_mismatch"] == 1


async def test_misquote_overrides_a_judge_pass(monkeypatch) -> None:
    """A judge that waved the misquote through is corrected — the column is the
    authority, and this direction DEMOTES, so it is never gated on dominance."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge('{"verdicts": ["supported", "supported"]}')
    report = await verify_finding_faithfulness(
        body=_CONF_BODY, citations=[_subclaim_citation(1, eff=0.41)], judge_llm=judge
    )
    cv = _verdict_for(report, "effective confidence of 0.68")
    assert cv.reason == "metadata_mismatch"
    assert report.supported_claims == 1
    assert report.faithfulness_score == 0.5


async def test_unverifiable_when_no_confidence_column(monkeypatch) -> None:
    """A UNIT finding cites SIGNALS, which carry no confidence column — today's
    path, counted, never a fabricated pass."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body="The unit's effective confidence of 0.68 is stated here.\n",
        citations=[{"marker": "[1]", "signal_id": str(uuid4())}],
    )
    assert report.counters["metadata_unverifiable"] == 1
    assert "metadata_verified" not in report.counters
    cv = _verdict_for(report, "effective confidence of 0.68")
    assert cv.reason == "no_citation"  # unchanged: today's verdict


# ---------------------------------------------------------------------------
# 1b. LOOKUP — tier / below-floor language (the DR Congo artifact shape)
# ---------------------------------------------------------------------------

_TIER_DOMINANT_BODY = (
    "The picture is quiet this window [[ref:1]].\n"
    "These indications are below verification thresholds "
    "[[ref:6]][[ref:7]].\n"
)


def _tier_citations(*, six: str, seven: str) -> list[dict[str, Any]]:
    """Ordinals 6/7 stamped with the named tier ("" = an untiered BASIS entry)."""
    return [
        _subclaim_citation(1),
        _subclaim_citation(6, tier=six or None),
        _subclaim_citation(7, tier=seven or None),
    ]


async def test_below_threshold_matching_periphery_tier_is_supported(
    monkeypatch,
) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_TIER_DOMINANT_BODY,
        citations=_tier_citations(six="periphery", seven="periphery"),
    )
    cv = _verdict_for(report, "below verification thresholds")
    assert cv.verdict == VERDICT_SUPPORTED
    assert "tier=periphery" in (cv.detail or "")
    assert report.counters["metadata_verified"] == 1


async def test_below_threshold_on_basis_tier_is_a_mismatch(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_TIER_DOMINANT_BODY,
        citations=_tier_citations(six="periphery", seven=""),
    )
    cv = _verdict_for(report, "below verification thresholds")
    assert cv.reason == "metadata_mismatch"
    assert "7=basis" in (cv.detail or "")
    assert report.counters["metadata_mismatch"] == 1


async def test_no_tier_stamps_anywhere_is_unverifiable(monkeypatch) -> None:
    """A pre-C-TIER composition carries no tier stamps — we cannot know, so we
    do not decide."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_TIER_DOMINANT_BODY,
        citations=[_subclaim_citation(1), _subclaim_citation(6), _subclaim_citation(7)],
    )
    assert report.counters["metadata_unverifiable"] == 1
    assert "metadata_verified" not in report.counters


# ---------------------------------------------------------------------------
# 2. ANTI-LAUNDERING — the DR Congo mixed clause
# ---------------------------------------------------------------------------

_DRC_CLAIM = (
    "Weakly-supported signals from the economic_coercion and energy_security "
    "units hint at an absence of observable sanctions or energy-security "
    "pressures, but these indications are below verification thresholds and "
    "should be treated as tentative [[ref:6]][[ref:7]]."
)


async def test_mixed_clause_is_checked_but_never_lifted(monkeypatch) -> None:
    """The metadata leg checks out, but the clause ALSO asserts a first-order
    absence a column cannot certify — counted, verdict untouched."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge('{"verdicts": ["contradicted"]}')
    report = await verify_finding_faithfulness(
        body=_DRC_CLAIM + "\n",
        citations=_tier_citations(six="periphery", seven="periphery")[1:],
        judge_llm=judge,
    )
    cv = _verdict_for(report, "below verification thresholds")
    assert cv.verdict != VERDICT_SUPPORTED
    assert report.counters["metadata_verified_not_dominant"] == 1
    assert "metadata_verified" not in report.counters


async def test_mixed_clause_misquote_still_flags(monkeypatch) -> None:
    """A misquote is a misquote wherever it sits — the demote direction is NOT
    gated on dominance."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge('{"verdicts": ["supported"]}')
    report = await verify_finding_faithfulness(
        body=_DRC_CLAIM + "\n",
        citations=[
            _subclaim_citation(6, tier="periphery"),
            _subclaim_citation(7),
        ],
        judge_llm=judge,
    )
    cv = _verdict_for(report, "below verification thresholds")
    assert cv.reason == "metadata_mismatch"
    assert report.counters["metadata_mismatch"] == 1


# ---------------------------------------------------------------------------
# 3. Byte-compat + persistence
# ---------------------------------------------------------------------------


async def test_finding_with_no_metadata_prose_is_untouched(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    body = "Alpha struck Bravo base on Monday [1].\nCharlie seized the port [9].\n"
    cites = [{"marker": "[1]", "signal_id": str(uuid4())}]
    report = await verify_finding_faithfulness(body=body, citations=cites)
    assert report.counters == {}
    assert report.checkable_claims == 2
    assert report.supported_claims == 1
    assert report.faithfulness_score == 0.5


async def test_detail_is_persisted_on_span_and_ledger(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_CONF_BODY, citations=[_subclaim_citation(1, eff=0.41)]
    )
    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    verification = payload["data"]["verification"]
    row = next(
        r for r in verification["claim_verdicts"] if "0.68" in r["text"]
    )
    assert row["reason"] == "metadata_mismatch" and "0.41" in row["detail"]
    span = next(
        s for s in verification["unsupported_spans"] if "0.68" in s["text"]
    )
    assert "0.41" in span["detail"]
    assert verification["counters"]["metadata_mismatch"] == 1
