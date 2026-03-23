"""
Unit tests for the cognitive architecture shared modules:
  - legba.shared.confidence
  - legba.shared.contradictions
  - legba.shared.lifecycle
  - legba.shared.schemas.cognitive

All functions under test are pure (no DB access).  Run with:
    PYTHONPATH=src pytest tests/test_cognitive.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.shared.confidence import (
    compute_composite_confidence,
    compute_corroboration,
    compute_temporal_freshness,
)
from legba.shared.contradictions import (
    CONTRADICTORY_PREDICATES,
    detect_contradiction,
    should_auto_create_hypothesis,
)
from legba.shared.lifecycle import (
    EventLifecycleStatus,
    check_transition,
)
from legba.shared.schemas.cognitive import (
    ConfidenceComponents,
    EvidenceItem,
    EventLifecycle,
    SignalProvenance,
)


# ===========================================================================
# confidence.py — compute_composite_confidence
# ===========================================================================


class TestCompositeConfidence:
    """Tests for the gatekeeper confidence formula."""

    def test_all_components_provided(self):
        """Full component dict produces a value in (0, 1)."""
        result = compute_composite_confidence({
            "source_reliability": 0.8,
            "classification_confidence": 0.9,
            "temporal_freshness": 0.7,
            "corroboration": 0.6,
            "specificity": 0.5,
        })
        assert 0.0 < result < 1.0
        # Gate = 0.8 * 0.9 = 0.72
        # Modifier = 0.4*0.7 + 0.35*0.6 + 0.25*0.5 = 0.28 + 0.21 + 0.125 = 0.615
        # Confidence = 0.72 * 0.615 = 0.4428
        assert result == pytest.approx(0.72 * 0.615, abs=1e-6)

    def test_gatekeeper_low_source_reliability(self):
        """Low source_reliability crushes output regardless of other components."""
        result = compute_composite_confidence({
            "source_reliability": 0.1,
            "classification_confidence": 1.0,
            "temporal_freshness": 1.0,
            "corroboration": 1.0,
            "specificity": 1.0,
        })
        # Gate = 0.1 * 1.0 = 0.1; Modifier = 0.4 + 0.35 + 0.25 = 1.0
        # Confidence = 0.1
        assert result == pytest.approx(0.1, abs=1e-6)

    def test_gatekeeper_low_classification_confidence(self):
        """Low classification_confidence crushes output regardless of other components."""
        result = compute_composite_confidence({
            "source_reliability": 1.0,
            "classification_confidence": 0.1,
            "temporal_freshness": 1.0,
            "corroboration": 1.0,
            "specificity": 1.0,
        })
        # Gate = 1.0 * 0.1 = 0.1
        assert result == pytest.approx(0.1, abs=1e-6)

    def test_missing_components_default(self):
        """Missing keys fall back to module defaults (source_reliability=0.5,
        classification_confidence=0.5, temporal_freshness=1.0, corroboration=0.0,
        specificity=0.5)."""
        result = compute_composite_confidence({})
        # Gate = 0.5 * 0.5 = 0.25
        # Modifier = 0.4*1.0 + 0.35*0.0 + 0.25*0.5 = 0.4 + 0.0 + 0.125 = 0.525
        # Confidence = 0.25 * 0.525 = 0.13125
        assert result == pytest.approx(0.25 * 0.525, abs=1e-6)

    def test_empty_dict_returns_reasonable_default(self):
        """Empty components dict still returns a finite number in [0, 1]."""
        result = compute_composite_confidence({})
        assert 0.0 <= result <= 1.0

    def test_all_zeros(self):
        """All components at 0.0 -> 0.0."""
        result = compute_composite_confidence({
            "source_reliability": 0.0,
            "classification_confidence": 0.0,
            "temporal_freshness": 0.0,
            "corroboration": 0.0,
            "specificity": 0.0,
        })
        assert result == 0.0

    def test_all_ones(self):
        """All components at 1.0 -> gate(1.0) * modifier(1.0) = 1.0."""
        result = compute_composite_confidence({
            "source_reliability": 1.0,
            "classification_confidence": 1.0,
            "temporal_freshness": 1.0,
            "corroboration": 1.0,
            "specificity": 1.0,
        })
        # Modifier = 0.4 + 0.35 + 0.25 = 1.0
        assert result == pytest.approx(1.0, abs=1e-6)


# ===========================================================================
# confidence.py — compute_temporal_freshness
# ===========================================================================


class TestTemporalFreshness:
    """Tests for the piecewise-linear freshness decay."""

    def test_fresh_signal_zero_hours(self):
        """A signal at the current timestamp -> freshness 1.0."""
        now = datetime.now(timezone.utc)
        assert compute_temporal_freshness(now, now=now) == pytest.approx(1.0)

    def test_24h_old(self):
        """24 hours old -> freshness 0.5."""
        now = datetime.now(timezone.utc)
        ts = now - timedelta(hours=24)
        assert compute_temporal_freshness(ts, now=now) == pytest.approx(0.5, abs=1e-6)

    def test_72h_old(self):
        """72 hours old -> freshness ~0.1."""
        now = datetime.now(timezone.utc)
        ts = now - timedelta(hours=72)
        assert compute_temporal_freshness(ts, now=now) == pytest.approx(0.1, abs=1e-6)

    def test_168h_old_and_beyond(self):
        """168 hours (1 week) or older -> freshness 0.0."""
        now = datetime.now(timezone.utc)
        ts_168 = now - timedelta(hours=168)
        assert compute_temporal_freshness(ts_168, now=now) == 0.0
        ts_500 = now - timedelta(hours=500)
        assert compute_temporal_freshness(ts_500, now=now) == 0.0

    def test_future_timestamp_clamps_to_1(self):
        """A timestamp in the future (hours < 0) clamps to freshness 1.0."""
        now = datetime.now(timezone.utc)
        future = now + timedelta(hours=10)
        # hours = max((now - future).total_seconds()/3600, 0.0) = 0.0
        assert compute_temporal_freshness(future, now=now) == pytest.approx(1.0)

    def test_intermediate_12h(self):
        """12 hours is halfway between 0h (1.0) and 24h (0.5) -> 0.75."""
        now = datetime.now(timezone.utc)
        ts = now - timedelta(hours=12)
        assert compute_temporal_freshness(ts, now=now) == pytest.approx(0.75, abs=1e-6)

    def test_intermediate_48h(self):
        """48 hours is halfway between 24h (0.5) and 72h (0.1) -> 0.3."""
        now = datetime.now(timezone.utc)
        ts = now - timedelta(hours=48)
        assert compute_temporal_freshness(ts, now=now) == pytest.approx(0.3, abs=1e-6)


# ===========================================================================
# confidence.py — compute_corroboration
# ===========================================================================


class TestCorroboration:
    """Tests for the source-count-to-corroboration mapping."""

    def test_zero_sources(self):
        assert compute_corroboration(0) == 0.0

    def test_one_source(self):
        assert compute_corroboration(1) == 0.3

    def test_two_sources(self):
        assert compute_corroboration(2) == 0.6

    def test_three_sources(self):
        assert compute_corroboration(3) == 0.8

    def test_four_sources(self):
        assert compute_corroboration(4) == 0.9

    def test_five_plus_sources(self):
        assert compute_corroboration(5) == 1.0
        assert compute_corroboration(10) == 1.0
        assert compute_corroboration(100) == 1.0

    def test_negative_input_clamps_to_zero(self):
        """Negative source counts should map to 0.0 via dict miss."""
        assert compute_corroboration(-1) == 0.0
        assert compute_corroboration(-999) == 0.0


# ===========================================================================
# contradictions.py — detect_contradiction
# ===========================================================================


class TestDetectContradiction:
    """Tests for predicate and value contradiction detection."""

    def test_allied_vs_hostile_detected(self):
        """AlliedWith and HostileTo are contradictory (same subject + value)."""
        existing = [{
            "id": 1,
            "subject": "Iran",
            "predicate": "HostileTo",
            "value": "Russia",
            "confidence": 0.7,
        }]
        result = detect_contradiction("Iran", "AlliedWith", "Russia", existing)
        assert len(result) == 1
        assert result[0]["contradiction_type"] == "predicate"
        assert result[0]["predicate"] == "HostileTo"

    def test_hostile_vs_allied_symmetric(self):
        """HostileTo vs AlliedWith is also detected (symmetry check)."""
        existing = [{
            "id": 2,
            "subject": "Iran",
            "predicate": "AlliedWith",
            "value": "Russia",
            "confidence": 0.8,
        }]
        result = detect_contradiction("Iran", "HostileTo", "Russia", existing)
        assert len(result) == 1
        assert result[0]["contradiction_type"] == "predicate"

    def test_supports_vs_opposes_not_in_canonical(self):
        """Supports/Opposes are not in the canonical predicate set —
        no contradiction detected (they'd need to be added to the map)."""
        existing = [{
            "id": 3,
            "subject": "USA",
            "predicate": "Opposes",
            "value": "Treaty",
            "confidence": 0.6,
        }]
        result = detect_contradiction("USA", "Supports", "Treaty", existing)
        # Neither 'Supports' nor 'Opposes' are in CONTRADICTORY_PREDICATES keys,
        # so the lookup returns frozenset() and no contradiction fires.
        assert len(result) == 0

    def test_member_of_vs_withdrew_from(self):
        """MemberOf contradicts WithdrewFrom."""
        existing = [{
            "id": 4,
            "subject": "UK",
            "predicate": "WithdrewFrom",
            "value": "EU",
            "confidence": 0.95,
        }]
        result = detect_contradiction("UK", "MemberOf", "EU", existing)
        assert len(result) == 1
        assert result[0]["contradiction_type"] == "predicate"

    def test_no_contradiction_when_compatible(self):
        """Compatible predicates (AlliedWith + TradesWith) produce no contradiction."""
        existing = [{
            "id": 5,
            "subject": "France",
            "predicate": "TradesWith",
            "value": "Germany",
            "confidence": 0.9,
        }]
        result = detect_contradiction("France", "AlliedWith", "Germany", existing)
        assert len(result) == 0

    def test_value_contradiction_single_valued_predicate(self):
        """LeaderOf is single-valued — different value triggers value contradiction."""
        existing = [{
            "id": 6,
            "subject": "France",
            "predicate": "LeaderOf",
            "value": "Macron",
            "confidence": 0.9,
        }]
        result = detect_contradiction("France", "LeaderOf", "Dupont", existing)
        assert len(result) == 1
        assert result[0]["contradiction_type"] == "value"
        assert result[0]["value"] == "Macron"

    def test_no_value_contradiction_multi_valued_predicate(self):
        """AlliedWith is multi-valued — different values are NOT contradictory."""
        existing = [{
            "id": 7,
            "subject": "France",
            "predicate": "AlliedWith",
            "value": "Germany",
            "confidence": 0.9,
        }]
        result = detect_contradiction("France", "AlliedWith", "UK", existing)
        assert len(result) == 0

    def test_empty_existing_facts(self):
        """No existing facts -> no contradictions."""
        result = detect_contradiction("X", "AlliedWith", "Y", [])
        assert result == []

    def test_case_insensitive_subject_matching(self):
        """Subject matching is case-insensitive."""
        existing = [{
            "id": 8,
            "subject": "IRAN",
            "predicate": "HostileTo",
            "value": "Russia",
            "confidence": 0.7,
        }]
        result = detect_contradiction("iran", "AlliedWith", "russia", existing)
        assert len(result) == 1

    def test_predicate_is_case_sensitive(self):
        """Predicates are case-sensitive — 'alliedwith' won't match 'AlliedWith'."""
        existing = [{
            "id": 9,
            "subject": "Iran",
            "predicate": "hostileto",  # lowercase — not in CONTRADICTORY_PREDICATES
            "value": "Russia",
            "confidence": 0.7,
        }]
        # "AlliedWith" has contra set {"HostileTo", "SanctionedBy"},
        # but existing fact has "hostileto" (lowercase), so no match.
        result = detect_contradiction("Iran", "AlliedWith", "Russia", existing)
        assert len(result) == 0

    def test_different_subject_no_contradiction(self):
        """Different subjects never contradict each other."""
        existing = [{
            "id": 10,
            "subject": "China",
            "predicate": "HostileTo",
            "value": "Russia",
            "confidence": 0.7,
        }]
        result = detect_contradiction("Iran", "AlliedWith", "Russia", existing)
        assert len(result) == 0


# ===========================================================================
# contradictions.py — CONTRADICTORY_PREDICATES coverage
# ===========================================================================


class TestContradictoryPredicatesCoverage:
    """Verify the map covers expected pairs and is symmetric where declared."""

    def test_expected_pairs_present(self):
        """Key geopolitical predicate pairs are present."""
        assert "HostileTo" in CONTRADICTORY_PREDICATES["AlliedWith"]
        assert "AlliedWith" in CONTRADICTORY_PREDICATES["HostileTo"]
        assert "SanctionedBy" in CONTRADICTORY_PREDICATES["TradesWith"]
        assert "TradesWith" in CONTRADICTORY_PREDICATES["SanctionedBy"]
        assert "WithdrewFrom" in CONTRADICTORY_PREDICATES["MemberOf"]
        assert "MemberOf" in CONTRADICTORY_PREDICATES["WithdrewFrom"]
        assert "SignatoryTo" in CONTRADICTORY_PREDICATES["WithdrewFrom"]

    def test_symmetry(self):
        """If A lists B as contradictory, B should list A."""
        for pred_a, contra_set in CONTRADICTORY_PREDICATES.items():
            for pred_b in contra_set:
                assert pred_b in CONTRADICTORY_PREDICATES, (
                    f"{pred_b} is listed as contradictory to {pred_a} "
                    f"but has no entry in CONTRADICTORY_PREDICATES"
                )
                assert pred_a in CONTRADICTORY_PREDICATES[pred_b], (
                    f"{pred_a} contradicts {pred_b} but not vice versa"
                )


# ===========================================================================
# contradictions.py — should_auto_create_hypothesis
# ===========================================================================


class TestShouldAutoCreateHypothesis:
    """Tests for the hypothesis auto-creation trigger."""

    def test_both_above_threshold(self):
        """Both facts above confidence 0.5 and sufficient signal refs -> True."""
        assert should_auto_create_hypothesis(
            contradiction={"confidence": 0.8},
            new_fact={"confidence": 0.7},
            signal_ref_count=3,
        ) is True

    def test_old_fact_below_threshold(self):
        """Existing fact at or below 0.5 -> False."""
        assert should_auto_create_hypothesis(
            contradiction={"confidence": 0.5},
            new_fact={"confidence": 0.8},
            signal_ref_count=5,
        ) is False

    def test_new_fact_below_threshold(self):
        """New fact at or below 0.5 -> False."""
        assert should_auto_create_hypothesis(
            contradiction={"confidence": 0.9},
            new_fact={"confidence": 0.5},
            signal_ref_count=5,
        ) is False

    def test_insufficient_signal_references(self):
        """Even with high confidence, insufficient signals -> False."""
        assert should_auto_create_hypothesis(
            contradiction={"confidence": 0.9},
            new_fact={"confidence": 0.9},
            signal_ref_count=1,
            min_signal_refs=2,
        ) is False

    def test_exact_threshold_boundary(self):
        """Confidence of exactly 0.51 with min_signal_refs met -> True."""
        assert should_auto_create_hypothesis(
            contradiction={"confidence": 0.51},
            new_fact={"confidence": 0.51},
            signal_ref_count=2,
        ) is True

    def test_zero_signal_refs(self):
        """Zero signal refs -> False regardless of confidence."""
        assert should_auto_create_hypothesis(
            contradiction={"confidence": 0.9},
            new_fact={"confidence": 0.9},
            signal_ref_count=0,
        ) is False


# ===========================================================================
# lifecycle.py — EventLifecycleStatus enum
# ===========================================================================


class TestEventLifecycleStatusEnum:
    """Verify the enum has all 6 expected values."""

    def test_has_all_six_values(self):
        expected = {"emerging", "developing", "active", "evolving", "resolved", "reactivated"}
        actual = {s.value for s in EventLifecycleStatus}
        assert actual == expected

    def test_string_coercion(self):
        """The enum is a str subclass — string comparisons work."""
        assert EventLifecycleStatus.EMERGING == "emerging"
        assert EventLifecycleStatus.ACTIVE == "active"


# ===========================================================================
# lifecycle.py — check_transition
# ===========================================================================


class TestCheckTransition:
    """Tests for the event lifecycle state machine.

    Time-dependent conditions use unittest.mock.patch to freeze
    datetime.now inside the lifecycle module.
    """

    # --- EMERGING transitions -----------------------------------------------

    def test_emerging_to_developing_on_signal_count(self):
        """EMERGING with signal_count >= 3 -> DEVELOPING."""
        event = {
            "lifecycle_status": "emerging",
            "signal_count": 3,
            "confidence": 0.3,
            "last_signal_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
        }
        assert check_transition(event) == EventLifecycleStatus.DEVELOPING

    def test_emerging_to_resolved_no_signals_48h(self):
        """EMERGING with no signals in 48h -> RESOLVED."""
        old = datetime.now(timezone.utc) - timedelta(hours=49)
        event = {
            "lifecycle_status": "emerging",
            "signal_count": 1,
            "confidence": 0.2,
            "last_signal_at": old,
            "created_at": old,
        }
        assert check_transition(event) == EventLifecycleStatus.RESOLVED

    def test_emerging_stays_when_recent_and_few_signals(self):
        """EMERGING with < 3 signals and signals within 48h -> None."""
        event = {
            "lifecycle_status": "emerging",
            "signal_count": 2,
            "confidence": 0.3,
            "last_signal_at": datetime.now(timezone.utc) - timedelta(hours=10),
            "created_at": datetime.now(timezone.utc) - timedelta(hours=12),
        }
        assert check_transition(event) is None

    def test_emerging_prefers_developing_over_resolved(self):
        """When both developing and resolved conditions met, developing wins
        (first-match ordering)."""
        # signal_count >= 3 AND last_signal > 48h ago
        old = datetime.now(timezone.utc) - timedelta(hours=50)
        event = {
            "lifecycle_status": "emerging",
            "signal_count": 5,
            "confidence": 0.4,
            "last_signal_at": old,
            "created_at": old,
        }
        # DEVELOPING check is first in TRANSITION_RULES, so it wins
        assert check_transition(event) == EventLifecycleStatus.DEVELOPING

    # --- DEVELOPING transitions ---------------------------------------------

    def test_developing_to_active(self):
        """DEVELOPING with signal_count >= 5 and confidence >= 0.6 -> ACTIVE."""
        event = {
            "lifecycle_status": "developing",
            "signal_count": 5,
            "confidence": 0.6,
            "last_signal_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc) - timedelta(hours=24),
        }
        assert check_transition(event) == EventLifecycleStatus.ACTIVE

    def test_developing_to_resolved_no_signals_72h(self):
        """DEVELOPING with no signals in 72h -> RESOLVED."""
        old = datetime.now(timezone.utc) - timedelta(hours=73)
        event = {
            "lifecycle_status": "developing",
            "signal_count": 3,
            "confidence": 0.4,
            "last_signal_at": old,
            "created_at": old,
        }
        assert check_transition(event) == EventLifecycleStatus.RESOLVED

    def test_developing_stays_when_not_enough_signals(self):
        """DEVELOPING with signal_count < 5 and recent signals -> None."""
        event = {
            "lifecycle_status": "developing",
            "signal_count": 4,
            "confidence": 0.5,
            "last_signal_at": datetime.now(timezone.utc) - timedelta(hours=10),
            "created_at": datetime.now(timezone.utc) - timedelta(hours=48),
        }
        assert check_transition(event) is None

    # --- ACTIVE transitions -------------------------------------------------

    def test_active_to_evolving_on_velocity(self):
        """ACTIVE with velocity_change > 2.0 -> EVOLVING."""
        event = {
            "lifecycle_status": "active",
            "signal_count": 10,
            "confidence": 0.8,
            "last_signal_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc) - timedelta(days=3),
            "velocity_change": 2.5,
        }
        assert check_transition(event) == EventLifecycleStatus.EVOLVING

    def test_active_to_resolved_no_signals_7_days(self):
        """ACTIVE with no signals in 7 days -> RESOLVED."""
        old = datetime.now(timezone.utc) - timedelta(days=8)
        event = {
            "lifecycle_status": "active",
            "signal_count": 10,
            "confidence": 0.8,
            "last_signal_at": old,
            "created_at": old - timedelta(days=5),
            "velocity_change": 0.0,
        }
        assert check_transition(event) == EventLifecycleStatus.RESOLVED

    def test_active_stays_when_recent_and_stable(self):
        """ACTIVE with recent signals and low velocity -> None."""
        event = {
            "lifecycle_status": "active",
            "signal_count": 10,
            "confidence": 0.8,
            "last_signal_at": datetime.now(timezone.utc) - timedelta(hours=12),
            "created_at": datetime.now(timezone.utc) - timedelta(days=5),
            "velocity_change": 0.5,
        }
        assert check_transition(event) is None

    # --- EVOLVING transitions -----------------------------------------------

    def test_evolving_to_active_velocity_stabilized(self):
        """EVOLVING with velocity_change < 1.5 -> ACTIVE."""
        event = {
            "lifecycle_status": "evolving",
            "signal_count": 15,
            "confidence": 0.8,
            "last_signal_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc) - timedelta(days=5),
            "velocity_change": 1.0,
        }
        assert check_transition(event) == EventLifecycleStatus.ACTIVE

    def test_evolving_stays_with_high_velocity(self):
        """EVOLVING with velocity_change >= 1.5 -> None."""
        event = {
            "lifecycle_status": "evolving",
            "signal_count": 15,
            "confidence": 0.8,
            "last_signal_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc) - timedelta(days=5),
            "velocity_change": 1.5,
        }
        assert check_transition(event) is None

    # --- REACTIVATED transitions --------------------------------------------

    def test_reactivated_to_developing_immediate(self):
        """REACTIVATED -> DEVELOPING (immediate, unconditional)."""
        event = {
            "lifecycle_status": "reactivated",
            "signal_count": 1,
            "confidence": 0.3,
            "last_signal_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
        }
        assert check_transition(event) == EventLifecycleStatus.DEVELOPING

    # --- RESOLVED transitions -----------------------------------------------

    def test_resolved_no_automatic_transition(self):
        """RESOLVED has no automatic outbound transitions.
        Reactivation is caller-driven (set to REACTIVATED externally)."""
        old = datetime.now(timezone.utc) - timedelta(days=30)
        event = {
            "lifecycle_status": "resolved",
            "signal_count": 20,
            "confidence": 0.9,
            "last_signal_at": old,
            "created_at": old - timedelta(days=60),
        }
        assert check_transition(event) is None

    # --- Edge cases ---------------------------------------------------------

    def test_unknown_status_returns_none(self):
        """An unrecognized lifecycle_status returns None gracefully."""
        event = {
            "lifecycle_status": "BOGUS_STATUS",
            "signal_count": 10,
            "confidence": 0.9,
            "last_signal_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
        }
        assert check_transition(event) is None

    def test_string_iso_timestamp_parsing(self):
        """check_transition handles ISO-8601 string timestamps."""
        old = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
        event = {
            "lifecycle_status": "emerging",
            "signal_count": 1,
            "confidence": 0.2,
            "last_signal_at": old,
            "created_at": old,
        }
        assert check_transition(event) == EventLifecycleStatus.RESOLVED


# ===========================================================================
# schemas/cognitive.py — Pydantic model tests
# ===========================================================================


class TestConfidenceComponentsSchema:
    """Tests for ConfidenceComponents Pydantic model."""

    def test_default_values(self):
        cc = ConfidenceComponents()
        assert cc.source_reliability == 0.5
        assert cc.classification_confidence == 0.5
        assert cc.temporal_freshness == 1.0
        assert cc.corroboration == 0.0
        assert cc.specificity == 0.5

    def test_custom_values(self):
        cc = ConfidenceComponents(
            source_reliability=0.9,
            classification_confidence=0.8,
            temporal_freshness=0.7,
            corroboration=0.6,
            specificity=0.4,
        )
        assert cc.source_reliability == 0.9
        assert cc.corroboration == 0.6

    def test_validation_rejects_out_of_range(self):
        """Values outside [0.0, 1.0] should raise a validation error."""
        with pytest.raises(Exception):
            ConfidenceComponents(source_reliability=1.5)
        with pytest.raises(Exception):
            ConfidenceComponents(corroboration=-0.1)


class TestEvidenceItemSchema:
    """Tests for EvidenceItem Pydantic model."""

    def test_with_signal_id(self):
        sid = uuid4()
        item = EvidenceItem(signal_id=sid, type="direct", confidence=0.8)
        assert item.signal_id == sid
        assert item.event_id is None
        assert item.url is None

    def test_with_event_id(self):
        eid = uuid4()
        item = EvidenceItem(event_id=eid, type="derived", confidence=0.6)
        assert item.event_id == eid
        assert item.signal_id is None

    def test_with_url_external(self):
        item = EvidenceItem(url="https://example.com/report", type="external")
        assert item.url == "https://example.com/report"
        assert item.type == "external"

    def test_default_values(self):
        item = EvidenceItem()
        assert item.signal_id is None
        assert item.event_id is None
        assert item.url is None
        assert item.type == "direct"
        assert item.confidence == 0.5
        assert item.added_cycle is None


class TestSignalProvenanceSchema:
    """Tests for SignalProvenance Pydantic model."""

    def test_default_empty_state(self):
        prov = SignalProvenance()
        assert prov.raw_source == ""
        assert prov.fetched_at is None
        assert prov.normalized_by == ""
        assert prov.classified_by == ""
        assert prov.classification_confidence == 0.0
        assert prov.ner_by == ""
        assert prov.entities_extracted == []
        assert prov.dedup_checked == {}
        assert prov.dedup_nearest == {}
        assert prov.embedded_by == ""
        assert prov.clustered_into is None
        assert prov.cluster_similarity == 0.0
        assert prov.validated_by is None
        assert prov.validation_verdict == {}

    def test_populated_state(self):
        prov = SignalProvenance(
            raw_source="https://feeds.example.com/rss",
            fetched_at=datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc),
            ner_by="spacy:en_core_web_lg:3.7",
            entities_extracted=["Iran", "Russia"],
            classification_confidence=0.85,
        )
        assert prov.raw_source == "https://feeds.example.com/rss"
        assert len(prov.entities_extracted) == 2
        assert prov.classification_confidence == 0.85


class TestEventLifecycleSchema:
    """Tests for EventLifecycle Pydantic model."""

    def test_default_emerging_status(self):
        lc = EventLifecycle()
        assert lc.status == "emerging"
        assert lc.changed_at is None

    def test_custom_status(self):
        now = datetime.now(timezone.utc)
        lc = EventLifecycle(status="active", changed_at=now)
        assert lc.status == "active"
        assert lc.changed_at == now
