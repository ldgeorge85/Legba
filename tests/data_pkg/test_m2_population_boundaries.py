# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M-2 — every faithfulness aggregate names its judge population.

The 2026-08-02 engine review found ``judge_pipeline_version`` written twice in
``verify.py``, asserted by one test, described in two docs as the thing that
stops pre/post populations pooling — and read by nothing. Band calibration, the
Brier plane, the scorecard and the gold-set loop all pooled across the 07-30
judge swap and the 07-31 train.

These tests pin the boundary at each aggregate, INCLUDING the one that is
deliberately not split: an unexplained absence of a split is indistinguishable
from an oversight, and the next reader would either "fix" it wrongly or file it
as a bug again.
"""

from __future__ import annotations

from legba.data import correctness_axis
from legba.data.provenance.verify import JUDGE_PIPELINE_VERSION


# ---------------------------------------------------------------------------
# The Brier plane — deliberately NOT split, and it says so
# ---------------------------------------------------------------------------


def _calibration_payload(**over):
    from legba.data.analysts.deterministic_handlers import calibration_tracking as ct

    kwargs = dict(
        brier=0.2, sample_size=10, reliability_bins=[], per_analyst={},
        rolling=[], drift_z=None, drift_threshold=2.0, resolution_sources={},
        self_consistency_only=False, brier_exogenous=0.2,
        brier_self_consistency=None, brier_pooled=0.2,
        exogenous_sample_size=10, self_consistency_fraction=0.0,
        insufficient_exogenous=False, forecast_acute={}, warnings=[],
        target_id=None,
    )
    kwargs.update(over)
    return ct._build_finding(**kwargs)


def test_brier_plane_declares_its_non_split_with_a_reason():
    """Silence would read as an oversight. The payload states the decision, the
    current stamp, and the one real coupling it refuses to fake a fix for."""
    payload = _calibration_payload()
    pop = payload.data["judge_pipeline_population"]
    assert pop["split_by_judge_pipeline_version"] is False
    assert pop["current_judge_pipeline_version"] == JUDGE_PIPELINE_VERSION
    # WHY: neither scored quantity comes from the judge...
    assert pop["scored_quantities_are_judge_produced"] is False
    # ...but the verify gate does decide which claims exist to resolve, and that
    # selection effect is acknowledged rather than silently corrected for.
    assert pop["verify_gate_selection_effect"] is True
    assert "SELECTION effect" in pop["note"]
    assert "not split" in pop["note"].lower()


def test_brier_plane_declaration_reaches_the_readable_body():
    body = _calibration_payload().body
    assert "judge_pipeline_population=not_split" in body
    assert JUDGE_PIPELINE_VERSION in body


def test_brier_plane_never_carries_the_operator_correctness_axis():
    """The standing never-pool rule at the Brier boundary (labels_api P2-5)."""
    correctness_axis.assert_not_pooled(
        _calibration_payload().data, what="the calibration_tracking payload"
    )


# ---------------------------------------------------------------------------
# Band calibration — split, with priors annotated
# ---------------------------------------------------------------------------


def test_band_calibration_never_carries_the_operator_correctness_axis():
    from legba.data.analysts.deterministic_handlers import (
        band_calibration_tracker as bct,
    )

    summary = bct.summarize_claims(
        [{"dimension": "escalation", "direction": "deterioration",
          "outcome_14": "held", "outcome_28": None}],
        lookback_days=14,
        population={"judge_pipeline_version": JUDGE_PIPELINE_VERSION},
    )
    correctness_axis.assert_not_pooled(
        summary, what="the band_calibration summary"
    )
    # Bands are ordinal verdicts, not probabilities — and not correctness either.
    assert summary["no_brier"] is True


def test_band_calibration_priors_carry_their_own_n_and_never_a_combined_rate():
    from legba.data.analysts.deterministic_handlers import (
        band_calibration_tracker as bct,
    )

    blocks = bct.summarize_prior_populations(
        [
            {"judge_pipeline_version": "2026-07-31/1", "dimension": "escalation",
             "direction": "deterioration", "outcome_14": "held",
             "outcome_28": None},
            {"judge_pipeline_version": None, "dimension": "escalation",
             "direction": "deterioration", "outcome_14": "reverted",
             "outcome_28": None},
        ],
        lookback_days=14,
    )
    assert len(blocks) == 2
    for block in blocks:
        # Each block is self-contained: its own stamp, its own n, its own rates.
        assert "judge_pipeline_version" in block
        assert block["claims_total"] == 1
        assert block["horizons"]["14d"]["persistence_rate"] in (0.0, 1.0)
        # No block reports anything about the OTHER population.
        assert "combined" not in block
        assert "pooled" not in block


# ---------------------------------------------------------------------------
# The scorecard rollup — every critic row names its judge
# ---------------------------------------------------------------------------


def test_scorecard_row_carries_the_split_key():
    from legba.data.registry.v3_api import ScorecardRow

    row = ScorecardRow(
        id="1", analyst_id="escalation", overall_score=0.92,
        produced_at="2026-08-03T00:00:00+00:00",
        judge_pipeline_version="2026-08-03/1",
    )
    assert row.judge_pipeline_version == "2026-08-03/1"
    # A pre-stamp row is honestly None, never coerced to the current version —
    # the UI must be able to render it as its own series.
    legacy = ScorecardRow(
        id="2", analyst_id="escalation", overall_score=0.84,
        produced_at="2026-07-20T00:00:00+00:00",
    )
    assert legacy.judge_pipeline_version is None


def test_scorecard_route_selects_the_split_key_from_the_verification_block():
    """Guards the SQL: the stamp lives at data.data.verification, one level down
    from the critique payload root, and reading it from the wrong nesting would
    silently return NULL for every row (which is exactly what 'no reader' looked
    like)."""
    import inspect

    from legba.data.registry import v3_api

    src = inspect.getsource(v3_api.build_v3_router)
    assert (
        "data->'data'->'verification'->>'judge_pipeline_version'" in src
    ), "the scorecard rollup must read the stamp from the verification block"


# ---------------------------------------------------------------------------
# The scorer's own faithfulness mean
# ---------------------------------------------------------------------------


def test_unit_scorer_faithfulness_sql_filters_and_groups_on_the_split_key():
    from legba.data.analysts.deterministic_handlers import (
        unit_correctness_scorer as ucs,
    )

    # The headline mean is FILTERED to one pipeline...
    assert "judge_pipeline_version' = $3" in ucs._FAITHFULNESS_SQL
    # ...and the priors are GROUPED by it, so each gets its own mean rather than
    # collapsing into a single excluded count.
    assert "GROUP BY 1" in ucs._FAITHFULNESS_PRIORS_SQL
    assert "avg(confidence)" in ucs._FAITHFULNESS_PRIORS_SQL
    # The excluded counter and the priors read the SAME complement, so the two
    # can be reconciled by a reader.
    for sql in (ucs._FAITHFULNESS_EXCLUDED_SQL, ucs._FAITHFULNESS_PRIORS_SQL):
        assert "COALESCE(" in sql and "<> $3" in sql
