# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane N-D — the deterministic span → cited-source checks.

Each case is a live 2026-11-11 specimen (sweep_quality_1111b), with the cited
text trimmed to the passage that decides it.
"""
from __future__ import annotations

import pytest

from legba.data.gazetteer import gazetteer
from legba.data.provenance import span_evidence_checks as sec

# CBC 01f2c353 — the RU head's [51].
CBC = (
    "Russia launched 162 drones of various types and a ballistic missile at "
    "Ukraine overnight, Ukraine's air force said. Russia attacked 10 Ukrainian "
    "regions overnight, hitting fifteen sites. Russia's Defence Ministry said "
    "its forces shot down 155 Ukrainian drones overnight above eight Russian "
    "regions, as well as the Crimean peninsula."
)
TOI = "6 power substations hit in Russian strikes across Ukraine's 4 regions"
RU_BLUF = (
    "Russia's strike posture holds into day two of the 10-11 November Abu Dhabi "
    "talks — 162 drones and a ballistic missile overnight into 10 November "
    "across eight Ukrainian regions [11][51]."
)


def _resolver(texts):
    return lambda ref: texts.get(ref)


# ---------------------------------------------------------------------------
# rule 2 — numbers
# ---------------------------------------------------------------------------


def test_ru_eight_ukrainian_regions_is_caught_and_162_passes() -> None:
    flagged = sec.unsupported_numbers(RU_BLUF, _resolver({"51": CBC, "11": "Zelensky said."}))
    phrases = [f["phrase"] for f in flagged]
    assert phrases == ["eight ukrainian region"]
    assert flagged[0]["refs"] == ["11", "51"]
    # "eight Russian regions" in the source does NOT license "eight Ukrainian".
    assert "162" not in [f["number"] for f in flagged]


def test_dates_years_indices_and_scores_are_not_quantities() -> None:
    nums = [m.surface for m in sec.extract_numbers(
        "On 10 November 2026, day two, rank 17, at 0.85·kinetic_strike and "
        "intensity 7.6, five days old, country_g20_ru, H200s, 06:27, 3rd tanker"
    )]
    assert nums == []


def test_a_number_before_a_marker_is_checked_against_that_marker() -> None:
    # Journal d4ff1266's lead: the 162 sits in front of the Times of India ref.
    text = (
        "Russia put 162 drones across Ukraine [[ref:aaaaaaaa-0000-0000-0000-000000000001]]"
        " — six substations in four regions, energy in Odesa "
        "[[ref:bbbbbbbb-0000-0000-0000-000000000002]]."
    )
    texts = {
        "aaaaaaaa-0000-0000-0000-000000000001": TOI,
        "bbbbbbbb-0000-0000-0000-000000000002": "Strikes hit Odesa energy sites.",
    }
    flagged = sec.unsupported_numbers(text, _resolver(texts))
    # 162 is flagged (ToI carries no 162); "six substations" / "four regions"
    # pass on the PRECEDING marker (ToI says 6 substations, 4 regions).
    assert [f["phrase"] for f in flagged] == ["162 drone"]


def test_a_clause_whose_refs_resolve_to_nothing_is_not_judged() -> None:
    assert sec.unsupported_numbers("It fired 99 drones [120].", lambda r: None) == []


def test_a_computed_count_passes_when_it_matches_the_citations() -> None:
    text = "Two routes run through one regime [1] and [2]."
    flagged = sec.unsupported_numbers(
        text, _resolver({"1": "Moonshot reached Nvidia silicon", "2": "H200s arrived"}),
        computed_counts=lambda refs: [len(refs)],
    )
    assert flagged == []


# ---------------------------------------------------------------------------
# rule 4 — incidents and outcome verbs
# ---------------------------------------------------------------------------

TANKERS = {
    "0faa410e": "Iran says 3rd oil tanker explodes after hitting naval mine in Strait of Hormuz",
    "229572ba": "IRGC Navy reports blast in third tanker in Hormuz Strait",
    "5ed90ffe": "Two oil tankers catch fire after explosions in Strait of Hormuz",
    "fc967ac3": "Two more offending tankers explode in Strait of Hormuz",
    "db887bad": "British maritime agency reports projectile strike on tanker off Oman's coast",
}


def test_same_incident_groups_the_two_restatements() -> None:
    groups = sorted(sorted(g) for g in sec.incident_groups(TANKERS))
    assert groups == [["0faa410e", "229572ba"], ["5ed90ffe", "fc967ac3"], ["db887bad"]]


def test_distinct_event_count_is_four_not_five() -> None:
    # A+B (one report of two tankers), C (the third tanker, twice), D (UKMTO).
    assert sec.distinct_incident_count(TANKERS) == 4


def test_titles_without_a_quantity_are_never_merged_on_topic() -> None:
    assert not sec.same_incident(
        "Tanker struck off Oman", "Tanker hit near Hormuz, crew evacuated"
    )


def test_kills_over_exploded_and_burning_is_overstated() -> None:
    sent = "four tanker kills in one window"
    cited = [
        "Two oil tankers catch fire after explosions in the Strait of Hormuz.",
        "The IRGC claimed that the destruction of US coastal radar had increased "
        "the risks facing tankers.",
    ]
    assert sec.overstated_outcomes(sent, cited) == ["kills"]


def test_a_death_in_the_source_licenses_killing() -> None:
    assert sec.overstated_outcomes(
        "the strike killing about 20 people",
        ["The strike resulted in the killing of 20 people."],
    ) == []


# ---------------------------------------------------------------------------
# rule 3 — actors
# ---------------------------------------------------------------------------

US_BLUF = (
    "The US-Iran confrontation is drifting toward negotiation, held back by "
    "tanker attacks and Israeli pressure on Trump, even as diplomatic channels "
    "advance [18][40]."
)
#: The live [18] (6b40bd42) — US 3 mentions, Ukraine 4, Russia 4.
ZELENSKY = (
    "Ukrainian President Volodymyr Zelensky said on Tuesday he was very happy "
    "with messages he was getting from the US in terms of moving towards a "
    "resolution of the more than 4-1/2-year-old war with Russia. Zelensky was "
    "addressing journalists after a day of talks with Nordic leaders in Denmark, "
    "as a first day of US-led trilateral discussions between Ukraine, Russia and "
    "the US wrapped up in Abu Dhabi. To read the full story "
    "[https://www.hindustantimes.com/world-news/us-news/zelensky-says-he-is-"
    "encouraged-by-us-signals-on-ending-russia-ukraine-war-i-see-changes]"
    "(https://www.hindustantimes.com/world-news/us-news/zelensky-says-he-is-"
    "encouraged-by-us-signals-on-ending-russia-ukraine-war-i-see-changes)"
)
RUBIO = (
    "Rubio said he saw no need to resume major combat operations against Iran. "
    "He also hailed US-mediated talks between Russia and Ukraine in Abu Dhabi."
)


@pytest.fixture()
def lex():
    out = sec.lexicon_from_gazetteer(gazetteer())
    if out is None:  # pragma: no cover — the exemplar profile declares them
        pytest.skip("profile gazetteer declares no country tokens")
    return out


def test_us_bluf_citing_a_russia_ukraine_item_is_flagged(lex) -> None:
    flagged = sec.actor_mismatched_citations(
        US_BLUF, [("[18]", ZELENSKY), ("[40]", RUBIO)], lex, self_key="us",
    )
    assert [f["marker"] for f in flagged] == ["[18]"]
    assert "ir" in flagged[0]["span_actors"]
    # [40] names Iran, so the ACTOR check passes it; its Russia-Ukraine half is a
    # topic mis-sourcing an actor check cannot see (stated, not claimed).


def test_a_neighbour_named_beside_the_desk_country_is_not_a_mismatch(lex) -> None:
    flagged = sec.actor_mismatched_citations(
        "The US-Iran standoff holds [9].",
        [("[9]", "The US and China traded statements; the US said talks continue.")],
        lex, self_key="us",
    )
    assert flagged == []  # China named once, the US twice: about the US


def test_a_span_about_its_own_country_alone_is_not_judged(lex) -> None:
    assert sec.actor_mismatched_citations(
        "North Korea's posture holds [9].",
        [("[9]", "South Korea and South Korean officials said Seoul is watching.")],
        lex, self_key="kp",
    ) == []


def test_desk_country_only_citation_passes_when_it_names_no_outsider(lex) -> None:
    flagged = sec.actor_mismatched_citations(
        "Russia's posture holds while Putin chairs the summit [17].",
        [("[17]", "Russian President Vladimir Putin will chair the summit in Moscow.")],
        lex, self_key="ru",
    )
    assert flagged == []


def test_citation_naming_no_country_is_not_judged(lex) -> None:
    assert sec.actor_mismatched_citations(
        US_BLUF, [("[84]", "Tankers continue to burn despite hopes.")], lex, self_key="us",
    ) == []
