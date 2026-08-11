# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The JUDGE PIPELINE VERSION stamp (2026-07-31) — the population SPLIT key.

The verify gate is the product's keystone, so every structural change to it
ships behind ONE version stamp on the critique (the MATCHER_VERSION idiom).
Anything reading faithfulness history — band calibration, the gold-set loop, the
correctness scorer, the scorecard, the two-panel readout's own dossier query —
partitions on it, so critiques graded under different verify pipelines are never
POOLED.

That mattered for the 2026-07-31 train (V-F + V-C + V-D + V-B were expected to
shift mean faithfulness UPWARD, a MEASUREMENT CORRECTION rather than findings
getting better), and it matters differently for the 2026-08-02 F-A PRECISION
train, where the shift is NOT one-way: hard-fail COUNT falls sharply while mean
faithfulness may dip slightly, because W1's tighter route withdraws the V-B
supported overrides that were certifying claims the slice check did not cover.
Fewer false hard fails AND fewer unearned passes is the intended shape — and
only the split key makes it legible as that instead of as a quality movement.
"""

from __future__ import annotations

import re
from uuid import uuid4

from legba.data.provenance.models import CritiquePayload
from legba.data.provenance.verify import (
    JUDGE_PIPELINE_VERSION,
    build_faithfulness_critique_payload,
    verify_finding_faithfulness,
)


def test_version_value_and_shape() -> None:
    """ONE bump per train, ``<train date>/<n>`` — V-I1 guard 6.

    Bumped from ``2026-08-09/1`` (guard 5 + rec #8's NULL score) for one
    verify-BEHAVIOR change, the round-5 §10-5 class: the V-I1 confirmation
    fingerprint now reads PROSE DIRECTION. A claim taking one side of a
    direction axis (no-change/new, rise/fall, open/closed, begin/end,
    above/below, improve/worsen) whose "confirming" quote takes the OPPOSITE
    side about the same subject was never confirmed by it — the suppression
    withdraws. Critique ``037f769f`` ("no material change since the prior
    7 August read" suppressed by a quote reporting a casualty figure "absent
    from the prior 7 August read" — every numeral and the one endpoint match,
    the PROSE diverges) is the live case; the 69-pair replay under the
    2026-08-05/1 + 2026-08-09/1 stamps flips only it. Withdraw-only, like
    guard 5, so hard-fail count may RISE by exactly this class — a measurement
    correction; pooling across the boundary would read it as a fleet movement.
    """
    assert JUDGE_PIPELINE_VERSION == "2026-08-10/1"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}/\d+", JUDGE_PIPELINE_VERSION)


async def test_stamped_on_every_critique(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body="Alpha struck Bravo base on Monday [1].\n",
        citations=[{"marker": "[1]", "signal_id": str(uuid4())}],
    )
    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    verification = payload["data"]["verification"]
    assert verification["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION
    # The block still validates as a CritiquePayload (extra='forbid' at the top
    # level; ``data`` is open JSONB, which is where the stamp lives).
    CritiquePayload.model_validate(payload)


async def test_stamped_on_the_trace_envelope_too(monkeypatch) -> None:
    """``report.as_dict()`` is what the actor returns into the run trace — it
    records which verify pipeline produced the number, not only the critique."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(body="", citations=[])
    assert report.as_dict()["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION


def test_one_stamp_for_the_whole_train() -> None:
    """A single module constant — a per-call or per-kind stamp would let two
    findings from the same deploy land in different populations."""
    import legba.data.provenance.verify as V

    assert isinstance(V.JUDGE_PIPELINE_VERSION, str)
    src = __import__("inspect").getsource(V)
    # Exactly one assignment; every other occurrence is a read.
    assert src.count("JUDGE_PIPELINE_VERSION = ") == 1


# ---------------------------------------------------------------------------
# THE READERS (2026-08-02)
#
# This module's own docstring has always claimed that "anything reading
# faithfulness history ... partitions on it". That claim was FALSE for the
# stamp's whole life: repo-wide the only occurrences were the two writes in
# verify.py, this test file, and prose in STATUS/CHANGELOG. The population
# split key split nothing, while band calibration and the correctness scorer
# pooled populations straight across the 07-30 judge swap that moved mean
# faithfulness +7pp.
#
# A stamp with no reader is decoration. These pin that it has one.
# ---------------------------------------------------------------------------


def test_the_split_key_has_readers_not_just_writers() -> None:
    """The exact defect: writers, a test, and no consumer anywhere."""
    import inspect

    from legba.data.analysts.deterministic_handlers import (
        band_calibration_tracker,
        unit_correctness_scorer,
    )

    for mod in (band_calibration_tracker, unit_correctness_scorer):
        src = inspect.getsource(mod)
        assert "JUDGE_PIPELINE_VERSION" in src, (
            f"{mod.__name__} aggregates faithfulness-derived verdicts and must "
            "partition on the judge pipeline, not pool across a judge swap"
        )
        # ...and it must actually reach the SQL, not merely be imported.
        assert "judge_pipeline_version" in src, mod.__name__


def test_band_calibration_reports_its_population_boundary() -> None:
    """Filtering silently would trade one dishonesty for another: the readout
    has to say which population it covers and what it dropped."""
    from legba.data.analysts.deterministic_handlers import band_calibration_tracker

    summary = band_calibration_tracker.summarize_claims(
        [],
        lookback_days=365,
        population={
            "judge_pipeline_version": JUDGE_PIPELINE_VERSION,
            "excluded_pre_stamp": 7,
            "excluded_other_pipeline": 3,
        },
    )
    pop = summary["population"]
    assert pop["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION
    assert pop["excluded_pre_stamp"] == 7
    assert pop["excluded_other_pipeline"] == 3


def test_correctness_scorer_faithfulness_sql_filters_on_the_stamp() -> None:
    """The row's `data` column is the whole CritiquePayload dump, so the stamp
    is one level down at `data.data.verification` — an easy path to get wrong,
    and getting it wrong silently reverts to pooling everything."""
    from legba.data.analysts.deterministic_handlers import unit_correctness_scorer

    sql = unit_correctness_scorer._FAITHFULNESS_SQL
    assert "data->'data'->'verification'->>'judge_pipeline_version'" in sql
    # The exclusion counter reads the same path, so the two can never disagree.
    excluded = unit_correctness_scorer._FAITHFULNESS_EXCLUDED_SQL
    assert "data->'data'->'verification'->>'judge_pipeline_version'" in excluded
