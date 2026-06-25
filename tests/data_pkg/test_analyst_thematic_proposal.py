# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""thematic_proposal handler — proposes thematic frames for uncovered hot
situations (Phase 5b detect → propose → promote).

Pure-logic coverage of term extraction + the covered/uncovered + intensity-floor
filter + the synthetic (deps=None) summary path. The live read (situations +
active thematic targets via deps.pg_pool) is exercised against the running stack.
"""
from __future__ import annotations

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
