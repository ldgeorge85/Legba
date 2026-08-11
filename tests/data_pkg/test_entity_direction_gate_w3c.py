# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W3-C (2026-08-04) — the COMPASS-DIRECTION gate on the V-G6 alias predicate.

V-G6 added a phonetic third rung to the entity-resolution mint ladder so the
next "Avi Balut" folds onto Avi Bluth instead of becoming a fourth row for one
Israeli officer. The W3-C audit then sampled what that predicate accepts and ran
a full population census: of the 726 pairs it takes across the live person
table, **30 differ only by an opposing compass direction** — "East Germany" /
"West Germany", "Eastern Pendleton" / "Western Pendleton", "Northeast El Paso" /
"Northwest El Paso", "East Slopes" / "West Slopes" — and every one is a
guaranteed-wrong merge. The direction IS the distinction, not a spelling of it.
V-G6's own scope note had predicted exactly this ("Southwest Asian" /
"Southeast Asian" was named as a residual false positive inside the person set);
the census found it thirty times over.

These fixtures are the permanent floor under that finding. The pairs below are
VERBATIM from the census and from the labeled sample — if any of them ever
becomes foldable again, the class has silently returned.

The counterweight fixture is "Mosaab Gharbi" / "Mossab Gharbi": one real man, an
Ennahdha member arrested in Mannouba in July 2024, labeled ``same_person`` with
an Amnesty citation. A prefix or stem test for "west" would read his surname as
the Arabic ``gharb`` and refuse the only kind of pair the predicate exists to
find. Whole-token matching is the difference, and it is pinned here.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from legba.data._entity_canon import (
    DIRECTIONAL_TOKENS,
    differs_by_direction,
    leads_with_direction,
)
from legba.data.analysts.deterministic_handlers import entity_resolution as ER

# ---------------------------------------------------------------------------
# The census: pairs the gate MUST refuse. Read straight off the live scan.
# ---------------------------------------------------------------------------

GUARANTEED_WRONG_PAIRS: tuple[tuple[str, str], ...] = (
    # The report's headline cases.
    ("West Germany", "East Germany"),
    ("Eastern Pendleton", "Western Pendleton"),
    ("Eastern Monmouth", "Western Monmouth"),
    ("Northeast El Paso", "Northwest El Paso"),
    ("West Slopes", "East Slopes"),
    # V-G6's own predicted residual false positive.
    ("Southwest Asian", "Southeast Asian"),
    # The rest of the NWS forecast-zone family the census turned up.
    ("Eastern Chester", "Western Chester"),
    ("Eastern Essex", "Western Essex"),
    ("Eastern Garrett", "Western Garrett"),
    ("Eastern Greenbrier", "Western Greenbrier"),
    ("Eastern Highland", "Western Highland"),
    ("Eastern Montgomery", "Western Montgomery"),
    ("Eastern Tucker", "Western Tucker"),
    ("Western Bergen", "Eastern Bergen"),
    ("Western Mineral", "Eastern Mineral"),
    ("Western Passaic", "Eastern Passaic"),
    ("Western Roosevelt", "Eastern Roosevelt"),
    # Places, regions and theatres where the bearing is the whole referent.
    ("East Carteret", "West Carteret"),
    ("East Norriton", "West Norriton"),
    ("East Hartford", "West Hartford"),
    ("Eastern Ukraine", "Western Ukraine"),
    ("Western European", "Eastern European"),
    ("Western US", "Eastern US"),
    ("West Asian", "East Asian"),
    ("West El Paso", "East El Paso"),
    ("Southeast MT", "Southwest MT"),
    ("Battlegroup West", "Battlegroup East"),
    ("Joshua Tree NP East", "Joshua Tree NP West"),
    # Case must not rescue a pair.
    ("EAST CENTRAL", "West central"),
    ("East Point", "WEST POINT"),
    # Positional qualifiers, not only compass bearings.
    ("Upper Merryall", "Lower Merryall"),
    # Non-English bearings the live table actually carries.
    ("Nord Kivu", "Sud Kivu"),
    ("Norte Region", "Sur Region"),
)

# ---------------------------------------------------------------------------
# Pairs the gate must LEAVE ALONE — the transliteration duplicates V-G6 exists
# for, plus the lookalike people it is separately known to get wrong (those are
# a precision problem for the human adjudicator, NOT this gate's business).
# ---------------------------------------------------------------------------

MUST_NOT_GATE_PAIRS: tuple[tuple[str, str], ...] = (
    ("Mosaab Gharbi", "Mossab Gharbi"),      # gharb ⊄ gharbi — the whole point
    ("Avi Balut", "Avi Bluth"),              # the V-G6 defect itself
    ("Avi Balut", "Avi Blot"),
    ("Sergei Lavrov", "Sergey Lavrov"),
    ("Volodymyr Zelenskyy", "Volodymyr Zelensky"),
    ("Israel Katz", "Yisrael Katz"),
    ("Oleksandr Sersky", "Oleksandr Syrskyi"),
    ("Ziyad al - Nakhalah", "Ziad al - Nakhalah"),
    ("Hong Myung - Po", "Hong Myung - bo"),
    ("Cash Patel", "Kash Patel"),
    ("Nan Lin", "Nina Lin"),                 # different people, but not by bearing
    ("Ali Ansari", "Al Ansari"),
)


@pytest.mark.parametrize("name_a,name_b", GUARANTEED_WRONG_PAIRS)
def test_the_census_pairs_are_refused(name_a: str, name_b: str) -> None:
    """100% of these were hand-confirmed wrong. None may ever fold."""
    assert differs_by_direction(name_a, name_b), f"{name_a} || {name_b}"
    # Order-independent: the predicate sees pairs in whichever order the scan
    # produced them, and a gate that only worked one way would be a coin flip.
    assert differs_by_direction(name_b, name_a)


@pytest.mark.parametrize("name_a,name_b", MUST_NOT_GATE_PAIRS)
def test_genuine_variants_are_untouched(name_a: str, name_b: str) -> None:
    assert not differs_by_direction(name_a, name_b), f"{name_a} || {name_b}"
    assert not differs_by_direction(name_b, name_a)


def test_a_direction_token_shared_by_both_sides_is_not_a_distinction() -> None:
    """The gate fires on a DIFFERING direction, not on the mere presence of one.

    Otherwise "North Ossetia"/"North Osetia" — a spelling variant of one place —
    would be refused for carrying a bearing at all.
    """
    assert not differs_by_direction("North Ossetia", "North Osetia")
    assert not differs_by_direction("Upper Zakum", "Upper Zakim")
    assert not differs_by_direction("Nord Stream", "Nord Streem")


def test_a_misspelled_direction_is_refused_and_that_is_the_intended_cost() -> None:
    """"Upper"/"Uppar" differ, and one of them IS a direction, so the pair is
    refused even though it may well be one place spelled twice.

    Stated rather than left implicit: a refusal costs a fold NOT APPLIED, which
    is today's behaviour — a new row is minted, exactly as before. A wrong merge
    tombstones a real entity and nothing detects it. The gate is deliberately
    asymmetric about which of those two errors it is willing to make."""
    assert differs_by_direction("Upper Zakum", "Uppar Zakum")


def test_the_token_list_matches_on_whole_tokens_only() -> None:
    """The Gharbi rule, stated as a property rather than one example."""
    assert "gharb" in DIRECTIONAL_TOKENS
    assert "gharbi" not in DIRECTIONAL_TOKENS
    assert "north" in DIRECTIONAL_TOKENS
    assert "northrop" not in DIRECTIONAL_TOKENS
    assert all(t == t.lower() for t in DIRECTIONAL_TOKENS), "SQL lower()s both sides"


def test_leading_direction_is_first_token_only() -> None:
    """A surname that reads as a bearing must not condemn a real person."""
    assert leads_with_direction("Eastern Pendleton")
    assert leads_with_direction("Nord Stream")
    assert leads_with_direction("Upper Swat")
    assert not leads_with_direction("Oliver North")
    assert not leads_with_direction("Veronica Lake")
    assert not leads_with_direction("")


# ---------------------------------------------------------------------------
# The SHIPPED probe: the gate is in the SQL, bound as a parameter, and
# re-applied in Python.
# ---------------------------------------------------------------------------


def test_the_gate_is_in_the_shipped_predicate_as_a_bound_parameter() -> None:
    """A retyped word list is how a before/after measurement stops comparing
    like with like — the census script binds the SAME ``$2`` array."""
    sql = ER._TRANSLIT_PROBE_SQL
    assert "$2::text[]" in sql, "the direction list is a PARAMETER, never inline"
    assert "IS DISTINCT FROM i.toks[d]" in sql, "positional, on the differing token"
    assert ER._DIRECTION_TOKEN_ARRAY == sorted(DIRECTIONAL_TOKENS)
    # The V-G6 conditions this gate was added ALONGSIDE, not instead of.
    assert "levenshtein(" in sql and "dmetaphone_alt(" in sql


class _GateConn:
    """Returns ``keeper`` for the phonetic probe — i.e. a database whose planner
    surfaced the row anyway (an older build, a hand-written call site). The
    Python re-check is what must still refuse it."""

    def __init__(self, keeper: dict[str, Any] | None) -> None:
        self._keeper = keeper
        self.calls: list[tuple[Any, ...]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((sql, *args))
        return self._keeper


async def test_the_probe_refuses_a_direction_pair_the_sql_let_through() -> None:
    keeper = {
        "id": uuid.uuid4(),
        "entity_class": "person",
        "canonical_name": "Western Pendleton",
    }
    conn = _GateConn(keeper)
    got = await ER._probe_transliteration_variant(conn, "Eastern Pendleton", "person")
    assert got is None, "the compass gate is enforced in Python too"


async def test_the_probe_still_folds_the_avi_case_and_binds_the_tokens() -> None:
    keeper = {
        "id": uuid.uuid4(),
        "entity_class": "person",
        "canonical_name": "Avi Bluth",
    }
    conn = _GateConn(keeper)
    got = await ER._probe_transliteration_variant(conn, "Avi Balut", "person")
    assert got is keeper, "V-G6's own defect must still be caught"
    assert conn.calls[0][2] == ER._DIRECTION_TOKEN_ARRAY, "tokens bound to $2"


async def test_the_probe_still_folds_gharbi() -> None:
    """The regression the whole-token rule exists for."""
    keeper = {
        "id": uuid.uuid4(),
        "entity_class": "person",
        "canonical_name": "Mosaab Gharbi",
    }
    conn = _GateConn(keeper)
    assert await ER._probe_transliteration_variant(conn, "Mossab Gharbi", "person")
