# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CW-3 — the deictic detector and the referent inliner.

Every string in the FIRES / DOES-NOT-FIRE tables below is a VERBATIM thesis
from ``planning/K4_R3_WORKSHEET_LABELED.csv`` with its measured label, so this
file is the detector's regression suite AND its evidence. Measured over the
120 labeled rows the detector fires on 12 — of which **0** were correct
matches — and on none of the 48 correct matches.

The two tables matter equally. A guard that skips questions is only as good as
the rows it leaves alone, and the near-misses here ("the recent attacks", "the
Red Sea explosion", "the announced repatriation") are all rows that DID score
correct matches: they are what a looser detector would have cost.
"""
from __future__ import annotations

import pytest

from legba.data.analysts import question_text as qt


# ---------------------------------------------------------------------------
# FIRES — R3 theses that scored 0 correct matches across every row they carried
# ---------------------------------------------------------------------------

_DEICTIC = [
    pytest.param(
        "Is the framing of the incident being driven by an orchestrated "
        "campaign from Russian or Iranian state actors, or is it a "
        "spontaneous convergence of reactions?",
        "the incident",
        id="the-incident (3 rows, 0 correct)",
    ),
    pytest.param(
        "Will the narrative extend to concrete policy actions or military "
        "posturing by Iran or Russia in response to the alleged Ukrainian "
        "attack?",
        "the alleged Ukrainian attack",
        id="alleged-Ukrainian-attack (6 rows, 0 correct)",
    ),
    pytest.param(
        "Could the cited coalition strain and Bilawal's warning translate "
        "into a formal no-confidence motion against the prime minister?",
        "the cited coalition",
        id="the-cited-X (1 row, 0 correct)",
    ),
    pytest.param(
        "Is the coordinated messaging part of a broader government-led "
        "information campaign on online safety beyond the Telegram lawsuit?",
        "the coordinated messaging",
        id="the-coordinated-messaging (1 row, 0 correct)",
    ),
    pytest.param(
        "Has an official U.S. license for Patriot missile production been "
        "formally issued, or is the narrative pre-emptive political "
        "signaling?",
        "the narrative",
        id="the-narrative (1 row, 0 correct)",
    ),
]


@pytest.mark.parametrize("thesis, span", _DEICTIC)
def test_a_dangling_referent_is_detected(thesis, span):
    spans = qt.deictic_spans(thesis)
    assert spans, thesis
    assert span in spans
    assert qt.is_deictic(thesis)


# ---------------------------------------------------------------------------
# DOES NOT FIRE — what a looser detector would have cost
# ---------------------------------------------------------------------------

_SELF_CONTAINED = [
    pytest.param(
        "What is the quantitative impact of the recent attacks on actual "
        "vessel transit volumes and cargo throughput in the Black Sea?",
        id="temporal-not-anaphoric: 'the recent attacks' (1 row, 1 correct)",
    ),
    pytest.param(
        "Do Iranian statements of Hormuz closure reflect an actual "
        "restriction on vessel movements, given recent transits reported?",
        id="'recent transits' (5 rows, 3 correct)",
    ),
    pytest.param(
        "Could the Red Sea explosion reported by UKMTO signal a broader "
        "maritime threat to UK shipping interests in the region?",
        id="named antecedent: 'the Red Sea explosion' (1 row, 1 correct)",
    ),
    pytest.param(
        "Will the announced repatriation of 5,000 Rohingya refugees be "
        "operationalized, and what mechanisms will ensure voluntary return?",
        id="'announced' is not anaphoric (1 row, 1 correct)",
    ),
    pytest.param(
        "Will continued elite reshuffles undermine cohesion within Ukraine's "
        "military leadership?",
        id="no determiner at all (3 rows, 2 correct)",
    ),
    pytest.param(
        'Contested fact: which value of "operates in" for "israel defense '
        'forces" is correct? 2 competing value clusters; current surfaced '
        'winner: "the west bank".',
        id="a contested-fact thesis names both endpoints",
    ),
    pytest.param(
        "Could the SEC investigation into Trump Media evolve into a criminal "
        "case that triggers impeachment or other removal proceedings?",
        id="named antecedent: 'the SEC investigation'",
    ),
    pytest.param(
        "Is there emerging elite dissent within the ruling party that could "
        "accelerate a leadership challenge?",
        id="'the ruling party' is a participle, not a court ruling",
    ),
    pytest.param(
        "Does the repeated use of the \"cockroach\" label across "
        "international and domestic media indicate any behind-the-scenes "
        "narrative framing effort?",
        id="hyphenated 'behind-the-scenes' must not split into a phrase",
    ),
]


@pytest.mark.parametrize("thesis", _SELF_CONTAINED)
def test_a_self_contained_thesis_is_left_alone(thesis):
    assert qt.deictic_spans(thesis) == []


@pytest.mark.parametrize("thesis", ["", "   ", None])
def test_an_empty_thesis_is_not_deictic(thesis):
    assert qt.deictic_spans(thesis) == []


def test_an_indefinite_phrase_is_a_generic_not_a_back_reference():
    assert qt.deictic_spans("Will an incident of this kind recur?") == []


def test_a_demonym_is_not_an_antecedent():
    """"the alleged Ukrainian attack" still does not say WHICH attack.
    Treating any capitalised word as a name is how a narrow detector becomes
    no detector at all."""
    assert qt.deictic_spans("in response to the Ukrainian attack")
    assert qt.deictic_spans("in response to the Kerch Bridge attack") == []


def test_a_naming_complement_after_the_noun_counts_too():
    """English puts the antecedent on either side of the head noun. A
    left-only scan would flag "the strike on the Jordan base", which says
    exactly what it is about."""
    assert qt.deictic_spans(
        "Will the strike on the Jordan base draw a direct Iranian reply?"
    ) == []
    assert qt.deictic_spans("Is the framing of the incident orchestrated?")


def test_an_anaphoric_modifier_beats_a_name_and_a_date():
    """"the cited" says outright that the referent is elsewhere; a
    capitalised neighbour does not rescue it."""
    assert qt.deictic_spans("Could the cited Karachi strain widen?")


# ---------------------------------------------------------------------------
# The inliner
# ---------------------------------------------------------------------------


def test_a_dangling_thesis_is_made_self_contained():
    out = qt.inline_referents(
        "Is the framing of the incident state-orchestrated?",
        "Narrative of Ukrainian attack on Iranian vessel, Caspian Sea",
    )
    assert out == (
        "Is the framing of the incident state-orchestrated?"
        " (in reference to: Narrative of Ukrainian attack on Iranian vessel, "
        "Caspian Sea)"
    )
    # The question is APPENDED to, never rewritten — a human reading the row
    # still sees what the analyst asked.
    assert out.startswith("Is the framing of the incident state-orchestrated?")


def test_a_self_contained_thesis_is_returned_untouched():
    thesis = "Will Iran proceed with closing the Strait of Hormuz?"
    assert qt.inline_referents(thesis, "Some finding title") == thesis


def test_inlining_is_idempotent():
    """Harvest paths re-run over the same source; a thesis must never
    accumulate the same clause twice."""
    once = qt.inline_referents("Is the incident orchestrated?", "Caspian vessel")
    assert qt.inline_referents(once, "Caspian vessel") == once
    assert qt.inline_referents(once, "A DIFFERENT title") == once


def test_no_context_leaves_the_thesis_deictic_and_therefore_visible():
    """An unresolvable referent must not be papered over: the thesis stays as
    written, so the matcher's guard still refuses to match it blind."""
    thesis = "Is the incident orchestrated?"
    assert qt.inline_referents(thesis, "") == thesis
    assert qt.is_deictic(qt.inline_referents(thesis, None))


def test_a_context_already_in_the_thesis_is_not_repeated():
    thesis = "Is the framing of the incident in Caspian Sea orchestrated?"
    assert qt.inline_referents(thesis, "Caspian Sea") == thesis


def test_the_inlined_context_is_bounded():
    out = qt.inline_referents("Is the incident orchestrated?", "T" * 5000)
    assert len(out) < 400
    assert qt.MAX_CONTEXT_CHARS < 400


# ---------------------------------------------------------------------------
# CW-8 — an OFFICE with nothing to bind it to
# ---------------------------------------------------------------------------

_IRAN_PM = (
    "How entrenched is the military's loyalty to the Supreme Leader versus "
    "the Prime Minister, and could rising PM-military tension lead to a "
    "power shift?"
)


def test_the_iran_premiership_row_is_caught():
    """The R3 row that opened this: Iran abolished the premiership in 1989,
    and the reason nobody caught it is the reason the matcher could not —
    the thesis names two offices and no country."""
    offices = qt.ungrounded_office(_IRAN_PM)
    assert "prime minister" in offices and "supreme leader" in offices
    assert qt.named_referents(_IRAN_PM) == []


def test_a_hyphenated_compound_cannot_smuggle_an_office_in_as_a_name():
    """"PM-military" reads as a capitalised run and would have grounded the
    Iran row on its own initials."""
    assert qt.named_referents("rising PM-military tension") == []
    assert qt.named_referents("the Iran-Iraq theater") == ["Iran-Iraq"]
    assert qt.named_referents("the Trump-brokered plan") == ["Trump-brokered"]


@pytest.mark.parametrize(
    "thesis",
    [
        "Will Iran proceed with closing the Strait of Hormuz?",
        "Could the SEC investigation into Trump Media evolve into a criminal "
        "case that triggers impeachment or other removal proceedings?",
        "Will the continuing decline in President Lee's approval rating "
        "translate into parliamentary pressure or an impeachment move?",
        "Could the cited coalition strain and Bilawal's warning translate "
        "into a formal no-confidence motion against the prime minister?",
    ],
)
def test_an_office_with_a_referent_is_a_real_question(thesis):
    """Naming a country, an institution or a person binds the office. Only
    the unbound shape is refused."""
    assert qt.ungrounded_office(thesis) == []


@pytest.mark.parametrize(
    "thesis",
    [
        "Is there emerging evidence of systematic rerouting of oil and gas "
        "shipments away from Hormuz to alternative corridors?",
        'Contested fact: which value of "operates in" for "idf" is correct?',
        "",
    ],
)
def test_a_thesis_naming_no_office_is_never_flagged(thesis):
    assert qt.ungrounded_office(thesis) == []


def test_sentence_initial_capitals_are_not_referents():
    """"Will Iran ..." must read one referent, not two."""
    assert qt.named_referents("Will Iran act?") == ["Iran"]
    assert qt.named_referents("Could the military intervene?") == []
