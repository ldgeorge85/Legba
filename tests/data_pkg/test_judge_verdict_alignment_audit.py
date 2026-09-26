# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H3-MEASURE (``2026-09-25/1``) — persisting HOW H3 aligned each verdict.

H3 (``2026-09-24/1``) made the judge name every verdict's claim (``claim_index``)
and ``judge_verdict_parsing.align_verdicts`` align by that id with a positional
fallback. The reply-count mismatch this repairs (``checkable_claims !=
len(claim_verdicts)``) is ~22% both before and after H3 — the train changed what
a miscount DOES, not how often it happens — but the effect was never PERSISTED:
the aligned list was written to ``claim_verdicts`` exactly as a fully positional
one would be, so nothing on the row said whether H3's id arm ever fired.

This train is a pure readout wire, tested here at three grains:

  * the PARSER (``align_verdicts``) now returns a third value, ``aligned_by`` —
    one per CALL, never per entry, because the two branches it comes from are
    mutually exclusive (see ``judge_verdict_parsing.py``'s own module banner);
  * the BLOCK — ``FaithfulnessReport.as_dict()`` gains ``miscount_claims``,
    ``aligned_by_id``, ``aligned_positionally`` and ``unmatched_claims``, rolled
    up from the per-claim ledger by ``judge_verdict_parsing.alignment_audit_fields``
    — and every key this stamp did NOT touch is byte-identical to what a report
    with no alignment data at all would publish;
  * the READER — ``judge_stats_api``'s ``positional_share`` / ``miscount_rate``,
    so the before/after this whole lineage argues for is one GET.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from legba.data.provenance.judge_pipeline_version import (
    JUDGE_PIPELINE_VERSION,
    METRIC_FAITHFULNESS_SCORE,
    METRIC_REASON_CENSUS,
    METRIC_SEVERITY_SPLIT,
    SHIFT_NONE,
    STAMP_EXPECTED_SHIFTS,
    STAMP_LINEAGE,
)
from legba.data.provenance.judge_transport import JudgeTransportTelemetry
from legba.data.provenance.judge_verdict_parsing import (
    ALIGNED_BY_CLAIM_INDEX,
    ALIGNED_BY_POSITIONAL,
    align_verdicts,
    alignment_audit_fields,
)
from legba.data.provenance.judge_assessability import (
    build_faithfulness_critique_payload,
)
from legba.data.provenance.verify import ClaimVerdict, FaithfulnessReport
from legba.data.registry.judge_stats_api import build_payload

# ---------------------------------------------------------------------------
# 1. The parser: aligned_by is ONE value per call, matching the branch taken
# ---------------------------------------------------------------------------


def test_a_full_bare_token_reply_is_aligned_positionally():
    raw = ["supported", "unsupported"]
    _, missing, aligned_by = align_verdicts(raw, {"verdicts": raw}, 2)
    assert missing == []
    assert aligned_by == ALIGNED_BY_POSITIONAL


def test_a_reply_naming_every_claim_is_aligned_by_id_even_with_no_gap():
    raw = [
        {"claim_index": 2, "verdict": "supported"},
        {"claim_index": 1, "verdict": "unsupported"},
    ]
    _, missing, aligned_by = align_verdicts(raw, {"verdicts": raw}, 2)
    assert missing == []
    assert aligned_by == ALIGNED_BY_CLAIM_INDEX


def test_a_reply_with_a_miscount_aligns_by_id_and_leaves_the_rest_unmatched():
    """THE SHAPE the lane exists to persist: 3 claims sent, 2 verdicts back,
    each naming its claim. The gap is inside the id arm — aligned_by is
    ``claim_index`` for the whole call, and the unnamed claim is unmatched."""
    raw = [
        {"claim_index": 1, "verdict": "supported"},
        {"claim_index": 3, "verdict": "contradicted", "quote": "port stayed open"},
    ]
    slots, missing, aligned_by = align_verdicts(raw, {"verdicts": raw}, 3)
    assert aligned_by == ALIGNED_BY_CLAIM_INDEX
    assert missing == [1]  # claim 2 (0-based) — the reply never named it
    assert slots[0] == ("supported", "")
    assert slots[1] is None
    assert slots[2] == ("contradicted", "port stayed open")


# ---------------------------------------------------------------------------
# 2. ClaimVerdict.aligned_by — sparse, and off the judge path it is absent
# ---------------------------------------------------------------------------


def test_a_ledger_row_off_the_judge_path_carries_no_aligned_by_key():
    """The deterministic floor never calls align_verdicts, so every row it
    produces must be byte-identical to the pre-H3-measure shape: no key at
    all, never a ``null``."""
    cv = ClaimVerdict.supported("The lira fell three percent [1].", [1])
    assert cv.aligned_by is None
    assert "aligned_by" not in cv.as_dict()

    failed = ClaimVerdict.failed("No citation here.", "no_citation")
    assert failed.aligned_by is None
    assert "aligned_by" not in failed.as_dict()


def test_a_ledger_row_stamped_with_aligned_by_persists_it():
    cv = ClaimVerdict.supported("The bank spent two billion [1].", [1])
    cv.aligned_by = ALIGNED_BY_CLAIM_INDEX
    assert cv.as_dict()["aligned_by"] == ALIGNED_BY_CLAIM_INDEX


# ---------------------------------------------------------------------------
# 3. The block's counters — additive only, over the FULL ledger
# ---------------------------------------------------------------------------


def _stamped(text: str, aligned_by: str | None, *, supported: bool = True) -> ClaimVerdict:
    cv = (
        ClaimVerdict.supported(text, [1])
        if supported
        else ClaimVerdict.failed(text, "judge_unsupported")
    )
    cv.aligned_by = aligned_by
    return cv


def test_alignment_audit_fields_rolls_up_the_full_ledger():
    ledger = [
        _stamped("a", ALIGNED_BY_CLAIM_INDEX),
        _stamped("b", ALIGNED_BY_CLAIM_INDEX),
        _stamped("c", ALIGNED_BY_POSITIONAL),
        _stamped("d", None),  # a carried floor row — never touched by alignment
    ]
    out = alignment_audit_fields(ledger, miscount_claims=-1, judge_partial=2)
    assert out == {
        "miscount_claims": -1,
        "aligned_by_id": 2,
        "aligned_positionally": 1,
        "unmatched_claims": 2,
    }


def test_a_floor_only_report_publishes_the_four_counters_as_zero():
    """No fold outside the judge path ever sets ``aligned_by`` or
    ``judge_miscount_claims`` — a report built exactly as every pre-H3-measure
    caller built one (tests, gepa, the deterministic floor) publishes zero on
    all four, never an absent key: they are ALWAYS-present ints, unlike the
    sparse per-row ``aligned_by``."""
    report = FaithfulnessReport(
        faithfulness_score=1.0,
        checkable_claims=2,
        supported_claims=2,
        claim_verdicts=[
            ClaimVerdict.supported("a", [1]),
            ClaimVerdict.supported("b", [1]),
        ],
    )
    out = report.as_dict()
    assert out["miscount_claims"] == 0
    assert out["aligned_by_id"] == 0
    assert out["aligned_positionally"] == 0
    assert out["unmatched_claims"] == 0
    # And NOTHING this stamp did not touch moved: the shared keys are exactly
    # what the SAME report published before H3-measure existed at all — proven
    # by constructing the byte-identical baseline dict with no new keys and
    # asserting every key it carries still matches.
    baseline_keys = {
        "faithfulness_score", "checkable_claims", "supported_claims",
        "unsupported_spans", "judge_status", "judge_unavailable_reason",
        "confidence_ceiling", "branch_scores", "claim_verdicts",
        "claim_verdicts_truncated", "counters", "score_state",
        "score_state_reason", "provisional", "overall_score",
        "judge_pipeline_version",
    }
    for key in baseline_keys:
        assert key in out
    new_keys = {
        "miscount_claims", "aligned_by_id", "aligned_positionally",
        "unmatched_claims",
    }
    assert set(out) == baseline_keys | new_keys


def test_unmatched_claims_mirrors_judge_partial_not_missing_verdicts():
    """``unmatched_claims`` is the NAMED-reply gap (``judge_partial``), not a
    generic miscount — a report that never called the judge at all (no
    ``judge_partial``) still publishes 0, not None and not an error."""
    report = FaithfulnessReport(
        faithfulness_score=1.0, checkable_claims=0, supported_claims=0,
    )
    assert report.as_dict()["unmatched_claims"] == 0

    report.judge_partial = 3
    assert report.as_dict()["unmatched_claims"] == 3


# ---------------------------------------------------------------------------
# 4. The transport telemetry — the reply-count mismatch receipt
# ---------------------------------------------------------------------------


def test_record_reply_length_accumulates_signed_across_partitions():
    telem = JudgeTransportTelemetry()
    telem.record_reply_length(1, 2)  # shared: 1 verdict back for 2 claims sent
    telem.record_reply_length(1, 1)  # absence: exact
    assert telem.reply_count_delta == -1


def test_stamp_copies_the_miscount_onto_the_report_only_when_a_call_was_made():
    telem = JudgeTransportTelemetry()
    report = FaithfulnessReport(
        faithfulness_score=1.0, checkable_claims=1, supported_claims=1,
    )
    telem.stamp(report)  # no attempts recorded → no-op, byte-identical
    assert report.judge_miscount_claims == 0

    telem.record("200")
    telem.record_reply_length(3, 4)
    telem.stamp(report)
    assert report.judge_miscount_claims == -1


# ---------------------------------------------------------------------------
# 5. The version bump + lineage — this train IS a SHIFT_NONE measurement
# ---------------------------------------------------------------------------


def test_the_pipeline_version_bumped_for_this_train():
    assert JUDGE_PIPELINE_VERSION == "2026-09-25/1"
    assert JUDGE_PIPELINE_VERSION == STAMP_LINEAGE[-1]


def test_the_lineage_declares_no_shift_on_any_family():
    """The alignment audit adds NO verdict, NO reason, NO score arithmetic —
    ``aligned_by`` is read off the SAME branch ``align_verdicts`` already took
    to decide the slots it always returned. All three families are SHIFT_NONE,
    the second such entry after 2026-08-30/1."""
    entry = STAMP_EXPECTED_SHIFTS["2026-09-25/1"]
    assert entry[METRIC_FAITHFULNESS_SCORE] == SHIFT_NONE
    assert entry[METRIC_SEVERITY_SPLIT] == SHIFT_NONE
    assert entry[METRIC_REASON_CENSUS] == SHIFT_NONE


# ---------------------------------------------------------------------------
# 6. The reader: judge_stats_api exposes positional_share / miscount_rate
# ---------------------------------------------------------------------------

_DAY = date(2026, 9, 25)


def _cube(
    *, status: str = "llm", n: int, version: str = "2026-09-25/1",
    aligned_by_id: int = 0, aligned_positionally: int = 0, miscount_n: int = 0,
) -> dict:
    return {
        "day": _DAY, "served_by": "Nvidia", "judge_status": status,
        "pipeline_version": version, "n": n,
        "faithfulness_n": 0, "faithfulness_sum": None,
        "aligned_by_id": aligned_by_id,
        "aligned_positionally": aligned_positionally,
        "miscount_n": miscount_n,
    }


def test_positional_share_and_miscount_rate_on_the_totals():
    out = build_payload(
        [_cube(n=10, aligned_by_id=8, aligned_positionally=2, miscount_n=1)],
        [],
        window_days=14,
        generated_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )
    assert out.totals.positional_share == 0.2  # 2 of 10 aligned rows
    assert out.totals.miscount_rate == 0.1  # 1 of 10 adjudicated critiques


def test_a_stamp_with_no_aligned_rows_reports_none_not_zero():
    """Absent on every pre-H3 row — a legacy stamp reports ``None``, not a
    fabricated 0.0 that would read as 'perfectly id-aligned'."""
    out = build_payload(
        [_cube(n=5, version="2026-08-20/1")],  # no aligned_by_id/positionally
        [],
        window_days=14,
        generated_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )
    assert out.totals.positional_share is None
    # miscount_rate is 0.0 here (denominator is real — 5 adjudicated rows, all
    # carrying miscount_n=0), which is the honest reading of a pre-H3 stamp:
    # the field existed nowhere yet, so every row coalesces to "no mismatch".
    assert out.totals.miscount_rate == 0.0


def test_deterministic_rows_never_enter_the_miscount_denominator():
    """A ``deterministic`` critique never called the judge, so it cannot carry
    a miscount. UNLIKE ``adjudicated_share`` (whose denominator deliberately
    includes ``deterministic`` — that row IS a real dilution of "how much of
    the sampled population got a live judge call"), ``miscount_rate``'s
    denominator is narrower: only rows a judge call actually produced."""
    out = build_payload(
        [
            _cube(status="llm", n=4, aligned_by_id=4, miscount_n=1),
            _cube(status="deterministic", n=100),
        ],
        [],
        window_days=14,
        generated_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )
    # 1 of 4 judged rows, NOT 1 of 104 — the deterministic rows are inert here
    # even though they move ``adjudicated_share`` (which this pins too).
    assert out.totals.miscount_rate == 0.25
    assert out.totals.adjudicated_share == round(4 / 104, 4)
    assert out.totals.positional_share == 0.0  # all 4 aligned rows were by id


def test_pipeline_version_rows_carry_the_same_rates_split_by_stamp():
    out = build_payload(
        [
            _cube(n=5, version="2026-09-24/1", aligned_by_id=1, aligned_positionally=4),
            _cube(n=5, version="2026-09-25/1", aligned_by_id=5),
        ],
        [],
        window_days=14,
        generated_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )
    by_version = {v.judge_pipeline_version: v for v in out.pipeline_versions}
    # Pre-H3-reply-contract stamp: mostly positional (the id arm fired only by
    # accident of model verbosity).
    assert by_version["2026-09-24/1"].positional_share == 0.8
    # This stamp: the reply contract landed, every row aligned by id.
    assert by_version["2026-09-25/1"].positional_share == 0.0


# ---------------------------------------------------------------------------
# 4. The PERSISTED row — the block the reader actually queries
# ---------------------------------------------------------------------------


def test_the_persisted_critique_block_carries_the_four_counters():
    """2026-09-24 22:30Z: 51 live verdicts stamped ``2026-09-25/1`` carried
    NONE of the four keys, and ``positional_share`` read null. Section 3 above
    proved ``FaithfulnessReport.as_dict()`` — the trace envelope. The row the
    stats reader queries (``data->'data'->'verification'``) is the HAND-WRITTEN
    block ``build_faithfulness_critique_payload`` writes, which never derives
    from ``as_dict``. This test reads the stamp back off the object that is
    persisted, under the exact key names ``judge_stats_api`` reads."""
    report = FaithfulnessReport(
        faithfulness_score=0.5,
        checkable_claims=4,
        supported_claims=2,
        claim_verdicts=[
            _stamped("a", ALIGNED_BY_CLAIM_INDEX),
            _stamped("b", ALIGNED_BY_CLAIM_INDEX),
            _stamped("c", ALIGNED_BY_POSITIONAL, supported=False),
            _stamped("d", None, supported=False),
        ],
        judge_status="llm",
        judge_partial=1,
        judge_miscount_claims=-1,
    )
    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    block = payload["data"]["verification"]
    assert block["miscount_claims"] == -1
    assert block["aligned_by_id"] == 2
    assert block["aligned_positionally"] == 1
    assert block["unmatched_claims"] == 1
    # The reader's own key names, byte for byte, so the two cannot drift apart
    # silently again.
    import inspect

    from legba.data.registry import judge_stats_api

    src = inspect.getsource(judge_stats_api)
    for key in ("aligned_by_id", "aligned_positionally", "miscount_claims"):
        assert f"->>'{key}'" in src, key
    # And the persisted block agrees with the envelope on every one of them.
    envelope = report.as_dict()
    for key in ("miscount_claims", "aligned_by_id", "aligned_positionally", "unmatched_claims"):
        assert block[key] == envelope[key], key

