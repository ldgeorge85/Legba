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

The R10 section at the foot of the file is the same exercise one sweep later
(2026-07-29): a live re-test found 26 of 28 numeric / measure / currency / time
surfaces STILL minting profiles, in classes these predicates did not model at
all (unit-bearing measures, dotted/coded currency prefixes, ranges, spaced or
abbreviated percents, bare number+magnitude, scaffolded clock times, year
ranges, decades). Same contract: every leak rejected, every digit-bearing real
referent kept.
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


# ===========================================================================
# R10 (2026-07-29 DQ sweep) — the SECOND measure faucet.
#
# The E6 pass above closed the currency/quantity shapes it censused. A live
# re-test of the gate found a further 26-of-28 numeric / measure / currency /
# time strings still minting entity_profiles rows, in classes the earlier
# predicates simply did not model:
#
#   * a measure with a unit the set never held (nautical miles, centimetres,
#     dwt) or an adjective between the number and the unit ("53 NAUTICAL
#     miles");
#   * a currency prefix written with dots ("U.S.$318") or as a code glued to
#     the digits ("RMB7.92bn"), and RANGE forms ("$ 123m-$184 m");
#   * a percent spaced off its number ("3.50 % to 3.75 %") or abbreviated
#     ("12pc rate");
#   * a bare number+magnitude with no unit at all ("1.4 bln", "59B");
#   * a clock time carrying an attachment or forming a range ("4:27 p.m. on
#     Tuesday", "8:30pm to 1:30pm") — the old _CLOCK_RE was anchored, so it
#     only ever matched a BARE time;
#   * a year RANGE ("2003 to 2006") and a bare DECADE ("2030s", "70s").
#
# The keep battery is the point of the exercise: every one of these classes is
# reachable by a legitimate referent that also carries digits, so the additions
# are shaped to require that EVERY token be a number / qualifier / unit before
# anything is rejected.
# ===========================================================================

# The exact live surfaces the R10 census found leaking (must now be junk).
_R10_LEAKS = [
    "53 nautical miles", "U.S.$318 Million", "4:27 p.m. on Tuesday",
    "175 centimeters", "$ 123m-$184 m", "8:30pm to 1:30pm", "80 km / h",
    "RMB7.92bn ( $ 1.1bn", "2003 to 2006", "1.4 bln", "12pc rate",
    "50,000 dwt", "3.50 % to 3.75 %", "2030s", "70s", "59B",
    "above US$ 331 billion",
]

# Near-miss variants of the same classes — the shapes the census would have
# found next tick.
_R10_VARIANTS = [
    "12 nautical miles", "US$ 318 million", "10:15 a.m. on Monday",
    "1.8 metres", "1999-2001", "the 1990s", "'80s", "45 pct",
    "€2.4bn to €3.1bn", "120 knots", "8,500 teu", "3.2 mln",
]

# REAL referents that carry digits / measures / colons and MUST survive. Every
# R10 predicate has at least one adversary here.
_R10_KEEP = [
    # designators: the dash-range rule must not eat a hyphenated model number
    "F-16", "Su-57", "B-52", "T-72", "COVID-19", "RS-28 Sarmat", "MiG-31",
    # numbered institutions / events / places
    "G20", "Article 5", "COP30", "9/11 Commission", "9/11", "Article 370",
    "Section 232", "Chapter 11", "Studio 54", "Highway 61", "Level 3",
    "Vision 2030", "September 11", "October 7", "March 2022",
    # colon / time adversaries
    "Star Wars: Episode IV", "Psalm 23:1",
    # words the R10 qualifier additions introduce ("square", "rate", "above",
    # "nautical", "h") must never be junk on their own or in a real name
    "Times Square", "Tiananmen Square", "Exchange Rate", "Above the Law",
    "Scotland Yard", "Foot Locker", "H Mart", "Knots Landing",
    # magnitude-suffix adversaries: a ONE-digit stem is a brand far more often
    # than a measure
    "3M Company", "5G Americas",
]


@pytest.mark.parametrize("s", _R10_LEAKS)
def test_r10_live_leak_is_junk(s):
    assert is_junk_entity(s) is True, f"R10 leak not caught: {s!r}"


@pytest.mark.parametrize("s", _R10_VARIANTS)
def test_r10_class_variant_is_junk(s):
    assert is_junk_entity(s) is True, f"R10 variant not caught: {s!r}"


@pytest.mark.parametrize("s", _R10_KEEP)
def test_r10_real_entity_survives(s):
    assert is_junk_entity(s) is False, f"R10 over-reach — wrongly junk: {s!r}"
