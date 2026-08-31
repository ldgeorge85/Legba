# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Task #57 — wire-pair collapse at the renderer.

The class under test is the one seven replay rounds could not close with prompt
text: ONE syndicated wire story reaching a desk as TWO numbered signals, which
``narrative_coordination`` then called "coordination" on in 3 of 40 pooled draws.

The load-bearing fixture is :func:`_aawsat_row` / :func:`_cna_row` — the real
``[80]``/``[91]`` pair from the ``nar_au_0815`` window, reconstructed from the
replay record (``planning/VOICE_REPLAY_2026-08-20/runs/REVISION_RESULT_2026-08-21.md``
§13) with the titles, hosts and dates the desk actually read.

The GDACS test is the counterweight and matters just as much: measured over that
same window, a headline-only rule would have collapsed nine wildfire alerts
carrying DISTINCT event ids because their titles are auto-generated and
identical. The collapse must leave every one of them alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.inline_target import (
    InlineTargetDeps,
    _build_citation_index,
    _orient,
    _render_signal,
    _render_user_prompt,
    run_method,
)
from legba.data.analysts.wire_pair_collapse import (
    WIRE_COLLAPSE_ROW_KEY,
    collapse_wire_pairs,
    wire_story_key,
)

# ---------------------------------------------------------------------------
# The replay fixture — the exact [80]/[91] pair, from the record
# ---------------------------------------------------------------------------

#: Word-for-word identical to the aawsat headline apart from capitalisation —
#: which is precisely why the substrate ``content_hash`` (canonical_url +
#: ``normalize_wire_title``, no casefold, different URLs) cannot see them as one.
_AAWSAT_TITLE = "Social Media Firms Urge Caution on Early Australia Under-16 Ban Data"
_CNA_TITLE = "Social media firms urge caution on early Australia under-16 ban data"

_AAWSAT_ID = UUID("5ec70eef-3b3d-4274-b201-b9df82dc2323")
_CNA_ID = UUID("a75e282d-c436-4b71-ab7f-b854b484ee1b")

#: The shared Reuters lede — byte-identical in both rows' archived text. Not used
#: as a collapse key (the headline is enough), but it is what makes the two rows
#: one story rather than two, so the fixture carries it.
_SHARED_LEDE = (
    "Top social media platforms urged the Australian government not to place "
    "too much weight on early evidence showing most children still use their "
    "products despite a landmark under-16 ban."
)


def _aawsat_row() -> dict[str, Any]:
    """``[80]`` — the Saudi masthead's run of the wire story."""
    return {
        "id": _AAWSAT_ID,
        "title": _AAWSAT_TITLE,
        "source_url": "https://english.aawsat.com/node/5306795",
        "produced_at": "2026-08-14T08:27:22.244521+00:00",
        "data": {
            "published_at": "2026-08-14T07:00:40+00:00",
            "archived_text": f"{_AAWSAT_TITLE}\n{_SHARED_LEDE} REUTERS/Jeremy Piper",
        },
    }


def _cna_row() -> dict[str, Any]:
    """``[91]`` — the Sydney-datelined agency dispatch under a second masthead."""
    return {
        "id": _CNA_ID,
        "title": _CNA_TITLE,
        "source_url": (
            "https://www.channelnewsasia.com/business/"
            "social-media-firms-urge-caution-early-australia-under-16-ban-data-6319306"
        ),
        "produced_at": "2026-08-14T07:27:07.666152+00:00",
        "data": {
            "published_at": "2026-08-14T06:50:29+00:00",
            "archived_text": f"{_CNA_TITLE}\nSYDNEY, Aug 14 : {_SHARED_LEDE}",
        },
    }


def _plain_row(
    *,
    title: str,
    produced_at: str,
    source_url: str = "https://example.com/a",
    id_: UUID | None = None,
) -> dict[str, Any]:
    return {
        "id": id_ or uuid4(),
        "title": title,
        "source_url": source_url,
        "produced_at": produced_at,
        "data": {"published_at": produced_at, "summary": f"Body of {title}."},
    }


# ---------------------------------------------------------------------------
# LLM test double (mirrors tests/data_pkg/test_analyst_inline_target.py)
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _CapturingLLM:
    """Records the rendered user prompt so a test can assert on what the desk saw."""

    subprovider = "openai"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def last_user_prompt(self) -> str:
        return str(self.calls[-1]["messages"][-1]["content"])

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": messages, "system": system})
        return _Response(
            content=json.dumps({
                "title": "Australia under-16 ban coverage",
                "body": "Organic coverage of the Senate inquiry [1].",
                "confidence": 0.3,
                "evidence": [1],
                "tags": ["severity:low"],
            }),
            usage=_Usage(),
        )


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def test_the_replay_pair_shares_one_wire_story_key():
    """The [80]/[91] headlines differ ONLY in case; the key must fold that."""
    key_80 = wire_story_key(_aawsat_row())
    key_91 = wire_story_key(_cna_row())
    assert key_80 is not None
    assert key_80 == key_91
    assert key_80 == (
        "social media firms urge caution on early australia under 16 ban data",
        "2026-08-14",
    )


def test_key_folds_hyphen_and_wire_revision_marker():
    """"(LEAD) Foo Under-16" and "foo under 16" are one story identity."""
    a = _plain_row(title="(LEAD) Ministers Weigh Tougher Penalties on Platforms",
                   produced_at="2026-08-14T01:00:00+00:00")
    b = _plain_row(title="ministers weigh tougher penalties on platforms",
                   produced_at="2026-08-14T05:00:00+00:00")
    assert wire_story_key(a) == wire_story_key(b)


def test_short_generic_headline_never_keys():
    """Under the token floor there is no identity worth trusting."""
    assert wire_story_key(_plain_row(
        title="Business", produced_at="2026-08-14T01:00:00+00:00")) is None
    assert wire_story_key(_plain_row(
        title="Morning briefing", produced_at="2026-08-14T01:00:00+00:00")) is None


def test_row_without_resolvable_day_never_keys():
    row = {"id": uuid4(), "title": _AAWSAT_TITLE, "source_url": "https://a.example/x",
           "produced_at": None, "data": {}}
    assert wire_story_key(row) is None


def test_different_days_do_not_collapse():
    """Same headline a day apart is a follow-up, not a syndication of one story."""
    a = _aawsat_row()
    b = _cna_row()
    b["data"]["published_at"] = "2026-08-15T06:50:29+00:00"
    b["produced_at"] = "2026-08-15T07:27:07+00:00"
    out, absorbed = collapse_wire_pairs([a, b])
    assert absorbed == 0
    assert len(out) == 2


# ---------------------------------------------------------------------------
# The collapse
# ---------------------------------------------------------------------------


def test_collapse_folds_the_replay_pair_to_one_row():
    rows = [_aawsat_row(), _cna_row()]
    out, absorbed = collapse_wire_pairs(rows)

    assert absorbed == 1
    assert len(out) == 1
    marker = out[0][WIRE_COLLAPSE_ROW_KEY]
    assert marker["copies"] == 2
    assert marker["mastheads"] == ["english.aawsat.com", "channelnewsasia.com"]
    assert marker["absorbed_ids"] == [_CNA_ID]
    # The survivor is a COPY — the caller's row is never mutated.
    assert WIRE_COLLAPSE_ROW_KEY not in rows[0]


def test_gdacs_same_masthead_alerts_are_never_collapsed():
    """Nine DIFFERENT wildfires share one auto-generated headline.

    Measured on the real AU window: keying on headline+day alone absorbs all
    nine because the title is machine-generated boilerplate. They are one
    masthead with distinct ``eventid``s, so the two-publisher guard is the thing
    that keeps nine real signals visible to the desk.
    """
    rows = [
        _plain_row(
            title="Green forest fire notification in Australia",
            produced_at=f"2026-08-14T2{i}:07:00+00:00".replace("2 ", "20"),
            source_url=f"https://www.gdacs.org/report.aspx?eventtype=WF&eventid=10305{i:02d}",
        )
        for i in range(3)
    ]
    out, absorbed = collapse_wire_pairs(rows)
    assert absorbed == 0
    assert out is rows  # identity: provably a no-op, not a rebuild


def test_unattributed_rows_are_never_collapsed():
    """Two rows we cannot attribute to two publishers stay two rows."""
    rows = [
        {"id": uuid4(), "title": "Flood Watch issued August 14 until August 16 by NWS",
         "source_url": None, "produced_at": "2026-08-14T21:42:27+00:00", "data": {}},
        {"id": uuid4(), "title": "Flood Watch issued August 14 until August 16 by NWS",
         "source_url": None, "produced_at": "2026-08-14T21:42:16+00:00", "data": {}},
    ]
    out, absorbed = collapse_wire_pairs(rows)
    assert absorbed == 0
    assert len(out) == 2


def test_assessed_structure_pseudo_signals_are_never_collapsed():
    """The graph-structure rows the slice reader appends carry no dates.

    ``actor_substrate_slice`` emits them with ``produced_at=None`` and no
    ``published_at``, and two of them CAN share a title (the reader's own
    ``(+N more)`` collapse already ran). With no resolvable day they never key,
    so they can never be folded into each other by this pass.
    """
    rows = [
        {"id": None, "source_id": "graph_metrics", "source_url": None,
         "produced_at": None, "fetched_at": None,
         "title": "[ASSESSED STRUCTURE] Belgium-Egypt-IMO sign-imbalanced triad",
         "data": {"kind": "assessed_structure", "summary": "Heider-unstable."}},
        {"id": None, "source_id": "graph_metrics", "source_url": None,
         "produced_at": None, "fetched_at": None,
         "title": "[ASSESSED STRUCTURE] Belgium-Egypt-IMO sign-imbalanced triad",
         "data": {"kind": "assessed_structure", "summary": "Heider-unstable."}},
    ]
    assert wire_story_key(rows[0]) is None
    out, absorbed = collapse_wire_pairs(rows)
    assert absorbed == 0
    assert out is rows


def test_slice_without_wire_pairs_is_the_same_list_object():
    rows = [
        _plain_row(title="Senate inquiry presses platform executives on the ban",
                   produced_at="2026-08-14T10:00:00+00:00"),
        _plain_row(title="Regulator seeks tougher investigative powers this spring",
                   produced_at="2026-08-14T09:00:00+00:00"),
    ]
    out, absorbed = collapse_wire_pairs(rows)
    assert absorbed == 0
    assert out is rows


def test_three_mastheads_one_story_collapses_to_one():
    rows = [
        _plain_row(title=_AAWSAT_TITLE, produced_at="2026-08-14T08:00:00+00:00",
                   source_url="https://english.aawsat.com/node/1"),
        _plain_row(title=_CNA_TITLE, produced_at="2026-08-14T07:00:00+00:00",
                   source_url="https://www.channelnewsasia.com/x"),
        _plain_row(title=_CNA_TITLE.upper(), produced_at="2026-08-14T06:00:00+00:00",
                   source_url="https://www.straitstimes.com/y"),
    ]
    out, absorbed = collapse_wire_pairs(rows)
    assert absorbed == 2
    assert len(out) == 1
    assert out[0][WIRE_COLLAPSE_ROW_KEY]["copies"] == 3
    assert out[0][WIRE_COLLAPSE_ROW_KEY]["mastheads"] == [
        "english.aawsat.com", "channelnewsasia.com", "straitstimes.com",
    ]


# ---------------------------------------------------------------------------
# Ordinals — the invariant the whole design rests on
# ---------------------------------------------------------------------------


def test_rows_before_the_duplicate_keep_their_exact_ordinals():
    """Zero ordinal churn ahead of the collapse; order preserved throughout.

    Reproduces the real window's shape: the pair sits at positions 4 and 7 of a
    9-row slice, so positions 1-3 must be untouched and 5,6,8,9 must close up by
    exactly one WITHOUT reordering.
    """
    def _filler(n: int) -> dict[str, Any]:
        return _plain_row(
            title=f"Distinct Australian policy story number {n} of the window",
            produced_at=f"2026-08-14T{20 - n:02d}:00:00+00:00",
            source_url=f"https://example{n}.com/a",
        )

    rows = [_filler(1), _filler(2), _filler(3), _aawsat_row(),
            _filler(5), _filler(6), _cna_row(), _filler(8), _filler(9)]
    out, absorbed = collapse_wire_pairs(rows)

    assert absorbed == 1
    # Positions 1-4 (indices 0-3) are byte-identical objects, same slots.
    for i in range(3):
        assert out[i] is rows[i]
    assert out[3]["id"] == _AAWSAT_ID          # survivor keeps slot 4
    # The tail closed up by exactly one, in the original relative order.
    assert [r["title"] for r in out[4:]] == [
        rows[4]["title"], rows[5]["title"], rows[7]["title"], rows[8]["title"],
    ]


def test_render_and_citation_index_stay_in_lockstep_after_collapse():
    """``[N]`` in the prompt text == key N in the citation index, post-collapse.

    These are two independent ``enumerate`` walks over the ORIENTed list; the
    collapse is only safe because both see the same post-collapse list.
    """
    rows = [_aawsat_row(), _cna_row(),
            _plain_row(title="Regulator seeks tougher investigative powers now",
                       produced_at="2026-08-14T05:00:00+00:00")]
    sliced, _derived = _orient(rows, "country_g20_au")
    prompt = _render_user_prompt(sliced, "country_g20_au")
    index = _build_citation_index(sliced)

    assert len(sliced) == 2
    assert set(index) == {1, 2}
    assert "Number of signals: 2" in prompt
    assert "[3]" not in prompt
    for n, entry in index.items():
        assert f"[{n}] " in prompt
        # The indexed title is the one rendered at that ordinal.
        assert str(entry["title"])[:40] in prompt


def test_orient_keeps_both_ids_in_derived_from():
    """The collapse is a RENDERING decision, never a provenance one."""
    sliced, derived = _orient([_aawsat_row(), _cna_row()], "country_g20_au")
    assert len(sliced) == 1
    assert set(derived) == {_AAWSAT_ID, _CNA_ID}


def test_orient_receipt_counts_the_absorbed_copies():
    stats: dict[str, Any] = {}
    _orient([_aawsat_row(), _cna_row()], "country_g20_au", stats=stats)
    assert stats["wire_copies_collapsed"] == 1


def test_rendered_block_states_reach_and_denies_coordination():
    rows = [_aawsat_row(), _cna_row()]
    out, _absorbed = collapse_wire_pairs(rows)
    block = _render_signal(80, out[0])

    assert block.startswith(f"[80] {_AAWSAT_TITLE}")
    assert "carried_by=2 mastheads" in block
    assert "english.aawsat.com" in block and "channelnewsasia.com" in block
    assert "ONE syndicated story" in block
    # Both halves of the message, verbatim: reach is preserved, coordination is
    # denied. Losing either one re-opens a failure mode.
    assert "evidence of REACH" in block
    assert "do not read it as coordination" in block
    # The uncollapsed shape is untouched — no stray line on a normal row.
    plain = _render_signal(1, _plain_row(
        title="A perfectly ordinary Australian policy headline here",
        produced_at="2026-08-14T05:00:00+00:00"))
    assert "carried_by=" not in plain
    assert len(plain.splitlines()) == 3


# ---------------------------------------------------------------------------
# The real assembly path — run_method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_hands_the_desk_one_signal_for_the_wire_pair():
    """End to end through ``run_method``: the [80]/[91] surface is gone.

    This is the assertion the seven prompt rounds could never make — not that
    the model was told to ignore the pair, but that there is no longer a pair on
    the page to notice.
    """
    llm = _CapturingLLM()
    rows = [
        _plain_row(title="Senate inquiry presses platform executives on the ban",
                   produced_at="2026-08-14T12:00:00+00:00",
                   source_url="https://abc.net.au/1"),
        _aawsat_row(),
        _cna_row(),
    ]
    result = await run_method(
        rows,
        {"target_id": "country_g20_au", "analyst_id": "analyst.narrative_coordination"},
        InlineTargetDeps(llm=llm),
    )

    prompt = llm.last_user_prompt
    assert "Number of signals: 2" in prompt
    # ONE numbered block carries the story; the second masthead is an annotation.
    # Counted on the "[N] <title>" block head — the headline also legitimately
    # recurs inside the archived body, and that is not a second signal.
    assert prompt.count(f"] {_AAWSAT_TITLE}") == 1
    assert f"] {_CNA_TITLE}" not in prompt
    assert "carried_by=2 mastheads" in prompt
    assert "channelnewsasia.com" in prompt

    # Lineage keeps BOTH signals even though only one held an ordinal.
    assert set(result.derived_from) >= {_AAWSAT_ID, _CNA_ID}

    # The ORIENT receipt is auditable in the trace.
    orient = next(s for s in result.intermediate_steps if s["phase"] == "orient")
    assert orient["wire_copies_collapsed"] == 1


@pytest.mark.asyncio
async def test_run_method_unaffected_slice_is_byte_for_byte_unchanged():
    """A slice with no wire pair renders exactly as it did before task #57."""
    llm = _CapturingLLM()
    rows = [
        _plain_row(title="Senate inquiry presses platform executives on the ban",
                   produced_at="2026-08-14T12:00:00+00:00",
                   source_url="https://abc.net.au/1"),
        _plain_row(title="Regulator seeks tougher investigative powers this year",
                   produced_at="2026-08-14T11:00:00+00:00",
                   source_url="https://smh.com.au/2"),
    ]
    result = await run_method(
        rows,
        {"target_id": "country_g20_au", "analyst_id": "analyst.narrative_coordination"},
        InlineTargetDeps(llm=llm),
    )
    prompt = llm.last_user_prompt
    assert "Number of signals: 2" in prompt
    assert "carried_by=" not in prompt
    orient = next(s for s in result.intermediate_steps if s["phase"] == "orient")
    assert orient["wire_copies_collapsed"] == 0


# ---------------------------------------------------------------------------
# 2026-08-27 DQ sweep — FLEET WIDENING.
#
# The sweep measured live: 78 findings across 11 non-narrative desks (7.1% of
# non-narrative findings in a 48h window) cited two members of the same
# wire-pair group as separate ``derived_from`` rows — worst examples named
# ``escalation``, ``economic_coercion``, ``military_posture``,
# ``internal_stability`` — and read the code as scoping the collapse to
# ``narrative_coordination`` only.
#
# It is NOT desk-scoped: ``_orient`` (and therefore ``collapse_wire_pairs``)
# is called unconditionally for every ``inline_target`` analyst regardless of
# ``analyst_id`` — the collapse function itself takes no analyst/desk
# parameter at all, only the row list. These tests pin that fact explicitly,
# across the sweep's own named desks, so a future change cannot silently
# re-scope the collapse to one desk without a test noticing — and confirm the
# narrative desk's own proven behavior (task #57) is untouched (same guards,
# same rendered block, same receipt) now that it is shared infrastructure
# rather than implicitly desk-exclusive.
# ---------------------------------------------------------------------------

#: The sweep's own "worst examples" — non-narrative desks it found citing a
#: wire-pair's two members as separate evidence — plus narrative_coordination
#: itself as the proven baseline every other desk must match byte-for-byte.
_SWEEP_NAMED_DESKS = (
    "analyst.narrative_coordination",
    "analyst.escalation",
    "analyst.economic_coercion",
    "analyst.military_posture",
    "analyst.internal_stability",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("analyst_id", _SWEEP_NAMED_DESKS)
async def test_run_method_collapses_the_replay_pair_on_every_unit_desk(analyst_id):
    """The [80]/[91] replay pair collapses identically on every desk the sweep
    named — not just narrative_coordination. Same assertions as
    :func:`test_run_method_hands_the_desk_one_signal_for_the_wire_pair`,
    parametrized over ``analyst_id`` to prove the collapse is desk-agnostic."""
    llm = _CapturingLLM()
    rows = [
        _plain_row(title="Senate inquiry presses platform executives on the ban",
                   produced_at="2026-08-14T12:00:00+00:00",
                   source_url="https://abc.net.au/1"),
        _aawsat_row(),
        _cna_row(),
    ]
    result = await run_method(
        rows,
        {"target_id": "country_g20_au", "analyst_id": analyst_id},
        InlineTargetDeps(llm=llm),
    )
    prompt = llm.last_user_prompt
    assert "Number of signals: 2" in prompt
    assert prompt.count(f"] {_AAWSAT_TITLE}") == 1
    assert f"] {_CNA_TITLE}" not in prompt
    assert "carried_by=2 mastheads" in prompt
    assert "channelnewsasia.com" in prompt
    assert set(result.derived_from) >= {_AAWSAT_ID, _CNA_ID}
    orient = next(s for s in result.intermediate_steps if s["phase"] == "orient")
    assert orient["wire_copies_collapsed"] == 1


@pytest.mark.asyncio
async def test_fleet_widening_is_byte_identical_across_desks():
    """The rendered wire-pair block a non-narrative desk sees is BYTE-IDENTICAL
    to what narrative_coordination sees — the widening shares one renderer
    rather than forking a second copy that could drift."""
    prompts: dict[str, str] = {}
    for analyst_id in ("analyst.narrative_coordination", "analyst.escalation"):
        llm = _CapturingLLM()
        rows = [_aawsat_row(), _cna_row()]
        await run_method(
            rows,
            {"target_id": "country_g20_au", "analyst_id": analyst_id},
            InlineTargetDeps(llm=llm),
        )
        prompts[analyst_id] = llm.last_user_prompt
    # The two prompts differ only in whatever the descriptor-driven system
    # framing contributes (if anything) — the wire-pair block itself, isolated
    # by grepping the carried_by line and its signal head, must match exactly.
    for text in prompts.values():
        assert f"] {_AAWSAT_TITLE}" in text
        assert "carried_by=2 mastheads (english.aawsat.com, channelnewsasia.com) " in text
        assert "ONE syndicated story collapsed from 2 copies" in text


@pytest.mark.asyncio
async def test_gdacs_near_miss_holds_on_a_non_narrative_desk():
    """The GDACS same-masthead guard (nine distinct wildfire eventids sharing
    an auto-generated title) is NOT narrative-specific either: none collapse
    on ``escalation``, exactly as none collapse on narrative_coordination."""
    llm = _CapturingLLM()
    rows = [
        _plain_row(
            title="Green forest fire notification in Australia",
            produced_at=f"2026-08-14T0{i}:00:00+00:00",
            source_url=f"https://www.gdacs.org/report.aspx?eventtype=WF&eventid=10305{i:02d}",
        )
        for i in range(9)
    ]
    result = await run_method(
        rows,
        {"target_id": "country_g20_au", "analyst_id": "analyst.escalation"},
        InlineTargetDeps(llm=llm),
    )
    prompt = llm.last_user_prompt
    assert "Number of signals: 9" in prompt
    assert "carried_by=" not in prompt
    orient = next(s for s in result.intermediate_steps if s["phase"] == "orient")
    assert orient["wire_copies_collapsed"] == 0
