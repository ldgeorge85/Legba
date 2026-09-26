# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-3 — the validity harness.

The permuted control fires on a boilerplate matcher; ``instrument_status``
degrades to ``unvalidated`` when any arm is missing and to ``stale`` when a
graded arm's grades expired; a stale grade never counts as a pass; the sampler
is stratified, deterministic, and never displaces an operator grade.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from legba.data.analysts.deterministic_handlers import _reference_validity as rv
from legba.data import correctness_axis


NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def _ref(unit, target, items, status="ok"):
    return {"unit": unit, "target_id": target, "items": items, "status": status}


def _item(entities):
    return {"ordinal": 1, "headline": "h", "sentence": "s",
            "entities": list(entities), "urls": ["https://a.example/1"],
            "materiality": "high"}


# ---------------------------------------------------------------------------
# V1 — the permuted control
# ---------------------------------------------------------------------------


def test_permutation_pairs_each_reference_with_a_different_desk_same_unit():
    refs = [
        _ref("escalation", "country_watch_il", [_item(["Iran"])]),
        _ref("escalation", "country_g20_sa", [_item(["Yemen"])]),
        _ref("energy_security", "country_g20_us", [_item(["Iran"])]),  # alone
    ]
    pairs = rv.permute_references(refs)
    # The two escalation refs swap with each other; energy_security is alone and
    # contributes nothing (no other desk of its unit to swap with).
    assert len(pairs) == 2
    for ref, other in pairs:
        assert ref["target_id"] != other
        # same unit as the ref it was drawn from
        assert other in ("country_watch_il", "country_g20_sa")


def test_permuted_control_fires_on_a_boilerplate_matcher():
    """A permuted reference scored against a DIFFERENT desk should almost never
    look engaged. If it does — because every head shares the word "escalation"
    and a boilerplate matcher keys on it — the permuted rate rises above the bar
    and the instrument disarms itself that day."""
    refs = [
        _ref("escalation", "il", [_item(["Iran"])]),
        _ref("escalation", "sa", [_item(["Iran"])]),  # same entity -> cross-hit
    ]
    # Every OTHER desk's head names Iran, so a permuted item finds itself
    # "engaged" everywhere — exactly the boilerplate the control catches.
    heads = {
        ("il", "escalation"): {"prose": "iran iran iran", "cited": []},
        ("sa", "escalation"): {"prose": "iran iran iran", "cited": []},
    }
    slices = {
        ("il", "escalation"): [{"signal_id": uuid4(), "canonical_url": None,
                                "prose": "iran", "folds": {"iran"}}],
        ("sa", "escalation"): [{"signal_id": uuid4(), "canonical_url": None,
                                "prose": "iran", "folds": {"iran"}}],
    }
    rate, n = rv.permuted_attention_rate(
        refs, home_by_target={}, slices=slices, heads=heads, frames={},
    )
    assert n == 2
    assert rate is not None and rate > rv.V1_PERMUTED_BAR


def test_permuted_control_stays_low_on_a_discriminating_matcher():
    """The healthy case: the swapped reference IS carried by the other desk's
    slice (so it collects and the rate is defined), but that desk's head is
    about something else, so it does not engage — a low, real rate under bar."""
    refs = [
        _ref("escalation", "il", [_item(["North Korea"])]),
        _ref("escalation", "jp", [_item(["Iran"])]),
    ]
    # The permuted pairing scores IL's NK reference against JP's desk and JP's
    # Iran reference against IL's desk. Each OTHER slice carries the swapped
    # entity (so the item collects), but neither head names it (so it does not
    # engage) — the signature of a matcher that discriminates.
    heads = {
        ("il", "escalation"): {"prose": "israel coalition politics", "cited": []},
        ("jp", "escalation"): {"prose": "japan trade policy", "cited": []},
    }
    slices = {
        ("il", "escalation"): [{"signal_id": uuid4(), "canonical_url": None,
                                "prose": "iran enrichment", "folds": {"iran"}}],
        ("jp", "escalation"): [{"signal_id": uuid4(), "canonical_url": None,
                                "prose": "north korea missile",
                                "folds": {"northkorea"}}],
    }
    rate, n = rv.permuted_attention_rate(
        refs, home_by_target={}, slices=slices, heads=heads, frames={},
    )
    # Both items collect (2 collected) and neither engages -> pooled 0/2 = 0.0,
    # comfortably under bar.
    assert n == 2
    assert rate == 0.0


# ---------------------------------------------------------------------------
# instrument_status
# ---------------------------------------------------------------------------


def test_status_valid_only_when_all_three_arms_pass():
    status, reasons = rv.instrument_status(
        permuted=0.05, v2=0.9, v2_n=15,
        v3_precision=0.85, v3_recall=0.75, v3_n=25,
        had_stale_grades=False,
    )
    assert status == rv.STATUS_VALID
    assert reasons == []


def test_status_unvalidated_when_an_arm_is_missing():
    """The shipped default state: V1 passes (it is continuous and cheap) but no
    human has run V2/V3, so the instrument is UNVALIDATED and nothing may quote
    it or page on it."""
    status, reasons = rv.instrument_status(
        permuted=0.02, v2=None, v2_n=0,
        v3_precision=None, v3_recall=None, v3_n=0,
        had_stale_grades=False,
    )
    assert status == rv.STATUS_UNVALIDATED
    assert any("V2" in r for r in reasons)
    assert any("V3" in r for r in reasons)


def test_v1_is_a_hard_stop_even_when_v2_and_v3_pass():
    """A permuted control above bar means the matcher scores boilerplate, which
    makes every other arm's grade a grade of the wrong thing."""
    status, reasons = rv.instrument_status(
        permuted=0.40, v2=0.95, v2_n=20,
        v3_precision=0.9, v3_recall=0.8, v3_n=30,
        had_stale_grades=False,
    )
    assert status == rv.STATUS_UNVALIDATED
    assert any("boilerplate" in r for r in reasons)


def test_status_stale_is_distinct_from_unvalidated():
    """"We measured this and the measurement expired" and "we never measured
    this" call for different actions; collapsing them would hide a lapsed
    validation behind a never-started one."""
    status, reasons = rv.instrument_status(
        permuted=0.02, v2=None, v2_n=0,
        v3_precision=None, v3_recall=None, v3_n=0,
        had_stale_grades=True,
    )
    assert status == rv.STATUS_STALE


def test_a_tiny_n_pass_is_not_a_pass():
    """The tiny-n rule: a precision above bar on n=2 is not validated."""
    status, _r = rv.instrument_status(
        permuted=0.02, v2=1.0, v2_n=2,
        v3_precision=1.0, v3_recall=1.0, v3_n=2,
        had_stale_grades=False,
    )
    assert status != rv.STATUS_VALID


# ---------------------------------------------------------------------------
# The graded arms
# ---------------------------------------------------------------------------


def _grade(label, *, arm, engaged=None, pipeline="2026-09-05/1",
           age_days=1):
    snap = {"instrument": "desk_reference", "pipeline_version": pipeline}
    if arm == "v2":
        snap["v2"] = {"question": "q"}
    else:
        snap["v3"] = {"engaged": engaged}
    return {
        "id": uuid4(), "finding_id": uuid4(), "unit_analyst_id": "escalation",
        "target_id": "il", "label": label,
        "labeled_by": "operator",
        "labeled_at": NOW - timedelta(days=age_days),
        "finding_snapshot": snap,
    }


def test_v3_precision_recall_from_the_2x2():
    graded = [
        # instrument said gap (engaged False), human agreed -> TP
        _grade(correctness_axis.LABEL_CORRECT, arm="v3", engaged=False),
        # instrument said gap, human overturned -> FP (desk did engage)
        _grade(correctness_axis.LABEL_INCORRECT, arm="v3", engaged=False),
        # instrument said engaged, human overturned -> FN (a real missed gap)
        _grade(correctness_axis.LABEL_INCORRECT, arm="v3", engaged=True),
    ]
    p, r, n = rv.v3_precision_recall(graded)
    assert n == 3
    assert p == 1 / 2   # TP / (TP+FP)
    assert r == 1 / 2   # TP / (TP+FN)


def test_a_stale_grade_never_counts_as_a_pass():
    """A grade whose pipeline_version is not the running one, or that is older
    than MAX_GRADE_AGE_DAYS, is dropped from its arm — even a perfect one."""
    current = _grade(correctness_axis.LABEL_CORRECT, arm="v2")
    old_pipeline = _grade(correctness_axis.LABEL_CORRECT, arm="v2",
                          pipeline="2026-08-01/1")
    expired = _grade(correctness_axis.LABEL_CORRECT, arm="v2",
                     age_days=rv.MAX_GRADE_AGE_DAYS + 5)
    assert rv._grade_is_current(current, pipeline_version="2026-09-05/1", now=NOW)
    assert not rv._grade_is_current(
        old_pipeline, pipeline_version="2026-09-05/1", now=NOW
    )
    assert not rv._grade_is_current(
        expired, pipeline_version="2026-09-05/1", now=NOW
    )


def test_a_sample_row_is_not_a_grade():
    """A row still under the sampling-frame prefix is UNGRADED — nobody has
    overwritten its placeholder label — and must not be read as a verdict."""
    row = {"labeled_by": rv.SAMPLE_LABELED_BY_PREFIX + "2026-09-05/1"}
    assert rv._is_sample_row(row) is True
    assert rv._is_sample_row({"labeled_by": "operator"}) is False


# ---------------------------------------------------------------------------
# The sampler
# ---------------------------------------------------------------------------


def test_sample_is_stratified_across_units_and_deterministic():
    pairs = (
        [{"unit": "escalation", "target_id": f"t{i}", "output_id": uuid4()}
         for i in range(10)]
        + [{"unit": "energy_security", "target_id": f"e{i}", "output_id": uuid4()}
           for i in range(10)]
    )
    a = rv.choose_sample(pairs, day_key="2026-09-12", size=6)
    b = rv.choose_sample(pairs, day_key="2026-09-12", size=6)
    assert [p["target_id"] for p in a] == [p["target_id"] for p in b]  # replayable
    units = [p["unit"] for p in a]
    # Round-robin across units means both are represented, not all-one-unit.
    assert "escalation" in units and "energy_security" in units


def test_sample_snapshot_carries_both_arms():
    pair = {
        "unit": "escalation", "target_id": "il", "output_id": uuid4(),
        "n_items": 3, "n_anchorable": 2, "n_collected": 2, "n_engaged": 0,
        "attention_rate": 0.0, "attention_arms": {"e1_named": 0, "e2_cited": 0},
        "uncollected": [],
    }
    snap = rv.build_sample_snapshot(
        pair, pipeline_version="2026-09-05/1", sampled_at=NOW
    )
    assert snap["instrument"] == "desk_reference"
    assert snap["pipeline_version"] == "2026-09-05/1"
    assert "v2" in snap and "v3" in snap
    assert snap["v3"]["engaged"] is False  # the instrument's own verdict, stated
