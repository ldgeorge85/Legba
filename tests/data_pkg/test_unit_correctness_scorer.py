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
    """Routes the handler's three reads to per-unit canned rows by SQL shape."""

    def __init__(self, data_by_unit):
        self._data = data_by_unit

    async def fetch(self, sql, *args):
        unit = args[0]
        d = self._data.get(unit, {})
        if "FROM unit_reference_labels" in sql:
            return d.get("labels", [])
        if "kind = 'critique'" in sql:
            return d.get("faithfulness", [])
        if "kind = 'finding'" in sql:
            return d.get("findings", [])
        return []


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
