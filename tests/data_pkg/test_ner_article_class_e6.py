# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E6-faucet-2 — an article-prefixed surface never defaults to `person`.

The `_classify_entity_text` tier-2 fallback ("two title-cased tokens, no cue →
person") is the mechanical root of the 53% person-skew and the article-twin
merge blockage (persons never auto-merge, so a mis-classed "the X" can never
fold onto its bare "X" twin). The fix: an article-prefixed surface with no cue
falls through to `entity`, not `person`.

Locked here:
  * article-prefixed no-cue surfaces → `entity` (NOT person);
  * article-prefixed CUE surfaces still classify by their cue (org/location/…);
  * UN-prefixed two-cap names still default to `person` (unchanged);
  * a name initial "A." is NOT treated as the article "a".
"""

from __future__ import annotations

import pytest

from legba.data.filters.ner import _classify_entity_text as clf


@pytest.mark.parametrize("s", [
    # Article-prefixed, NO cue among cfeatures above → entity (NOT person).
    "the Indian Ocean", "The Economist", "the Kerch Strait", "the White House",
    "a Su-34", "the World Cup", "the Palace of Justice", "the Strait of Malacca",
])
def test_article_no_cue_is_entity_not_person(s):
    assert clf(s) == "entity", f"{s!r} should be entity, got {clf(s)!r}"


@pytest.mark.parametrize("s,expected", [
    ("the Russian Foreign Ministry", "organization"),  # cue "ministry"
    ("the Russia - ASEAN Summit", "event"),            # cue "summit"
    ("The Iran War", "event"),                          # cue "war"
    ("the Second World War", "event"),                  # cue "war"
])
def test_article_with_cue_still_classifies_by_cue(s, expected):
    # The cue scan runs ABOVE the article rule, so a real cue still wins — the
    # article rule only changes the no-cue person default.
    assert clf(s) == expected, f"{s!r} should be {expected}, got {clf(s)!r}"


@pytest.mark.parametrize("s", [
    "Vladimir Putin", "Sergey Lavrov", "Xi Jinping", "Donald Trump",
])
def test_unprefixed_two_cap_still_person(s):
    assert clf(s) == "person", f"{s!r} should stay person, got {clf(s)!r}"


def test_name_initial_not_article():
    # "A." is a name initial, not the article "a" — must stay person.
    assert clf("A. Merkel") == "person"
