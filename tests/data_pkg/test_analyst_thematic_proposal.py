# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""thematic_proposal handler — proposes thematic frames for uncovered hot
situations (Phase 5b detect → propose → promote).

Pure-logic coverage of term extraction + the covered/uncovered + intensity-floor
filter + the synthetic (deps=None) summary path. The live read (situations +
active thematic targets via deps.pg_pool) is exercised against the running stack.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from legba.data.analysts.deterministic import SUB_HANDLERS, OUTPUT_KIND_BY_SUB_HANDLER
from legba.data.analysts.deterministic_handlers import thematic_proposal as sd
from legba.runtime.analyst_method import AnalystMethodResult


def test_registered_in_dispatch_table():
    assert "thematic_proposal" in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER["thematic_proposal"].value == "finding"


def test_candidate_terms_drops_stopwords_and_short_and_caps():
    terms = sd.candidate_terms(
        "Iran escalates diplomatic protests against US naval attacks in the Gulf"
    )
    # stopwords (against, the, in) + sub-3-char (us) dropped; salient nouns kept.
    assert "against" not in terms and "the" not in terms
    assert "iran" in terms and "diplomatic" in terms
    assert len(terms) <= 6
    # most-distinctive (longest) first.
    assert terms[0] == "diplomatic"


def test_suggested_predicate_is_compilable_contains_any():
    from legba.data.predicates import compile_predicate, PredicateSurface

    pred = sd.suggested_predicate(["iran", "missile", "strike"])
    assert pred == 'contains_any(["iran", "missile", "strike"])'
    compile_predicate(pred, PredicateSurface.TARGET_SCOPE)  # must compile


def test_build_proposals_filters_floor_and_covered():
    rows = [
        {"situation_signature": "sig:a", "name": "Iran missile strike on US base",
         "intensity_score": 1.8},
        {"situation_signature": "sig:b", "name": "Quiet trade talks resume",
         "intensity_score": 0.4},   # below floor → skipped
        {"situation_signature": "sig:c", "name": "Sudan civil conflict deepens",
         "intensity_score": 1.6},
        # INFLECTED coverage (regression for the convergence bug): "Iranian
        # attacks" must count as covered by an "iran"/"attack" predicate even
        # though "iranian" is NOT a substring of "iran" (the old one-directional
        # check missed this → re-proposed forever).
        {"situation_signature": "sig:d", "name": "Saudi condemns Iranian attacks",
         "intensity_score": 1.7},
    ]
    # An active thematic target already frames iran/attack.
    covered = 'contains_any(["iran", "tehran"]) and contains_any(["attack", "war"])'
    props = sd._build_proposals(rows, covered_text=covered, floor=1.5)
    sigs = {p["situation_signature"] for p in props}
    assert "sig:c" in sigs          # uncovered + above floor → proposed
    assert "sig:a" not in sigs       # covered (bare iran/strike→missile... iran matches)
    assert "sig:b" not in sigs       # below the intensity floor
    assert "sig:d" not in sigs       # covered via inflection (iranian⊇iran, attacks⊇attack)
    # the proposal carries a usable suggested predicate + target id.
    c = next(p for p in props if p["situation_signature"] == "sig:c")
    assert c["suggested_predicate"].startswith("contains_any([")
    assert c["suggested_target_id"].startswith("situation_")


def test_suggested_target_id_no_collision_on_shared_term():
    """Two unrelated situations sharing a longest term get DISTINCT target ids
    (signature-hash suffix) so the operator doesn't see a collision."""
    rows = [
        {"situation_signature": "sig:1", "name": "Turkey pushes diplomatic role",
         "intensity_score": 1.6},
        {"situation_signature": "sig:2", "name": "India escalates diplomatic protest",
         "intensity_score": 1.6},
    ]
    props = sd._build_proposals(rows, covered_text="", floor=1.5)
    ids = {p["suggested_target_id"] for p in props}
    assert len(ids) == 2  # distinct despite both leading with "diplomatic"


async def test_handle_synthetic_summarizes_proposals():
    inputs = [
        {"situation_signature": "sig:x", "name": "Sahel insurgency spreads across borders",
         "intensity_score": 1.9},
    ]
    result = await sd.handle(inputs, {"analyst_id": "thematic_proposal"}, None)
    assert isinstance(result, AnalystMethodResult)
    data = result.finding.data
    assert data["sub_handler"] == "thematic_proposal"
    assert data["proposal_count"] == 1
    assert data["proposals"][0]["situation_signature"] == "sig:x"
    assert result.finding.kind_marker == "finding"
    assert "proposal" in result.finding.tags


async def test_handle_synthetic_no_proposals_when_all_below_floor():
    inputs = [{"situation_signature": "sig:lo", "name": "minor item", "intensity_score": 0.2}]
    result = await sd.handle(inputs, {}, None)
    assert result.finding.data["proposal_count"] == 0
    assert "No uncovered high-intensity" in result.finding.body


async def test_empty_proposal_run_is_trace_only():
    """A run with no candidate frames is suppressed from the feed
    (force_trace_only) so it doesn't repeat 'nothing to propose' every tick."""
    result = await sd.handle([], {"analyst_id": "thematic_proposal"}, None)
    assert result.force_trace_only is True


async def test_nonempty_proposals_emit_on_synthetic_path():
    """A run with candidate frames emits to the feed — the synthetic (deps=None)
    path has no prior to dedup against, so force_trace_only is False."""
    rows = [{"situation_signature": "sig:c", "name": "Sudan civil conflict deepens",
             "intensity_score": 1.6}]
    result = await sd.handle(rows, {"analyst_id": "thematic_proposal"}, None)
    assert result.finding.data["proposal_count"] >= 1
    assert result.force_trace_only is False


# ---------------------------------------------------------------------------
# A2 (verify-path structural fix, 2026-07-31) — real citations + derived_from.
#
# thematic_proposal shipped 100% citation-less (JUDGE_READOUT #1) despite
# HAVING evidence rows (the situations it read each carry a real ``id`` PK).
# These assert the producer now cites the situation directly and carries a
# real lineage, instead of the prior always-empty ``evidence``/``derived_from``.
# ---------------------------------------------------------------------------


def test_proposal_carries_situation_id_when_present():
    sid = uuid4()
    rows = [{"id": sid, "situation_signature": "sig:c",
             "name": "Sudan civil conflict deepens", "intensity_score": 1.6}]
    props = sd._build_proposals(rows, covered_text="", floor=1.5)
    assert len(props) == 1
    assert props[0]["situation_id"] == str(sid)


def test_proposal_situation_id_absent_when_row_carries_no_id():
    """The synthetic/unit-test path (no ``id`` on the row) degrades to
    ``situation_id: None`` — never a fabricated ref."""
    rows = [{"situation_signature": "sig:c", "name": "Sudan civil conflict deepens",
             "intensity_score": 1.6}]
    props = sd._build_proposals(rows, covered_text="", floor=1.5)
    assert props[0]["situation_id"] is None


def test_proposal_lineage_dedupes_and_skips_unresolvable():
    sid1, sid2 = uuid4(), uuid4()
    proposals = [
        {"situation_id": str(sid1)},
        {"situation_id": str(sid2)},
        {"situation_id": str(sid1)},   # duplicate — deduped
        {"situation_id": None},        # unresolvable — skipped
        {},                             # missing key — skipped
    ]
    lineage = sd.proposal_lineage(proposals)
    assert lineage == [sid1, sid2]
    assert all(isinstance(u, UUID) for u in lineage)


def test_build_finding_emits_citations_from_situation_ids():
    """ACCEPTANCE (A2): the finding's ``data.citations`` is non-empty and
    resolvable whenever the proposals carry real situation ids — the smallest
    correct fix to thematic_proposal's finding construction."""
    sid = uuid4()
    proposals = [sd._proposal("sig:c", "Sudan civil conflict deepens", 1.6, ["sudan"], sid)]
    finding = sd._build_finding(proposals)
    citations = finding.data["citations"]
    assert isinstance(citations, list) and citations
    assert citations[0]["ref_kind"] == "situation"
    assert citations[0]["ref_id"] == str(sid)


def test_build_finding_citations_empty_when_no_proposals():
    """Honest-empty: no proposals -> empty citations AND empty derived_from
    (never a violation of the non-empty-when-derived_from-is invariant)."""
    finding = sd._build_finding([])
    assert finding.data["citations"] == []
    assert sd.proposal_lineage([]) == []


async def test_handle_live_shaped_rows_set_real_derived_from_and_citations():
    """ACCEPTANCE (A2): a run over rows carrying real situation ids (the live
    shape — ``_resolve_pool`` now SELECTs ``id``) emits BOTH a non-empty
    ``derived_from`` lineage and a non-empty, resolvable ``data.citations`` —
    the finding previously carried NEITHER."""
    sid = uuid4()
    rows = [{"id": sid, "situation_signature": "sig:c",
             "name": "Sudan civil conflict deepens", "intensity_score": 1.6}]
    result = await sd.handle(rows, {"analyst_id": "thematic_proposal"}, None)
    assert result.derived_from == [sid]
    citations = result.finding.data["citations"]
    assert citations and citations[0]["ref_id"] == str(sid)
    # the non-empty-citations-when-derived_from-is-non-empty invariant (A2).
    assert bool(result.finding.data["citations"]) == bool(result.derived_from)
