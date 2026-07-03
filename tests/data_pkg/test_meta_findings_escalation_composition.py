# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S2-T4 — THEMATIC composition pilot: escalation_composition.

escalation_composition fuses ONE dimension — the ``escalation`` UNIT — ACROSS
every g20+watch desk into ONE global escalation read (the ANALYST axis, not the
TARGET axis the country/region/world compositions fuse along). It is the SAME
meta_findings_synthesizer kind; the thematic behavior is descriptor +
the S2-T4 READ_SLICE thematic branch + the cross-desk T7 correlation guard.

This suite locks:

  * ``thematic_dimension`` — the substrate-marker discriminator;
  * the READ_SLICE thematic branch — one head per desk (post-supersession,
    verify-floored, meta EXCLUDED) across ALL desks, with the g20+watch desk
    roster diffed to NAME any desk with no head as a gap;
  * ``_run`` selects the THEMATIC prompt + cites the per-desk reads;
  * the T7 CORRELATION GUARD — two desks' heads on the SAME underlying signal are
    de-duped (one independent component), the fused confidence is NOT inflated
    (capped to the de-duplicated ceiling, never a sum), and the audit is stamped.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import meta_findings_synthesizer as synth


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _CapturingConn:
    """Fake asyncpg.Connection that records each fetch() call's params."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return list(self._rows)


class _ThematicConn:
    """Fake conn routing the two READ_SLICE queries by SQL content:

      * the desk-roster query (``target_descriptors``) → desk rows;
      * the slice query (``analyst_outputs``) → escalation-unit head rows.
    """

    def __init__(
        self,
        *,
        roster: list[dict[str, Any]],
        slice_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._roster = roster
        self._slice_rows = slice_rows or []
        self.roster_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.slice_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        if "target_descriptors" in query:
            self.roster_calls.append((query, params))
            return list(self._roster)
        self.slice_calls.append((query, params))
        return list(self._slice_rows)


def _thematic_descriptor() -> SimpleNamespace:
    """A thematic escalation_composition descriptor stub: NO targets block (global
    run), other_analysts=[escalation], substrate.thematic_dimension='escalation',
    and method.llm.verify present (it is ALSO verify-declaring, like the world)."""
    return SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=[
                SimpleNamespace(id="escalation", time_window="24h", data_types=[])
            ],
            substrate={"direct_queries": False, "thematic_dimension": "escalation"},
            targets=None,
        ),
        method=SimpleNamespace(llm={"verify": {"factory_kind": "stack_ref"}}),
    )


class _CannedLLM:
    """LLM double returning a fixed payload; captures the system prompt."""

    subprovider = "escalation_test_double"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Any],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": list(messages), "system": system})

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50
            reasoning_tokens = 0

        resp = SimpleNamespace()
        resp.content = json.dumps(self._payload)
        resp.usage = _Usage()
        return resp


class _NeverCalledLLM:
    subprovider = "never_called"

    async def chat_complete(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        raise AssertionError("LLM must not be called on the empty-slice path")


class _Deps:
    def __init__(self, llm: Any) -> None:
        self.llm = llm


def _escalation_head_row(
    *,
    uid: UUID,
    target_id: str,
    title: str,
    effective_confidence: float = 0.6,
    derived_from: list[str] | None = None,
) -> dict[str, Any]:
    """A verify-floored escalation UNIT head row (as the thematic slice returns it —
    carries ``effective_confidence`` + ``faithfulness_score`` + ``derived_from``)."""
    return {
        "id": uid,
        "kind": "finding",
        "title": title,
        "body": f"{title} escalation read body",
        "confidence": 0.7,
        "effective_confidence": effective_confidence,
        "faithfulness_score": 0.9,
        "severity": None,
        "data": {"evidence": []},
        "evidence": [],
        "target_id": target_id,
        "target_version": None,
        "analyst_id": "escalation",
        "analyst_version": "vtest",
        "produced_at": "2026-07-02T00:00:00+00:00",
        "derived_from": list(derived_from or []),
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }


def _roster(*desks: tuple[str, str]) -> list[dict[str, Any]]:
    return [{"descriptor_id": did, "name": name} for did, name in desks]


_IN = "country_g20_in"
_DE = "country_g20_de"
_IR = "country_watch_ir"


def _thematic_options(**extra: Any) -> dict[str, Any]:
    opts = {
        "analyst_id": "escalation_composition",
        "thematic_dimension": "escalation",
        "run_id": uuid4(),
    }
    opts.update(extra)
    return opts


# ---------------------------------------------------------------------------
# thematic_dimension — the mode discriminator
# ---------------------------------------------------------------------------


def test_thematic_dimension_reads_substrate_marker():
    assert synth.thematic_dimension(_thematic_descriptor()) == "escalation"


def test_thematic_dimension_absent_returns_none():
    # A world-shaped descriptor (substrate has NO thematic_dimension) → None, so
    # it stays on the world/region/country/legacy branches.
    desc = SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=[SimpleNamespace(id="region_composition")],
            substrate={"direct_queries": False},
            targets=None,
        )
    )
    assert synth.thematic_dimension(desc) is None
    # An empty / whitespace marker is also None (never a spurious thematic run).
    blank = SimpleNamespace(
        subscription=SimpleNamespace(substrate={"thematic_dimension": "  "})
    )
    assert synth.thematic_dimension(blank) is None
    # No substrate at all → None.
    assert synth.thematic_dimension(SimpleNamespace(subscription=None)) is None


# ---------------------------------------------------------------------------
# read_other_analyst_findings — the dedupe_heads (one-head-per-desk) filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_reader_dedupe_heads_folds_one_head_per_desk():
    """``dedupe_heads=True`` forces the head-fold for a target-LESS analyst-dimension
    read: superseded folded + DISTINCT ON (analyst_id, target_id) → one head per
    desk. No target scope; the escalation unit is first-order → meta EXCLUDED."""
    conn = _CapturingConn(rows=[])
    await synth.read_other_analyst_findings(
        conn,
        analyst_ids=["escalation"],
        verify_floor=0.0,
        dedupe_heads=True,
    )
    q, p = conn.calls[0]
    assert "f.superseded_by IS NULL" in q
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in q
    assert "JOIN LATERAL" in q                      # verify-floor gate
    assert "Faithfulness verify%" in q
    # target-LESS: no target scope on the dimension read.
    assert "f.target_id = $" not in q
    assert "f.target_id = ANY(" not in q
    # meta EXCLUDED (the unit is first-order) — the exclusion clause is present.
    assert "->> 'meta'" in q
    assert p[0] == ["escalation"]


# ---------------------------------------------------------------------------
# READ_SLICE thematic branch — one head per desk + roster gap diff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thematic_read_slice_reads_dimension_and_diffs_roster():
    """A target-less run whose descriptor carries the thematic marker reads the
    escalation heads across desks + diffs the g20+watch roster → present/gap."""
    desc = _thematic_descriptor()
    heads = [
        _escalation_head_row(uid=uuid4(), target_id=_IN, title="India"),
        _escalation_head_row(uid=uuid4(), target_id=_DE, title="Germany"),
    ]
    conn = _ThematicConn(
        roster=_roster((_IN, "India"), (_DE, "Germany"), (_IR, "Iran")),
        slice_rows=heads,
    )
    rows = await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)

    # (1) the slice reads the escalation DIMENSION, one head per desk.
    assert conn.slice_calls, "thematic run must read the escalation heads"
    sq, sp = conn.slice_calls[0]
    assert sp[0] == ["escalation"]
    assert "f.superseded_by IS NULL" in sq
    assert "DISTINCT ON (f.analyst_id, f.target_id)" in sq
    assert "JOIN LATERAL" in sq                     # verify-floored
    assert "->> 'meta'" in sq                       # meta EXCLUDED (unit first-order)
    assert "f.target_id = $" not in sq              # NOT target-scoped
    assert sp[-1] == synth.DEFAULT_VERIFY_FLOOR

    # (2) the g20+watch desk roster was resolved (thematic branch, NOT the world
    # region roster).
    assert conn.roster_calls, "thematic run must resolve the desk roster"
    rq, _ = conn.roster_calls[0]
    assert "array['g20', 'watch']" in rq

    # (3) coverage: IN/DE present, IR a NAMED gap — denormalized onto every row.
    assert rows and all(r.get("_desk_mode") == synth.THEMATIC_MODE_PRESENT for r in rows)
    cov = rows[0]["_thematic_coverage"]
    modes = {c["desk_id"]: c["mode"] for c in cov}
    assert modes == {
        _IN: synth.THEMATIC_MODE_PRESENT,
        _DE: synth.THEMATIC_MODE_PRESENT,
        _IR: synth.THEMATIC_MODE_GAP,
    }


@pytest.mark.asyncio
async def test_thematic_read_slice_no_heads_returns_empty_no_roster_diff():
    """ZERO escalation heads → empty slice (the actor NOOPs); no coverage stamp."""
    desc = _thematic_descriptor()
    conn = _ThematicConn(roster=_roster((_IN, "India")), slice_rows=[])
    rows = await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)
    assert rows == []


# ---------------------------------------------------------------------------
# _run — thematic prompt selection + cross-desk citations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thematic_run_selects_thematic_prompt_and_cites_desks():
    india, germany = uuid4(), uuid4()
    rows = [
        _escalation_head_row(uid=india, target_id=_IN, title="India"),
        _escalation_head_row(uid=germany, target_id=_DE, title="Germany"),
    ]
    body = (
        "BLUF: escalation risk is concentrated. India is escalating [[ref:1]], "
        "while Germany stays quiet [[ref:2]]. An unsupported aside [[ref:9]] must "
        "never be cited."
    )
    llm = _CannedLLM(
        {
            "title": "Global escalation read",
            "body": body,
            "confidence": 0.55,
            "evidence": ["risk concentrated in India"],
            "tags": ["escalation"],
        }
    )
    result = await synth.run_method(list(rows), _thematic_options(), _Deps(llm))

    # Prompt: the THEMATIC (escalation) prompt — NOT world/region/country/legacy.
    assert llm.calls, "thematic run must call the LLM"
    assert llm.calls[-1]["system"] == synth._THEMATIC_COMPOSITION_SYSTEM
    assert llm.calls[-1]["system"] != synth._WORLD_OVER_REGIONS_SYSTEM
    assert llm.calls[-1]["system"] != synth._COMPOSITION_SYSTEM
    assert llm.calls[-1]["system"] != synth._SYSTEM_PROMPT

    # Lineage: derived_from is EXACTLY the fused escalation head ids.
    assert set(result.derived_from) == {india, germany}

    # Citations: the two resolved ordinals (out-of-range one dropped); each carries
    # its desk (target_id) so the cross-desk read names which desk it rests on.
    cites = result.finding.data["citations"]
    assert {c["ref_id"] for c in cites} == {str(india), str(germany)}
    assert all(c["ref_kind"] == "finding" for c in cites)
    by_id = {str(india): _IN, str(germany): _DE}
    for c in cites:
        assert c["target_id"] == by_id[c["ref_id"]]
        assert c["source"] == "escalation"

    # A thematic run is NOT the world contested/region path.
    assert "contested" not in result.finding.data
    assert "region_coverage" not in result.finding.data


@pytest.mark.asyncio
async def test_thematic_run_names_desk_gap_in_prompt_and_data():
    """Degrade-not-drop: a desk with NO escalation head is NAMED as a gap — in the
    appended DESK COVERAGE prompt block AND stamped into data.desk_gaps."""
    india = uuid4()
    row = _escalation_head_row(uid=india, target_id=_IN, title="India")
    # READ_SLICE would denormalize this coverage onto the row; simulate it.
    row["_thematic_coverage"] = [
        {"desk_id": _IN, "desk_name": "India", "mode": synth.THEMATIC_MODE_PRESENT,
         "input_count": 1},
        {"desk_id": _IR, "desk_name": "Iran", "mode": synth.THEMATIC_MODE_GAP,
         "input_count": 0},
    ]
    body = "BLUF: only India is assessed [[ref:1]]."
    llm = _CannedLLM(
        {"title": "t", "body": body, "confidence": 0.4, "evidence": [], "tags": ["e"]}
    )
    result = await synth.run_method([row], _thematic_options(), _Deps(llm))

    # The DESK COVERAGE gap block reached the model, naming the gap desk.
    user_prompt = llm.calls[-1]["messages"][0]["content"]
    assert "DESK COVERAGE" in user_prompt
    assert "Iran" in user_prompt and _IR in user_prompt

    # data stamps the coverage + the named gap (machine-checkable auditability).
    assert result.finding.data["desk_gaps"] == ["Iran"]
    modes = {c["desk_id"]: c["mode"] for c in result.finding.data["desk_coverage"]}
    assert modes == {_IN: synth.THEMATIC_MODE_PRESENT, _IR: synth.THEMATIC_MODE_GAP}


# ---------------------------------------------------------------------------
# T7 CORRELATION GUARD — cross-desk shared-signal de-duplication (REQUIRED)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correlation_guard_fires_on_shared_signal_across_desks():
    """REQUIRED: two desks' escalation heads that cite the SAME underlying signal
    are ONE independent evidence unit — the guard FIRES (de-dupes + flags the
    correlation), the fused read does NOT double-count it, and the confidence is
    capped to the de-duplicated ceiling (never a sum / noisy-OR)."""
    shared_signal = str(uuid4())          # ONE wire signal both desk units cite
    india, germany = uuid4(), uuid4()
    rows = [
        _escalation_head_row(
            uid=india, target_id=_IN, title="India",
            effective_confidence=0.6, derived_from=[shared_signal, str(uuid4())],
        ),
        _escalation_head_row(
            uid=germany, target_id=_DE, title="Germany",
            effective_confidence=0.6, derived_from=[shared_signal],
        ),
    ]
    body = (
        "BLUF: a single cross-border incident drives risk. India is escalating "
        "[[ref:1]] and Germany is drawn in [[ref:2]]."
    )
    # The model over-claims 0.9 as if the two desks were independent corroboration.
    llm = _CannedLLM(
        {
            "title": "Global escalation read",
            "body": body,
            "confidence": 0.9,
            "evidence": [],
            "tags": ["escalation"],
        }
    )
    result = await synth.run_method(list(rows), _thematic_options(), _Deps(llm))

    guard = result.finding.data["correlation_guard"]
    # (1) the guard FIRED — shared lineage detected across the two desks.
    assert guard["shared_lineage_detected"] is True
    assert guard["cited_heads"] == 2
    # (2) the two heads COLLAPSE to ONE independent evidence unit (de-duped).
    assert guard["independent_components"] == 1
    grp = guard["correlated_groups"][0]
    assert set(grp["ordinals"]) == {1, 2}
    assert set(grp["desks"]) == {_IN, _DE}
    assert shared_signal in grp["shared_signals"]
    # (3) the de-duplicated ceiling is the single component's max eff (0.6) — NOT a
    # sum (1.2) or a noisy-OR that grows with the correlated duplicate.
    assert guard["dedup_confidence_ceiling"] == pytest.approx(0.6)
    # (4) the fused confidence is CAPPED to the ceiling (was 0.9) — not inflated.
    assert guard["confidence_capped"] is True
    assert guard["confidence_before"] == pytest.approx(0.9)
    assert result.finding.confidence == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_correlation_guard_independent_desks_not_capped():
    """Contrast: two desks on DISJOINT signals are independent — no correlation,
    no dedup, no cap (the ceiling is the max of the independent components)."""
    india, germany = uuid4(), uuid4()
    rows = [
        _escalation_head_row(
            uid=india, target_id=_IN, title="India",
            effective_confidence=0.6, derived_from=[str(uuid4())],
        ),
        _escalation_head_row(
            uid=germany, target_id=_DE, title="Germany",
            effective_confidence=0.5, derived_from=[str(uuid4())],
        ),
    ]
    body = "India is escalating [[ref:1]]; Germany is calm [[ref:2]]."
    llm = _CannedLLM(
        {"title": "t", "body": body, "confidence": 0.55, "evidence": [], "tags": ["e"]}
    )
    result = await synth.run_method(list(rows), _thematic_options(), _Deps(llm))

    guard = result.finding.data["correlation_guard"]
    assert guard["shared_lineage_detected"] is False
    assert guard["independent_components"] == 2
    assert guard["correlated_groups"] == []
    assert guard["dedup_confidence_ceiling"] == pytest.approx(0.6)
    # 0.55 < 0.6 → not capped.
    assert guard["confidence_capped"] is False
    assert result.finding.confidence == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# _correlation_guard — pure unit (no LLM, no _run)
# ---------------------------------------------------------------------------


def test_correlation_guard_pure_three_heads_two_correlated():
    """A three-way slice where heads 1 & 3 share a signal and head 2 is alone →
    two independent components; the shared component caps to its max eff."""
    s = "sig-shared"
    citations = [
        {"ordinal": 1, "target_id": _IN, "effective_confidence": 0.5,
         "derived_from": [s, "sig-a"]},
        {"ordinal": 2, "target_id": _DE, "effective_confidence": 0.8,
         "derived_from": ["sig-b"]},
        {"ordinal": 3, "target_id": _IR, "effective_confidence": 0.4,
         "derived_from": [s]},
    ]
    guard = synth._correlation_guard(citations)
    assert guard["cited_heads"] == 3
    assert guard["independent_components"] == 2       # {1,3} + {2}
    assert guard["shared_lineage_detected"] is True
    grp = guard["correlated_groups"][0]
    assert set(grp["ordinals"]) == {1, 3}
    assert set(grp["desks"]) == {_IN, _IR}
    assert grp["shared_signals"] == [s]
    # ceiling = max over INDEPENDENT components: comp{1,3} max eff 0.5, comp{2} 0.8
    # → 0.8 (the correlated pair collapses to its own max 0.5, NOT 0.5+0.4).
    assert guard["dedup_confidence_ceiling"] == pytest.approx(0.8)


def test_correlation_guard_no_effconf_leaves_ceiling_none():
    """HONESTY: with no effective_confidence on any citation, the guard fabricates
    NO ceiling (None) — nothing to cap against."""
    citations = [
        {"ordinal": 1, "target_id": _IN, "derived_from": ["x"]},
        {"ordinal": 2, "target_id": _DE, "derived_from": ["y"]},
    ]
    guard = synth._correlation_guard(citations)
    assert guard["dedup_confidence_ceiling"] is None
    assert guard["shared_lineage_detected"] is False


# ---------------------------------------------------------------------------
# honest-empty — no escalation heads ⇒ confidence 0.0, no LLM, no guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thematic_honest_empty_no_llm_no_guard():
    llm = _NeverCalledLLM()
    result = await synth.run_method([], _thematic_options(), _Deps(llm))
    assert result.finding.confidence == 0.0
    assert "empty_slice" in result.finding.tags
    assert result.derived_from == []
    assert "citations" not in result.finding.data
    assert "correlation_guard" not in result.finding.data
