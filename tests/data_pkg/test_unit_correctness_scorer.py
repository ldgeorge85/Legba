# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2-T5 — the per-unit correctness-vs-reference scorer.

Covers, mostly as PURE functions (no DB):

  * the Source-ID Overlap RECALL metric (id-set build + coercion + None-skip,
    recall / jaccard / mean),
  * best-label disjunctive max, aggregation over multiple targets, and the REAL
    0.0 (a finding that cited NONE of the canonical evidence — a true signal,
    not a default),
  * the T5 HONESTY done-criterion: ``correctness_vs_reference is None`` (never a
    fabricated number) when there are 0 gold labels / only text-only labels /
    no finding to score, each with its status string,
  * the handler over a fake substrate (full pull → recall + faithfulness mean)
    and over ``deps=None`` (today's empty-gold state → honest None for every unit).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import unit_correctness_scorer as ucs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _f(cited, citations_only=None):
    """A latest-head finding's id-sets (full C, citations-only C)."""
    return {
        "cited_ids": set(cited),
        "citations_only_ids": set(
            citations_only if citations_only is not None else cited
        ),
    }


def _l(label_id, gold):
    """One gold label row's id-set."""
    return {"label_id": label_id, "gold_ids": set(gold)}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registered_in_dispatch_table():
    assert "unit_correctness_scorer" in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER["unit_correctness_scorer"].value == "finding"


# ---------------------------------------------------------------------------
# ID-set build + core set metrics
# ---------------------------------------------------------------------------


def test_coerce_uuid():
    u = uuid4()
    assert ucs._coerce_uuid(str(u)) == u
    assert ucs._coerce_uuid(u) == u
    assert ucs._coerce_uuid(None) is None
    assert ucs._coerce_uuid("not-a-uuid") is None


def test_finding_source_ids_union_coercion_and_none_skip():
    u1, u2, u3 = uuid4(), uuid4(), uuid4()
    data = {"data": {"citations": [
        {"marker": "[1]", "signal_id": str(u1)},
        {"marker": "[2]", "signal_id": None},  # unresolved marker → skipped
        {"marker": "[3]"},                      # no signal_id key → skipped
    ]}}
    derived = [u2, str(u3)]
    # Union of the resolved citation signal_ids + the derived_from uuids.
    assert ucs._finding_source_ids(data, derived) == {u1, u2, u3}
    # citations-only drops derived_from (the tighter prose-bound variant).
    assert ucs._finding_source_ids(data, derived, citations_only=True) == {u1}
    # String- vs UUID-variant ids canonicalize equal → no double counting.
    data2 = {"data": {"citations": [{"signal_id": str(u1)}]}}
    assert ucs._finding_source_ids(data2, [u1]) == {u1}
    # Robust to a missing nested data block.
    assert ucs._finding_source_ids({}, None) == set()


def test_gold_source_ids():
    u1, u2 = uuid4(), uuid4()
    assert ucs._gold_source_ids([str(u1), u2]) == {u1, u2}
    assert ucs._gold_source_ids(None) == set()
    assert ucs._gold_source_ids([None, "bad"]) == set()


def test_recall_jaccard_mean():
    a, b = uuid4(), uuid4()
    assert ucs._recall({a, b}, {a, b}) == 1.0
    assert ucs._recall({a}, {a, b}) == 0.5
    assert ucs._recall(set(), {a}) == 0.0      # cited none of the gold → real 0
    assert ucs._recall({a}, set()) is None     # |G| == 0 → undefined, never 0
    assert ucs._jaccard({a, b}, {a}) == 0.5
    assert ucs._jaccard(set(), set()) is None
    assert ucs._mean([]) is None
    assert ucs._mean([0.0, 1.0]) == 0.5


# ---------------------------------------------------------------------------
# Per-unit scoring — the honesty null rule
# ---------------------------------------------------------------------------


def test_score_unit_no_labels_is_none():
    out = ucs.score_unit({}, {})
    assert out["correctness_vs_reference"] is None
    assert out["labeled_target_count"] == 0
    assert out["scored_target_count"] == 0
    assert out["status"] == ucs._STATUS_NO_LABELS


def test_score_unit_text_only_labels_is_none():
    labels = {"t1": [_l("L1", [])]}  # empty canonical_source_ids → not scorable
    out = ucs.score_unit({"t1": _f([uuid4()])}, labels)
    assert out["correctness_vs_reference"] is None
    assert out["labeled_target_count"] == 1
    assert out["scored_target_count"] == 0
    assert out["status"] == ucs._STATUS_NONE_SCORABLE


def test_score_unit_no_finding_is_none():
    g = uuid4()
    out = ucs.score_unit({}, {"t1": [_l("L1", [g])]})  # scorable label, no finding
    assert out["correctness_vs_reference"] is None
    assert out["scored_target_count"] == 0
    assert out["status"] == ucs._STATUS_NO_FINDING
    assert out["per_target"]["t1"]["reason"] == "no_finding"


# ---------------------------------------------------------------------------
# Per-unit scoring — the metric
# ---------------------------------------------------------------------------


def test_score_unit_basic_recall_breadth_not_punished():
    g1, g2, extra = uuid4(), uuid4(), uuid4()
    # Finding rests on g1 (a canonical row) + a broader extra source; gold = {g1,g2}.
    out = ucs.score_unit({"t1": _f([g1, extra])}, {"t1": [_l("L1", [g1, g2])]})
    assert out["correctness_vs_reference"] == 0.5      # 1 of 2 canonical rows
    pt = out["per_target"]["t1"]
    assert pt["match"] == 0.5
    assert pt["best_label_id"] == "L1"
    assert pt["intersection_size"] == 1
    assert pt["gold_size"] == 2
    assert pt["cited_size"] == 2
    # Jaccard (DIAGNOSTIC) IS punished by the extra cite: 1 / |{g1,g2,extra}|.
    assert pt["jaccard"] == 1 / 3
    assert out["status"] == ucs._STATUS_SCORED


def test_score_unit_best_label_disjunctive_max():
    g1, g2, g3 = uuid4(), uuid4(), uuid4()
    findings = {"t1": _f([g1, g2])}
    # Two acceptable gold answers; the fully-covered one wins (max recall).
    labels = {"t1": [_l("Lpartial", [g1, g3]), _l("Lfull", [g1, g2])]}
    out = ucs.score_unit(findings, labels)
    assert out["correctness_vs_reference"] == 1.0
    assert out["per_target"]["t1"]["best_label_id"] == "Lfull"


def test_score_unit_aggregation_over_multiple_targets():
    g1, g2, g3, g4 = uuid4(), uuid4(), uuid4(), uuid4()
    findings = {"t1": _f([g1]), "t2": _f([g3, g4])}
    labels = {"t1": [_l("L1", [g1, g2])], "t2": [_l("L2", [g3, g4])]}
    out = ucs.score_unit(findings, labels)
    # t1 recall 0.5, t2 recall 1.0 → simple mean 0.75.
    assert out["correctness_vs_reference"] == 0.75
    assert out["scored_target_count"] == 2
    assert out["labeled_target_count"] == 2


def test_score_unit_real_zero_is_not_none():
    g1, other = uuid4(), uuid4()
    # Finding cited NONE of the canonical evidence → a true 0.0, not a null.
    out = ucs.score_unit({"t1": _f([other])}, {"t1": [_l("L1", [g1])]})
    assert out["correctness_vs_reference"] == 0.0
    assert out["scored_target_count"] == 1
    assert out["per_target"]["t1"]["match"] == 0.0
    assert out["status"] == ucs._STATUS_SCORED


def test_score_unit_meta_no_target():
    g1 = uuid4()
    out = ucs.score_unit({None: _f([g1])}, {None: [_l("Lm", [g1])]})
    assert out["correctness_vs_reference"] == 1.0
    assert ucs._META_TARGET_KEY in out["per_target"]


def test_score_unit_citations_only_diagnostic():
    g1, g2 = uuid4(), uuid4()
    # Full C rests on g1 (citation) + g2 (derived_from); citations-only is just g1.
    findings = {"t1": _f([g1, g2], citations_only=[g1])}
    labels = {"t1": [_l("L1", [g1, g2])]}
    out = ucs.score_unit(findings, labels)
    assert out["correctness_vs_reference"] == 1.0          # full C covers both
    assert out["correctness_citations_only"] == 0.5        # prose-bound covers 1/2


# ---------------------------------------------------------------------------
# Handler — honest None (today's empty-gold state) + a fake-substrate pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_deps_none_reports_honest_none():
    result = await ucs.handle([], {"sub_handler": "unit_correctness_scorer"}, None)
    data = result.finding.data
    assert data["sub_handler"] == "unit_correctness_scorer"
    units = data["units"]
    assert set(units) == set(ucs._DEFAULT_UNITS)
    for rec in units.values():
        # The T5 done-criterion: 0 gold labels → None, NEVER 0.0 / a default.
        assert rec["correctness_vs_reference"] is None
        assert rec["faithfulness"] is None
        assert rec["n_labeled"] == 0
        assert rec["status"] == ucs._STATUS_NO_LABELS
    assert data["scored_unit_count"] == 0
    assert data["total_gold_labels"] == 0
    assert "unit_correctness_no_gold" in result.finding.tags
    assert result.finding.confidence == 1.0
    assert result.usage["prompt_tokens"] == 0


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


class _FakeConn:
    """Routes the handler's reads to canned rows by SQL shape.

    Five shapes now: the FLEET-WIDE operator gold-set pull (M-1, no args — it
    reads every ``correctness_labels`` row in one trip), then the per-unit
    reference labels / faithfulness / prior-population / findings reads.
    """

    def __init__(self, data_by_unit, operator_rows=None):
        self._data = data_by_unit
        self._operator_rows = operator_rows or []

    async def fetch(self, sql, *args):
        # PRIMARY axis — fleet-wide, takes no args, so route it BEFORE any
        # per-unit lookup (args[0] would IndexError).
        if "FROM correctness_labels" in sql:
            return self._operator_rows
        unit = args[0]
        d = self._data.get(unit, {})
        if "FROM unit_reference_labels" in sql:
            return d.get("labels", [])
        # The M-2 prior-population rollup also matches `kind = 'critique'`, so
        # its distinctive GROUP BY must be tested first.
        if "kind = 'critique'" in sql and "GROUP BY" in sql:
            return d.get("faithfulness_priors", [])
        if "kind = 'critique'" in sql:
            return d.get("faithfulness", [])
        if "kind = 'finding'" in sql:
            return d.get("findings", [])
        return []

    async def fetchrow(self, sql, *args):
        # The judge-pipeline exclusion count (P3 §5a) — how many faithfulness
        # critiques the current-pipeline filter left out.
        if "COALESCE" in sql and "judge_pipeline_version" in sql:
            unit = args[0]
            return {"n": self._data.get(unit, {}).get("faithfulness_excluded", 0)}
        return None


class _FakeDeps:
    def __init__(self, pool):
        self.pg_pool = pool


@pytest.mark.asyncio
async def test_handle_scores_units_over_fake_substrate():
    u_cite, u_derived, u_gold_extra = uuid4(), uuid4(), uuid4()
    data_by_unit = {
        "escalation": {
            "findings": [{
                "target_id": "country_g20_us",
                "data": {"data": {"citations": [{"signal_id": str(u_cite)}]}},
                "derived_from": [u_derived],
            }],
            "labels": [{
                "label_id": "L1",
                "target_id": "country_g20_us",
                "canonical_source_ids": [u_cite, u_gold_extra],
            }],
            "faithfulness": [{"confidence": 1.0}, {"confidence": 0.5}],
        },
    }
    deps = _FakeDeps(_FakePool(_FakeConn(data_by_unit)))
    result = await ucs.handle(
        [],
        {"sub_handler": "unit_correctness_scorer",
         "units": ["escalation", "energy_security"]},
        deps,
    )
    units = result.finding.data["units"]

    esc = units["escalation"]
    # C(f) = {u_cite (citation), u_derived (derived_from)}; G = {u_cite, u_gold_extra}
    # → recall = 1/2.
    assert esc["correctness_vs_reference"] == 0.5
    assert esc["faithfulness"] == 0.75              # mean(1.0, 0.5)
    assert esc["n_labeled"] == 1
    assert esc["n_findings"] == 1
    assert esc["scored_target_count"] == 1
    assert esc["status"] == ucs._STATUS_SCORED

    # The second unit has no rows → the honest empty result, not a fabricated 0.
    es = units["energy_security"]
    assert es["correctness_vs_reference"] is None
    assert es["status"] == ucs._STATUS_NO_LABELS

    assert result.finding.data["scored_unit_count"] == 1
    assert result.finding.data["total_gold_labels"] == 1


# ---------------------------------------------------------------------------
# P3 §5a — the faithfulness mean names its judge population.
#
# The mean is taken over a lookback window that can straddle a judge swap. When
# the grading model changed on 2026-07-30 20:14Z mean faithfulness moved +7pp
# on its own, which would read here as every unit improving overnight. The
# `judge_pipeline_version` stamp existed for exactly this and had no reader.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_faithfulness_mean_reports_its_judge_population():
    from legba.data.provenance.verify import JUDGE_PIPELINE_VERSION

    data_by_unit = {
        "escalation": {
            "findings": [],
            "labels": [],
            # Only current-pipeline critiques come back from the filtered read.
            "faithfulness": [{"confidence": 1.0}, {"confidence": 0.5}],
            # ...and the fake reports what the filter left behind.
            "faithfulness_excluded": 11,
        },
    }
    deps = _FakeDeps(_FakePool(_FakeConn(data_by_unit)))
    result = await ucs.handle(
        [], {"sub_handler": "unit_correctness_scorer", "units": ["escalation"]}, deps
    )

    esc = result.finding.data["units"]["escalation"]
    assert esc["faithfulness"] == 0.75  # mean over the CURRENT pipeline only
    pop = esc["faithfulness_population"]
    assert pop["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION
    assert pop["n_scored"] == 2
    # The excluded count is REPORTED — a mean over 2 of 13 rows must never look
    # like a mean over 13.
    assert pop["excluded_other_pipeline"] == 11


@pytest.mark.asyncio
async def test_faithfulness_population_is_honest_when_the_pull_fails():
    """A failed pull must still name the pipeline, never imply a measured 0."""
    from legba.data.provenance.verify import JUDGE_PIPELINE_VERSION

    class _BoomPool:
        def acquire(self):
            raise RuntimeError("pull exploded")

    result = await ucs.handle(
        [],
        {"sub_handler": "unit_correctness_scorer", "units": ["escalation"]},
        _FakeDeps(_BoomPool()),
    )
    esc = result.finding.data["units"]["escalation"]
    assert esc["faithfulness"] is None
    pop = esc["faithfulness_population"]
    assert pop["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION
    assert pop["n_scored"] == 0


# ---------------------------------------------------------------------------
# M-1 — the PRIMARY (operator gold-set) axis.
#
# The handler read `unit_reference_labels` (1 row, retired analyst, 0 source
# ids) and reported None every day of its life, while the 8 real operator
# verdicts in `correctness_labels` surfaced in one API overlay and nowhere
# else. These pin the rewire: the operator axis is the headline, the
# source-overlap axis is kept as a named diagnostic, and the two never mix.
# ---------------------------------------------------------------------------


def _op(unit, label):
    return {"unit_analyst_id": unit, "label": label}


@pytest.mark.asyncio
async def test_operator_axis_is_the_headline_and_carries_its_n():
    operator_rows = [
        _op("escalation", "correct"),
        _op("escalation", "partially_correct"),
        _op("energy_security", "incorrect"),
    ]
    conn = _FakeConn({}, operator_rows=operator_rows)
    result = await ucs.handle(
        [],
        {
            "sub_handler": "unit_correctness_scorer",
            "units": ["escalation", "energy_security"],
        },
        _FakeDeps(_FakePool(conn)),
    )
    data = result.finding.data
    units = data["units"]

    assert units["escalation"]["correctness_operator"] == 0.75
    assert units["escalation"]["n_operator_scored"] == 2
    assert units["escalation"]["operator_mix"]["correct"] == 1
    assert units["energy_security"]["correctness_operator"] == 0.0

    # Fleet = every verdict pooled once (1.0 + 0.5 + 0.0) / 3.
    assert data["correctness_operator"] == pytest.approx(0.5)
    assert data["operator_fleet"]["n_scored"] == 3
    assert data["total_operator_labels"] == 3
    assert data["operator_scored_unit_count"] == 2

    # The TITLE leads with the operator axis (it used to lead with a permanent
    # `None` from the table nobody feeds).
    assert result.finding.title.startswith("Unit correctness (operator gold set)")
    assert "correctness 0.50" in result.finding.title


@pytest.mark.asyncio
async def test_tiny_n_is_flagged_never_presented_as_measured():
    conn = _FakeConn({}, operator_rows=[_op("escalation", "correct")])
    result = await ucs.handle(
        [], {"sub_handler": "unit_correctness_scorer", "units": ["escalation"]},
        _FakeDeps(_FakePool(conn)),
    )
    rec = result.finding.data["units"]["escalation"]
    assert rec["correctness_operator"] == 1.0     # reported...
    assert rec["operator_sufficient"] is False    # ...but never called measured
    assert "indicative only" in rec["operator_status"]
    assert "unit_correctness_operator_tiny_n" in result.finding.tags


@pytest.mark.asyncio
async def test_no_operator_verdicts_is_an_honest_null_with_its_own_tag():
    result = await ucs.handle(
        [], {"sub_handler": "unit_correctness_scorer", "units": ["escalation"]},
        _FakeDeps(_FakePool(_FakeConn({}))),
    )
    rec = result.finding.data["units"]["escalation"]
    assert rec["correctness_operator"] is None
    assert rec["n_operator_labels"] == 0
    assert rec["operator_status"] == "no operator verdicts"
    assert "unit_correctness_no_operator_labels" in result.finding.tags
    assert "honest null" in result.finding.title


@pytest.mark.asyncio
async def test_the_two_correctness_axes_stay_separate():
    """A unit can be null on one axis and scored on the other; nothing averages
    them (labels_api P2-5, the standing never-pool rule)."""
    conn = _FakeConn({}, operator_rows=[_op("escalation", "incorrect")])
    result = await ucs.handle(
        [], {"sub_handler": "unit_correctness_scorer", "units": ["escalation"]},
        _FakeDeps(_FakePool(conn)),
    )
    rec = result.finding.data["units"]["escalation"]
    assert rec["correctness_operator"] == 0.0        # the operator judged it
    assert rec["correctness_vs_reference"] is None   # no reference label exists
    assert rec["n_operator_labels"] == 1
    assert rec["n_labeled"] == 0                     # a DIFFERENT table's count
    # Both honesty tags coexist — one per axis.
    assert "unit_correctness_no_gold" in result.finding.tags
    assert "unit_correctness_operator_tiny_n" in result.finding.tags


@pytest.mark.asyncio
async def test_operator_pull_failure_degrades_to_the_honest_empty_axis():
    class _BoomFetchConn(_FakeConn):
        async def fetch(self, sql, *args):
            if "FROM correctness_labels" in sql:
                raise RuntimeError("gold-set pull exploded")
            return await super().fetch(sql, *args)

    result = await ucs.handle(
        [], {"sub_handler": "unit_correctness_scorer", "units": ["escalation"]},
        _FakeDeps(_FakePool(_BoomFetchConn({}))),
    )
    rec = result.finding.data["units"]["escalation"]
    assert rec["correctness_operator"] is None       # never a stubbed 0.0
    assert "unit_correctness_scorer.operator_pull_failed" in (
        result.finding.data["warnings"]
    )


# ---------------------------------------------------------------------------
# M-2 — prior judge populations are ANNOTATED beside the headline, never mixed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prior_judge_populations_are_annotated_not_pooled():
    from legba.data.provenance.verify import JUDGE_PIPELINE_VERSION

    data_by_unit = {
        "escalation": {
            "findings": [],
            "labels": [],
            "faithfulness": [{"confidence": 0.9}],
            "faithfulness_excluded": 12,
            "faithfulness_priors": [
                {"version": "2026-07-31/1", "n_scored": 8,
                 "mean_faithfulness": 0.82},
                {"version": None, "n_scored": 4, "mean_faithfulness": 0.55},
            ],
        },
    }
    result = await ucs.handle(
        [], {"sub_handler": "unit_correctness_scorer", "units": ["escalation"]},
        _FakeDeps(_FakePool(_FakeConn(data_by_unit))),
    )
    pop = result.finding.data["units"]["escalation"]["faithfulness_population"]

    # The headline is the CURRENT stamp only — 0.9, not a pooled 0.78.
    assert result.finding.data["units"]["escalation"]["faithfulness"] == 0.9
    assert pop["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION
    assert pop["n_scored"] == 1

    priors = {p["judge_pipeline_version"]: p for p in pop["prior_populations"]}
    assert priors["2026-07-31/1"]["n_scored"] == 8
    assert priors["2026-07-31/1"]["faithfulness"] == 0.82
    assert priors["2026-07-31/1"]["pre_stamp"] is False
    # A NULL stamp is a real population (everything graded before the split key
    # existed), labelled as such rather than dropped.
    assert priors[None]["pre_stamp"] is True
    assert priors[None]["n_scored"] == 4
    # The prior n's sum to what the filter excluded — the counters reconcile.
    assert sum(p["n_scored"] for p in pop["prior_populations"]) == (
        pop["excluded_other_pipeline"]
    )
