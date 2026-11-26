# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The record a prior consult turn compacts to.

The skeleton lane: ``consult_turn_record`` is a pure leaf — a persisted turn
pair in, a typed :class:`TurnRecord` out, and a deterministic render of the
block the model would see. What is proven here:

* a turn carrying two dated probabilities — one resolved, one open —
  renders the OPEN one verbatim and the resolved one as a line;
* a resolver whose date cannot be read stays open forever (the safe
  direction: never compact what cannot be proven closed);
* provenance tags survive: a line naming none of the turn's cited ids keeps
  the census's ``model knowledge`` tag on the record;
* citation ids are preserved and substrate bodies are absent;
* ``render`` is deterministic — same record, same ``now``, same bytes;
* the structured fields win over prose (the data block is authoritative).
"""

from __future__ import annotations

from datetime import date
from uuid import NAMESPACE_URL, uuid5

from legba.data.analysts import consult_turn_record as tr
from legba.data.analysts.consult_turn_record import (
    DatedProbability,
    from_turn,
    is_open_resolver,
    render,
)

#: The wall clock these tests render against — fixed so the resolved/open
#: split is a fact of the test, not of the day it ran.
NOW = date(2026, 11, 4)


def _uuid(*parts: object) -> str:
    return str(uuid5(NAMESPACE_URL, "c3/" + "/".join(str(p) for p in parts)))


_CITED = _uuid("cited-body")
#: The claims as the extractor keeps them — the sentence INCLUDING its
#: terminator, which is what "verbatim" means on the record.
_OPEN_CLAIM = (
    f"0.7 that the corridor reopens — resolved by the 2026-12-01 summit "
    f"[{_CITED}]."
)
#: Deliberately long: a resolved claim renders as a LINE, so this one must be
#: longer than the squash keeps for the "full sentence absent" assertion to
#: mean anything.
_RESOLVED_CLAIM = (
    "0.6 that the September ceasefire would hold, because both sides had "
    "already spent their ammunition stocks and the border towns were "
    "emptied — resolved by the 2026-09-30 observer report."
)


def _answer() -> str:
    return (
        "## Bottom line\n\n"
        "The corridor stays closed through winter; the ceasefire was a "
        "pause, not a settlement.\n\n"
        "---\n\n"
        "## 2. The evidence\n\n"
        f"{_OPEN_CLAIM}\n\n"
        f"{_RESOLVED_CLAIM}\n\n"
        "A long supporting paragraph the compaction is built to drop: "
        + "x" * 400
        + ".\n\n"
        "## 3. What would change my mind\n\n"
        "- a second independent outlet names the successor by 2026-11-30\n"
        "- Baltic sabotage events exceed three in a fortnight by 2026-12-15\n"
    )


def _payload() -> dict:
    return {
        "turn_id": "turn-0001",
        "question": "Will the France corridor reopen this year?",
        "answer": _answer(),
        "cited_refs": [
            {"id": _CITED, "kind": "signal", "description": "corridor signal row"},
            {"id": _uuid("other"), "kind": "finding", "description": None},
        ],
        "watch_candidates": [
            "a second independent outlet names the successor by 2026-11-30"
        ],
        "unanswered_aspects": ["whether the northern spur is included"],
        "challenges": [
            {"text": "but the September call said it would hold", "accepted": True},
            {"text": "are you sure about the spur?", "accepted": None},
        ],
    }


# ---------------------------------------------------------------------------
# from_turn — the projection
# ---------------------------------------------------------------------------


def test_from_turn_lifts_the_fixed_fields():
    record = from_turn(_payload())
    assert record.turn_id == "turn-0001"
    assert record.question == "Will the France corridor reopen this year?"
    assert "corridor stays closed" in record.bottom_line
    # Citation ids preserved; bodies were never on the record.
    assert [r.ref_id for r in record.refs] == [_CITED, _uuid("other")]
    assert record.refs[0].kind == "signal"
    assert record.refs[0].line == "corridor signal row"
    assert record.refs[1].line == ""
    # The structured fields carry through, order kept.
    assert record.watches == (
        "a second independent outlet names the successor by 2026-11-30",
    )
    assert record.unanswered_aspects == ("whether the northern spur is included",)
    assert [c.accepted for c in record.challenges] == [True, None]


def test_from_turn_parses_both_probabilities_with_their_resolvers():
    record = from_turn(_payload())
    assert len(record.probabilities) == 2
    open_prob, resolved_prob = record.probabilities
    assert open_prob.claim == _OPEN_CLAIM
    assert open_prob.resolve_by == "2026-12-01"
    assert "the 2026-12-01 summit" in open_prob.resolver
    assert resolved_prob.claim == _RESOLVED_CLAIM
    assert resolved_prob.resolve_by == "2026-09-30"


def test_from_turn_reads_the_answer_key_and_a_bare_cited_ref_list():
    record = from_turn(
        {
            "content": "The short answer. 0.5 that it holds — resolved by "
            "2026-10-01.",
            "cited_refs": [_uuid("bare")],
        }
    )
    assert record.bottom_line.startswith("The short answer")
    assert [r.ref_id for r in record.refs] == [_uuid("bare")]
    assert record.probabilities[0].resolve_by == "2026-10-01"


# ---------------------------------------------------------------------------
# is_open_resolver — the one rule
# ---------------------------------------------------------------------------


def test_is_open_resolver():
    assert is_open_resolver(DatedProbability(claim="c", resolve_by="2026-12-01"), NOW)
    # The day itself has not passed — still under test, still verbatim.
    assert is_open_resolver(DatedProbability(claim="c", resolve_by="2026-11-04"), NOW)
    assert not is_open_resolver(
        DatedProbability(claim="c", resolve_by="2026-09-30"), NOW
    )
    # An unreadable horizon is open forever: never compact what cannot be
    # proven closed. Same for a mapping-shaped prob and an unparseable now.
    assert is_open_resolver(DatedProbability(claim="c", resolve_by=""), NOW)
    assert is_open_resolver({"resolve_by": "not a date"}, NOW)
    assert is_open_resolver({"resolve_by": "2026-09-30"}, "garbage")


# ---------------------------------------------------------------------------
# render — the block the model sees
# ---------------------------------------------------------------------------


def test_the_open_probability_renders_verbatim_and_the_resolved_one_as_a_line():
    block = render(from_turn(_payload()), now=NOW)
    assert _OPEN_CLAIM in block
    assert "[open]" in block
    # The resolved claim's full sentence is NOT carried — it renders as a
    # compact line naming its resolver date.
    assert _RESOLVED_CLAIM not in block
    resolved_lines = [
        line for line in block.splitlines() if "[resolved 2026-09-30]" in line
    ]
    assert len(resolved_lines) == 1
    assert len(resolved_lines[0]) < len(_RESOLVED_CLAIM)


def test_provenance_tags_survive():
    block = render(from_turn(_payload()), now=NOW)
    # The resolved claim names no cited ref: tagged model knowledge, as the
    # census would tag it live. The open claim names _CITED: untagged.
    resolved_line = next(
        line for line in block.splitlines() if "[resolved 2026-09-30]" in line
    )
    assert "model knowledge" in resolved_line
    open_line = next(
        line for line in block.splitlines() if _OPEN_CLAIM in line
    )
    assert "model knowledge" not in open_line


def test_citation_ids_are_preserved_and_bodies_are_absent():
    block = render(from_turn(_payload()), now=NOW)
    assert _CITED in block
    assert _uuid("other") in block
    # The 400-char supporting paragraph is gone; nothing but the section
    # extracts and the ids ride the record.
    assert "x" * 400 not in block


def test_change_my_mind_watches_unanswered_and_challenges_render():
    block = render(from_turn(_payload()), now=NOW)
    assert "Baltic sabotage events exceed three in a fortnight" in block
    assert "whether the northern spur is included" in block
    assert "but the September call said it would hold [accepted]" in block
    assert "are you sure about the spur? [undetermined]" in block


def test_render_is_deterministic():
    record = from_turn(_payload())
    assert render(record, now=NOW) == render(record, now=NOW)
    # Same record, different day: only the resolved/open verdict may move.
    # Same day, same bytes — the render is a function of the turn + now.
    assert render(record, now="2026-11-04") == render(record, now=NOW)


def test_render_never_compacts_a_date_it_cannot_read():
    record = from_turn(
        {
            "question": "q",
            "answer": (
                "0.5 that the vote happens — resolved by the October ballot. "
                "No ISO date anywhere."
            ),
        }
    )
    block = render(record, now=NOW)
    assert "resolved by the October ballot" in block
    assert "[open]" in block
