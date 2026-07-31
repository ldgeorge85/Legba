# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-D (2026-07-31) — a CONTRADICTION must carry a resolvable evidence QUOTE.

Readout structural finding #4, present in BOTH judges: ``judge_contradicted`` —
the platform's highest-severity verdict, "the finding misstates its own cited
source" — was stamped on claims the cited evidence plainly CONFIRMS. Half the
Cerebras hard-fails and three quarters of the same-model hard-fails were false,
so the readout carries a standing warning: gate nothing on the hard/soft split.

The mechanical rule tested here: a hard fail must be able to POINT AT the
refutation. The judge is asked for a ``quotes`` array parallel to ``verdicts``;
a ``contradicted`` verdict keeps the HARD class only when its quote is a
verbatim run of the evidence THIS call showed it. Otherwise it demotes to the
soft ``judge_contradicted_unquoted``, counted ``hardfail_demoted_no_quote``.

Surfaces:

  1. RESOLUTION — quoted verbatim → hard; missing / paraphrased / invented /
     lifted-from-the-claim / too-short → soft, counted. The SCORE is identical
     either way: only the severity label moves.
  2. PROMPT — the quote requirement is stated wherever the JSON shape is stated
     (one shared constant), and the per-kind judge profile versions are bumped.
  3. SCOPE — only the judge's contradiction is gated. The deterministic hard
     reasons carry their proof by construction and are untouched.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

import legba.data.provenance.verify as V
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    FAIL_CLASS_SOFT,
    verify_finding_faithfulness,
)

_EVIDENCE_TITLE = "Reserves fell for a third consecutive month, the bank said"


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


_BODY = (
    "The central bank raised rates by fifty basis points [1].\n"
    "Reserves rose sharply on the month [1].\n"
)


def _citations() -> list[dict[str, Any]]:
    return [
        {"marker": "[1]", "signal_id": str(uuid4()), "title": _EVIDENCE_TITLE}
    ]


async def _run(quote: str | None):
    payload: dict[str, Any] = {"verdicts": ["supported", "contradicted"]}
    if quote is not None:
        payload["quotes"] = ["", quote]
    judge = _StubJudge(payload)
    report = await verify_finding_faithfulness(
        body=_BODY, citations=_citations(), judge_llm=judge
    )
    return report, judge


def _contradiction_span(report):
    return next(s for s in report.unsupported_spans if "Reserves rose" in s.text)


# ---------------------------------------------------------------------------
# 1. Resolution
# ---------------------------------------------------------------------------


async def test_verbatim_quote_earns_the_hard_class(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report, _ = await _run(_EVIDENCE_TITLE)
    span = _contradiction_span(report)
    assert span.reason == "judge_contradicted"
    assert span.as_dict()["fail_class"] == FAIL_CLASS_HARD
    assert "hardfail_demoted_no_quote" not in report.counters


async def test_partial_verbatim_run_still_resolves(monkeypatch) -> None:
    """A quote need only be a VERBATIM RUN of the evidence, not the whole entry
    — whitespace and case are folded, surrounding quote marks tolerated."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report, _ = await _run('  "reserves FELL for a third   consecutive month"  ')
    assert _contradiction_span(report).reason == "judge_contradicted"


@pytest.mark.parametrize(
    "quote,label",
    [
        (None, "no quotes array at all"),
        ("", "empty quote"),
        ("Reserves declined over three months running", "paraphrase"),
        ("The bank confirmed a currency peg was abandoned", "invented"),
        ("Reserves rose sharply on the month", "lifted from the CLAIM"),
        ("reserves", "too short to mean anything"),
    ],
)
async def test_unresolvable_quote_demotes_to_soft(
    monkeypatch, quote: str | None, label: str
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report, _ = await _run(quote)
    span = _contradiction_span(report)
    assert span.reason == "judge_contradicted_unquoted", label
    assert span.as_dict()["fail_class"] == FAIL_CLASS_SOFT, label
    assert report.counters["hardfail_demoted_no_quote"] == 1, label


async def test_the_demotion_never_moves_the_score(monkeypatch) -> None:
    """Only the SEVERITY label moves — the claim fails either way, so
    faithfulness is byte-identical with and without a resolvable quote."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    quoted, _ = await _run(_EVIDENCE_TITLE)
    unquoted, _ = await _run(None)
    assert quoted.faithfulness_score == unquoted.faithfulness_score == 0.5
    assert quoted.checkable_claims == unquoted.checkable_claims
    assert quoted.supported_claims == unquoted.supported_claims


async def test_misaligned_quotes_array_is_ignored_wholesale(monkeypatch) -> None:
    """A quotes array of the wrong LENGTH cannot be trusted to line up with the
    verdicts — every contradiction demotes rather than risk crediting a quote to
    the wrong claim."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        {"verdicts": ["supported", "contradicted"], "quotes": [_EVIDENCE_TITLE]}
    )
    report = await verify_finding_faithfulness(
        body=_BODY, citations=_citations(), judge_llm=judge
    )
    assert _contradiction_span(report).reason == "judge_contradicted_unquoted"


async def test_supported_and_unsupported_verdicts_are_unaffected(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge({"verdicts": ["supported", "unsupported"]})
    report = await verify_finding_faithfulness(
        body=_BODY, citations=_citations(), judge_llm=judge
    )
    assert _contradiction_span(report).reason == "judge_unsupported"
    assert "hardfail_demoted_no_quote" not in report.counters


# ---------------------------------------------------------------------------
# 2. Prompt + versioning
# ---------------------------------------------------------------------------


async def test_quote_rule_reaches_the_judge_prompt(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    _, judge = await _run(None)
    assert judge.prompts and '"quotes"' in judge.prompts[0]
    assert "VERBATIM" in judge.prompts[0]


def test_quote_rule_is_one_shared_constant() -> None:
    """Stated wherever the JSON shape is stated, from ONE constant — so the
    requirement cannot drift between the unit, composition and absence routes."""
    rule = V._JUDGE_QUOTE_RULE
    assert '"quotes"' in rule and "VERBATIM" in rule
    assert rule in V._ABSENCE_JUDGE_SYSTEM


def test_judge_profile_versions_bumped_for_the_prompt_change() -> None:
    """A prompt change is a VISIBLE per-kind version bump (the versioned-profile
    contract), so calibration history splits cleanly on it."""
    assert V._JUDGE_PROFILES[V.CLAIM_KIND_CITATION_SUPPORT].version == "citsupp.v4"
    assert V._JUDGE_PROFILES[V.CLAIM_KIND_ABSENCE].version == "absence.v2"


# ---------------------------------------------------------------------------
# 3. Scope — the deterministic hard reasons are untouched
# ---------------------------------------------------------------------------


async def test_deterministic_hard_reasons_need_no_quote(monkeypatch) -> None:
    """``unresolved_citation`` IS its own proof (the marker resolves to nothing)
    and the M13 guard carries the matched surface in its span text — neither is
    a judge verdict, so neither is gated on a quote."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=(
            "Charlie seized the port on Tuesday [9].\n"
            "Cooperation with former President Trump resumed [1].\n"
        ),
        citations=_citations(),
    )
    reasons = {s.reason for s in report.unsupported_spans}
    assert "unresolved_citation" in reasons
    assert "stale_leader" in reasons
    assert "hardfail_demoted_no_quote" not in report.counters
