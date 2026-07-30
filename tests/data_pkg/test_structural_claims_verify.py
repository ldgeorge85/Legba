# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C2b (P4-6) — the ``structural_claims`` verify profile.

Deterministic RE-DERIVATION of a structural finding's asserted quantities
(no LLM, no DB): a correct claim → ``structural_verified``; a miscounted one →
FLAGGED; a pure-telemetry finding (no claims block) → no-op; the badge derivation
flips; and the OFF-safe gate keeps the critique from demoting effective_confidence
by default. Also asserts the P2-4 faithfulness/fail_class machinery is UNCHANGED.
"""
from __future__ import annotations

from uuid import uuid4

from legba.data.provenance import verify as V
from legba.data.provenance.kinds import (
    STRUCTURAL_CLAIMS_VERIFY_ANALYSTS,
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
    structural_badge,
    structural_claims_verify_opt_in,
    verify_exempt_reason,
)


# ---------------------------------------------------------------------------
# Re-derivation core
# ---------------------------------------------------------------------------


def test_correct_sum_claim_is_structural_verified():
    """The geo_convergence rollup identity (formed = cell + country) re-derives."""
    data = {
        "structural_claims": [
            {
                "id": "formed_bins_rollup",
                "statement": "currently_formed_bins (5) = cell (2) + country (3)",
                "op": "sum",
                "asserted": 5,
                "basis": [2, 3],
            }
        ]
    }
    report = V.verify_structural_claims(data=data)
    assert report.had_claims is True
    assert report.checkable == 1
    assert report.supported == 1
    assert report.miscount == 0
    assert report.unverifiable == 0
    assert report.structural_verified is True
    assert report.score == 1.0


def test_miscounted_claim_is_flagged():
    """A finding whose headline number disagrees with its own recorded parts is
    FLAGGED (structural_miscount) — the analyst-miscount catch."""
    data = {
        "structural_claims": [
            {
                "id": "formed_bins_rollup",
                "op": "sum",
                "asserted": 6,           # WRONG — parts sum to 5
                "basis": [2, 3],
            }
        ]
    }
    report = V.verify_structural_claims(data=data)
    assert report.miscount == 1
    assert report.supported == 0
    assert report.structural_verified is False
    v = report.claim_verdicts[0]
    assert v.verdict == V.STRUCTURAL_MISCOUNT
    assert v.rederived == 5


def test_distinct_count_over_carrier_set():
    """The narrative-echo shape: asserted distinct-family/carrier count == the
    distinct members of the recorded set."""
    ok = {
        "structural_claims": [
            {"op": "distinct_count", "asserted": 3, "basis": ["news", "gis", "health", "news"]}
        ]
    }
    assert V.verify_structural_claims(data=ok).structural_verified is True
    bad = {
        "structural_claims": [
            {"op": "distinct_count", "asserted": 4, "basis": ["news", "gis", "health", "news"]}
        ]
    }
    assert V.verify_structural_claims(data=bad).miscount == 1


def test_distinct_count_with_field_projection():
    data = {
        "structural_claims": [
            {
                "op": "distinct_count",
                "field": "family",
                "asserted": 2,
                "basis": [
                    {"signal_id": "a", "family": "news"},
                    {"signal_id": "b", "family": "gis"},
                    {"signal_id": "c", "family": "news"},
                ],
            }
        ]
    }
    assert V.verify_structural_claims(data=data).structural_verified is True


def test_count_and_derived_from_sentinel():
    """``@derived_from`` re-derives against the finding's ACTUAL lineage ids."""
    df = [uuid4(), uuid4(), uuid4()]
    data = {
        "structural_claims": [
            {"op": "count", "asserted": 3, "basis": V.STRUCTURAL_DERIVED_FROM_SENTINEL}
        ]
    }
    assert V.verify_structural_claims(data=data, derived_from=df).structural_verified is True
    # A wrong contributor count against the real lineage is flagged.
    bad = {
        "structural_claims": [
            {"op": "count", "asserted": 9, "basis": V.STRUCTURAL_DERIVED_FROM_SENTINEL}
        ]
    }
    assert V.verify_structural_claims(data=bad, derived_from=df).miscount == 1


def test_equals_scalar_identity():
    data = {"structural_claims": [{"op": "equals", "asserted": "US", "basis": "US"}]}
    assert V.verify_structural_claims(data=data).structural_verified is True


def test_unverifiable_claims_never_fake_a_pass():
    """Unknown op / non-list basis / missing asserted are unverifiable — never a
    fabricated pass, and they keep the finding UN-certified (honest)."""
    for claim in (
        {"op": "median", "asserted": 3, "basis": [1, 2, 3]},   # unknown op
        {"op": "count", "asserted": 3, "basis": "not-a-list"},  # bad basis
        {"op": "sum", "basis": [1, 2]},                          # no asserted
        {"op": "distinct_count", "field": "x", "asserted": 1,
         "basis": [{"y": 1}]},                                   # field missing
        "not-an-object",
    ):
        report = V.verify_structural_claims(data={"structural_claims": [claim]})
        assert report.had_claims is True
        assert report.unverifiable == 1
        assert report.checkable == 0
        assert report.structural_verified is False
        assert report.claim_verdicts[0].verdict == V.STRUCTURAL_UNVERIFIABLE


def test_no_claims_block_is_a_noop():
    """A pure-telemetry finding (no structural_claims block) → had_claims False
    (the caller writes NO critique; the row keeps its honest structural badge)."""
    assert V.verify_structural_claims(data={"formed_fired": 2}).had_claims is False
    assert V.verify_structural_claims(data={"structural_claims": []}).had_claims is False
    assert V.verify_structural_claims(data=None).had_claims is False


def test_mixed_unverifiable_blocks_certification():
    """A supported claim alongside an unverifiable one is NOT structural_verified
    (we certify only when EVERY declared claim re-derived + matched)."""
    data = {
        "structural_claims": [
            {"op": "count", "asserted": 2, "basis": [1, 2]},
            {"op": "median", "asserted": 1, "basis": [1]},
        ]
    }
    report = V.verify_structural_claims(data=data)
    assert report.supported == 1
    assert report.unverifiable == 1
    assert report.structural_verified is False


# ---------------------------------------------------------------------------
# Critique payload + OFF-safe gate
# ---------------------------------------------------------------------------


def test_critique_payload_off_safe_by_default(monkeypatch):
    """Gate OFF (default): overall_score pinned to 1.0 so no consumer demotes a
    structural finding — but the structural_verified marker + ledger are shown."""
    monkeypatch.delenv("LEGBA_STRUCTURAL_VERIFY_GATE", raising=False)
    report = V.verify_structural_claims(
        data={"structural_claims": [{"op": "sum", "asserted": 6, "basis": [2, 3]}]}
    )  # a MISCOUNT (parts sum to 5)
    fid = uuid4()
    payload = V.build_structural_critique_payload(report, analyzed_output_id=fid)
    # OFF-safe: the gate JOIN key is 1.0 even though the claim is flagged.
    assert payload["overall_score"] == 1.0
    assert payload["data"]["verification"]["structural_verified"] is False
    assert payload["data"]["verification"]["structural_verify"] is True
    assert payload["data"]["verification"]["miscount_claims"] == 1
    # honest fraction still recorded for display / the gated path.
    assert payload["data"]["verification"]["structural_score"] == 0.0
    assert str(fid) == payload["analyzed_output_id"].__str__()
    assert payload["title"].startswith("Structural verify")


def test_critique_payload_gate_on_demotes(monkeypatch):
    """Gate ON: overall_score carries the honest fraction so a miscount demotes
    via the existing min(confidence, overall_score) gate."""
    monkeypatch.setenv("LEGBA_STRUCTURAL_VERIFY_GATE", "1")
    assert V.structural_verify_gate_enabled() is True
    report = V.verify_structural_claims(
        data={"structural_claims": [{"op": "sum", "asserted": 6, "basis": [2, 3]}]}
    )
    payload = V.build_structural_critique_payload(report, analyzed_output_id=uuid4())
    assert payload["overall_score"] == 0.0  # flagged → demotes when gated


def test_critique_payload_explicit_gate_override():
    report = V.verify_structural_claims(
        data={"structural_claims": [{"op": "sum", "asserted": 5, "basis": [2, 3]}]}
    )
    p_off = V.build_structural_critique_payload(report, analyzed_output_id=uuid4(), gate=False)
    p_on = V.build_structural_critique_payload(report, analyzed_output_id=uuid4(), gate=True)
    # A VERIFIED finding scores 1.0 either way (no demotion regardless of gate).
    assert p_off["overall_score"] == 1.0
    assert p_on["overall_score"] == 1.0
    assert p_off["data"]["verification"]["structural_verified"] is True


# ---------------------------------------------------------------------------
# Opt-in registry + badge derivation
# ---------------------------------------------------------------------------


def test_opt_in_registry_is_subset_of_exempt_set():
    """Drift guard — you cannot structurally-verify a NON-structural analyst, so
    every STRUCTURAL_CLAIMS_VERIFY analyst must also be verify-EXEMPT."""
    assert STRUCTURAL_CLAIMS_VERIFY_ANALYSTS <= STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
    # The claim-bearing structural analysts this wave opts in.
    assert "geo_convergence_scan" in STRUCTURAL_CLAIMS_VERIFY_ANALYSTS
    assert "indicator_tracker" in STRUCTURAL_CLAIMS_VERIFY_ANALYSTS
    assert "thematic_proposal" in STRUCTURAL_CLAIMS_VERIFY_ANALYSTS


def test_opt_in_helper():
    assert structural_claims_verify_opt_in("geo_convergence_scan") is True
    assert structural_claims_verify_opt_in("graph_mining") is False   # exempt but telemetry-only
    assert structural_claims_verify_opt_in("country_assessor") is False
    assert structural_claims_verify_opt_in(None) is False


def test_structural_badge_flip():
    """The badge flips to 'structural-verified' ONLY for a structural analyst
    WITH a passing structural critique; else honest 'structural'; None otherwise."""
    assert structural_badge("geo_convergence_scan", True) == "structural-verified"
    assert structural_badge("geo_convergence_scan", False) == "structural"
    assert structural_badge("geo_convergence_scan", None) == "structural"
    # A non-structural analyst is never badged, verdict or not.
    assert structural_badge("country_assessor", True) is None
    assert structural_badge(None, True) is None
    # Base reason is unchanged for the plain path.
    assert verify_exempt_reason("geo_convergence_scan") == "structural"


# ---------------------------------------------------------------------------
# End-to-end reference: geo_convergence_scan's rollup-identity claim shape
# ---------------------------------------------------------------------------


def test_geo_convergence_summary_emits_verifiable_claim():
    """geo_convergence_scan is a STRUCTURAL_CLAIMS_VERIFY_ANALYSTS member
    whose summary finding USED to carry this exact ``formed_bins_rollup``
    claim shape (``currently_formed_bins == cell_bins_formed +
    country_bins_formed``) via its own (now-removed) ``_build_summary``.

    2026-07-29: that summary role moved to ``alert_trigger_scan``'s own
    TRACE_ONLY receipt post-fold (geo_convergence_scan's ``handle()`` is now
    a deprecation stub emitting no structural_claims block at all — a
    no-op per the STRUCTURAL_CLAIMS_VERIFY_ANALYSTS registry's own contract:
    "an opted-in analyst whose finding carries no structural_claims block is
    a NO-OP"). This test is kept as the WORKED-EXAMPLE reference for the
    claim shape geo_convergence_scan's registry membership documents,
    constructed inline rather than via the removed function."""
    data = {
        "structural_claims": [
            {
                "id": "formed_bins_rollup",
                "statement": (
                    "currently_formed_bins (5) = cell_bins_formed (2) + "
                    "country_bins_formed (3)"
                ),
                "op": "sum",
                "asserted": 5,
                "basis": [2, 3],
            },
        ],
    }
    report = V.verify_structural_claims(data=data)
    assert report.had_claims is True
    assert report.structural_verified is True
    assert report.claim_verdicts[0].claim_id == "formed_bins_rollup"


# ---------------------------------------------------------------------------
# P2-4 is UNBROKEN — the faithfulness/fail_class machinery is untouched
# ---------------------------------------------------------------------------


def test_p2_4_fail_class_machinery_unchanged():
    """The structural profile is ADDITIVE — the P2-4 fail_class table + labels
    are byte-identical (no new reason leaked into the faithfulness taxonomy)."""
    assert V.fail_class_for_reason("no_citation") == V.FAIL_CLASS_SOFT
    assert V.fail_class_for_reason("unresolved_citation") == V.FAIL_CLASS_HARD
    assert V.fail_class_for_reason("judge_contradicted") == V.FAIL_CLASS_HARD
    # The structural reasons are NOT part of the faithfulness fail_class table
    # (they live in their own StructuralClaimVerdict.verdict namespace) — an
    # unknown reason conservatively classifies soft, never hard.
    assert V.fail_class_for_reason(V.STRUCTURAL_MISCOUNT) == V.FAIL_CLASS_SOFT


def test_faithfulness_floor_still_scores_citations():
    """The deterministic faithfulness floor is unchanged: a cited claim is
    supported, an uncited fact-assertion is not."""
    body = "Tehran resumed uranium enrichment at Fordow [1]."
    citations = [{"marker": "[1]", "signal_id": "sig-1"}]
    report = V._deterministic_floor(body, citations)
    assert report.supported_claims == 1
    assert report.checkable_claims == 1
    assert report.faithfulness_score == 1.0
