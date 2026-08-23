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
    """ONE bump per train, ``<train date>/<n>`` — RUST-1, the EVIDENCE-BYTES
    fix (panel 2026-08-16 §V-1).

    Bumped from ``2026-08-15/1`` (Phase J) because the hard/soft SPLIT moves:
    the evidence map was shown to the judge ``ensure_ascii=True``-escaped
    while ``quote_corpus`` scored the UNESCAPED values, so a span copied
    VERBATIM from what the judge was shown could never resolve when it
    contained non-ASCII or crossed a newline — 36% of contradiction attempts
    failed to resolve their quote (77 ``judge_contradicted_unquoted`` vs
    114+21 resolved over 14 days). Now the render sites pass
    ``ensure_ascii=False`` AND the quote side un-escapes literal JSON string
    escapes before resolution (both renderings repaired; raw form tried
    first, so pure-ASCII single-line behavior is byte-identical). EXPECTED
    SHIFT: hard-fail count RISES and the unquoted demotion FALLS,
    concentrated on non-ASCII-heavy sources; the SCORE is unchanged by
    construction. Pooling across this boundary would read the severity-split
    correction as a fleet movement.
    """
    assert JUDGE_PIPELINE_VERSION == "2026-08-21/1"
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
    findings from the same deploy land in different populations.

    2026-08-15: the constant + its lineage banner moved to the sibling
    ``judge_pipeline_version`` module (the size-gate seam); ``verify``
    re-exports it, so the ONE assignment lives there and the historical import
    surface (``from ...verify import JUDGE_PIPELINE_VERSION``) is unchanged.
    """
    import legba.data.provenance.judge_pipeline_version as JPV
    import legba.data.provenance.verify as V

    assert isinstance(V.JUDGE_PIPELINE_VERSION, str)
    # The re-export IS the module constant — one stamp, two import paths.
    assert V.JUDGE_PIPELINE_VERSION == JPV.JUDGE_PIPELINE_VERSION
    inspect = __import__("inspect")
    # Exactly one assignment, in the extracted module; verify only imports.
    assert inspect.getsource(JPV).count("JUDGE_PIPELINE_VERSION = ") == 1
    assert inspect.getsource(V).count("JUDGE_PIPELINE_VERSION = ") == 0


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
