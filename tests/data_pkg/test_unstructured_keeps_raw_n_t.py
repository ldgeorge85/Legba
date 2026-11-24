# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-T (2026-11-13) — a completion that fails to parse keeps its raw text on the
row. narrative_coordination fell back to ``unstructured`` 23 times in a week
(5 in one batch at 10:01-10:04 today, "Expecting ',' delimiter" at char
5,558-6,620) and no raw output survived to say why."""

from __future__ import annotations

from legba.data.analysts.inline_target import _coerce_finding

BROKEN = (
    '{"title": "No coordinated narrative", "body": "**BLUF:** No coordinated '
    'narrative appears [1].", "confidence": 0.6, "indicators": [{"statement": '
    '"Outlets repeat "VILLAGE: protest" verbatim", "status": "not_observed"}]}'
)


def test_the_raw_completion_rides_the_unstructured_row() -> None:
    finding = _coerce_finding(BROKEN, fallback_title="x")
    assert "unstructured" in finding.tags
    assert finding.data["raw_llm_response"] == BROKEN
