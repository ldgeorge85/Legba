# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phrase-list headers split on semicolons only.

``watch_candidates`` and ``unanswered_aspects`` are semicolon-separated lists
of phrases. A single phrase that contains commas must stay one item; splitting
it on commas would turn the dated fragment into a standing watch with no
subject. ``cited_refs`` keeps its comma-separated form. Pure - no database.
"""

from __future__ import annotations

from legba.data.analysts.consult_reply_parsing import _parse_sentinel_final

_A = "3f69f001-e65c-48ac-b5a3-99f128ecbcee"
_B = "0b5a1c3e-1111-4c2d-9e8f-123456789abc"


def _final(headers: str) -> dict:
    payload = _parse_sentinel_final(f"<<<FINAL>>>\n{headers}\n\nThe answer prose.")
    assert payload is not None
    return payload


def test_single_watch_item_with_commas_stays_one_item() -> None:
    item = "the regulator publishes the ruling, naming the supplier, by 2026-12-01"
    payload = _final(f"uncertainty: 0.3\nwatch_candidates: {item}")
    assert payload["watch_candidates"] == [item]


def test_single_unanswered_aspect_with_commas_stays_one_item() -> None:
    item = "whether the vote, once held, binds the ministry"
    payload = _final(f"uncertainty: 0.3\nunanswered_aspects: {item}")
    assert payload["unanswered_aspects"] == [item]


def test_semicolon_lists_still_split() -> None:
    payload = _final(
        "uncertainty: 0.3\n"
        "watch_candidates: a, b by 2026-12-01; c by 2026-12-05\n"
        "unanswered_aspects: one gap; another gap"
    )
    assert payload["watch_candidates"] == ["a, b by 2026-12-01", "c by 2026-12-05"]
    assert payload["unanswered_aspects"] == ["one gap", "another gap"]


def test_cited_refs_still_split_on_commas() -> None:
    payload = _final(f"uncertainty: 0.3\ncited_refs: {_A}, {_B}")
    assert payload["cited_refs"] == [_A, _B]
