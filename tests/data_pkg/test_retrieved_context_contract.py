# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-N2 — dated retrieval for the GATHERING inline_target analysts.

THE GAP THIS CLOSES, precisely. D1 (``_tradecraft._DATED_CLAIM_RULE``) requires
every load-bearing claim to carry "the date of the reporting that supports it,
taken from that source's OWN printed date". For the cadence slice that works —
``_render_signal`` prints ``ingested=`` / ``published=`` on every block. For a
GATHERED document it did not: ``_gather`` rendered ``[N] <title> — <snippet>``
with **no date at all**, while numbering those blocks into the SAME flat ``[N]``
space as the slice.

So the four GATHERING analysts — the only ones with a GATHER loop, searching a
~106k-document corpus that spans years — were told to date every claim from the
printed dates, and shown evidence with no printed dates. The only date visible
on a retrieved block was whatever its prose happened to mention, which is a date
INSIDE the story, not the date OF the reporting.

Two halves, and they must ship together (the same argument
``scripts/voice_prompt_puts.py`` makes for the units):

  * CODE — the retrieved block now renders its own collection and publication
    dates and is marked RETRIEVED. Pinned in the first section below.
  * PROMPT — the shared ``RETRIEVED_CONTEXT_RULE``, plus per-analyst wording in
    the three descriptors that carry a prompt. Pinned in the rest.

NOTE ON PROVENANCE. This work was commissioned against a report that finding
``16f6a460`` had conflated a ``search_corpus`` date onto a today-event. The live
DB refutes that: that run made ZERO tool calls, and its "23 Nov 2023" was
correctly sourced from cited signal [65] (Dawn, collected the same day, reporting
a funeral for people killed on that 2023 date). The finding's real defect was the
serialization bug V-N1 fixes. The gap pinned HERE is a separate, structural one
found by reading the render: it is real, it is unfollowable-by-construction, and
it is not evidenced by that finding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.analysts._tradecraft import (
    RETRIEVED_CONTEXT_RULE,
    with_preamble_if_absent,
)
from legba.data.analysts.inline_target import _render_gathered_block, _render_signal
from legba.data.analysts.unit_grounding import (
    UNIT_GROUNDING_CLAUSE,
    with_grounding_clause,
)

DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"

#: The GATHERING ``inline_target`` analysts that carry a descriptor prompt.
#:
#: NOT "the non-units" — that is what this tuple was called until DS-1
#: (2026-08-06) showed the two axes are different. GATHERING is about where the
#: evidence comes from and is what THIS file is about; UNIT-hood is about what
#: shape the answer takes (``test_voice_contract``). ``disruption_status`` is
#: on both lists: it gathers, so every rule below applies to it, AND it is the
#: ninth bounded unit, so the HOUSE READ CONTRACT applies too. Conflating them
#: is precisely how it spent the voice wave with half a contract.
#:
#: ``country_assessor`` gathers but is deliberately absent: it is ``state:
#: draft`` (retired live) and takes its prompt from ``method.prompt_module``,
#: so it has no ``method.system_prompt`` to stamp. It still inherits the
#: code-appended rule.
GATHERERS: tuple[str, ...] = (
    "cross_doc_corroborator",
    "corpus_researcher",
    "disruption_status",
)


def _norm(text: str) -> str:
    """Whitespace-normalized — the constant is wrapped one way in Python and
    another in a YAML block scalar; the WORDS are the contract."""
    return " ".join(text.split())


def _descriptor(name: str) -> dict[str, Any]:
    return yaml.safe_load((DESCRIPTORS / f"analyst_{name}.yaml").read_text())


def _system_prompt(name: str) -> str:
    return _descriptor(name)["method"]["system_prompt"]


def _assembled(name: str) -> str:
    """The prompt the analyst actually runs — the same composition
    ``inline_target._effective_system_prompt`` performs at run time."""
    return with_grounding_clause(with_preamble_if_absent(_system_prompt(name)) or "")


# ---------------------------------------------------------------------------
# The CODE half — a retrieved block must be dated and marked
# ---------------------------------------------------------------------------

_ENTRY = {
    "title": "Palestinians hold mass funeral in Gaza",
    "snippet": "Mourners gathered for 112 people retrieved from rubble.",
    "source": "https://example.test/story",
}
_FIELDS = {
    "fetched_at": "2026-08-04T16:49:08+00:00",
    "published_at": "2026-08-04T15:52:06+00:00",
}


def test_a_gathered_block_prints_its_own_dates() -> None:
    """The defect in one assertion: this block used to carry no date at all."""
    block = _render_gathered_block(65, _ENTRY, _FIELDS)
    assert "2026-08-04T16:49:08+00:00" in block
    assert "published=2026-08-04T15:52:06+00:00" in block
    assert block.startswith("[65] Palestinians hold mass funeral in Gaza")
    assert "Mourners gathered" in block


def test_a_gathered_block_says_it_was_retrieved() -> None:
    """A slice signal and an archive document share one [N] space. The block
    must say which it is — the model cannot weigh recency it cannot see."""
    assert "RETRIEVED" in _render_gathered_block(65, _ENTRY, _FIELDS)
    assert "RETRIEVED" not in _render_signal(
        1, {"title": "t", "produced_at": "2026-08-04T00:00:00+00:00", "data": {}},
    )


@pytest.mark.parametrize(
    "fields",
    [None, {}, {"fetched_at": None, "published_at": None}, {"published_at": ""}],
)
def test_a_missing_date_renders_as_an_absent_field_never_a_fabricated_one(
    fields: Any,
) -> None:
    """Honest absence, the house posture everywhere else in this render path.

    A corpus doc with no ``published_at`` must not acquire one, and must not
    render a placeholder a model could read as a real date."""
    block = _render_gathered_block(7, _ENTRY, fields)
    # No date field at all, rather than an empty or null-looking one.
    assert "published=" not in block
    assert "collected" not in block
    assert "None" not in block
    assert "unknown" not in block.lower()
    # It still renders, is still numbered, and is still marked RETRIEVED — a
    # dateless document is citable, just undated.
    assert block.startswith("[7] ")
    assert "RETRIEVED" in block
    assert "Mourners gathered" in block


def test_the_gathered_section_header_warns_that_retrieved_docs_may_be_old() -> None:
    """The header is where the [N]-space collision is explained."""
    from legba.data.analysts import inline_target as it

    src = Path(it.__file__).read_text()
    assert "THESE ARE RETRIEVED DOCUMENTS, NOT THIS RUN'S SLICE" in src
    assert "may be of ANY age" in src


# ---------------------------------------------------------------------------
# The PROMPT half — the shared rule reaches every inline_target analyst
# ---------------------------------------------------------------------------


def test_the_shared_clause_carries_the_retrieved_context_rule() -> None:
    """``with_grounding_clause`` is the ONE unconditional append every
    inline_target analyst passes through — units and non-units alike."""
    assert _norm(RETRIEVED_CONTEXT_RULE) in _norm(UNIT_GROUNDING_CLAUSE)


@pytest.mark.parametrize("name", GATHERERS)
def test_the_assembled_gatherer_prompt_states_the_retrieved_rule(name: str) -> None:
    """Asserted on the ASSEMBLED prompt, so it is a property of what the
    analyst really runs rather than of a constant nobody reaches."""
    assert _norm(RETRIEVED_CONTEXT_RULE) in _norm(_assembled(name))


@pytest.mark.parametrize("name", GATHERERS)
def test_the_retrieved_rule_separates_reporting_date_from_event_date(
    name: str,
) -> None:
    """The distinction the render alone cannot make: a story published today
    about a strike in 2023 carries both dates, and they are not the same date."""
    assembled = _norm(_assembled(name)).lower()
    assert "when the reporting ran" in assembled
    assert "may be far older" in assembled


@pytest.mark.parametrize("name", GATHERERS)
def test_the_retrieved_rule_requires_explicit_attribution_of_old_material(
    name: str,
) -> None:
    """Archive material may be used — it may not be passed off as current."""
    assembled = _norm(_assembled(name)).lower()
    assert "attribute" in assembled
    assert "report describes" in assembled or "report found" in assembled


@pytest.mark.parametrize("name", GATHERERS)
def test_each_gatherer_descriptor_states_the_rule_in_its_own_terms(
    name: str,
) -> None:
    """The shared clause is generic by design; each descriptor names the trap
    ITS method walks into. A descriptor that only inherits the generic rule has
    not been thought about, so the per-analyst wording is pinned too."""
    prompt = _norm(_system_prompt(name)).lower()
    assert "retrieved" in prompt
    assert "date" in prompt
    # Each names its own failure mode.
    expected = {
        # Two documents years apart are not two confirmations of a current fact.
        "cross_doc_corroborator": "age changes what corroboration means",
        # A deep read reaches back in time; it may not present WAS as IS.
        "corpus_researcher": "date what you retrieved",
        # A transit count from an old document is not the lane's current state.
        "disruption_status": "a figure is only as current as its source",
    }[name]
    assert expected in prompt


def test_the_corroborator_is_told_not_to_narrate_its_plan() -> None:
    """The prompt half of V-N1. The write path now recovers a contract behind a
    plan sentence; this asks the model not to emit one in the first place."""
    prompt = _norm(_system_prompt("cross_doc_corroborator"))
    assert "NO PLANNING PROSE IN YOUR OUTPUT" in prompt
    assert "We will use search_corpus." in prompt


def test_country_assessor_has_no_descriptor_prompt_to_stamp() -> None:
    """Pins WHY the fourth gatherer is absent from ``GATHERERS`` — so a future
    reader does not "fix" the omission by adding a prompt block to a retired
    descriptor that reads its prompt from code."""
    desc = _descriptor("country_assessor")
    assert desc["identity"]["state"] == "draft"
    assert "system_prompt" not in desc["method"]
    assert desc["method"]["prompt_module"]


def test_the_put_script_ships_every_descriptor_this_release_changes() -> None:
    """Tonight is ONE ``--apply``. A descriptor edited here but missing from the
    script is a prompt that silently never reaches production — the exact
    both-halves-together failure the script's own docstring warns about.

    Asserted against the UNION of the script's two tuples, not against
    ``NON_UNITS`` alone: which half a gatherer is stamped in is a question about
    its ANSWER shape (DS-1 moved ``disruption_status`` across that line), and
    the property this test exists for — "the script ships it at all" — is
    indifferent to that.
    """
    import ast

    src = (Path(__file__).resolve().parents[2] / "scripts" / "voice_prompt_puts.py")
    tree = ast.parse(src.read_text())
    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Tuple):
                found[node.target.id] = [
                    e.value for e in node.value.elts if isinstance(e, ast.Constant)
                ]
    assert set(GATHERERS) <= set(found["UNITS"]) | set(found["NON_UNITS"])
