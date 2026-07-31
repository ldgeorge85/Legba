# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-F (2026-07-31) — CLAIM-SPLITTER HYGIENE: non-propositional spans dropped.

The live artifact (judge readout 2026-07-31 §6, dossier ``soft_fail#2``): a
Mexico ``leadership_transition`` finding put a bare ``(not_observed)`` status
token on its own line; the splitter kept it, ``_is_fact_asserting`` accepted it
(``not_observed`` supplies two 2+-letter runs inside ONE token), and it reached
the verdict ledger as a ``no_citation`` soft-fail — a manufactured defect on a
span that carries no proposition to grade.

Three surfaces:

  1. The PREDICATE (``_is_propositional``) — a span reducing to one
     whitespace-free token is rejected; anything with a space is kept
     (under-dropping is the cheap error: a dropped span RAISES the score).
  2. INTEGRATION — the artifact body no longer produces a ``(not_observed)``
     ledger row / unsupported span, on BOTH the floor and the judge paths.
  3. RECEIPTS — the drop is counted ``claims_dropped_nonpropositional`` on the
     report AND in the persisted ``data.verification.counters`` block.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from legba.data.provenance.verify import (
    _is_propositional,
    _segment_claims,
    _segment_claims_with_drops,
    build_faithfulness_critique_payload,
    verify_finding_faithfulness,
)

# ---------------------------------------------------------------------------
# The LIVE artifact — finding d0fdaf33 (Mexico – Low leadership-transition risk).
# The trailing status token is the span that reached the ledger as no_citation.
# ---------------------------------------------------------------------------

_ARTIFACT_BODY = (
    "Mexico's federal executive is mid-term with no scheduled national vote "
    "in the window [8].\n"
    "Cabinet turnover reported this cycle is routine administrative churn [16].\n"
    "\n"
    "(not_observed)\n"
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubJudge:
    subprovider = "stub-judge"

    def __init__(self, verdicts_json: str) -> None:
        self._json = verdicts_json
        self.prompts: list[str] = []

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        self.prompts.append(messages[0]["content"])
        return _Response(self._json)


def _citations() -> list[dict[str, Any]]:
    return [
        {"marker": "[8]", "signal_id": str(uuid4())},
        {"marker": "[16]", "signal_id": str(uuid4())},
    ]


# ---------------------------------------------------------------------------
# 1. The predicate
# ---------------------------------------------------------------------------


def test_bare_status_tokens_are_non_propositional() -> None:
    for span in (
        "(not_observed)",
        "not_observed",
        "- (not_observed)",
        "(not_observed) [3]",
        "N/A",
        "TBD",
        "—",
        "n/a.",
    ):
        assert _is_propositional(span) is False, span


def test_real_prose_stays_propositional() -> None:
    for span in (
        "No coup attempt is reported in the collected signals.",
        "Cabinet turnover reported this cycle is routine churn [16].",
        "- **Severity:** low",
        "Large-scale exercise: not observed",
        "not observed",  # two tokens — a fragment, but NOT a bare status token
    ):
        assert _is_propositional(span) is True, span


def test_heading_shaped_spans_are_left_alone() -> None:
    """Headings stay in the stream: already inert for BOTH the floor and the
    judge, and they are the preceding-span CONTEXT the W31 unscoped-absence
    backstop reads — dropping them would move a live detector for no gain."""
    for span in ("### Assessment", "**Drivers**", "- **Severity:** low"):
        assert _is_propositional(span) is True, span


def test_splitter_reports_the_drop() -> None:
    kept, dropped = _segment_claims_with_drops(_ARTIFACT_BODY)
    assert dropped == ["(not_observed)"]
    assert "(not_observed)" not in kept
    # The public splitter returns exactly the kept list.
    assert _segment_claims(_ARTIFACT_BODY) == kept


# ---------------------------------------------------------------------------
# 2. Integration — the artifact no longer manufactures a verdict row
# ---------------------------------------------------------------------------


async def test_artifact_no_longer_reaches_the_ledger_on_the_floor(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_ARTIFACT_BODY, citations=_citations()
    )
    texts = [cv.text for cv in report.claim_verdicts]
    assert not any("not_observed" in t for t in texts)
    assert not any("not_observed" in s.text for s in report.unsupported_spans)
    # The two REAL cited claims are graded, and nothing else: the artifact used
    # to add a third, always-failing row.
    assert report.checkable_claims == 2
    assert report.supported_claims == 2
    assert report.faithfulness_score == 1.0


async def test_artifact_not_sent_to_the_judge(monkeypatch) -> None:
    """The judge grades the same list the floor does — a status token is not a
    claim for EITHER, so the judge is never asked to verdict on it (and the
    one-verdict-per-claim length contract stays honest)."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge('{"verdicts": ["supported", "supported"]}')
    report = await verify_finding_faithfulness(
        body=_ARTIFACT_BODY, citations=_citations(), judge_llm=judge
    )
    assert report.judge_status == "llm"
    assert judge.prompts and "not_observed" not in judge.prompts[0]
    assert not any("not_observed" in cv.text for cv in report.claim_verdicts)


# ---------------------------------------------------------------------------
# 3. Receipts
# ---------------------------------------------------------------------------


async def test_drop_is_counted_on_report_and_payload(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_ARTIFACT_BODY, citations=_citations()
    )
    assert report.counters["claims_dropped_nonpropositional"] == 1
    assert report.as_dict()["counters"]["claims_dropped_nonpropositional"] == 1
    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    counters = payload["data"]["verification"]["counters"]
    assert counters["claims_dropped_nonpropositional"] == 1


async def test_counters_are_sparse_when_nothing_fires(monkeypatch) -> None:
    """A body with no non-propositional span carries NO counter — the map is
    sparse, so an unaffected finding's block is byte-identical to pre-train."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body="Mexico's executive is mid-term with no scheduled vote [8].\n",
        citations=_citations(),
    )
    assert "claims_dropped_nonpropositional" not in report.counters
    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    assert payload["data"]["verification"]["counters"] == {}
