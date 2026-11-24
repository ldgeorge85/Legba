# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-T (2026-11-13) — one invented ordinal no longer fails the whole country
Assessment and pages. KR (7e52999f, cited [6] over [1..5]) and ZA (4442e7ef,
[4] over [1..3]) today, and 8 / 11 November ([5] over [1..4]): the stray is
almost always N+1. The sentence carrying it is withheld and recorded verbatim
on the row; the rest publishes. A body mostly written off the record still
fails exactly as before."""

from __future__ import annotations

import pytest

from legba.data.analysts import assessment_channel as ac

from tests.data_pkg.test_assessment_channel_d6 import (  # noqa: F401 — _regime_on is autouse
    _ProseLLM,
    _payload,
    _regime_on,
    _run_channel,
)


def test_a_stray_sentence_is_withheld_and_returned_verbatim() -> None:
    body = (
        "## Bottom line\n"
        "Strikes widened [[ref:1]]. Posture held [[ref:2]].\n"
        "- A sixth desk agrees [[ref:6]].\n"
        "Unrest is fiscal [[ref:1]]."
    )
    out, withheld = ac.withhold_stray_ordinal_sentences(body, [1, 2, 3])
    assert withheld == [{"ordinals": [6], "text": "- A sixth desk agrees [[ref:6]]."}]
    assert "[[ref:6]]" not in out and "Posture held [[ref:2]]." in out
    assert "\n- \n" not in out and not out.rstrip().endswith("-")
    assert [m["ordinal"] for m in ac.resolve_markers(out, [1, 2, 3])] == [1, 2, 1]


def test_mostly_stray_is_still_a_construction_failure() -> None:
    body = "A [[ref:7]]. B [[ref:8]]. C [[ref:1]]."
    out, withheld = ac.withhold_stray_ordinal_sentences(body, [1, 2])
    assert withheld == [] and out == body
    with pytest.raises(ac.AssessmentConstructionError):
        ac.resolve_markers(out, [1, 2])


@pytest.mark.asyncio
async def test_the_channel_publishes_the_rest_with_the_defect_on_the_row() -> None:
    payload = _payload()
    n = len(payload["blocks"])
    llm = _ProseLLM(
        "**Headline**\n\n"
        "The first read leads [[ref:1]]. It is consistent with the next [[ref:1]]. "
        f"A desk the record does not carry agrees [[ref:{n + 1}]]. "
        "The read holds [[ref:1]]."
    )
    _, result = await _run_channel(payload, llm=llm)
    assessment = result.finding.data["assessment"]
    assert assessment["withheld_stray_ordinals"][0]["ordinals"] == [n + 1]
    assert f"[[ref:{n + 1}]]" not in result.finding.body
    assert any(s.get("kind") == "stray_ordinal_withheld" for s in result.intermediate_steps)
