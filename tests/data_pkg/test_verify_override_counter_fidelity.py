# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-G8 (2026-08-03) — the counters and the ledger must reconcile.

The 08-03 counter audit's third flag, and the only one nobody had written down:

    9 of the 11 rows where ``hardfail_demoted_no_quote`` fired show counter-count
    greater than surviving-label-count. In every one checked, the same row also
    carries ``absence_slice_verified``: the deterministic absence check,
    independently verifying the SAME claim the judge tried and failed to hard-fail
    without a quote, overrides the demoted soft-fail entirely to ``supported`` —
    erasing it, not just relabeling it. Only ~40% of demotion attempts (6 of 15
    combined) leave a visible trace today.

The precedence itself is CORRECT — deterministic evidence beats an
under-evidenced LLM contradiction attempt. What was wrong is that it was
undocumented and unmeasurable: a counter records an ATTEMPT, the ledger holds
what SURVIVED, and where an override erased a verdict the two differ by design.
A calibration read that mistook one for the other would be wrong by 60% with no
way to notice.

So the erase emits its own receipt and the arithmetic closes, for every reason R:

    <R's attempt counter>  ==  surviving R rows  +  override_erased_R
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import legba.data.provenance.verify as V
from legba.data.provenance.verify import (
    VERDICT_SUPPORTED,
    verify_finding_faithfulness,
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubJudge:
    """An UNQUOTED contradiction on the main path; a clean pass on the slice."""

    subprovider = "stub-judge"

    def __init__(self) -> None:
        self.slice_calls = 0

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        if system == V._ABSENCE_SLICE_JUDGE_SYSTEM:
            self.slice_calls += 1
            return _Response(json.dumps({"verdicts": ["supported"], "quotes": [""]}))
        import re

        n = len(re.findall(r"^\d+\.\s", messages[0]["content"], re.MULTILINE))
        return _Response(
            json.dumps(
                {
                    "verdicts": ["contradicted"] * max(n, 1),
                    # A quote that is not a verbatim run of the shown evidence →
                    # V-D demotes to judge_contradicted_unquoted and the counter
                    # hardfail_demoted_no_quote fires.
                    "quotes": ["a paraphrase that resolves against nothing"] * max(n, 1),
                }
            )
        )


class _SliceConn:
    def __init__(self, titles: list[str]) -> None:
        self._titles = titles

    async def fetchrow(self, sql: str, *a: Any):
        if "analyst_traces" in sql:
            return {"input_row_refs": [uuid4() for _ in self._titles]}
        return None

    async def fetch(self, sql: str, *a: Any):
        return [
            {
                "title": t,
                "body": "",
                "source_id": "source.wire",
                "provenance_kind": "",
                "row_kind": "signal",
            }
            for t in self._titles
        ]


_CLAIM = "No new sanctions designations were reported this window [1]."


def _citations() -> list[dict[str, Any]]:
    return [
        {"marker": "[1]", "signal_id": str(uuid4()), "title": "Ministry budget briefing"}
    ]


async def test_the_live_shape_an_absence_pass_erasing_a_demoted_hard_fail(
    monkeypatch,
) -> None:
    """The measured collision, reproduced: attempt counted, ledger row gone."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge()
    report = await verify_finding_faithfulness(
        body=f"{_CLAIM}\n",
        citations=_citations(),
        judge_llm=judge,
        target_id="country_g20_ru",
        slice_conn=_SliceConn(["Central bank holds rates steady"]),
        run_id=uuid4(),
    )
    # The judge ATTEMPTED an unquoted hard fail…
    assert report.counters["hardfail_demoted_no_quote"] == 1
    # …the absence check then verified the same claim outright…
    assert report.counters["absence_slice_verified"] == 1
    cv = next(c for c in report.claim_verdicts if "sanctions" in c.text)
    assert cv.verdict == VERDICT_SUPPORTED, "the deterministic check wins — by design"
    # …and the ERASE is now its own receipt rather than a silent gap.
    assert report.counters["override_erased_judge_contradicted_unquoted"] == 1


async def test_the_arithmetic_closes(monkeypatch) -> None:
    """attempt counter == surviving rows + erases, for every reason."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body=f"{_CLAIM}\n",
        citations=_citations(),
        judge_llm=_StubJudge(),
        target_id="country_g20_ru",
        slice_conn=_SliceConn(["Central bank holds rates steady"]),
        run_id=uuid4(),
    )
    reason = "judge_contradicted_unquoted"
    surviving = sum(1 for cv in report.claim_verdicts if cv.reason == reason)
    erased = report.counters.get(f"override_erased_{reason}", 0)
    assert report.counters["hardfail_demoted_no_quote"] == surviving + erased


async def test_an_untouched_verdict_emits_no_erase_receipt(monkeypatch) -> None:
    """Sparse: the receipt appears only where an erase actually happened."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body=f"{_CLAIM}\n", citations=_citations(), judge_llm=_StubJudge()
    )
    assert report.counters["hardfail_demoted_no_quote"] == 1
    assert not [k for k in report.counters if k.startswith("override_erased_")]
    cv = next(c for c in report.claim_verdicts if "sanctions" in c.text)
    assert cv.reason == "judge_contradicted_unquoted", "it survived; nothing erased"


# ---------------------------------------------------------------------------
# The seam itself, exercised directly
# ---------------------------------------------------------------------------


def _report_with(reason: str) -> V.FaithfulnessReport:
    return V.FaithfulnessReport(
        faithfulness_score=0.0,
        checkable_claims=1,
        supported_claims=0,
        unsupported_spans=[V.UnsupportedSpan(text="c", reason=reason)],
        claim_verdicts=[V.ClaimVerdict.failed("c", reason, [])],
        counters={"some_attempt_counter": 1},
        score_denominator=1,
    )


def test_a_supporting_override_records_the_erase() -> None:
    out = V._apply_claim_overrides(
        _report_with("judge_contradicted_unrefuted"),
        [V._ClaimOverride(text="c", supported=True, counter="absence_slice_verified")],
    )
    assert out.counters["override_erased_judge_contradicted_unrefuted"] == 1
    assert out.claim_verdicts[0].verdict == VERDICT_SUPPORTED


def test_a_reason_CHANGING_override_records_the_erase_too() -> None:
    """A different failure reason erases the first one just as thoroughly."""
    out = V._apply_claim_overrides(
        _report_with("judge_contradicted_unquoted"),
        [
            V._ClaimOverride(
                text="c",
                supported=False,
                counter="absence_slice_contradicted",
                reason="absence_slice_contradicted",
            )
        ],
    )
    assert out.counters["override_erased_judge_contradicted_unquoted"] == 1
    assert out.claim_verdicts[0].reason == "absence_slice_contradicted"


def test_an_override_that_keeps_the_same_reason_erases_nothing() -> None:
    out = V._apply_claim_overrides(
        _report_with("metadata_mismatch"),
        [
            V._ClaimOverride(
                text="c",
                supported=False,
                counter="metadata_mismatch",
                reason="metadata_mismatch",
            )
        ],
    )
    assert not [k for k in out.counters if k.startswith("override_erased_")]


def test_annotate_only_erases_nothing_and_emits_nothing() -> None:
    """Carrying a finding onto a row without moving its verdict is the point."""
    out = V._apply_claim_overrides(
        _report_with("judge_contradicted_unquoted"),
        [
            V._ClaimOverride(
                text="c",
                supported=True,
                counter="metadata_verified_not_dominant",
                detail="metadata leg checked and holds",
                annotate_only=True,
            )
        ],
    )
    assert not [k for k in out.counters if k.startswith("override_erased_")]
    assert out.claim_verdicts[0].reason == "judge_contradicted_unquoted"
    assert "metadata leg checked" in (out.claim_verdicts[0].detail or "")


def test_a_previously_SUPPORTED_row_has_no_reason_to_erase() -> None:
    report = V.FaithfulnessReport(
        faithfulness_score=1.0,
        checkable_claims=1,
        supported_claims=1,
        claim_verdicts=[V.ClaimVerdict.supported("c", [])],
        score_denominator=1,
    )
    out = V._apply_claim_overrides(
        report,
        [
            V._ClaimOverride(
                text="c",
                supported=False,
                counter="absence_slice_contradicted",
                reason="absence_slice_contradicted",
            )
        ],
    )
    assert not [k for k in out.counters if k.startswith("override_erased_")]


def test_the_precedence_contract_is_written_down() -> None:
    """The audit spent a section rediscovering this from the data."""
    import inspect

    src = inspect.getsource(V)
    assert "PRECEDENCE CONTRACT" in src
    assert "override_erased_" in V._apply_claim_overrides.__doc__
