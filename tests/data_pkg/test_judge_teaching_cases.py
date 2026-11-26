# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The judge regression set: worked cases kept out of the prompts.

Misreads on real subjects taught several judge rules; the set retells them
with invented places and names. Inside a platform prompt they
anchored grading on their own subjects and aged; they now live in
``tests/fixtures/judge_regression/teaching_cases.json``. These tests keep the
set well formed (every expected verdict is a word its route can answer) and
keep the cases out of the prompts they were taken from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legba.data.grading_text import ABSENCE_VERDICT_TOKENS, FAITHFULNESS_VERDICT_TOKENS
from legba.data.provenance import composition_integrity
from legba.data.provenance import verify as V

_CASES_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "judge_regression" / "teaching_cases.json"
)

#: The verdict words each route's judge may answer with.
_ROUTE_VERDICTS = {
    "absence": set(ABSENCE_VERDICT_TOKENS),
    "absence_slice": {"supported", "contradicted", "unsupported"},
    "composition": set(FAITHFULNESS_VERDICT_TOKENS),
}


def _cases() -> list[dict]:
    return json.loads(_CASES_PATH.read_text(encoding="utf-8"))["cases"]


def test_the_set_is_well_formed() -> None:
    cases = _cases()
    assert cases
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    for case in cases:
        assert set(case) == {
            "id", "route", "claim", "evidence", "expected_verdict", "teaches",
        }, case["id"]
        assert case["route"] in _ROUTE_VERDICTS, case["id"]
        assert case["expected_verdict"] in _ROUTE_VERDICTS[case["route"]], case["id"]
        assert case["claim"].strip() and case["teaches"].strip(), case["id"]
        assert case["evidence"] and all(e.strip() for e in case["evidence"]), case["id"]


#: Distinctive words of the real cases. None may appear in a platform judge
#: prompt again.
_REAL_CASE_MARKERS = (
    "Kalimantan", "EPR", "Japan", "Ukraine", "Iran-Oman", "smart-defence",
    "port blockade", "macro-economic", "mobilisation",
)


@pytest.mark.parametrize(
    "name,prompt",
    [
        ("generic", V._GENERIC_JUDGE_SYSTEM),
        ("independent", V._INDEPENDENT_JUDGE_SYSTEM),
        ("survey", V._NULL_RESULT_JUDGE_SYSTEM),
        ("absence", V._ABSENCE_JUDGE_SYSTEM),
        ("absence_slice", V._ABSENCE_SLICE_JUDGE_SYSTEM),
        ("composition_rubric", composition_integrity.RUBRIC),
    ],
)
def test_no_real_case_sits_in_a_platform_prompt(name: str, prompt: str) -> None:
    for marker in _REAL_CASE_MARKERS:
        assert marker not in prompt, (name, marker)
