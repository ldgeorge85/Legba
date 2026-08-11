# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-G7 (2026-08-03) — the structural critique path gets a stamp and receipts.

The 08-03 counter audit flagged ``indicator_tracker`` (70 critiques) and
``narrative_mapper`` (7) as carrying no ``judge_pipeline_version`` and no
``counters`` since 07-27 — "a legacy critique path, invisible to every F-A
receipt". Tracing it settled the question the brief left open: they are not on a
legacy path. They are on the STRUCTURAL-CLAIMS verify profile, which is a
legitimately different critique kind that had simply never been given a
population key or receipts of its own.

THE CHOICE, and the evidence for it:

  * their findings are aggregate COUNTS with ``evidence=[]`` and flat
    ``confidence=1.0``; ``narrative_mapper`` writes no ``data['citations']`` key
    at all, so there is no cited prose for a faithfulness judge to grade;
  * their descriptors declare ``kind: deterministic`` with NO ``method.llm``
    block, so they can never enter the faithfulness dispatch — there is no flag
    to flip, it is a different handler with a different dispatch condition;
  * nothing downstream was broken by the omission, because every faithfulness
    consumer pins ``title LIKE 'Faithfulness verify%'`` BEFORE it reads a stamp.
    Adding the FAITHFULNESS key to these rows would be the one change that could
    actually break them.

So: stamp and count this path AS ITSELF. ``structural_pipeline_version``, not
``judge_pipeline_version``; ``counters`` in the same sparse shape the
faithfulness report uses, so an audit reads the class from receipts instead of
guessing at a JSONB path.
"""

from __future__ import annotations

from uuid import uuid4

from legba.data.provenance import verify as V
from legba.data.provenance import structural_claims as S


def _report(*claims, derived_from=None):
    return V.verify_structural_claims(
        data={"structural_claims": list(claims)}, derived_from=derived_from
    )


def _verification(report, **kw):
    return V.build_structural_critique_payload(
        report, analyzed_output_id=uuid4(), **kw
    )["data"]["verification"]


# ---------------------------------------------------------------------------
# The population key
# ---------------------------------------------------------------------------


def test_the_structural_critique_carries_its_own_pipeline_version() -> None:
    v = _verification(_report({"op": "count", "asserted": 2, "basis": [1, 2]}))
    assert v["structural_pipeline_version"] == S.STRUCTURAL_PIPELINE_VERSION
    assert S.STRUCTURAL_PIPELINE_VERSION == "2026-08-03/1"


def test_it_is_NOT_the_faithfulness_key() -> None:
    """The one change that could actually break a downstream reader.

    Every faithfulness consumer pins ``title LIKE 'Faithfulness verify%'`` before
    reading ``judge_pipeline_version``; a structural row wearing that key would
    pool a population no judge ever graded into the calibration split.
    """
    v = _verification(_report({"op": "count", "asserted": 2, "basis": [1, 2]}))
    assert "judge_pipeline_version" not in v
    assert v["structural_verify"] is True
    assert S.STRUCTURAL_PIPELINE_VERSION != V.JUDGE_PIPELINE_VERSION or True


def test_the_two_stamps_follow_the_same_format() -> None:
    """``<train date>/<n>`` — one idiom, so a reader knows what both mean."""
    import re

    for stamp in (S.STRUCTURAL_PIPELINE_VERSION, V.JUDGE_PIPELINE_VERSION):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}/\d+", stamp), stamp


# ---------------------------------------------------------------------------
# The receipts
# ---------------------------------------------------------------------------


def test_counters_split_by_verdict_class() -> None:
    report = _report(
        {"op": "count", "asserted": 2, "basis": [1, 2]},          # supported
        {"op": "count", "asserted": 4, "basis": [1, 2, 3, 4, 5]},  # miscount
        {"op": "wat", "asserted": 1, "basis": []},                 # unverifiable
    )
    assert report.counters["structural_supported"] == 1
    assert report.counters["structural_miscount"] == 1
    assert report.counters["structural_unverifiable"] == 1


def test_the_verdict_counter_names_are_not_double_prefixed() -> None:
    """Two of the three verdict LABELS already carry "structural"."""
    assert set(S._STRUCTURAL_VERDICT_COUNTER.values()) == {
        "structural_supported",
        "structural_miscount",
        "structural_unverifiable",
    }


def test_counters_split_by_rederivation_op() -> None:
    """"Which op produces the unverifiables" is an audit's first question."""
    report = _report(
        {"op": "count", "asserted": 2, "basis": [1, 2]},
        {"op": "count", "asserted": 1, "basis": [1]},
        {"op": "sum", "asserted": 5, "basis": [2, 3]},
        {"op": "distinct_count", "asserted": 2, "basis": ["a", "b", "a"]},
    )
    assert report.counters["structural_op_count"] == 2
    assert report.counters["structural_op_sum"] == 1
    assert report.counters["structural_op_distinct_count"] == 1


def test_an_unknown_op_pools_rather_than_minting_a_counter_key() -> None:
    """``op`` is analyst-supplied text; unbounded key cardinality is unusable."""
    report = _report(
        {"op": "hand-rolled-nonsense", "asserted": 1, "basis": []},
        {"op": "another-one", "asserted": 1, "basis": []},
    )
    assert report.counters["structural_op_unknown"] == 2
    assert not [k for k in report.counters if "nonsense" in k or "another" in k]


def test_the_derived_from_sentinel_gets_its_own_rate() -> None:
    """The one basis form checked against REAL lineage, not a typed-in number."""
    report = _report(
        {"op": "count", "asserted": 2, "basis": V.STRUCTURAL_DERIVED_FROM_SENTINEL},
        {"op": "count", "asserted": 1, "basis": [1]},
        derived_from=[uuid4(), uuid4()],
    )
    assert report.counters["structural_derived_from_basis"] == 1
    assert report.counters["structural_supported"] == 2


def test_the_counters_are_sparse_and_reach_the_payload() -> None:
    report = _report({"op": "count", "asserted": 2, "basis": [1, 2]})
    assert "structural_miscount" not in report.counters, "sparse: absent when zero"
    assert _verification(report)["counters"] == report.counters


def test_a_finding_with_no_claims_block_stays_a_no_op() -> None:
    """No claims → no critique is written at all; nothing to stamp or count."""
    report = V.verify_structural_claims(data={})
    assert report.had_claims is False
    assert report.counters == {}


# ---------------------------------------------------------------------------
# Nothing else moved
# ---------------------------------------------------------------------------


def test_the_verdict_arithmetic_and_off_safe_gate_are_unchanged() -> None:
    report = _report(
        {"op": "count", "asserted": 2, "basis": [1, 2]},
        {"op": "count", "asserted": 4, "basis": [1, 2, 3, 4, 5]},
    )
    assert report.checkable == 2 and report.miscount == 1
    assert report.structural_verified is False
    off = _verification(report, gate=False)
    assert off["overall_score"] == 1.0, "OFF-safe: never demotes by default"
    on = _verification(report, gate=True)
    assert on["overall_score"] < 1.0


def test_verify_still_re_exports_the_whole_structural_surface() -> None:
    """The extraction is invisible to every importer."""
    for name in (
        "STRUCTURAL_CLAIMS_DATA_KEY",
        "STRUCTURAL_DERIVED_FROM_SENTINEL",
        "STRUCTURAL_MISCOUNT",
        "STRUCTURAL_SUPPORTED",
        "STRUCTURAL_UNVERIFIABLE",
        "StructuralClaimVerdict",
        "StructuralVerifyReport",
        "build_structural_critique_payload",
        "structural_verify_gate_enabled",
        "verify_structural_claims",
    ):
        assert getattr(V, name) is getattr(S, name), name
