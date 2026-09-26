# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HISTORICAL SERIES grounding block and the `observation` citation (7g-2).

Three things are load-bearing here and each has its own test:

  * the **`stale_tense` marker** renders EXACTLY as
    ``(historical: valid YYYY..YYYY, recorded YYYY-MM)`` — on the prompt line,
    inside the captured evidence the judge grades, and on the citation. One
    string, one producer, three surfaces;
  * the block takes **one ordinal per series line**, and the section renderer
    reserves that whole run so nothing downstream collides with it;
  * the grant is **OFF by default**, and with it off the rendered prompt is
    byte-identical to the six-block render — asserted on the BYTES, not on a
    count.
"""

from __future__ import annotations

import pytest

from legba.data.analysts import history_grounding as hg
from legba.data.analysts import unit_grounding as ug
from legba.data.provenance.kinds import GROUNDING_REF_KINDS, is_grounding_citation


def _obs(**over):
    row = {
        "observation_id": "11111111-1111-4111-8111-111111111111",
        "ref": "observation:11111111-1111-4111-8111-111111111111",
        "collection_id": "collection.series_pilot_2016_2026",
        "series_id": "wb.gdp_growth_annual_pct",
        "indicator_name": "GDP growth (annual %)",
        "provider": "world_bank",
        "subject": "US",
        "subject_kind": "country",
        "subject_name": "United States",
        "value": 2.8,
        "value_display": "2.8",
        "value_text": None,
        "unit": "pct_per_year",
        "valid_from": "2025-01-01",
        "valid_to": "2025-12-31",
        "record_time": "2026-07-13T00:00:00+00:00",
        "source_url": "https://api.worldbank.org/v2/country/US/indicator/X",
        "sha256": "abc123",
        "origin_class": "archive",
        "licence_class": "public",
    }
    row.update(over)
    return row


def _row(entries):
    return {
        ug.UNIT_GROUNDING_ROW_KEY: hg.GROUNDING_HISTORY,
        ug.GROUNDING_PAYLOAD_KEY: list(entries),
    }


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------


def test_the_stale_tense_marker_renders_exactly_as_specified():
    assert hg.stale_tense_marker(_obs()) == (
        "(historical: valid 2025..2025, recorded 2026-07)"
    )


def test_a_multi_year_period_prints_both_ends():
    assert hg.stale_tense_marker(
        _obs(valid_from="2016-01-01", valid_to="2018-12-31")
    ) == "(historical: valid 2016..2018, recorded 2026-07)"


def test_a_missing_time_prints_UNKNOWN_never_a_guessed_year():
    """A marker that quietly invents a period is worse than no marker: the
    whole point of it is that the period is CHECKABLE."""
    marker = hg.stale_tense_marker(_obs(record_time=None, valid_to=None))
    assert marker == "(historical: valid 2025..????, recorded ????-??)"
    assert "2026" not in marker


def test_the_marker_is_on_the_prompt_line_the_evidence_and_the_citation():
    """ONE string, THREE surfaces. A read that re-asserts a 2016 figure as
    current has to be graded against a row that says 2016 — and it only is if
    the marker survives every hop between the prompt and the judge."""
    entry = _obs()
    marker = hg.stale_tense_marker(entry)
    lines = hg.history_block_lines([entry], 12)
    evidence = hg.observation_evidence_text(entry, 12)
    citation = hg.observation_citation(entry, 12)
    assert marker in "\n".join(lines)
    assert marker in evidence
    assert citation["observation"]["stale_tense"] == marker
    assert marker in citation["evidence_text"]
    assert marker in citation["title"]


# ---------------------------------------------------------------------------
# One ordinal per LINE
# ---------------------------------------------------------------------------


def test_each_series_line_takes_its_own_ordinal():
    entries = [
        _obs(series_id="wb.gdp", observation_id="a" * 8 + "-1111-4111-8111-" + "b" * 12),
        _obs(series_id="wb.cpi", observation_id="c" * 8 + "-1111-4111-8111-" + "d" * 12),
        _obs(series_id="eia.oil", observation_id="e" * 8 + "-1111-4111-8111-" + "f" * 12),
    ]
    lines = hg.history_block_lines(entries, 12)
    body = "\n".join(lines)
    assert "[12]" in body and "[13]" in body and "[14]" in body
    assert hg.ordinal_span(entries) == 3


def test_the_section_renderer_reserves_the_whole_run():
    """The killer defect this prevents: a block that CLAIMS ordinals 12-14
    while the renderer advances by one would hand ordinal 13 to the next block
    as well, and two different pieces of evidence would answer to one [N]."""
    entries = [_obs(series_id=f"s{i}") for i in range(3)]
    rows = [
        _row(entries),
        {
            ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_QUESTIONS,
            ug.GROUNDING_PAYLOAD_KEY: [
                {"question_id": "q1", "question": "why?", "asked_at": "2026-09-01"}
            ],
        },
    ]
    signals, grounding = ug.partition_grounding_rows(rows)
    assert signals == []
    # HISTORICAL SERIES sorts LAST (GROUNDING_BLOCK_KINDS), so the questions
    # block takes 12 and the three history lines take 13-15.
    text, stamped = ug.render_grounding_section(grounding, start_ordinal=12)
    ordinals = {r[ug.UNIT_GROUNDING_ROW_KEY]: n for n, r in stamped}
    assert ordinals[ug.GROUNDING_QUESTIONS] == 12
    assert ordinals[hg.GROUNDING_HISTORY] == 13
    for n in (13, 14, 15):
        assert f"[{n}]" in text
    assert ug.block_ordinal_span(_row(entries)) == 3

    # And the advance itself, with a block rendered AFTER the run — the order
    # the partition will never produce today, asserted anyway because the
    # arithmetic must hold whichever way the block vocabulary is later ordered.
    text2, stamped2 = ug.render_grounding_section(
        [_row(entries), rows[1]], start_ordinal=12,
    )
    assert [n for n, _ in stamped2] == [12, 15]
    assert "[15] STANDING OPEN QUESTIONS" in text2


def test_every_other_block_still_claims_exactly_one_ordinal():
    for kind in ug.GROUNDING_BLOCK_KINDS:
        if kind == hg.GROUNDING_HISTORY:
            continue
        assert ug.block_ordinal_span({ug.UNIT_GROUNDING_ROW_KEY: kind}) == 1


def test_grounding_citations_returns_one_citation_per_line():
    entries = [
        _obs(series_id="a", observation_id="aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"),
        _obs(series_id="b", observation_id="bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb"),
    ]
    pairs = ug.grounding_citations(_row(entries), 7)
    assert [n for n, _ in pairs] == [7, 8]
    assert [c["ref_id"] for _, c in pairs] == [
        "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
        "bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb",
    ]


def test_the_single_ordinal_blocks_still_answer_through_the_same_front_door():
    row = {
        ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_SITUATIONS,
        ug.GROUNDING_PAYLOAD_KEY: [
            {"situation_id": "s1", "name": "frame", "status": "open"}
        ],
    }
    pairs = ug.grounding_citations(row, 4)
    assert len(pairs) == 1
    assert pairs[0][0] == 4
    assert pairs[0][1]["ref_kind"] == ug.GROUNDING_SITUATIONS


def test_citation_for_block_refuses_the_history_row_rather_than_guessing():
    """It is not a single-ordinal block and has no single citation. Answering
    would mean picking a line (a lie about what the ordinal points at) or
    minting a block-level ref_kind the verify path would score as unresolved."""
    assert ug.citation_for_block(_row([_obs()]), 12) is None


# ---------------------------------------------------------------------------
# The citation
# ---------------------------------------------------------------------------


def test_the_observation_ref_kind_is_registered_for_the_verify_path():
    assert "observation" in GROUNDING_REF_KINDS


def test_a_cited_observation_is_admitted_as_grounding_evidence():
    citation = hg.observation_citation(_obs(), 12)
    assert is_grounding_citation(citation)
    assert citation["ref_kind"] == "observation"
    # A real uuid — an observations row HAS one, so keeping it is not a
    # fabricated anchor. And no signal_id: it is not a signals row.
    assert citation["ref_id"] == "11111111-1111-4111-8111-111111111111"
    assert "signal_id" not in citation


def test_the_citation_carries_the_row_so_it_reads_without_a_round_trip():
    obs = hg.observation_citation(_obs(), 12)["observation"]
    assert obs["value"] == "2.8"
    assert obs["unit"] == "pct_per_year"
    assert obs["provider"] == "world_bank"
    assert obs["sha256"] == "abc123"
    assert obs["source_url"].startswith("https://api.worldbank.org")


def test_a_row_we_cannot_point_at_is_never_rendered_as_citable():
    assert hg.observation_citation(_obs(observation_id=None), 12) is None


def test_the_number_is_the_providers_own_precision_never_rounded():
    """A citation must match the row it points at. A rounded figure in the
    prompt beside an unrounded one in the citation is a drift a judge scores
    as unfaithful."""
    entry = _obs(value_display="13586.086904109588", value=13586.086904109588)
    assert "13586.086904109588" in "\n".join(hg.history_block_lines([entry], 1))
    assert (
        hg.observation_citation(entry, 1)["observation"]["value"]
        == "13586.086904109588"
    )


def test_the_cite_rule_forbids_reading_a_past_figure_as_a_current_one():
    rule = hg.HISTORY_CITE_RULE
    assert "never" in rule.lower()
    assert "valid period" in rule
    assert rule in "\n".join(hg.history_block_lines([_obs()], 1))
    assert rule in hg.observation_evidence_text(_obs(), 1)


def test_an_empty_block_renders_nothing_at_all():
    assert hg.history_block_lines([], 1) == []
    assert ug.grounding_citations(_row([]), 1) == []


# ---------------------------------------------------------------------------
# The grant — OFF by default, byte-identical
# ---------------------------------------------------------------------------


class _Conn:
    """Records every statement so an UNGRANTED run can be proven to cost
    nothing — not merely to render nothing."""

    def __init__(self):
        self.queries: list[str] = []

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        return []


@pytest.mark.asyncio
async def test_ungranted_fires_no_query_at_all():
    conn = _Conn()
    rows = await ug.gather_unit_grounding_rows(
        conn, analyst_id=None, target_filter="country_g20_us"
    )
    assert rows == []
    assert not any("observations" in q for q in conn.queries)
    assert not any("collection_descriptors" in q for q in conn.queries)


@pytest.mark.asyncio
async def test_granted_resolves_the_desk_before_it_reads_the_table():
    conn = _Conn()
    await ug.gather_unit_grounding_rows(
        conn,
        analyst_id=None,
        target_filter="country_g20_us",
        offer_history=True,
    )
    # The desk is resolved off the LOADED holdings' own subjects block; with
    # no holding the series read never fires.
    assert any("collection_descriptors" in q for q in conn.queries)
    assert not any("FROM observations" in q for q in conn.queries)


def test_the_ungranted_prompt_is_byte_identical_to_the_six_block_render():
    """Asserted on the BYTES. A count assertion would pass on a render that
    silently moved a space, and the acceptance step for a grant is that an
    ungranted desk's prompt did not move at all."""
    six = [
        {
            ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_SITUATIONS,
            ug.GROUNDING_PAYLOAD_KEY: [
                {"situation_id": "s1", "name": "frame", "status": "open"}
            ],
        },
        {
            ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_QUESTIONS,
            ug.GROUNDING_PAYLOAD_KEY: [
                {"question_id": "q1", "question": "why?", "asked_at": "2026-09-01"}
            ],
        },
    ]
    without, stamped_without = ug.render_grounding_section(six, start_ordinal=3)
    with_history, stamped_with = ug.render_grounding_section(
        six + [_row([_obs()])], start_ordinal=3
    )
    assert with_history.startswith(without)
    assert with_history != without
    assert [n for n, _ in stamped_without] == [n for n, _ in stamped_with][:2]


def test_the_history_block_is_last_and_is_a_registered_receipt():
    assert ug.GROUNDING_BLOCK_KINDS[-1] == hg.GROUNDING_HISTORY
    assert ug.GROUNDING_RECEIPT_KEYS[hg.GROUNDING_HISTORY] == "grounding_history_ref"
    assert ug.grounding_receipts([])["grounding_history_ref"] == 0
    assert ug.grounding_receipts([_row([_obs()])])["grounding_history_ref"] == 1


def test_the_descriptor_grant_is_declared_and_bounded():
    from legba.data.analysts.handler_options import known_kind_option_names

    names = set(known_kind_option_names("inline_target"))
    assert {"offer_history", "history_series_limit"} <= names


# ---------------------------------------------------------------------------
# The export's endnote
# ---------------------------------------------------------------------------


def test_the_exported_endnote_prints_the_ROW_not_a_label_for_it():
    """A reader of the printed document must be able to check the number
    against the provider without joining to anything — which is the whole
    point of citing a holding rather than recalling one."""
    from legba.data.registry.export_api import (
        _citation_export_entry,
        _md_citation_line,
    )

    citation = hg.observation_citation(_obs(), 12)
    entry = _citation_export_entry(citation, {})
    assert entry["citation_kind"] == "observation"
    assert entry["ref_id"] == "11111111-1111-4111-8111-111111111111"
    assert entry["observation"]["stale_tense"] == hg.stale_tense_marker(_obs())

    line = _md_citation_line(entry)
    for part in (
        "GDP growth (annual %)",
        "world_bank",
        "wb.gdp_growth_annual_pct",
        "US",
        "2.8 pct_per_year",
        "(historical: valid 2025..2025, recorded 2026-07)",
        "https://api.worldbank.org",
        "file sha256:abc123",
        "historical observation",
    ):
        assert part in line, f"{part!r} missing from {line!r}"


def test_a_signal_endnote_is_untouched_by_the_observation_branch():
    """An existing reader of an exported pack must see no change."""
    from legba.data.registry.export_api import _md_citation_line

    line = _md_citation_line({
        "citation_kind": "signal",
        "marker": "[3]",
        "title": "A headline",
        "canonical_url": "https://example.invalid/a",
        "resolved": True,
    })
    assert line == "- [3] A headline — https://example.invalid/a"
