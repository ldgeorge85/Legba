# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E6-faucet-1 — the junk gate must reject spaced-currency / measure relics.

The 2026-07-12 review live-tested ``is_junk_entity`` and found the currency /
quantity relic class LEAKING past the gate (spaced ``$`` symbol, ``mln``/``tln``
magnitude abbreviations, ``per cent`` / ``per day`` tails, leading approximators)
— so the 238 rows E6a pruned would re-accumulate from the live flow. This locks
the extended ``_is_quantity_unit_phrase`` behaviour:

  * every relic shape the census found is now junk;
  * a battery of REAL entities (military units, aircraft, place names carrying a
    number, orgs, summits) stays NON-junk — the gate must not over-reach.
"""

from __future__ import annotations

import pytest

from legba.data._entity_canon import is_junk_entity


# The exact live shapes that LEAKED before the fix (must now be junk).
_RELICS = [
    "$ 307 mln", "80,000 barrels per day", "1,100 tln won",
    "almost $ 800 million", "€ 1.5B", "36 per cent", "$ 5B", "$ 2.2B",
    "28.3 million cubic meters", "10.97 rubles", "US$ 525 million",
    "1 billion rubles", "396.4 billion cubic meters", "£ 2.4bn",
    "2,655 tln won", "an additional $ 25.74 million", "just $ 10",
    "some 4 million barrels", "90 trln won", "US$ 9.62 bill".replace(" bill", " bln"),
    "$ 729.3 bln", "200 bln - won".replace(" - ", " "), "40 tln won",
]

# REAL entities that share surface features but must NEVER be flagged junk.
_KEEP = [
    # (NB: 2-char tokens like "G7" are dropped by the pre-existing len<=2 rule,
    # unrelated to this currency faucet — so they are not asserted here.)
    "150th Infantry Division", "4th Marine Division", "Boeing 737",
    "MiG-29", "Su-34", "Sea King", "G20", "COP28", "Area 51",
    "Fortune 500", "Group of 20", "the 51st State", "Route 66",
    "Fifth Avenue", "New Year", "Fort Worth", "The Economist",
    "Exercise Sea Breeze", "9/11", "North Sea", "Strait of Hormuz",
    "Lake Chad", "Mekong River", "the Indian Ocean", "World Cup Group I",
    "United Nations", "the World Economic Forum",
]


@pytest.mark.parametrize("s", _RELICS)
def test_currency_quantity_relic_is_junk(s):
    assert is_junk_entity(s) is True, f"relic not caught: {s!r}"


@pytest.mark.parametrize("s", _KEEP)
def test_real_entity_not_flagged_junk(s):
    assert is_junk_entity(s) is False, f"real entity wrongly flagged junk: {s!r}"
