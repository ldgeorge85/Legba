# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11 SWEEP-2 - the FINAL frame's marker never reaches the persisted answer.

Two live Opus answers (consult_turns d8d95f90 / 2794032b, 10-04) persisted a
literal ``<<<FINAL>>`` and the whole header block above the prose: the
sentinel test was a strict ``startswith("<<<FINAL>>>")`` and a model that
dropped one closing bracket fell through to the salvage path, which keeps the
text verbatim. ``consult_reply_parsing._sentinel_lead`` is the one extraction
point; one test per form the prompt can emit. Pure - no database.
"""

from __future__ import annotations

import pytest

from legba.data.analysts.consult_reply_parsing import (
    _parse_round_reply,
    _parse_sentinel_final,
)

_UUID = "3f69f001-e65c-48ac-b5a3-99f128ecbcee"
_HEADERS = (
    "uncertainty: 0.25\n"
    f"cited_refs: {_UUID}\n"
    "unanswered_aspects: one gap; another\n"
    "watch_candidates: a signal\n"
    "\n"
    "## Bottom line\nThe answer prose."
)


def _assert_clean(payload: dict | None) -> None:
    assert payload is not None
    answer = payload["answer"]
    assert "FINAL" not in answer.upper().replace("BOTTOM LINE", "")
    assert "<<" not in answer and "uncertainty:" not in answer
    assert "cited_refs" not in answer
    assert answer.startswith("## Bottom line")
    assert payload["uncertainty"] == 0.25
    assert payload["cited_refs"] == [_UUID]
    assert payload["watch_candidates"] == ["a signal"]


@pytest.mark.parametrize(
    "marker",
    [
        "<<<FINAL>>>",  # the documented form
        "<<<FINAL>>",  # the 10-04 live leak: one closing bracket dropped
        "<<<FINAL>",
        "<<<FINAL",  # no closing bracket at all
        "<<FINAL>>",
        "<<< FINAL >>>",  # inner spaces
        "<<<final>>>",  # case
        "<<<FINAL>>>>",  # one bracket too many
        "**<<<FINAL>>>**",  # bold decoration
        "`<<<FINAL>>>`",  # code-span decoration
        "### <<<FINAL>>>",  # heading decoration
    ],
)
def test_every_marker_form_opens_the_frame_and_is_stripped(marker):
    _assert_clean(_parse_sentinel_final(f"{marker}\n{_HEADERS}"))


def test_surrounding_whitespace_and_newlines():
    _assert_clean(_parse_sentinel_final(f"\n\n   <<<FINAL>>   \n{_HEADERS}\n\n"))


def test_mid_text_on_its_own_line_after_narration():
    raw = f"I have enough. Let me compose the final answer.\n\n<<<FINAL>>\n{_HEADERS}"
    _assert_clean(_parse_sentinel_final(raw))


def test_mid_text_ending_a_narration_line():
    raw = f"I have enough; composing the final answer now. <<<FINAL>>>\n{_HEADERS}"
    _assert_clean(_parse_sentinel_final(raw))


def test_the_round_parser_takes_the_drifted_form_as_a_final():
    payload = _parse_round_reply(f"<<<FINAL>>\n{_HEADERS}")
    assert payload is not None and payload.get("final") is True
    _assert_clean(payload)


def test_a_longer_word_or_a_quoted_marker_is_not_the_frame():
    assert _parse_sentinel_final("<<<FINALLY we can say\nprose") is None
    # A marker quoted MID-sentence is a quotation, never the frame.
    assert _parse_sentinel_final("The sentinel <<<FINAL>>> opens a reply.\nprose") is None
