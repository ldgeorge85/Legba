# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W3-C (2026-08-04) — the NER person gate: the person table is not all persons.

A 60-pair random sample of the V-G6 phonetic-alias backlog found NINE pairs
(15%) whose ``entity_class='person'`` rows denote no human. Every one arrived
through :func:`legba.data.filters.ner._classify_entity_text`, and the live
payloads say exactly how:

  * the two-capitalised-tokens PERSON DEFAULT at the bottom of the ladder,
    reached because GLiREL emitted the surface as an endpoint of a locative
    predicate the ladder has no reading for ("Eastern Pendleton" / part of,
    "Kfar Shoba" / border with, "Media Media" / located in); and
  * :data:`_PERSON_SUBJECT_PREDICATES`, which returns ``person`` for the SUBJECT
    of "member of" BEFORE the cue ladder is consulted — which is how "Asian
    Championships" and a lower-cased Uzbek phrase became people.

The fix rests only on the SURFACE, never on the predicate. Treating the locative
predicates as a person veto was the obvious move and was measured against the
live relation payloads first: 18,304 relations carry a person subject under
"part of" and 13,399 under "located in", because GLiREL puts real people there
constantly. That veto would have demoted thousands of genuine names to shrink a
nine-row defect class, so it was not taken; this module pins that decision too.

THE REGRESSION SET IS THE LABELED SAMPLE. Every surface below marked a person
comes from ``planning/TRANSLITERATION_LABELED_SAMPLE_2026-08-04.csv`` — 41 rows
labeled ``same_person`` with a citation apiece, plus the ``different_person``
rows, which are still PEOPLE (two of them, which is a different problem). None
of them may lose ``person``, under any predicate.
"""

from __future__ import annotations

import pytest

from legba.data._entity_canon import canonicalize_entity
from legba.data.filters.ner import _classify_entity_text as clf

# ---------------------------------------------------------------------------
# The confirmed non-people, with the predicate the LIVE payload actually
# carried. Read off `signals.payload->'entities'`, not invented.
# ---------------------------------------------------------------------------

CONFIRMED_NON_PERSONS: tuple[tuple[str, str], ...] = (
    # NWS forecast ZONES — "BLIZZARD WARNING-Western Pendleton County and a Wind
    # Advisory for Eastern Pendleton" was ingested and NER'd into two people.
    ("Eastern Pendleton", "part of"),
    ("Western Pendleton", "part of"),
    ("Eastern Monmouth", "part of"),
    ("Western Monmouth", "part of"),
    # A reduplicated common noun (Turkish/Kurdish "medya" = media).
    ("Media Media", "located in"),
    ("Medya Medya", "located in"),
    # Uzbek: "at the opening ceremony", a case-inflected phrase.
    ("ochilish marosimidagi", "member of"),
    ("ochilish marosimida", "member of"),
    # A competition, singular and plural.
    ("Asian Championships", "member of"),
    ("Asian Championship", "member of"),
    # A municipality in Hasbaya District, South Lebanon.
    ("Kfar Shoba", "border with"),
    ("Kfar Shouba", "border with"),
    # The South Atlantic island group, and its live misspelling.
    ("Tristan da Cunha", "part of"),
    ("Tristan de Cunha", "part of"),
)

# The compass census's org/location siblings — same mechanism, other classes.
CONFIRMED_NON_PERSONS_CENSUS: tuple[tuple[str, str], ...] = (
    ("World Cup Group K", "part of"),
    ("World Cup Group G", "part of"),
    ("Southwest Asian", "part of"),
    ("Southeast Asian", "part of"),
    ("East Germany", "part of"),
    ("West Germany", "part of"),
    ("Nord Stream", "located in"),
    ("Central Command", "part of"),
    ("Eastern DR Congo", "part of"),      # took `person` off the "dr" role cue
    ("East General Stewart Way", "part of"),   # …and off "general"
)

# ---------------------------------------------------------------------------
# The regression set: every confirmed PERSON in the labeled sample.
# ---------------------------------------------------------------------------

LABELED_PEOPLE: tuple[str, ...] = (
    # same_person rows (41), in sample order.
    "Shamsail Saraliyev", "Shamsail Saraliev",
    "Dmitry Milyayev", "Dmitry Millaev",
    "Ezzeddine Onahi", "Ezzeddine Unahi",
    "Robbie Nock", "Robbie Knock",
    "Ziyad al - Nakhalah", "Ziad al - Nakhalah",
    "Brice Oligui Ngema", "Brice Oligui Nguema",
    "Luka Modric", "Luca Modric",
    "Alexander Avdeyev", "Alexander Avdeev",
    "Cash Patel", "Kash Patel",
    "Dmitry Protopov", "Dmitry Protopopov",
    "Volodymyr Zelenskyy", "Volodymyr Zelensky",
    "Abdourahamane Tiani", "Abdourahamane Tchiani",
    "Dmytro Lubinets", "Dmitry Lubinets",
    "Nasry Asfura", "Nasri Asfoura",
    "Nezar Amidi", "Nizar Amidi",
    "Sergei Tolchenov", "Sergey Tolchenov",
    "Mehdi Taremi", "Mehdi Tarami",
    "Ram Emanuel", "Ram Emmanuel",
    "Sergei Mikayev", "Sergey Mikayev",
    "Mosaab Gharbi", "Mossab Gharbi",
    "Killian Mbépé", "Killian Mbappé",
    "Seyed Ali Khamenei", "Seyed Ali Khameni",
    "Sergey Tsivilyov", "Sergey Tsivilev",
    "Alan Gagloev", "Alan Gagloyev",
    "Hong Myung - Po", "Hong Myung - bo",
    "Mikhail Gutseriev", "Mikhail Gutseriyev",
    "Mikhailo Drapaty", "Mikhail Drapatiy",
    "Avigdor Lieberman", "Avigdor Liberman",
    "Khwaja Asif",
    "Mykhailo Drapatyi", "Mykhailo Drapaty",
    "Thomas Toochel", "Thomas Toukhel",
    "Zoran Milanovic", "Zoran Milanović",
    "Feran Torres", "Ferran Torres",
    "Pavlo Palisa", "Pavel Palisa",
    "Israel Katz", "Yisrael Katz",
    "Mikhail Razvozhayev", "Mikhail Razvojayev",
    "Peter Magar", "Peter Magyar",
    "Khalil al - Hayya", "Khalil al - Haya'a",
    "Anna Evstigneeva", "Anna Yevstigneeva",
    "Oleksandr Sersky", "Oleksandr Syrskyi",
    "Angela Nikolaou", "Angela Nikolau",
    # different_person / unresolvable rows — still PEOPLE, both of them.
    "Xu Yan", "Xu Jian",
    "SAEED KHAN", "Saeed Ghani",
    "Nan Lin", "Nina Lin",
    "Brian Johnson", "Brianna Johnson",
    "Ali Ansari", "Al Ansari",
    "Tebow Curtoya", "Tebow Cortowa",
    "Mark Kokoria", "Mark Cocoria",
    "Pavlo Ivanov", "Pavel Ivanov",
    "Sait Saladinov", "Saida Saladinov",
    "Ahmed Aal",
    # The Avi cluster — the officer V-G6 was built for.
    "Avi Balut", "Avi Bluth", "Avi Blot",
)

#: The predicates the live payloads actually pair with these names. A gate that
#: only held for one of them would be a gate that holds by accident.
LIVE_PREDICATES: tuple[str, ...] = (
    "leader of", "member of", "employed by", "located in", "part of",
    "spouse", "occupation", "",
)


@pytest.mark.parametrize("surface,predicate", CONFIRMED_NON_PERSONS)
def test_a_confirmed_non_person_never_types_person(surface: str, predicate: str) -> None:
    """The nine defect classes from the labeled sample, by their live predicate."""
    assert clf(surface, predicate=predicate, slot="subject") != "person"


@pytest.mark.parametrize("surface,predicate", CONFIRMED_NON_PERSONS_CENSUS)
def test_the_census_non_persons_never_type_person(surface: str, predicate: str) -> None:
    assert clf(surface, predicate=predicate, slot="subject") != "person"


@pytest.mark.parametrize("name", LABELED_PEOPLE)
def test_every_labeled_person_still_types_person(name: str) -> None:
    """The floor. 63 cited rows; not one may be lost to the gate."""
    for predicate in LIVE_PREDICATES:
        assert clf(name, predicate=predicate, slot="subject") == "person", (
            f"{name!r} lost `person` under predicate {predicate!r}"
        )


def test_the_gate_never_reads_a_locative_predicate_as_a_person_veto() -> None:
    """Measured, then refused: 18,304 live relations put a person on the subject
    of "part of" and 13,399 on "located in". A predicate veto would have cost
    thousands of real names to fix nine rows, so the gate is surface-only."""
    for predicate in ("part of", "located in", "border with", "headquarters in"):
        assert clf("Volodymyr Zelensky", predicate=predicate, slot="subject") == "person"
        assert clf("Ziyad al - Nakhalah", predicate=predicate, slot="subject") == "person"


def test_the_name_shape_stays_loose_for_particles_and_hyphens() -> None:
    """"Ziyad al - Nakhalah", "Khalil al - Hayya", "Hong Myung - bo" are how the
    live payloads spell three real men. Requiring EVERY token capitalised — the
    tempting tightening — would have refused all three."""
    for name in ("Ziyad al - Nakhalah", "Khalil al - Hayya", "Hong Myung - bo"):
        assert clf(name, predicate="member of", slot="subject") == "person"


# ---------------------------------------------------------------------------
# The canon side: settlement heads and the island group.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", (
    "Kfar Shoba", "Kfar Shouba", "Kafr Qasim", "Beit Hanun", "Deir Ezzor",
    "Wadi Fukin", "Jabal Dabas", "Khirbet Zanuta", "Umm Qasr", "Tel Arad",
    "Camp Mystic", "Port Arthur", "Cape Canaveral", "Lake Nigat",
    "Tristan da Cunha", "Tristan de Cunha",
))
def test_a_settlement_head_types_location_not_person(surface: str) -> None:
    """Read off the live person table: every leading occurrence is a place."""
    assert canonicalize_entity(surface, "person")[1] == "location"


@pytest.mark.parametrize("name", ("Ras Baraka", "Ayn Rand", "Bay Ismoyo"))
def test_the_ambiguous_heads_were_excluded_on_purpose(name: str) -> None:
    """Ras Baraka is the mayor of Newark; Ayn Rand wrote novels; Bay Ismoyo
    shoots for the wires. They are why the head list is evidenced rather than
    enumerated, and they must keep ``person``."""
    assert canonicalize_entity(name, "person")[1] == "person"
    assert clf(name, predicate="leader of", slot="subject") == "person"
