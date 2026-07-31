# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QW1-B — per-unit DESK GROUNDING blocks + the per-unit slice ranking seam.

The P1 prompt gallery measured 8 of the 9 bounded units fanning out over ONE
byte-identical 120-row slice with nothing in the prompt telling them what the desk
already knew. This wave mirrors the composition CONTINUITY idiom downward and
widens it to four blocks — PRIOR READ, OPEN SITUATION REGISTER, DESK BASELINE,
STANDING OPEN QUESTIONS — plus a per-unit RE-RANK of the shared slice.

What these tests hold, in the order the design commits to it:

  * ABSENCE IS BYTE-IDENTICAL. No marked rows ⇒ the partition is the identity, no
    section renders, no receipt key appears, no ordinal moves. No ``slice_focus``
    ⇒ ``_orient`` returns the SAME list object in the SAME order.
  * THE RANKING SEAM IS A RE-RANK, NEVER A FILTER. Same rows, same count, same
    ``derived_from`` SET — only the order changes; a focus that matches nothing
    leaves the recency order byte-identical (stable sort).
  * EVERY BLOCK IS CITABLE, WITH AN HONEST REF SHAPE. The prior read carries its
    real finding uuid as ``ref_id``; the three synthetic blocks carry the REAL
    underlying ids and NO ``ref_id`` (no fabricated anchor); NOTHING carries
    ``ref_kind='finding'`` (that token would route the unit to the composition
    verify floor) and nothing carries a ``signal_id`` it does not have.
  * THE VERIFY PATH CONSUMES THEM WITHOUT A REWRITE. A clause citing a grounding
    block is SUPPORTED by the deterministic floor and the judge is handed the
    block's text — the same fix #116e made for signals.
  * HONEST ABSENCE ON THE BASELINE. A thin-history band is never rendered.
  * DEGRADE-NEVER-BREAK. One failing reader never suppresses its siblings; a
    target-less run gathers nothing rather than reading another desk's frames.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from legba.data.analysts import unit_grounding as ug
from legba.data.analysts.handler_options import (
    ANALYST_KIND_OPTIONS,
    RESERVED_OPTION_KEYS,
    known_kind_option_names,
    resolve_kind_options,
)
from legba.data.analysts.inline_target import (
    _orient,
    _resolve_slice_focus,
    _SLICE_FOCUS_CLASSES_OPTION,
    _SLICE_FOCUS_OPTION,
)
from legba.data.provenance.kinds import GROUNDING_REF_KINDS, is_grounding_citation
from legba.data.provenance.verify import (
    _deterministic_floor,
    _marker_to_evidence,
    _uses_subclaim_convention,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _RoutingConn:
    """Fake asyncpg connection routing by SQL text (mirrors the composition
    continuity tests' ``_RoutingConn``). Each family may instead be a callable
    that RAISES, so the per-block degrade guard is provable."""

    def __init__(
        self,
        *,
        prior_rows: list[dict[str, Any]] | None = None,
        situation_rows: list[dict[str, Any]] | None = None,
        baseline_rows: list[dict[str, Any]] | list[Exception] | None = None,
        question_rows: list[dict[str, Any]] | None = None,
        raise_on: tuple[str, ...] = (),
    ) -> None:
        self._prior = prior_rows or []
        self._situations = situation_rows or []
        self._baselines = baseline_rows or []
        self._questions = question_rows or []
        self._raise_on = raise_on
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        for needle in self._raise_on:
            if needle in query:
                raise RuntimeError(f"boom: {needle}")
        if "FROM desk_baselines" in query:
            return [dict(r) for r in self._baselines]
        if "FROM hypotheses" in query:
            return [dict(r) for r in self._questions]
        if "FROM situations" in query:
            return [dict(r) for r in self._situations]
        return [dict(r) for r in self._prior]

    def query_of(self, needle: str) -> tuple[str, tuple[Any, ...]]:
        for call in reversed(self.calls):
            if needle in call[0]:
                return call
        raise AssertionError(f"no captured query contains {needle!r}")

    def has_query(self, needle: str) -> bool:
        return any(needle in q for q, _ in self.calls)


_PRIOR_ID = uuid4()
_SITUATION_ID = uuid4()
_QUESTION_ID = uuid4()


def _prior_row(**over: Any) -> dict[str, Any]:
    row = {
        ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_PRIOR_READ,
        "id": _PRIOR_ID,
        "title": "Iran escalation holding at elevated",
        "body": "BLUF: tension flat versus the prior sweep.",
        "analyst_id": "escalation",
        "produced_at": "2026-07-30T07:00:00+00:00",
        "age_hours": 25.4,
        # These two ride the shared composition reader's SELECT. The unit path
        # must never surface them (see the citation assertions below).
        "effective_confidence": 0.71,
        "derived_from": [str(uuid4())],
    }
    row.update(over)
    return row


def _situations_row(n: int = 2) -> dict[str, Any]:
    return {
        ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_SITUATIONS,
        ug.GROUNDING_PAYLOAD_KEY: [
            {
                "situation_id": str(_SITUATION_ID if i == 0 else uuid4()),
                "name": f"Iran - open frame {i}",
                "status": "active",
                "intensity_score": 52.7 - i,
                "event_count": 31 - i,
                "last_event_at": "2026-07-31T12:00:00+00:00",
                "age_days": 30.2,
            }
            for i in range(n)
        ],
    }


def _baseline_row() -> dict[str, Any]:
    return {
        ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_BASELINE,
        ug.GROUNDING_PAYLOAD_KEY: [
            {
                "desk_id": "country_watch_ir",
                "metric": "signal_volume_24h",
                "expected": 41.2,
                "center_median": 39.0,
                "band_low": 21.4,
                "band_high": 61.0,
                "current": 118.0,
                "deviation": "above",
                "deviation_sigma": 3.7,
                "n_sigma": 2.0,
                "baseline_days": 28,
                "sample_days": 26,
                "active_days": 26,
                "computed_at": "2026-07-31T06:00:00+00:00",
            }
        ],
    }


def _questions_row() -> dict[str, Any]:
    return {
        ug.UNIT_GROUNDING_ROW_KEY: ug.GROUNDING_QUESTIONS,
        ug.GROUNDING_PAYLOAD_KEY: [
            {
                "question_id": str(_QUESTION_ID),
                "question": "Does the IRGC hold standing interdiction authority?",
                "asked_at": "2026-07-24T19:00:00+00:00",
                "age_days": 6.8,
                "analyst_id": "escalation",
            }
        ],
    }


def _signal(idx: int, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid4(),
        "title": f"signal {idx}",
        "source_url": f"https://example.test/{idx}",
        "produced_at": f"2026-07-{10 + idx:02d}T00:00:00+00:00",
        "data": {"summary": f"body of signal {idx}"},
        "entity_classes": [],
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# ABSENCE is byte-identical
# ---------------------------------------------------------------------------


def test_partition_of_an_unmarked_slice_is_the_identity():
    rows = [_signal(i) for i in range(5)]
    signals, grounding = ug.partition_grounding_rows(rows)
    assert grounding == []
    assert signals == rows


def test_render_of_no_blocks_emits_nothing():
    """A first run's prompt is byte-identical to the pre-grounding render."""
    text, stamped = ug.render_grounding_section([], start_ordinal=42)
    assert text == ""
    assert stamped == []


def test_receipts_always_report_every_block_kind():
    """A key that appeared only when a block resolved could not distinguish
    'no prior read' from 'the receipt changed shape'."""
    empty = ug.grounding_receipts([])
    assert empty == {k: 0 for k in ug.GROUNDING_RECEIPT_KEYS.values()}
    some = ug.grounding_receipts([_prior_row(), _baseline_row()])
    assert some["grounding_prior_ref"] == 1
    assert some["grounding_baseline_ref"] == 1
    assert some["grounding_situations_ref"] == 0
    assert some["grounding_questions_ref"] == 0


# ---------------------------------------------------------------------------
# Partition + render
# ---------------------------------------------------------------------------


def test_partition_lifts_marked_rows_in_canonical_order():
    """Ordinals must not depend on the order the reader happened to append."""
    rows = [
        _questions_row(),
        _signal(1),
        _baseline_row(),
        _signal(2),
        _prior_row(),
        _situations_row(),
    ]
    signals, grounding = ug.partition_grounding_rows(rows)
    assert [r["title"] for r in signals] == ["signal 1", "signal 2"]
    assert [r[ug.UNIT_GROUNDING_ROW_KEY] for r in grounding] == list(
        ug.GROUNDING_BLOCK_KINDS
    )


def test_partition_is_first_wins_per_kind():
    first, second = _prior_row(title="first"), _prior_row(title="second")
    _signals, grounding = ug.partition_grounding_rows([first, second])
    assert len(grounding) == 1
    assert grounding[0]["title"] == "first"


def test_rendered_ordinals_are_contiguous_from_the_start_ordinal():
    _signals, grounding = ug.partition_grounding_rows(
        [_prior_row(), _situations_row(), _baseline_row(), _questions_row()]
    )
    text, stamped = ug.render_grounding_section(grounding, start_ordinal=121)
    assert [n for n, _row in stamped] == [121, 122, 123, 124]
    for n in (121, 122, 123, 124):
        assert f"[{n}]" in text


def test_a_missing_block_does_not_leave_an_ordinal_gap():
    """Ordinals are positions among the blocks PRESENT — a desk with no baseline
    must not hand the model a hole in its own numbering."""
    _signals, grounding = ug.partition_grounding_rows(
        [_prior_row(), _questions_row()]
    )
    _text, stamped = ug.render_grounding_section(grounding, start_ordinal=7)
    assert [n for n, _row in stamped] == [7, 8]


def test_every_block_prints_its_own_dates():
    """TEMPORAL COLLAPSE guard: the model must be able to anchor 'when' on the
    block, never on run time — so every block carries its own clock."""
    _signals, grounding = ug.partition_grounding_rows(
        [_prior_row(), _situations_row(1), _baseline_row(), _questions_row()]
    )
    text, _stamped = ug.render_grounding_section(grounding, start_ordinal=1)
    assert "produced_at=2026-07-30T07:00:00+00:00" in text
    assert "age=25.4h" in text
    assert "last_event_at=2026-07-31T12:00:00+00:00" in text
    assert "computed_at=2026-07-31T06:00:00+00:00" in text
    assert "asked_at=2026-07-24T19:00:00+00:00" in text


def test_prior_read_block_prints_no_confidence_number():
    """A unit's signal blocks print no confidence anywhere; a number shown only
    on last cycle's OWN read is an anchor the unit would inherit."""
    _signals, grounding = ug.partition_grounding_rows([_prior_row()])
    text, _stamped = ug.render_grounding_section(grounding, start_ordinal=1)
    assert "confidence" not in text.lower()
    assert "0.71" not in text


def test_prior_read_body_is_bounded():
    row = _prior_row(body="x" * 5000)
    _signals, grounding = ug.partition_grounding_rows([row])
    text, _stamped = ug.render_grounding_section(grounding, start_ordinal=1)
    assert "x" * ug.PRIOR_BODY_CHARS in text
    assert "x" * (ug.PRIOR_BODY_CHARS + 1) not in text


def test_baseline_block_names_itself_as_not_a_forecast():
    _signals, grounding = ug.partition_grounding_rows([_baseline_row()])
    text, _stamped = ug.render_grounding_section(grounding, start_ordinal=1)
    assert "NOT a forecast" in text
    assert "normal band 21.4-61.0" in text
    assert "ABOVE" in text


# ---------------------------------------------------------------------------
# Citations — honest ref shapes, no fabricated anchors
# ---------------------------------------------------------------------------


def test_prior_read_citation_carries_its_real_finding_id_and_nothing_it_stripped():
    citation = ug.citation_for_block(_prior_row(), 121)
    assert citation is not None
    assert citation["ref_id"] == str(_PRIOR_ID)
    assert citation["ref_kind"] == ug.GROUNDING_PRIOR_READ
    assert citation["grounding"] == ug.GROUNDING_PRIOR_READ
    assert citation["marker"] == "[121]"
    assert citation["produced_at"] == "2026-07-30T07:00:00+00:00"
    # MEMORY, not corroboration: never bootstraps confidence, never folds into a
    # shared-lineage component with a current signal.
    assert "effective_confidence" not in citation
    assert "derived_from" not in citation
    # Not a signal — never claims a signal id it does not have.
    assert "signal_id" not in citation


def test_prior_read_with_no_resolvable_id_is_never_cited():
    """Never claim a prior read we cannot point at."""
    assert ug.citation_for_block(_prior_row(id=None), 5) is None
    assert ug.citation_for_block(_prior_row(id="not-a-uuid"), 5) is None


@pytest.mark.parametrize(
    "row_factory,id_key,expected",
    [
        (_situations_row, "situation_ids", str(_SITUATION_ID)),
        (_questions_row, "question_ids", str(_QUESTION_ID)),
    ],
)
def test_synthetic_blocks_carry_real_ids_and_no_fabricated_ref_id(
    row_factory, id_key, expected,
):
    citation = ug.citation_for_block(row_factory(), 9)
    assert citation is not None
    assert "ref_id" not in citation, "minting a ref_id would be a fabricated anchor"
    assert "signal_id" not in citation
    assert expected in citation[id_key]


def test_baseline_citation_uses_the_tables_real_composite_key():
    """``desk_baselines`` is keyed (desk_id, metric) with no uuid — the honest
    handle is that key, not a synthesized id."""
    citation = ug.citation_for_block(_baseline_row(), 9)
    assert citation is not None
    assert "ref_id" not in citation
    assert citation["baseline_keys"] == ["country_watch_ir:signal_volume_24h"]


@pytest.mark.parametrize(
    "row_factory",
    [_prior_row, _situations_row, _baseline_row, _questions_row],
)
def test_no_block_ever_stamps_the_composition_discriminator(row_factory):
    """``ref_kind='finding'`` is ``verify._uses_subclaim_convention``'s trigger —
    stamping it would route the whole unit finding to the sub-claim floor."""
    citation = ug.citation_for_block(row_factory(), 3)
    assert citation is not None
    assert citation["ref_kind"] != "finding"
    assert citation["ref_kind"] in GROUNDING_REF_KINDS
    assert not str(citation["marker"]).startswith("[[ref:")
    assert _uses_subclaim_convention([citation]) is False


def test_every_block_captures_the_bytes_the_model_was_shown():
    """``evidence_text`` IS what the judge grades against — it must be the
    rendered block, bounded, not a summary of it."""
    _signals, grounding = ug.partition_grounding_rows(
        [_prior_row(), _situations_row(), _baseline_row(), _questions_row()]
    )
    text, stamped = ug.render_grounding_section(grounding, start_ordinal=1)
    for n, row in stamped:
        citation = ug.citation_for_block(row, n)
        assert citation is not None
        assert citation["evidence_text"] in text
        assert len(citation["evidence_text"]) <= ug.EVIDENCE_TEXT_CHARS


# ---------------------------------------------------------------------------
# The verify path consumes the blocks with no rewrite
# ---------------------------------------------------------------------------


def test_a_clause_citing_a_grounding_block_is_supported_by_the_deterministic_floor():
    citation = ug.citation_for_block(_prior_row(), 121)
    body = (
        "Iranian naval interdiction has escalated since the prior read of "
        "2026-07-30 [121]."
    )
    report = _deterministic_floor(body, [citation])
    assert report.faithfulness_score == 1.0
    assert report.unsupported_spans == []


def test_an_uncited_grounding_ordinal_still_fails_the_floor():
    """The floor must not become permissive: an ordinal NOT backed by a block or
    a signal is still an unresolved citation."""
    citation = ug.citation_for_block(_prior_row(), 121)
    body = "Tehran mobilised three brigades on 2026-07-29 [999]."
    report = _deterministic_floor(body, [citation])
    assert report.faithfulness_score == 0.0
    assert report.unsupported_spans[0].reason == "unresolved_citation"


def test_the_judge_is_handed_the_block_text_not_an_opaque_handle():
    citation = ug.citation_for_block(_situations_row(), 122)
    evidence = _marker_to_evidence([citation])
    assert 122 in evidence
    assert "OPEN SITUATION REGISTER" in evidence[122]
    assert "Iran - open frame 0" in evidence[122]


def test_grounding_entries_do_not_disturb_signal_evidence():
    """Additive: a mixed citation list still resolves its SIGNAL entries exactly
    as before."""
    sid = str(uuid4())
    signal_citation = {
        "marker": "[3]",
        "signal_id": sid,
        "title": "Oil price rises",
        "snippet": "Brent up 4%.",
    }
    citation = ug.citation_for_block(_baseline_row(), 121)
    evidence = _marker_to_evidence([signal_citation, citation])
    assert evidence[3] == "Oil price rises — Brent up 4%."
    assert "DESK BASELINE" in evidence[121]


def test_is_grounding_citation_refuses_an_entry_with_no_captured_evidence():
    """An entry we cannot grade must NOT count as support."""
    assert is_grounding_citation(
        {"marker": "[1]", "ref_kind": "desk_baseline", "evidence_text": "x"}
    )
    assert not is_grounding_citation(
        {"marker": "[1]", "ref_kind": "desk_baseline", "evidence_text": "  "}
    )
    assert not is_grounding_citation({"marker": "[1]", "ref_kind": "desk_baseline"})
    assert not is_grounding_citation(
        {"marker": "[1]", "ref_kind": "finding", "evidence_text": "x"}
    )
    assert not is_grounding_citation(
        {"marker": "[1]", "ref_kind": "prior_read", "evidence_text": "x",
         "signal_id": "abc"}
    )


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_reader_excludes_thin_history_bands():
    """HONEST ABSENCE: a band resting on thin history is not rendered at all —
    it would read as authority it has not earned."""
    conn = _RoutingConn(baseline_rows=[])
    assert await ug.read_desk_baselines(conn, desk_id="country_watch_ir") == []
    sql, params = conn.query_of("FROM desk_baselines")
    assert "insufficient_history IS NOT TRUE" in sql
    assert params[0] == "country_watch_ir"


@pytest.mark.asyncio
async def test_baseline_reader_skips_a_row_with_no_metric_name():
    conn = _RoutingConn(
        baseline_rows=[
            {"desk_id": "d", "metric": None, "expected": 1.0},
            {"desk_id": "d", "metric": "signal_volume_24h", "expected": 1.0,
             "deviation": "within"},
        ]
    )
    out = await ug.read_desk_baselines(conn, desk_id="d")
    assert [b["metric"] for b in out] == ["signal_volume_24h"]


@pytest.mark.asyncio
async def test_question_reader_is_desk_scoped_and_open_only():
    conn = _RoutingConn(question_rows=[])
    await ug.read_desk_open_questions(conn, target_id="country_watch_ir")
    sql, params = conn.query_of("FROM hypotheses")
    assert "status = 'open_question'" in sql
    assert "target_id = $1" in sql
    assert params[0] == "country_watch_ir"


@pytest.mark.asyncio
async def test_question_reader_never_pads_a_missing_row():
    conn = _RoutingConn(
        question_rows=[
            {"id": None, "thesis": "orphan"},
            {"id": _QUESTION_ID, "thesis": "   "},
            {"id": _QUESTION_ID, "thesis": "a real standing question",
             "produced_at": "2026-07-24T19:00:00+00:00", "age_days": 6.8,
             "analyst_id": "escalation"},
        ]
    )
    out = await ug.read_desk_open_questions(conn, target_id="d")
    assert [q["question"] for q in out] == ["a real standing question"]


@pytest.mark.asyncio
async def test_gather_without_a_target_reads_nothing():
    """Every block is desk-scoped; an unscoped read would hand a desk another
    desk's frames (the D4 contamination class)."""
    conn = _RoutingConn()
    assert await ug.gather_unit_grounding_rows(
        conn, analyst_id="escalation", target_filter=None,
    ) == []
    assert conn.calls == []


@pytest.mark.asyncio
async def test_gather_without_an_analyst_id_still_returns_the_desk_blocks():
    """An unattributable 'previous read' is exactly the uncited prior this
    design refuses — but it must not cost the desk its other three blocks."""
    conn = _RoutingConn(
        situation_rows=[{"id": _SITUATION_ID, "name": "frame", "status": "active"}],
    )
    rows = await ug.gather_unit_grounding_rows(
        conn, analyst_id=None, target_filter="country_watch_ir",
    )
    kinds = {r[ug.UNIT_GROUNDING_ROW_KEY] for r in rows}
    assert ug.GROUNDING_PRIOR_READ not in kinds
    assert ug.GROUNDING_SITUATIONS in kinds


@pytest.mark.asyncio
async def test_one_failing_reader_never_suppresses_its_siblings():
    """DEGRADE-NEVER-BREAK, per block."""
    conn = _RoutingConn(
        situation_rows=[{"id": _SITUATION_ID, "name": "frame", "status": "active"}],
        question_rows=[
            {"id": _QUESTION_ID, "thesis": "standing",
             "produced_at": "2026-07-24T19:00:00+00:00", "age_days": 6.8},
        ],
        raise_on=("FROM desk_baselines",),
    )
    rows = await ug.gather_unit_grounding_rows(
        conn, analyst_id="escalation", target_filter="country_watch_ir",
    )
    kinds = {r[ug.UNIT_GROUNDING_ROW_KEY] for r in rows}
    assert ug.GROUNDING_BASELINE not in kinds
    assert ug.GROUNDING_SITUATIONS in kinds
    assert ug.GROUNDING_QUESTIONS in kinds


@pytest.mark.asyncio
async def test_gathered_rows_are_stamped_under_the_unit_marker():
    """The prior read comes off the SHARED composition reader, which stamps the
    COMPOSITION marker — the unit partition must own it, not inherit it."""
    conn = _RoutingConn(
        prior_rows=[{
            "id": _PRIOR_ID, "title": "prior", "body": "b",
            "analyst_id": "escalation",
            "produced_at": "2026-07-30T07:00:00+00:00", "age_hours": 25.0,
        }],
    )
    rows = await ug.gather_unit_grounding_rows(
        conn, analyst_id="escalation", target_filter="country_watch_ir",
    )
    prior = [r for r in rows if r.get(ug.UNIT_GROUNDING_ROW_KEY)
             == ug.GROUNDING_PRIOR_READ]
    assert len(prior) == 1
    _signals, partitioned = ug.partition_grounding_rows(rows)
    assert any(
        r[ug.UNIT_GROUNDING_ROW_KEY] == ug.GROUNDING_PRIOR_READ for r in partitioned
    )


@pytest.mark.asyncio
async def test_prior_read_is_scoped_to_this_unit_and_this_target():
    conn = _RoutingConn(prior_rows=[])
    await ug.gather_unit_grounding_rows(
        conn, analyst_id="escalation", target_filter="country_watch_ir",
    )
    sql, params = conn.query_of("AS age_hours")
    assert "f.analyst_id = $1" in sql
    assert "f.superseded_by IS NULL" in sql
    assert "Faithfulness verify%" in sql, "the verify GATE must still apply"
    assert params[0] == "escalation"
    assert params[1] == ug.PRIOR_LOOKBACK_HOURS
    assert params[-1] == "country_watch_ir"


# ---------------------------------------------------------------------------
# The prompt contract
# ---------------------------------------------------------------------------


def test_the_clause_is_appended_once_and_only_once():
    base = "You are the ESCALATION RISK unit."
    once = ug.with_grounding_clause(base)
    assert ug.UNIT_GROUNDING_CLAUSE in once
    assert ug.with_grounding_clause(once) == once


def test_the_clause_states_the_four_obligations_and_the_first_read_leg():
    clause = ug.UNIT_GROUNDING_CLAUSE
    assert "what CHANGED" in clause
    assert "NEVER on 'today'" in clause
    assert "SAY SO plainly" in clause
    assert "FIRST read" in clause
    # the two obligations the extra unit blocks create
    assert "DESK BASELINE band" in clause
    assert "STANDING OPEN QUESTION" in clause


def test_the_clause_never_touches_an_empty_prompt():
    assert ug.with_grounding_clause("") == ""


# ---------------------------------------------------------------------------
# The per-unit RANKING seam
# ---------------------------------------------------------------------------


def _focus_slice() -> list[dict[str, Any]]:
    return [
        _signal(1, title="Oil price rises", data={"summary": "Brent up"}),
        _signal(2, title="Iran strikes tankers in the Strait of Hormuz",
                data={"summary": "IRGC interdiction"}),
        _signal(3, title="St Paul mayor harassment inquiry",
                data={"summary": "local politics"}),
        _signal(4, title="IRGC downs drone", data={"summary": "air defence"},
                entity_classes=["Organization", "Event"]),
    ]


def test_no_focus_is_byte_identical_and_does_not_even_copy():
    rows = _focus_slice()
    kept, derived = _orient(list(rows), "country_watch_ir")
    kept_focus, derived_focus = _orient(list(rows), "country_watch_ir", focus=None)
    assert [r["title"] for r in kept] == [r["title"] for r in kept_focus]
    assert derived == derived_focus


def test_resolve_slice_focus_is_none_when_the_descriptor_declared_nothing():
    assert _resolve_slice_focus({}) is None
    assert _resolve_slice_focus({"target_id": "country_watch_ir"}) is None


def test_focus_reorders_but_never_reselects():
    """THE contract: same rows, same count, same derived_from SET — better
    order. A focus that dropped a row would be a filter wearing a ranking's
    clothes."""
    rows = _focus_slice()
    plain, plain_derived = _orient(list(rows), "t")
    focus = _resolve_slice_focus({_SLICE_FOCUS_OPTION: ["hormuz", "irgc"]})
    ranked, ranked_derived = _orient(list(rows), "t", focus=focus)
    assert len(ranked) == len(plain)
    assert {r["title"] for r in ranked} == {r["title"] for r in plain}
    assert set(ranked_derived) == set(plain_derived)
    assert [r["title"] for r in ranked] != [r["title"] for r in plain]
    assert "Hormuz" in ranked[0]["title"]


def test_a_focus_that_matches_nothing_leaves_the_order_byte_identical():
    """Stable sort: all-equal scores keep the exact recency order."""
    rows = _focus_slice()
    plain, _ = _orient(list(rows), "t")
    focus = _resolve_slice_focus({_SLICE_FOCUS_OPTION: ["antarctic-fisheries"]})
    ranked, _ = _orient(list(rows), "t", focus=focus)
    assert [r["title"] for r in ranked] == [r["title"] for r in plain]


def test_weights_are_honoured():
    rows = _focus_slice()
    focus = _resolve_slice_focus({_SLICE_FOCUS_OPTION: ["oil:5", "hormuz"]})
    ranked, _ = _orient(list(rows), "t", focus=focus)
    assert ranked[0]["title"] == "Oil price rises"


def test_entity_class_hints_rank_alongside_keyword_hints():
    rows = _focus_slice()
    focus = _resolve_slice_focus(
        {_SLICE_FOCUS_CLASSES_OPTION: ["Organization:3"]}
    )
    ranked, _ = _orient(list(rows), "t", focus=focus)
    assert ranked[0]["title"] == "IRGC downs drone"


def test_malformed_focus_tokens_are_dropped_not_guessed():
    focus = _resolve_slice_focus(
        {_SLICE_FOCUS_OPTION: ["good", "bad:notanumber", "zero:0", "  ", 7]}
    )
    assert focus is not None
    assert [t for t, _w in focus.terms] == ["good"]


def test_grounding_rows_are_never_ranked_or_packed_as_evidence():
    """They must not consume the token budget, must not be re-ranked, and must
    not enter derived_from — a unit is ANNOTATED by its memory, not DERIVED
    from it."""
    rows = _focus_slice() + [_prior_row(), _situations_row()]
    signals, grounding = ug.partition_grounding_rows(rows)
    assert len(grounding) == 2
    focus = _resolve_slice_focus({_SLICE_FOCUS_OPTION: ["hormuz"]})
    kept, derived = _orient(list(signals), "t", focus=focus)
    assert len(kept) == 4
    assert all(ug.UNIT_GROUNDING_ROW_KEY not in r for r in kept)
    assert str(_PRIOR_ID) not in {str(u) for u in derived}


# ---------------------------------------------------------------------------
# X-1 catalog + drift guards for the KIND lane
# ---------------------------------------------------------------------------


_INLINE_TARGET_SRC = (
    Path(__file__).resolve().parents[2]
    / "src" / "legba" / "data" / "analysts" / "inline_target.py"
).read_text()


def test_every_declared_kind_knob_is_actually_read_by_its_kind():
    """A declared-but-unread knob is dead config with extra steps. Proven
    BEHAVIOURALLY (the resolver must change what run_method sees), not by grep —
    the reads go through module constants, and a grep would pass on a constant
    nothing consumes."""
    for name in known_kind_option_names("inline_target"):
        assert f'"{name}"' in _INLINE_TARGET_SRC or f"'{name}'" in _INLINE_TARGET_SRC
        assert _resolve_slice_focus({name: ["hormuz"]}) is not None, (
            f"ANALYST_KIND_OPTIONS declares inline_target.{name} but "
            "run_method's option resolution ignores it"
        )


def test_no_kind_knob_collides_with_a_reserved_key():
    for kind, specs in ANALYST_KIND_OPTIONS.items():
        for spec in specs:
            assert spec.name not in RESERVED_OPTION_KEYS, (
                f"{kind}.{spec.name} collides with a runtime-owned key"
            )
            assert not spec.name.startswith("_")
            assert spec.doc.strip(), f"{kind}.{spec.name} has no doc"


def test_kind_resolution_accepts_a_well_formed_focus():
    res = resolve_kind_options(
        "inline_target",
        {"slice_focus": ["hormuz", "tanker:2.5"],
         "slice_focus_entity_classes": ["Organization"]},
    )
    assert res.rejected == ()
    assert res.accepted["slice_focus"] == ["hormuz", "tanker:2.5"]


@pytest.mark.parametrize(
    "value",
    [
        "hormuz",                       # not a list
        ["ok", ""],                     # empty member
        ["x" * 200],                    # unbounded token
        ["term:weight"],                # non-numeric weight
        [{"term": "hormuz"}],           # not a string
    ],
)
def test_kind_resolution_drops_a_malformed_focus_whole(value):
    """A value either lands intact or is dropped whole — never partially."""
    res = resolve_kind_options("inline_target", {"slice_focus": value})
    assert res.accepted == {}
    assert res.rejected[0].cause == "invalid_value"


def test_kind_resolution_still_refuses_runtime_owned_keys():
    res = resolve_kind_options("inline_target", {"analyst_id": "impostor"})
    assert res.accepted == {}
    assert res.rejected[0].cause == "reserved_key"


def test_kind_resolution_of_an_undeclared_kind_degrades_loudly():
    res = resolve_kind_options("meta_findings_synthesizer", {"slice_focus": ["x"]})
    assert res.accepted == {}
    assert res.rejected[0].cause == "unknown_kind"


def test_kind_resolution_of_an_unknown_key_degrades_loudly():
    res = resolve_kind_options("inline_target", {"no_such_knob": 3})
    assert res.accepted == {}
    assert res.rejected[0].cause == "unknown_key"


# ---------------------------------------------------------------------------
# Descriptor + runtime wiring for the KIND lane
# ---------------------------------------------------------------------------


def _descriptor_body(kind: str, method_kind: str, options: dict[str, Any]) -> dict:
    body: dict[str, Any] = {
        "identity": {
            "id": "escalation",
            "name": "Escalation Risk Unit",
            "schema_uri": "legba/analyst/1.0.0",
            "version": "0" * 16,
            "kind": kind,
            "type_signature": {
                "input_type": "legba.runtime.SignalList",
                "output_type": "legba.runtime.Finding",
                "deps_type": "legba.runtime.deps.StandardDeps",
            },
            "state": "active",
            "owner": "qw1b",
        },
        "subscription": {"targets": {"predicate": 'has_tag("g20")'}},
        "cadence": {"fallback_schedule": "0 7 * * *"},
        "method": {"kind": method_kind, "options": options},
    }
    if method_kind != "deterministic":
        body["method"]["system_prompt"] = "You are a unit."
    else:
        body["method"]["impl"] = "legba.data.analysts.deterministic"
    return body


def test_an_inline_target_descriptor_may_declare_slice_focus():
    from legba.data.schemas.analyst import AnalystDescriptor

    desc = AnalystDescriptor.model_validate(
        _descriptor_body("inline_target", "llm_planner",
                         {"slice_focus": ["hormuz", "tanker:2"]}),
        strict=False,
    )
    assert desc.method.options["slice_focus"] == ["hormuz", "tanker:2"]


def test_a_kind_with_no_catalog_still_cannot_carry_options():
    """The X-1 rule survives: a block no code path reads is refused, because a
    silent inert block is exactly the dead config X-1 exists to remove."""
    from legba.data.schemas.analyst import AnalystDescriptor

    with pytest.raises(ValueError, match="silently inert"):
        AnalystDescriptor.model_validate(
            _descriptor_body("meta_findings_synthesizer", "llm_planner",
                             {"slice_focus": ["hormuz"]}),
            strict=False,
        )


def test_the_runtime_merges_a_units_focus_into_the_run_options():
    from legba.runtime.dapr_actors import (
        HANDLER_OPTIONS_RECEIPT_PHASE,
        _merge_descriptor_options,
    )

    descriptor = SimpleNamespace(
        identity=SimpleNamespace(id="escalation", kind="inline_target", version="v1"),
        method=SimpleNamespace(options={"slice_focus": ["hormuz"]}),
    )
    options: dict[str, Any] = {"target_id": "country_watch_ir"}
    receipt = _merge_descriptor_options(options, descriptor)
    assert options["slice_focus"] == ["hormuz"]
    assert receipt["phase"] == HANDLER_OPTIONS_RECEIPT_PHASE
    assert receipt["status"] == "applied"
    assert receipt["analyst_kind"] == "inline_target"
    assert _resolve_slice_focus(options) is not None


def test_the_runtime_kind_lane_survives_an_analystkind_enum_member():
    """``str(AnalystKind.INLINE_TARGET)`` is ``'AnalystKind.INLINE_TARGET'``, not
    the value — a naive compare would route every enum-carrying descriptor down
    the ignore branch and make the knob permanently dead."""
    from legba.data.schemas.analyst import AnalystKind
    from legba.runtime.dapr_actors import _merge_descriptor_options

    descriptor = SimpleNamespace(
        identity=SimpleNamespace(
            id="escalation", kind=AnalystKind.INLINE_TARGET, version="v1",
        ),
        method=SimpleNamespace(options={"slice_focus": ["hormuz"]}),
    )
    options: dict[str, Any] = {}
    receipt = _merge_descriptor_options(options, descriptor)
    assert options["slice_focus"] == ["hormuz"]
    assert receipt["status"] == "applied"


def test_the_runtime_still_ignores_options_on_a_catalog_less_kind():
    from legba.runtime.dapr_actors import _merge_descriptor_options

    descriptor = SimpleNamespace(
        identity=SimpleNamespace(id="x", kind="predictor", version="v1"),
        method=SimpleNamespace(options={"slice_focus": ["hormuz"]}),
    )
    options: dict[str, Any] = {}
    receipt = _merge_descriptor_options(options, descriptor)
    assert options == {}
    assert receipt["status"] == "ignored_non_deterministic"


def test_a_descriptor_with_no_options_touches_neither_mapping_nor_trace():
    from legba.runtime.dapr_actors import _merge_descriptor_options

    descriptor = SimpleNamespace(
        identity=SimpleNamespace(id="escalation", kind="inline_target", version="v1"),
        method=SimpleNamespace(options=None),
    )
    options: dict[str, Any] = {"target_id": "t"}
    assert _merge_descriptor_options(options, descriptor) is None
    assert options == {"target_id": "t"}


# ---------------------------------------------------------------------------
# End to end through ``run_method`` — the only place all of it meets
# ---------------------------------------------------------------------------


class _CapturingLLM:
    """Records the assembled user prompt, returns a canned cited finding."""

    subprovider = "openai"

    def __init__(self, body: str) -> None:
        self.body = body
        self.user_prompt = ""
        self.system_prompt = ""

    async def chat_complete(self, messages: Any, **kwargs: Any):
        for m in messages or []:
            if m.get("role") == "user":
                self.user_prompt = m.get("content", "")
        self.system_prompt = kwargs.get("system", "") or ""
        import json as _json

        return SimpleNamespace(
            content=_json.dumps({
                "title": "Iran escalation",
                "body": self.body,
                "confidence": 0.6,
                "evidence": ["e"],
                "tags": ["severity:elevated"],
            }),
            usage=SimpleNamespace(
                prompt_tokens=10, completion_tokens=5, reasoning_tokens=0,
            ),
        )


async def _run(inputs: list[dict[str, Any]], body: str, **options: Any):
    from legba.data.analysts.inline_target import InlineTargetDeps, run_method

    llm = _CapturingLLM(body)
    result = await run_method(
        inputs,
        {"target_id": "country_watch_ir", "analyst_id": "escalation", **options},
        InlineTargetDeps(llm=llm),
    )
    return result, llm


@pytest.mark.asyncio
async def test_run_method_renders_the_section_after_the_signals_and_cites_it():
    inputs = _focus_slice() + [
        _prior_row(), _situations_row(), _baseline_row(), _questions_row(),
    ]
    body = (
        "No material change since the prior read of 2026-07-30 [5]. The desk's "
        "open frame remains active [6]. Volume sits above the desk's normal "
        "band [7]. The standing interdiction question is unanswered [8]. "
        "Iranian forces struck tankers [2]."
    )
    result, llm = await _run(inputs, body)

    prompt = llm.user_prompt
    assert prompt.index("=== DESK GROUNDING") > prompt.index("Number of signals:")
    assert ug.UNIT_GROUNDING_CLAUSE in llm.system_prompt

    citations = result.finding.data["citations"]
    by_marker = {c["marker"]: c for c in citations}
    assert by_marker["[5]"]["ref_kind"] == ug.GROUNDING_PRIOR_READ
    assert by_marker["[6]"]["ref_kind"] == ug.GROUNDING_SITUATIONS
    assert by_marker["[7]"]["ref_kind"] == ug.GROUNDING_BASELINE
    assert by_marker["[8]"]["ref_kind"] == ug.GROUNDING_QUESTIONS
    assert by_marker["[2]"]["signal_id"]
    # The whole prose grades clean on the deterministic floor — the blocks are
    # real evidence, not decoration.
    assert _deterministic_floor(body, citations).faithfulness_score == 1.0


@pytest.mark.asyncio
async def test_run_method_keeps_grounding_out_of_derived_from_and_stamps_receipts():
    inputs = _focus_slice() + [_prior_row(), _situations_row()]
    result, _llm = await _run(inputs, "Assessment resting on [1].")

    assert len(result.derived_from) == 4, "only the 4 signals are lineage"
    orient = next(s for s in result.intermediate_steps if s["phase"] == "orient")
    assert orient["in_count"] == 4, "grounding rows never count as inputs"
    assert orient["grounding_prior_ref"] == 1
    assert orient["grounding_baseline_ref"] == 0
    ground = next(
        s for s in result.intermediate_steps
        if s.get("kind") == "desk_grounding_blocks"
    )
    assert ground["blocks"] == 2
    assert ground["start_ordinal"] == 5


@pytest.mark.asyncio
async def test_run_method_without_grounding_rows_stamps_nothing_new():
    """Byte-identical absence: no section, no receipt keys, no extra step."""
    result, llm = await _run(_focus_slice(), "Assessment resting on [1].")
    assert "DESK GROUNDING" not in llm.user_prompt
    orient = next(s for s in result.intermediate_steps if s["phase"] == "orient")
    assert not any(k.startswith("grounding_") for k in orient)
    assert not any(
        s.get("kind") == "desk_grounding_blocks" for s in result.intermediate_steps
    )


@pytest.mark.asyncio
async def test_an_empty_signal_slice_still_noops_even_with_grounding_rows():
    """Synthesizing off memory alone, with no current evidence, is the
    fabrication this platform refuses — the NOOP must survive."""
    result, llm = await _run([_prior_row(), _situations_row()], "unused")
    assert result.finding.tags and "empty_slice" in result.finding.tags
    assert llm.user_prompt == "", "no LLM call may be made"
    assert result.derived_from == []
